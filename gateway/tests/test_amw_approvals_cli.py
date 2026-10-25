"""Tests for the local approve/deny CLI.

The placeholder key below follows the synthetic style used by the other
auth-adjacent tests; it is never a real credential.
"""

from __future__ import annotations

import importlib.util
import secrets
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "amw_approvals", ROOT / "scripts" / "amw_approvals.py"
)
assert _SPEC and _SPEC.loader
amw_approvals = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(amw_approvals)

PLACEHOLDER_KEY = f"synthetic-{secrets.token_hex(8)}"


def test_key_env_and_header_match_gateway_auth() -> None:
    from app.core.config import get_settings

    settings = get_settings()
    assert amw_approvals.API_KEY_ENV == "VALID_API_KEYS"
    assert "VALID_API_KEYS" in type(settings).model_fields
    assert amw_approvals.API_KEY_HEADER == settings.API_KEY_HEADER


def test_missing_key_exits_naming_env_var(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("VALID_API_KEYS", raising=False)
    assert amw_approvals.main(["list"]) == 1
    assert "VALID_API_KEYS" in capsys.readouterr().err


def test_blank_key_exits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("VALID_API_KEYS", "  , ")
    assert amw_approvals.main(["approve", "nonce"]) == 1
    assert "VALID_API_KEYS" in capsys.readouterr().err


def test_first_comma_separated_entry_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VALID_API_KEYS", f"  , {PLACEHOLDER_KEY} , second")
    assert amw_approvals._read_api_key() == PLACEHOLDER_KEY


def test_client_attaches_key_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VALID_API_KEYS", PLACEHOLDER_KEY)
    client = amw_approvals._client("http://127.0.0.1:9", amw_approvals._read_api_key())
    assert client.headers["X-API-Key"] == PLACEHOLDER_KEY


class _StubResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload
        self.status_code = 200

    def json(self) -> dict[str, object]:
        return self._payload

    def raise_for_status(self) -> None:
        return None


def test_every_command_sends_key_and_never_prints_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sent: list[tuple[str, str, dict[str, str]]] = []

    class _RecordingClient:
        def __init__(
            self,
            base_url: str = "",
            timeout: float | None = None,
            headers: dict[str, str] | None = None,
        ) -> None:
            self._headers = dict(headers or {})

        def get(self, path: str) -> _StubResponse:
            sent.append(("GET", path, dict(self._headers)))
            return _StubResponse({"approvals": []})

        def post(self, path: str) -> _StubResponse:
            sent.append(("POST", path, dict(self._headers)))
            return _StubResponse({"status": "approved"})

    monkeypatch.setattr(amw_approvals.httpx, "Client", _RecordingClient)
    monkeypatch.setenv("VALID_API_KEYS", PLACEHOLDER_KEY)
    assert amw_approvals.main(["list"]) == 0
    assert amw_approvals.main(["approve", "nonce-a"]) == 0
    assert amw_approvals.main(["deny", "nonce-b"]) == 0
    assert [method for method, _, _ in sent] == ["GET", "POST", "POST"]
    for _, _, headers in sent:
        assert headers == {"X-API-Key": PLACEHOLDER_KEY}
    out = capsys.readouterr()
    assert PLACEHOLDER_KEY not in out.out
    assert PLACEHOLDER_KEY not in out.err


def test_unreachable_gateway_error_hides_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("VALID_API_KEYS", PLACEHOLDER_KEY)
    assert amw_approvals.main(["--base-url", "http://127.0.0.1:9", "list"]) == 1
    out = capsys.readouterr()
    assert PLACEHOLDER_KEY not in out.out
    assert PLACEHOLDER_KEY not in out.err


def _random_port() -> int:
    return 1024 + secrets.randbelow(65535 - 1024 + 1)


def _random_10_host() -> str:
    return (
        f"10.{secrets.randbelow(256)}.{secrets.randbelow(256)}"
        f".{1 + secrets.randbelow(254)}"
    )


def _random_192_168_host() -> str:
    return f"192.168.{secrets.randbelow(256)}.{1 + secrets.randbelow(254)}"


def _random_invalid_host() -> str:
    return f"{uuid.uuid4().hex}.invalid"


def _random_127_host() -> str:
    return (
        f"127.{secrets.randbelow(256)}.{secrets.randbelow(256)}"
        f".{secrets.randbelow(256)}"
    )


def _allowed_url(case: str) -> str:
    # Deterministic labels only; random hosts/ports are built in the body so
    # xdist workers collecting independently see identical test ids.
    if case == "https-any-host":
        return f"https://{_random_invalid_host()}:{_random_port()}"
    if case == "https-no-port":
        return f"https://{_random_invalid_host()}"
    if case == "http-localhost":
        return f"http://localhost:{_random_port()}"
    if case == "http-127-1":
        return f"http://127.0.0.1:{_random_port()}"
    if case == "http-127-range":
        return f"http://{_random_127_host()}:{_random_port()}"
    if case == "http-ipv6-loopback":
        return f"http://[::1]:{_random_port()}"
    if case == "http-uppercase-localhost":
        return f"HTTP://localhost:{_random_port()}"
    raise AssertionError(f"unknown allowed case: {case}")


def _refused_url(case: str) -> str:
    if case == "http-rfc1918-10":
        return f"http://{_random_10_host()}:{_random_port()}"
    if case == "http-rfc1918-192-168":
        return f"http://{_random_192_168_host()}:{_random_port()}"
    if case == "http-public-hostname":
        return f"http://{_random_invalid_host()}:{_random_port()}"
    if case == "http-userinfo-trick":
        return f"http://127.0.0.1@{_random_invalid_host()}:{_random_port()}"
    if case == "http-localhost-userinfo-trick":
        return f"http://localhost@{_random_invalid_host()}:{_random_port()}/"
    if case == "http-localhost-lookalike":
        return f"http://localhost.{uuid.uuid4().hex}.invalid:{_random_port()}"
    if case == "http-ip-lookalike":
        return f"http://127.0.0.1.{uuid.uuid4().hex}.invalid:{_random_port()}"
    if case == "ftp-loopback":
        return f"ftp://localhost:{_random_port()}/"
    if case == "file-scheme":
        return "file:///etc/hosts"
    if case == "missing-host":
        return "http:///no-host"
    if case == "empty":
        return ""
    if case == "no-scheme":
        return f"localhost:{_random_port()}"
    raise AssertionError(f"unknown refused case: {case}")


@pytest.mark.parametrize(
    "case",
    [
        "https-any-host",
        "https-no-port",
        "http-localhost",
        "http-127-1",
        "http-127-range",
        "http-ipv6-loopback",
        "http-uppercase-localhost",
    ],
)
def test_allowed_base_url_sends_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], case: str
) -> None:
    sent: list[str] = []

    class _RecordingClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get(self, path: str) -> _StubResponse:
            sent.append(path)
            return _StubResponse({"approvals": []})

    monkeypatch.setattr(amw_approvals.httpx, "Client", _RecordingClient)
    monkeypatch.setenv("VALID_API_KEYS", PLACEHOLDER_KEY)
    assert amw_approvals.main(["--base-url", _allowed_url(case), "list"]) == 0
    assert sent == ["/v1/approvals/pending"]
    out = capsys.readouterr()
    assert PLACEHOLDER_KEY not in out.out
    assert PLACEHOLDER_KEY not in out.err


