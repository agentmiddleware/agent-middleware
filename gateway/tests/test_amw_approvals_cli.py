"""Tests for the local approve/deny CLI.

The placeholder key below follows the synthetic style used by the other
auth-adjacent tests; it is never a real credential.
"""

from __future__ import annotations

import importlib.util
import json
import secrets
from pathlib import Path
from typing import Any

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
    # RFC 1122 loopback, RFC 6335 fixed port: allowed http loopback URL.
    client = amw_approvals._client(
        f"http://{LOOPBACK_V4}:{HTTP_TEST_PORT}", amw_approvals._read_api_key()
    )
    try:
        assert client.headers["X-API-Key"] == PLACEHOLDER_KEY
    finally:
        client.close()


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
            trust_env: bool = True,
        ) -> None:
            assert trust_env is False
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
    key = secrets.token_hex(16)
    monkeypatch.setenv("VALID_API_KEYS", key)
    # RFC 1122 loopback; port 9 (discard) is closed, so connect fails.
    base_url = f"http://{LOOPBACK_V4}:9"
    assert amw_approvals.main(["--base-url", base_url, "list"]) == 1
    out = capsys.readouterr()
    assert "gateway unreachable at" in out.err
    assert base_url in out.err
    _assert_no_key_leak(key, out.out, out.err)


# Fixed documentation-only fixtures. Every host below is reserved and
# never routes to a real peer; no test in this file opens a real socket
# for refused URLs (see _ExplodingClient).
EXAMPLE_COM = "example.com"  # RFC 2606, reserved example domain
EXAMPLE_INVALID = "example.invalid"  # RFC 2606, invalid TLD never resolves
LOCALHOST_NAME = "localhost"  # RFC 6761, special-use loopback name
TEST_NET_1 = "192.0.2.1"  # RFC 5737, TEST-NET-1 documentation-only
TEST_NET_2 = "198.51.100.7"  # RFC 5737, TEST-NET-2 documentation-only
DOC_IPV6 = "2001:db8::1"  # RFC 3849, documentation-only IPv6
LOOPBACK_V4 = "127.0.0.1"  # RFC 1122, IPv4 loopback
LOOPBACK_V4_OTHER = "127.0.0.2"  # RFC 1122, same 127/8 loopback range
LOOPBACK_V6 = "::1"  # RFC 4291, IPv6 loopback
PRIVATE_10 = "10.0.0.1"  # RFC 1918, private-use address
PRIVATE_192_168 = "192.168.0.1"  # RFC 1918, private-use address
HTTPS_TEST_PORT = 8443  # fixed test port in the user range (RFC 6335)
HTTP_TEST_PORT = 8080  # fixed test port in the user range (RFC 6335)


def _allowed_url(case: str) -> str:
    if case == "https-any-host":
        return f"https://[{DOC_IPV6}]:{HTTPS_TEST_PORT}"
    if case == "https-no-port":
        return f"https://{EXAMPLE_COM}"
    if case == "http-localhost":
        return f"http://{LOCALHOST_NAME}:{HTTP_TEST_PORT}"
    if case == "http-127-1":
        return f"http://{LOOPBACK_V4}:{HTTP_TEST_PORT}"
    if case == "http-127-range":
        return f"http://{LOOPBACK_V4_OTHER}:{HTTP_TEST_PORT}"
    if case == "http-ipv6-loopback":
        return f"http://[{LOOPBACK_V6}]:{HTTP_TEST_PORT}"
    if case == "http-uppercase-localhost":
        return f"HTTP://{LOCALHOST_NAME}:{HTTP_TEST_PORT}"
    raise AssertionError(f"unknown allowed case: {case}")


