"""Fail-closed target and credential coverage for live mutation scripts."""

from __future__ import annotations

import asyncio
import importlib
import sys

import httpx
import pytest

from scripts.live_script_target import LiveTargetError, resolve_live_target

SCRIPT_MODULES = (
    "scripts.trust_plane_conformance",
    "scripts.stress_test_live",
)
TARGET_ENV = "AGENT_MIDDLEWARE_API_URL"
KEY_ENV = "AGENT_MIDDLEWARE_API_KEY"
CANARY_KEY = "key-must-never-be-printed"


def test_missing_target_is_rejected() -> None:
    with pytest.raises(LiveTargetError, match="--api-url"):
        resolve_live_target(None, confirm_production=False, environ={})


def test_staging_https_target_is_accepted() -> None:
    assert (
        resolve_live_target(
            None,
            confirm_production=False,
            environ={TARGET_ENV: "https://staging.example.test"},
        )
        == "https://staging.example.test"
    )


def test_cli_target_overrides_environment_target() -> None:
    assert (
        resolve_live_target(
            "https://cli.example.test:8443/",
            confirm_production=False,
            environ={TARGET_ENV: "https://environment.example.test"},
        )
        == "https://cli.example.test:8443"
    )


def test_explicit_empty_cli_target_does_not_fall_back_to_environment() -> None:
    with pytest.raises(LiveTargetError, match="--api-url"):
        resolve_live_target(
            "",
            confirm_production=False,
            environ={TARGET_ENV: "https://environment.example.test"},
        )


@pytest.mark.parametrize(
    "target",
    [
        " https://staging.example.test",
        "https://staging.example.test ",
        "\thttps://staging.example.test",
        "https://staging.example.test\x7f",
    ],
)
def test_surrounding_whitespace_and_controls_are_rejected(target: str) -> None:
    with pytest.raises(LiveTargetError, match="whitespace or control"):
        resolve_live_target(target, confirm_production=False, environ={})


@pytest.mark.parametrize(
    "target",
    [
        "https://staging.example.test:",
        "https://[::1]:",
    ],
)
def test_explicit_empty_ports_are_rejected(target: str) -> None:
    with pytest.raises(LiveTargetError, match="invalid port"):
        resolve_live_target(target, confirm_production=False, environ={})


def test_multiple_trailing_dns_root_dots_are_rejected() -> None:
    with pytest.raises(LiveTargetError, match="invalid hostname"):
        resolve_live_target(
            "https://staging.example.test..",
            confirm_production=False,
            environ={},
        )


def test_canonical_production_requires_confirmation() -> None:
    with pytest.raises(LiveTargetError, match="--confirm-production"):
        resolve_live_target(
            "https://API.THISISATEST.TECH.:443/",
            confirm_production=False,
            environ={},
        )

    assert (
        resolve_live_target(
            "https://API.THISISATEST.TECH.:443/",
            confirm_production=True,
            environ={},
        )
        == "https://api.thisisatest.tech"
    )


@pytest.mark.parametrize(
    ("target", "normalized"),
    [
        ("http://localhost:8000/", "http://localhost:8000"),
        ("http://LOCALHOST.:8000", "http://localhost:8000"),
        ("http://127.42.1.9:8000", "http://127.42.1.9:8000"),
        ("http://[::1]:8000/", "http://[::1]:8000"),
        ("http://[0:0:0:0:0:0:0:1]:8000", "http://[::1]:8000"),
    ],
)
def test_http_is_allowed_for_loopback(target: str, normalized: str) -> None:
    assert (
        resolve_live_target(target, confirm_production=False, environ={}) == normalized
    )