class _ExplodingClient:
    def __init__(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("refused URL must not open an HTTP client")


@pytest.mark.parametrize(
    "case",
    [
        "http-rfc1918-10",
        "http-rfc1918-192-168",
        "http-public-hostname",
        "http-userinfo-trick",
        "http-localhost-userinfo-trick",
        "http-localhost-lookalike",
        "http-ip-lookalike",
        "ftp-loopback",
        "file-scheme",
        "missing-host",
        "empty",
        "no-scheme",
    ],
)
def test_refused_base_url_makes_no_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], case: str
) -> None:
    monkeypatch.setattr(amw_approvals.httpx, "Client", _ExplodingClient)
    monkeypatch.setenv("VALID_API_KEYS", PLACEHOLDER_KEY)
    base_url = _refused_url(case)
    nonce = uuid.uuid4().hex
    for argv in (["list"], ["approve", nonce], ["deny", nonce]):
        assert amw_approvals.main(["--base-url", base_url, *argv]) == 1
        err = capsys.readouterr().err
        assert "refusing to send the API key" in err
        assert PLACEHOLDER_KEY not in err


def test_refusal_happens_before_key_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(amw_approvals.httpx, "Client", _ExplodingClient)
    monkeypatch.delenv("VALID_API_KEYS", raising=False)
    url = f"http://{_random_10_host()}:{_random_port()}"
    assert amw_approvals.main(["--base-url", url, "list"]) == 1
    err = capsys.readouterr().err
    assert "refusing to send the API key" in err
    assert "VALID_API_KEYS" not in err


def test_refused_env_base_url_makes_no_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(amw_approvals.httpx, "Client", _ExplodingClient)
    monkeypatch.setenv("VALID_API_KEYS", PLACEHOLDER_KEY)
    monkeypatch.setenv(
        amw_approvals.BASE_URL_ENV,
        f"http://{_random_invalid_host()}:{_random_port()}",
    )
    assert amw_approvals.main(["list"]) == 1
    out = capsys.readouterr()
    assert "refusing to send the API key" in out.err
    assert PLACEHOLDER_KEY not in out.out
    assert PLACEHOLDER_KEY not in out.err


def test_client_refuses_disallowed_url_directly() -> None:
    url = f"http://{_random_10_host()}:{_random_port()}"
    with pytest.raises(ValueError, match="refusing to send the API key"):
        amw_approvals._client(url, PLACEHOLDER_KEY)
