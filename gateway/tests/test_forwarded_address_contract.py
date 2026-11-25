"""Pin the uvicorn behavior the forwarded address contract in the docs rests on.

SECURITY_LIMITATIONS.md says: the container starts uvicorn without
``--forwarded-allow-ips``; uvicorn then trusts forwarding headers only from
loopback unless ``FORWARDED_ALLOW_IPS`` is set; a trusted peer's
``X-Forwarded-For`` is used and an untrusted peer's is ignored; ``*`` takes the
left-most entry, which a caller controls; an exact list or a CIDR takes the
right-most untrusted entry. If a uvicorn upgrade changes any of this, the docs
are wrong and this file fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import uvicorn
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

ROOT = Path(__file__).resolve().parents[1]


def test_no_launch_file_passes_forwarded_allow_ips() -> None:
    for relative in (
        "scripts/docker_entrypoint.sh",
        "docker-compose.yml",
        "Dockerfile",
    ):
        text = (ROOT / relative).read_text().lower()
        assert "forwarded-allow-ips" not in text, relative


def test_compose_loads_dotenv_into_the_container_environment() -> None:
    """The docs say a .env value applies where the launcher loads it, as Compose does."""
    compose = (ROOT / "docker-compose.yml").read_text()
    lines = [line.strip() for line in compose.splitlines()]
    assert "env_file:" in lines
    assert "- .env" in lines


def test_uvicorn_default_trusts_loopback_only(monkeypatch) -> None:
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)

    async def app(scope, receive, send):
        return None

    config = uvicorn.Config(app)
    assert config.proxy_headers is True
    assert config.forwarded_allow_ips == "127.0.0.1,::1"


def test_forwarded_allow_ips_is_read_from_the_process_environment(
    monkeypatch,
) -> None:
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.0.0.0/24")

    async def app(scope, receive, send):
        return None

    assert uvicorn.Config(app).forwarded_allow_ips == "10.0.0.0/24"


def test_an_explicit_forwarded_allow_ips_option_beats_the_environment(
    monkeypatch,
) -> None:
    """A command override can pass --forwarded-allow-ips; it wins over the variable."""
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.0.0.0/24")

    async def app(scope, receive, send):
        return None

    config = uvicorn.Config(app, forwarded_allow_ips="10.0.0.50")
    assert config.forwarded_allow_ips == "10.0.0.50"


@pytest.mark.parametrize(
    ("environment", "arguments", "expected"),
    [
        ({"UVICORN_FORWARDED_ALLOW_IPS": "10.0.0.50"}, [], "10.0.0.50"),
        (
            {
                "UVICORN_FORWARDED_ALLOW_IPS": "10.0.0.50",
                "FORWARDED_ALLOW_IPS": "10.0.0.0/24",
            },
            [],
            "10.0.0.50",
        ),
        (
            {"UVICORN_FORWARDED_ALLOW_IPS": "10.0.0.50"},
            ["--forwarded-allow-ips", "10.0.0.51"],
            "10.0.0.51",
        ),
        # Only the unprefixed variable: the CLI passes nothing and Config reads it.
        ({"FORWARDED_ALLOW_IPS": "10.0.0.0/24"}, [], None),
    ],
    ids=["prefixed", "prefixed-beats-plain", "option-beats-prefixed", "plain-only"],
)
def test_the_uvicorn_cli_reads_a_prefixed_variable_and_the_option(
    monkeypatch, environment, arguments, expected
) -> None:
    """The container runs the uvicorn CLI, so test that path, not only Config."""
    import importlib

    from click.testing import CliRunner

    # ``uvicorn.main`` the attribute is the click command; the module is separate.
    uvicorn_main = importlib.import_module("uvicorn.main")

    for name in ("UVICORN_FORWARDED_ALLOW_IPS", "FORWARDED_ALLOW_IPS"):
        monkeypatch.delenv(name, raising=False)
    captured: dict[str, object] = {}

    def fake_run(_app, **options):
        captured.update(options)

    monkeypatch.setattr(uvicorn_main, "run", fake_run)
    result = CliRunner().invoke(
        uvicorn_main.main, ["app.main:app", *arguments], env=environment
    )

    assert result.exit_code == 0, result.output
    assert captured["forwarded_allow_ips"] == expected


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("trusted", "peer", "forwarded_for", "expected"),
    [
        # Default: a loopback peer is trusted, any other peer is ignored.
        ("127.0.0.1,::1", "127.0.0.1", "172.16.0.7", "172.16.0.7"),
        ("127.0.0.1,::1", "10.0.0.50", "172.16.0.7", "10.0.0.50"),
        # An exact ingress address: its X-Forwarded-For is used.
        ("10.0.0.50", "10.0.0.50", "172.16.0.7", "172.16.0.7"),
        ("10.0.0.50", "172.16.1.1", "172.16.0.7", "172.16.1.1"),
        # A CIDR covering the ingress works.
        ("10.0.0.0/24", "10.0.0.77", "172.16.0.7", "172.16.0.7"),
        ("10.0.0.0/24", "172.16.1.1", "172.16.0.7", "172.16.1.1"),
        # Exact list: a caller-supplied left-most entry is ignored; the ingress
        # appended the real client, which is the right-most untrusted entry.
        ("10.0.0.50", "10.0.0.50", "172.16.0.1, 172.16.1.9", "172.16.1.9"),
        # Multiple trusted hops are skipped from the right.
        (
            "10.0.0.50,10.0.0.51",
            "10.0.0.50",
            "172.16.0.1, 172.16.1.9, 10.0.0.51",
            "172.16.1.9",
        ),
        # A catch-all CIDR behaves like "*" for entries of its address family:
        # every entry is trusted, so the left-most one wins and a caller controls it.
        ("0.0.0.0/0", "10.0.0.50", "172.16.0.1, 172.16.1.9", "172.16.0.1"),
        ("::/0", "fd00::50", "fd00::1, fd00::9", "fd00::1"),
        # An ingress that forwards the inbound header unchanged, without adding the
        # address it observed, lets the caller choose the client even when the
        # ingress is named exactly. Two callers get two different addresses.
        ("10.0.0.50", "10.0.0.50", "172.16.0.1", "172.16.0.1"),
        ("10.0.0.50", "10.0.0.50", "172.16.0.2", "172.16.0.2"),
        # "*": the left-most entry wins, and a caller controls it.
        ("*", "10.0.0.50", "172.16.0.1, 172.16.1.9", "172.16.0.1"),
        ("*", "172.16.1.1", "172.16.0.1", "172.16.0.1"),
    ],
)
async def test_which_forwarded_entry_becomes_the_client_address(
    trusted, peer, forwarded_for, expected
) -> None:
    seen: dict[str, str] = {}

    async def inner(scope, receive, send):
        seen["client"] = scope["client"][0]

    scope = {
        "type": "http",
        "headers": [(b"x-forwarded-for", forwarded_for.encode())],
        "client": (peer, 5000),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    await ProxyHeadersMiddleware(inner, trusted_hosts=trusted)(scope, receive, send)
    assert seen["client"] == expected