def test_remote_cleartext_is_rejected() -> None:
    with pytest.raises(LiveTargetError, match="require HTTPS"):
        resolve_live_target(
            "http://staging.example.test",
            confirm_production=False,
            environ={},
        )


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("https://user:password@staging.example.test", "embedded credentials"),
        ("https://staging.example.test?mode=test", "query string"),
        ("https://staging.example.test?", "query string"),
        ("https://staging.example.test#section", "fragment"),
        ("https://staging.example.test/v1", "path must be empty or /"),
    ],
)
def test_unsafe_url_components_are_rejected(target: str, message: str) -> None:
    with pytest.raises(LiveTargetError, match=message):
        resolve_live_target(target, confirm_production=False, environ={})


@pytest.mark.parametrize(
    ("target", "normalized"),
    [
        ("https://STAGING.EXAMPLE.TEST/", "https://staging.example.test"),
        ("https://staging.example.test:443/", "https://staging.example.test"),
        ("https://staging.example.test:8443/", "https://staging.example.test:8443"),
    ],
)
def test_target_normalization(target: str, normalized: str) -> None:
    assert (
        resolve_live_target(target, confirm_production=False, environ={}) == normalized
    )


REJECTED_CONFIGURATIONS = (
    pytest.param([], None, CANARY_KEY, id="missing-target"),
    pytest.param(
        ["--api-url", " https://staging.example.test"],
        None,
        CANARY_KEY,
        id="leading-whitespace",
    ),
    pytest.param(
        ["--api-url", "https://staging.example.test "],
        None,
        CANARY_KEY,
        id="trailing-whitespace",
    ),
    pytest.param(
        ["--api-url", "\thttps://staging.example.test"],
        None,
        CANARY_KEY,
        id="leading-tab",
    ),
    pytest.param(
        ["--api-url", "https://staging.example.test\x7f"],
        None,
        CANARY_KEY,
        id="trailing-control",
    ),
    pytest.param(
        ["--api-url", "https://staging.example.test:"],
        None,
        CANARY_KEY,
        id="dns-empty-port",
    ),
    pytest.param(
        ["--api-url", "https://[::1]:"],
        None,
        CANARY_KEY,
        id="ipv6-empty-port",
    ),
    pytest.param(
        ["--api-url", "https://staging.example.test.."],
        None,
        CANARY_KEY,
        id="multiple-trailing-root-dots",
    ),
    pytest.param(
        ["--api-url", "https://api.thisisatest.tech"],
        None,
        CANARY_KEY,
        id="production-without-confirmation",
    ),
    pytest.param(
        ["--api-url", "http://staging.example.test"],
        None,
        CANARY_KEY,
        id="remote-cleartext",
    ),
    pytest.param(
        ["--api-url", "https://user:password@staging.example.test"],
        None,
        CANARY_KEY,
        id="embedded-credentials",
    ),
    pytest.param(
        ["--api-url", "https://staging.example.test?mode=test"],
        None,
        CANARY_KEY,
        id="query-string",
    ),
    pytest.param(
        ["--api-url", "https://staging.example.test#fragment"],
        None,
        CANARY_KEY,
        id="fragment",
    ),
    pytest.param(
        ["--api-url", "https://staging.example.test/v1"],
        None,
        CANARY_KEY,
        id="non-root-path",
    ),
    pytest.param(
        ["--api-url", "https://staging.example.test"],
        None,
        None,
        id="missing-key",
    ),
)