def _refused_url(case: str) -> str:
    if case == "http-rfc1918-10":
        return f"http://{PRIVATE_10}:{HTTP_TEST_PORT}"
    if case == "http-rfc1918-192-168":
        return f"http://{PRIVATE_192_168}:{HTTP_TEST_PORT}"
    if case == "http-public-hostname":
        return f"http://{EXAMPLE_COM}:{HTTP_TEST_PORT}"
    if case == "http-userinfo-trick":
        return f"http://{LOOPBACK_V4}@{EXAMPLE_INVALID}:{HTTP_TEST_PORT}"
    if case == "http-localhost-userinfo-trick":
        return f"http://{LOCALHOST_NAME}@{EXAMPLE_INVALID}:{HTTP_TEST_PORT}/"
    if case == "http-userinfo-localhost":
        # RFC 6761 localhost, RFC 6335 fixed port; userinfo refuses outright.
        return f"http://guest@{LOCALHOST_NAME}:{HTTP_TEST_PORT}"
    if case == "https-userinfo-example":
        # RFC 2606 example.com; userinfo refuses even on allowed https.
        return f"https://guest@{EXAMPLE_COM}"
    if case == "http-malformed-ipv6-bracket":
        # RFC 3849 documentation IPv6, missing closing bracket: ValueError.
        return f"http://[{DOC_IPV6}"
    if case == "http-localhost-lookalike":
        return f"http://{LOCALHOST_NAME}.{EXAMPLE_INVALID}:{HTTP_TEST_PORT}"
    if case == "http-ip-lookalike":
        return f"http://{LOOPBACK_V4}.{EXAMPLE_INVALID}:{HTTP_TEST_PORT}"
    if case == "ftp-loopback":
        return f"ftp://{LOCALHOST_NAME}:{HTTP_TEST_PORT}/"
    if case == "file-scheme":
        return "file:///etc/hosts"
    if case == "missing-host":
        return "http:///no-host"
    if case == "empty":
        return ""
    if case == "no-scheme":
        return f"{LOCALHOST_NAME}:{HTTP_TEST_PORT}"
    if case == "http-tab-in-host":
        # RFC 6761 localhost variant with a tab; control char refuses.
        return f"http://local\thost:{HTTP_TEST_PORT}"
    if case == "http-non-numeric-port":
        # RFC 6761 localhost; non-numeric port refuses.
        return f"http://{LOCALHOST_NAME}:abc"
    if case == "https-newline":
        # RFC 2606 example.com; newline refuses even on https.
        return f"https://{EXAMPLE_COM}\n/"
    if case == "http-malformed-idna-label":
        # RFC 3492/5891 punycode, malformed on purpose; .invalid is RFC 2606.
        return f"http://xn--.{EXAMPLE_INVALID}:{HTTP_TEST_PORT}"
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
        "http-userinfo-localhost",
        "https-userinfo-example",
        "http-malformed-ipv6-bracket",
        "http-localhost-lookalike",
        "http-ip-lookalike",
        "ftp-loopback",
        "file-scheme",
        "missing-host",
        "empty",
        "no-scheme",
        "http-tab-in-host",
        "http-non-numeric-port",
        "https-newline",
        "http-malformed-idna-label",
    ],
)
def test_refused_base_url_makes_no_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], case: str
) -> None:
    monkeypatch.setattr(amw_approvals.httpx, "Client", _ExplodingClient)
    monkeypatch.setenv("VALID_API_KEYS", PLACEHOLDER_KEY)
    base_url = _refused_url(case)
    nonce = "test-nonce"
    for argv in (["list"], ["approve", nonce], ["deny", nonce]):
        assert amw_approvals.main(["--base-url", base_url, *argv]) == 1
        out = capsys.readouterr()
        assert "refusing to send the API key" in out.err
        assert "Traceback" not in out.err
        assert "Traceback" not in out.out
        assert PLACEHOLDER_KEY not in out.err
        assert PLACEHOLDER_KEY not in out.out


def test_refusal_happens_before_key_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(amw_approvals.httpx, "Client", _ExplodingClient)
    monkeypatch.delenv("VALID_API_KEYS", raising=False)
    url = f"http://{TEST_NET_1}:{HTTP_TEST_PORT}"
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
        f"http://{EXAMPLE_INVALID}:{HTTP_TEST_PORT}",
    )
    assert amw_approvals.main(["list"]) == 1
    out = capsys.readouterr()
    assert "refusing to send the API key" in out.err
    assert PLACEHOLDER_KEY not in out.out
    assert PLACEHOLDER_KEY not in out.err


def test_client_refuses_disallowed_url_directly() -> None:
    urls = [
        f"http://{TEST_NET_2}:{HTTP_TEST_PORT}",
        # RFC 3492/5891 punycode, malformed on purpose; .invalid is RFC 2606.
        f"http://xn--.{EXAMPLE_INVALID}:{HTTP_TEST_PORT}",
    ]
    for url in urls:
        with pytest.raises(ValueError, match="refusing to send the API key") as exc:
            amw_approvals._client(url, PLACEHOLDER_KEY)
        assert PLACEHOLDER_KEY not in str(exc.value)


