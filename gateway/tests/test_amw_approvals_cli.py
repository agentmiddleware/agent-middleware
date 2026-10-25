"""Tests for the local approve/deny CLI.

The placeholder key below follows the synthetic style used by the other
auth-adjacent tests; it is never a real credential.
"""

from __future__ import annotations

import importlib.util
import secrets
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