@pytest.mark.parametrize("module_name", SCRIPT_MODULES)
@pytest.mark.parametrize(
    ("argv", "environment_target", "api_key"),
    REJECTED_CONFIGURATIONS,
)
def test_rejected_configuration_never_creates_an_http_client(
    module_name: str,
    argv: list[str],
    environment_target: str | None,
    api_key: str | None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = importlib.import_module(module_name)
    monkeypatch.delenv(TARGET_ENV, raising=False)
    monkeypatch.delenv(KEY_ENV, raising=False)
    if environment_target is not None:
        monkeypatch.setenv(TARGET_ENV, environment_target)
    if api_key is not None:
        monkeypatch.setenv(KEY_ENV, api_key)

    class UnexpectedAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pytest.fail("rejected configuration created an HTTP client")

    monkeypatch.setattr(module.httpx, "AsyncClient", UnexpectedAsyncClient)

    assert asyncio.run(module.main(argv)) == 2
    output = capsys.readouterr()
    assert CANARY_KEY not in output.out
    assert CANARY_KEY not in output.err


@pytest.mark.parametrize("module_name", SCRIPT_MODULES)
def test_script_cli_target_overrides_env_and_key_is_not_printed(
    module_name: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = importlib.import_module(module_name)
    monkeypatch.setenv(TARGET_ENV, "https://environment.example.test")
    monkeypatch.setenv(KEY_ENV, CANARY_KEY)

    if module_name.endswith("trust_plane_conformance"):
        created_with: list[str] = []

        class FakeAsyncClient:
            def __init__(self, *, base_url: str, timeout: int) -> None:
                created_with.append(base_url)

            async def __aenter__(self) -> object:
                return object()

            async def __aexit__(self, *args: object) -> None:
                return None

        async def fake_run(client: object, suite: object) -> None:
            return None

        monkeypatch.setattr(module.httpx, "AsyncClient", FakeAsyncClient)
        monkeypatch.setattr(module, "run", fake_run)
    else:

        async def fake_setup_wallets() -> tuple[str, str]:
            return "sponsor", "agent"

        async def no_op(*args: object, **kwargs: object) -> None:
            return None

        monkeypatch.setattr(module, "setup_wallets", fake_setup_wallets)
        for name in (
            "test_budget_exhaustion",
            "test_expired_permit",
            "test_concurrent_permit_creation",
            "test_concurrent_governed_invokes",
            "test_unicode_payload",
            "test_tampered_permit",
            "test_cross_wallet_access",
            "test_timezone_extremes",
            "test_decimal_precision",
            "test_rapid_fire_idempotency",
            "test_permit_reuse_after_replay",
            "test_health_under_load",
        ):
            monkeypatch.setattr(module, name, no_op)

    assert (
        asyncio.run(module.main(["--api-url", "https://CLI.EXAMPLE.TEST:8443/"])) == 0
    )
    assert module.API_URL == "https://cli.example.test:8443"
    output = capsys.readouterr()
    assert CANARY_KEY not in output.out
    assert CANARY_KEY not in output.err
    if module_name.endswith("trust_plane_conformance"):
        assert created_with == ["https://cli.example.test:8443"]


def test_scripts_import_without_configuration_or_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(TARGET_ENV, raising=False)
    monkeypatch.delenv(KEY_ENV, raising=False)

    class UnexpectedAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pytest.fail("import created an HTTP client")

    monkeypatch.setattr(httpx, "AsyncClient", UnexpectedAsyncClient)
    for module_name in SCRIPT_MODULES:
        sys.modules.pop(module_name, None)
        imported = importlib.import_module(module_name)
        assert imported.API_URL == ""
        assert imported.API_KEY == ""


# scripts/adversarial_battery.py sends the operator's BOOTSTRAP_KEY on every
# request, so it gets the same target guard: an explicit target, HTTPS for
# anything off loopback, and confirmation for the production origin.
BATTERY_MODULE = "scripts.adversarial_battery"
BATTERY_TARGET_ENV = "API_URL"
BATTERY_KEY_ENV = "BOOTSTRAP_KEY"


class _RequestAttempted(Exception):
    """Raised by the fake urlopen once the battery tries to send a request."""


def _load_battery(
    monkeypatch: pytest.MonkeyPatch,
    *,
    environment_target: str | None,
    argv: list[str],
    bootstrap_key: str | None = CANARY_KEY,
) -> tuple[object, list[tuple[str, dict[str, str]]]]:
    """Import the battery fresh under the given environment and argv.

    The module is re-imported so a script that captures its target at import
    time is exercised the way an operator's shell would exercise it.
    """
    monkeypatch.delenv(BATTERY_TARGET_ENV, raising=False)
    monkeypatch.delenv(BATTERY_KEY_ENV, raising=False)
    if environment_target is not None:
        monkeypatch.setenv(BATTERY_TARGET_ENV, environment_target)
    if bootstrap_key is not None:
        monkeypatch.setenv(BATTERY_KEY_ENV, bootstrap_key)
    monkeypatch.setattr(sys, "argv", ["adversarial_battery.py", *argv])
    sys.modules.pop(BATTERY_MODULE, None)
    module = importlib.import_module(BATTERY_MODULE)

    sent: list[tuple[str, dict[str, str]]] = []

    def fake_urlopen(request: object, timeout: float | None = None) -> object:
        sent.append((request.full_url, dict(request.header_items())))  # type: ignore[attr-defined]
        raise _RequestAttempted

    monkeypatch.setattr(
        module.urllib.request.OpenerDirector,
        "open",
        lambda _self, request, timeout=None: fake_urlopen(request, timeout),
    )
    return module, sent


@pytest.mark.parametrize(
    ("environment_target", "argv"),
    [
        pytest.param(None, [], id="missing-target"),
        pytest.param("", [], id="empty-target"),
        pytest.param("http://staging.example.test", [], id="remote-cleartext"),
        pytest.param(
            "https://api.thisisatest.tech", [], id="production-without-confirmation"
        ),
        pytest.param(
            "https://API.THISISATEST.TECH./", [], id="production-alias-unconfirmed"
        ),
        pytest.param(
            "https://user:password@staging.example.test",
            [],
            id="embedded-credentials",
        ),
        pytest.param("https://staging.example.test/v1", [], id="non-root-path"),
        pytest.param(
            "https://staging.example.test",
            ["--api-url", "http://staging.example.test"],
            id="cli-remote-cleartext",
        ),
    ],
)
def test_adversarial_battery_rejects_unsafe_target_before_any_request(
    environment_target: str | None,
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module, sent = _load_battery(
        monkeypatch, environment_target=environment_target, argv=argv
    )

    assert module.main() == 2
    assert sent == []
    output = capsys.readouterr()
    assert CANARY_KEY not in output.out
    assert CANARY_KEY not in output.err


def test_adversarial_battery_requires_bootstrap_key_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, sent = _load_battery(
        monkeypatch,
        environment_target="https://staging.example.test",
        argv=[],
        bootstrap_key=None,
    )

    assert module.main() == 2
    assert sent == []


@pytest.mark.parametrize(
    ("environment_target", "argv", "origin"),
    [
        pytest.param(
            "https://STAGING.example.test:443/",
            [],
            "https://staging.example.test",
            id="https-staging",
        ),
        pytest.param(
            "http://127.0.0.1:8000/", [], "http://127.0.0.1:8000", id="loopback-http"
        ),
        pytest.param(
            "https://environment.example.test",
            ["--api-url", "https://cli.example.test:8443"],
            "https://cli.example.test:8443",
            id="cli-overrides-environment",
        ),
        pytest.param(
            "https://api.thisisatest.tech",
            ["--confirm-production"],
            "https://api.thisisatest.tech",
            id="confirmed-production",
        ),
    ],
)
def test_adversarial_battery_sends_to_the_normalized_accepted_target(
    environment_target: str,
    argv: list[str],
    origin: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module, sent = _load_battery(
        monkeypatch, environment_target=environment_target, argv=argv
    )

    with pytest.raises(_RequestAttempted):
        module.main()

    first_url, first_headers = sent[0]
    assert first_url == f"{origin}/v1/billing/wallets/sponsor"
    assert first_headers["X-api-key"] == CANARY_KEY
    output = capsys.readouterr()
    assert CANARY_KEY not in output.out
    assert CANARY_KEY not in output.err


def test_adversarial_battery_import_reads_no_target_or_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, _sent = _load_battery(
        monkeypatch,
        environment_target="http://staging.example.test",
        argv=[],
    )

    assert module.API_URL == ""
    assert module.BOOTSTRAP_KEY is None