def test_client_disables_trust_env_and_ignores_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # RFC 5737 TEST-NET-1, RFC 6335 fixed port: proxy decoy, never dialed.
    monkeypatch.setenv("HTTP_PROXY", f"http://{TEST_NET_1}:{HTTP_TEST_PORT}")
    # RFC 5737 TEST-NET-2, RFC 6335 fixed port: proxy decoy, never dialed.
    monkeypatch.setenv("ALL_PROXY", f"http://{TEST_NET_2}:{HTTP_TEST_PORT}")
    # RFC 6761 localhost, RFC 6335 fixed port: allowed http loopback URL.
    client = amw_approvals._client(
        f"http://{LOCALHOST_NAME}:{HTTP_TEST_PORT}", PLACEHOLDER_KEY
    )
    try:
        assert client.trust_env is False
        assert dict(client._mounts) == {}
        # Same env with trust_env=True mounts proxies, proving the decoys
        # above are live env httpx would otherwise honor. No request is sent.
        proving = amw_approvals.httpx.Client(trust_env=True)
        try:
            assert len(proving._mounts) > 0
        finally:
            proving.close()
    finally:
        client.close()


def _assert_no_key_leak(key: str, out: str, err: str) -> None:
    assert key not in out
    assert key not in err
    for start in range(len(key) - 7):
        part = key[start : start + 8]
        assert part not in out
        assert part not in err


@pytest.mark.parametrize("injected", ["\n", "\r", "\x85", "\x7f"])
def test_control_char_key_rejected_without_request(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    injected: str,
) -> None:
    token = secrets.token_hex(16)
    key = f"{token[:16]}{injected}{token[16:]}"
    monkeypatch.setattr(amw_approvals.httpx, "Client", _ExplodingClient)
    monkeypatch.setenv("VALID_API_KEYS", key)
    base_url = f"http://{LOOPBACK_V4}:{HTTP_TEST_PORT}"
    nonce = "test-nonce"
    for argv in (["list"], ["approve", nonce], ["deny", nonce]):
        assert amw_approvals.main(["--base-url", base_url, *argv]) == 1
        out = capsys.readouterr()
        assert "Traceback" not in out.err
        assert "Traceback" not in out.out
        assert "invalid API key" in out.err
        _assert_no_key_leak(key, out.out, out.err)


def test_http_error_hides_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    key = secrets.token_hex(16)

    class _LeakingClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get(self, path: str) -> _StubResponse:
            raise amw_approvals.httpx.ConnectError(f"dial boom {key} quoted")

        def post(self, path: str) -> _StubResponse:
            raise amw_approvals.httpx.ConnectError(f"dial boom {key} quoted")

    monkeypatch.setattr(amw_approvals.httpx, "Client", _LeakingClient)
    monkeypatch.setenv("VALID_API_KEYS", key)
    base_url = f"http://{LOOPBACK_V4}:{HTTP_TEST_PORT}"
    nonce = "test-nonce"
    for argv in (["list"], ["approve", nonce], ["deny", nonce]):
        assert amw_approvals.main(["--base-url", base_url, *argv]) == 1
        out = capsys.readouterr()
        assert "Traceback" not in out.err
        assert "Traceback" not in out.out
        assert "gateway unreachable at" in out.err
        assert base_url in out.err
        assert "ConnectError" in out.err
        assert "dial boom" not in out.err
        assert "dial boom" not in out.out
        _assert_no_key_leak(key, out.out, out.err)


def test_status_error_hides_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    key = secrets.token_hex(16)

    def _unauthorized(
        request: Any,
    ) -> Any:
        assert request.headers["X-API-Key"] == key
        return amw_approvals.httpx.Response(401, json={"detail": "unauthorized"})

    transport = amw_approvals.httpx.MockTransport(_unauthorized)
    _real_client = amw_approvals.httpx.Client

    def _mock_client(*args: Any, **kwargs: Any) -> Any:
        kwargs["transport"] = transport
        return _real_client(*args, **kwargs)

    monkeypatch.setattr(amw_approvals.httpx, "Client", _mock_client)
    monkeypatch.setenv("VALID_API_KEYS", key)
    base_url = f"http://{LOOPBACK_V4}:{HTTP_TEST_PORT}"
    nonce = "test-nonce"
    for argv in (["list"], ["approve", nonce], ["deny", nonce]):
        assert amw_approvals.main(["--base-url", base_url, *argv]) == 1
        out = capsys.readouterr()
        assert "Traceback" not in out.err
        assert "Traceback" not in out.out
        assert "HTTP 401" in out.err
        assert "gateway request failed: HTTP 401" in out.err
        assert base_url not in out.err
        _assert_no_key_leak(key, out.out, out.err)


def test_trailing_newline_key_accepted_and_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = secrets.token_hex(16)
    monkeypatch.setenv("VALID_API_KEYS", f"{key}\n")
    assert amw_approvals._read_api_key() == key
    # RFC 1122 loopback, RFC 6335 fixed port: allowed http loopback URL.
    client = amw_approvals._client(
        f"http://{LOOPBACK_V4}:{HTTP_TEST_PORT}", amw_approvals._read_api_key()
    )
    try:
        assert client.headers["X-API-Key"] == key
    finally:
        client.close()


def test_non_json_response_prints_fixed_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    key = secrets.token_hex(16)

    class _NonJsonResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            raise json.JSONDecodeError("Expecting value", "not json", 0)

    class _NonJsonClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get(self, path: str) -> _NonJsonResponse:
            return _NonJsonResponse()

        def post(self, path: str) -> _NonJsonResponse:
            return _NonJsonResponse()

    monkeypatch.setattr(amw_approvals.httpx, "Client", _NonJsonClient)
    monkeypatch.setenv("VALID_API_KEYS", key)
    base_url = f"http://{LOOPBACK_V4}:{HTTP_TEST_PORT}"
    nonce = "test-nonce"
    for argv in (["list"], ["approve", nonce], ["deny", nonce]):
        assert amw_approvals.main(["--base-url", base_url, *argv]) == 1
        out = capsys.readouterr()
        assert "Traceback" not in out.err
        assert "Traceback" not in out.out
        assert "gateway returned a non-JSON response" in out.err
        _assert_no_key_leak(key, out.out, out.err)


@pytest.mark.parametrize("kind", ["list", "null", "string"])
def test_unexpected_json_shape_prints_fixed_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    key = secrets.token_hex(16)
    payload: Any
    if kind == "list":
        payload = []
    elif kind == "null":
        payload = None
    else:
        payload = f"unexpected-{secrets.token_hex(8)}"

    class _UnexpectedResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> Any:
            return payload

    class _UnexpectedClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get(self, path: str) -> _UnexpectedResponse:
            return _UnexpectedResponse()

        def post(self, path: str) -> _UnexpectedResponse:
            return _UnexpectedResponse()

    monkeypatch.setattr(amw_approvals.httpx, "Client", _UnexpectedClient)
    monkeypatch.setenv("VALID_API_KEYS", key)
    # RFC 1122 loopback, RFC 6335 fixed port: allowed http loopback URL.
    base_url = f"http://{LOOPBACK_V4}:{HTTP_TEST_PORT}"
    nonce = "test-nonce"
    for argv in (["list"], ["approve", nonce], ["deny", nonce]):
        assert amw_approvals.main(["--base-url", base_url, *argv]) == 1
        out = capsys.readouterr()
        assert "Traceback" not in out.err
        assert "Traceback" not in out.out
        assert "gateway returned an unexpected response" in out.err
        if isinstance(payload, str):
            assert payload not in out.out
            assert payload not in out.err
        _assert_no_key_leak(key, out.out, out.err)


@pytest.mark.parametrize(
    "approvals",
    [
        "not-a-list",
        None,
        ["not-a-dict"],
        [{"nonce": "test-nonce"}],
        [
            {
                "nonce": "test-nonce",
                "tool": "test-tool",
                "principal": "test-principal",
                "deadline": "soon",
            }
        ],
    ],
)
def test_malformed_approvals_list_prints_fixed_message(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    approvals: Any,
) -> None:
    key = secrets.token_hex(16)

    class _MalformedResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> Any:
            return {"approvals": approvals}

    class _MalformedClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get(self, path: str) -> _MalformedResponse:
            return _MalformedResponse()

    monkeypatch.setattr(amw_approvals.httpx, "Client", _MalformedClient)
    monkeypatch.setenv("VALID_API_KEYS", key)
    # RFC 1122 loopback, RFC 6335 fixed port: allowed http loopback URL.
    base_url = f"http://{LOOPBACK_V4}:{HTTP_TEST_PORT}"
    assert amw_approvals.main(["--base-url", base_url, "list"]) == 1
    out = capsys.readouterr()
    assert "Traceback" not in out.err
    assert "Traceback" not in out.out
    assert "gateway returned an unexpected response" in out.err
    _assert_no_key_leak(key, out.out, out.err)


@pytest.mark.parametrize(
    "deadline",
    [1e300, float("nan"), float("inf"), float("-inf"), True, False],
)
def test_unrenderable_deadline_prints_fixed_message(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    deadline: Any,
) -> None:
    key = secrets.token_hex(16)
    nonce = f"test-nonce-{secrets.token_hex(4)}"

    class _DeadlineResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> Any:
            return {
                "approvals": [
                    {
                        "nonce": nonce,
                        "tool": "test-tool",
                        "principal": "test-principal",
                        "deadline": deadline,
                    }
                ]
            }

    class _DeadlineClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get(self, path: str) -> _DeadlineResponse:
            return _DeadlineResponse()

    monkeypatch.setattr(amw_approvals.httpx, "Client", _DeadlineClient)
    monkeypatch.setenv("VALID_API_KEYS", key)
    # RFC 1122 loopback, RFC 6335 fixed port: allowed http loopback URL.
    base_url = f"http://{LOOPBACK_V4}:{HTTP_TEST_PORT}"
    assert amw_approvals.main(["--base-url", base_url, "list"]) == 1
    out = capsys.readouterr()
    assert "Traceback" not in out.err
    assert "Traceback" not in out.out
    assert "gateway returned an unexpected response" in out.err
    assert out.out == ""
    assert nonce not in out.err
    _assert_no_key_leak(key, out.out, out.err)


def test_missing_approvals_key_prints_fixed_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    key = secrets.token_hex(16)

    class _MissingKeyResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> Any:
            return {}

    class _MissingKeyClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get(self, path: str) -> _MissingKeyResponse:
            return _MissingKeyResponse()

    monkeypatch.setattr(amw_approvals.httpx, "Client", _MissingKeyClient)
    monkeypatch.setenv("VALID_API_KEYS", key)
    # RFC 1122 loopback, RFC 6335 fixed port: allowed http loopback URL.
    base_url = f"http://{LOOPBACK_V4}:{HTTP_TEST_PORT}"
    assert amw_approvals.main(["--base-url", base_url, "list"]) == 1
    out = capsys.readouterr()
    assert "Traceback" not in out.err
    assert "Traceback" not in out.out
    assert "gateway returned an unexpected response" in out.err
    assert "no pending approvals" not in out.out
    assert out.out == ""
    _assert_no_key_leak(key, out.out, out.err)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"status": ""},
        {"status": None},
        {"status": 123},
        {"status": True},
        {"status": []},
        {"status": {}},
    ],
)
def test_decide_invalid_status_prints_fixed_message(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: Any,
) -> None:
    key = secrets.token_hex(16)

    class _StatusResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> Any:
            return payload

    class _StatusClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def post(self, path: str) -> _StatusResponse:
            return _StatusResponse()

    monkeypatch.setattr(amw_approvals.httpx, "Client", _StatusClient)
    monkeypatch.setenv("VALID_API_KEYS", key)
    # RFC 1122 loopback, RFC 6335 fixed port: allowed http loopback URL.
    base_url = f"http://{LOOPBACK_V4}:{HTTP_TEST_PORT}"
    nonce = "test-nonce"
    for argv in (["approve", nonce], ["deny", nonce]):
        assert amw_approvals.main(["--base-url", base_url, *argv]) == 1
        out = capsys.readouterr()
        assert "Traceback" not in out.err
        assert "Traceback" not in out.out
        assert "gateway returned an unexpected response" in out.err
        assert out.out == ""
        _assert_no_key_leak(key, out.out, out.err)
