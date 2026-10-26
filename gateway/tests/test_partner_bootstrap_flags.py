"""Operator bounds on partner_api_key_bootstrap: --daily-limit,
--expires-in-days, --max-uses reach the right request bodies, and stay
absent when not requested (so older servers see unchanged payloads).

CLI modes never print the minted secret; --key-file writes it at mode 0600.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import sys
import time
import uuid
from pathlib import Path

import pytest

import scripts.partner_api_key_bootstrap as bootstrap

# Runtime-generated fixtures only (no hardcoded secrets or ids). Distinct
# "mnt-"/"kpfx-" alphabets keep the minted secret's head/tail out of the
# server-provided prefix shown on stdout; the guard loop below removes any
# residual hex collision so leak assertions stay meaningful every run.
MINTED_KEY = f"mnt-{secrets.token_hex(16)}-{time.time_ns():x}"
BOOTSTRAP_SECRET = f"boot-{secrets.token_hex(16)}-{time.time_ns():x}"
KEY_ID = f"key-{uuid.uuid4().hex[:12]}"
KEY_PREFIX = f"kpfx-{secrets.token_hex(3)}"
WAL_SPONSOR = f"wal-{uuid.uuid4().hex[:12]}"
WAL_AGENT = f"wal-{uuid.uuid4().hex[:12]}"
while (
    MINTED_KEY[:8] in KEY_PREFIX
    or MINTED_KEY[-4:] in KEY_PREFIX
    or MINTED_KEY[-4:] in KEY_ID
    or MINTED_KEY[-4:] in WAL_AGENT
    or MINTED_KEY[-4:] in WAL_SPONSOR
):
    KEY_PREFIX = f"kpfx-{secrets.token_hex(3)}"
    KEY_ID = f"key-{uuid.uuid4().hex[:12]}"
    WAL_SPONSOR = f"wal-{uuid.uuid4().hex[:12]}"
    WAL_AGENT = f"wal-{uuid.uuid4().hex[:12]}"


class _FakeResponse:
    def __init__(self, payload: dict):
        self.status_code = 200
        self._payload = payload
        self.text = ""

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Stands in for httpx.Client; records every POST body by path."""

    def __init__(self, **kwargs):
        self.posts: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, path):
        return _FakeResponse({})

    def post(self, path, json):
        self.posts.append((path, json))
        if path == "/v1/billing/wallets/sponsor":
            return _FakeResponse({"wallet_id": WAL_SPONSOR})
        if path == "/v1/billing/wallets/agent":
            return _FakeResponse({"wallet_id": WAL_AGENT})
        return _FakeResponse(
            {"api_key": MINTED_KEY, "key_id": KEY_ID, "key_prefix": KEY_PREFIX}
        )


def _provision(monkeypatch, **overrides):
    captured = {}

    def factory(**kwargs):
        client = _FakeClient(**kwargs)
        captured["client"] = client
        return client

    monkeypatch.setattr(bootstrap.httpx, "Client", factory)
    result = bootstrap.provision(
        api_url="https://api.example.test",
        bootstrap_key="amw_live_test",
        sponsor_name="Partner Co",
        agent_id="partner-agent-001",
        budget_credits=1000.0,
        initial_credits=10000.0,
        key_name="partner-agent",
        sponsor_wallet_id=None,
        agent_wallet_id=None,
        **overrides,
    )
    return result, dict(captured["client"].posts)


def _cli_args(*extra: str) -> list[str]:
    return [
        "partner_api_key_bootstrap.py",
        "--api-url",
        "https://api.example.test",
        "--sponsor-name",
        "Synthetic Partner",
        "--agent-id",
        "synthetic-agent",
        *extra,
    ]


def _run_main(monkeypatch, extra: list[str]) -> int:
    monkeypatch.setenv("BOOTSTRAP_KEY", BOOTSTRAP_SECRET)
    monkeypatch.setattr(bootstrap.httpx, "Client", _FakeClient)
    monkeypatch.setattr(sys, "argv", _cli_args(*extra))
    return bootstrap.main()


def _assert_no_secret_leak(combined: str) -> None:
    assert MINTED_KEY not in combined
    assert MINTED_KEY[:8] not in combined
    assert MINTED_KEY[-4:] not in combined
    assert BOOTSTRAP_SECRET not in combined


def test_bounds_flags_reach_request_bodies(monkeypatch):
    result, posts = _provision(
        monkeypatch,
        daily_limit=250.0,
        expires_in_days=30,
        max_uses=1000,
    )
    assert posts["/v1/billing/wallets/agent"]["daily_limit"] == 250.0
    assert posts["/v1/api-keys"]["expires_in_days"] == 30
    assert posts["/v1/api-keys"]["max_uses"] == 1000
    assert result["api_key"] == MINTED_KEY


def test_bounds_default_to_absent(monkeypatch):
    _, posts = _provision(monkeypatch)
    assert "daily_limit" not in posts["/v1/billing/wallets/agent"]
    assert "expires_in_days" not in posts["/v1/api-keys"]
    assert "max_uses" not in posts["/v1/api-keys"]


@pytest.mark.parametrize("mode", ["--json", None])
def test_cli_output_modes_never_print_full_key(monkeypatch, capsys, mode):
    extra = [mode] if mode else []
    assert _run_main(monkeypatch, extra) == 0
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    _assert_no_secret_leak(combined)
    assert "[created]" in captured.err
    assert "[created]" not in captured.out
    if mode == "--json":
        payload = json.loads(captured.out)
        assert "api_key" not in payload
        assert "api_key_masked" not in payload
        assert "key_file" not in payload
        assert payload["key_id"] == KEY_ID
        assert payload["wallet_id"] == WAL_AGENT
        assert payload["key_prefix"] == KEY_PREFIX
    else:
        assert captured.out.startswith("Partner API key bootstrap OK\n")
        assert f"key_id:             {KEY_ID}\n" in captured.out
        assert f"key_prefix:         {KEY_PREFIX}\n" in captured.out
        assert "api_key" not in captured.out


@pytest.mark.parametrize("mode", ["--key-only", "--json", None])
def test_cli_key_file_writes_0600_and_omits_full_key_from_stdio(
    monkeypatch, capsys, tmp_path, mode
):
    key_path = tmp_path / "minted.key"
    extra = ["--key-file", str(key_path)]
    if mode:
        extra.append(mode)
    assert _run_main(monkeypatch, extra) == 0
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    _assert_no_secret_leak(combined)
    assert str(key_path) in captured.out
    assert key_path.read_text(encoding="utf-8") == MINTED_KEY
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    if mode == "--key-only":
        assert captured.out == f"{key_path}\n"
    elif mode == "--json":
        payload = json.loads(captured.out)
        assert "api_key" not in payload
        assert "api_key_masked" not in payload
        assert payload["key_file"] == str(key_path)
        assert payload["key_id"] == KEY_ID
    else:
        assert "api_key" not in captured.out
        assert f"key_file:           {key_path}\n" in captured.out


def test_key_file_refuses_existing_path(monkeypatch, capsys, tmp_path):
    key_path = tmp_path / "minted.key"
    key_path.write_text("preexisting", encoding="utf-8")
    with pytest.raises(SystemExit, match="already exists"):
        _run_main(monkeypatch, ["--key-file", str(key_path)])
    captured = capsys.readouterr()
    assert MINTED_KEY not in captured.out + captured.err
    assert key_path.read_text(encoding="utf-8") == "preexisting"


# Fixed, obviously synthetic values for the reserve/mint/write tests below.
FIXED_KEY = "fixed-test-key-value-001"
FIXED_BODY_SENTINEL = "fixed-sentinel-response-body-001"


def test_reserve_key_file_uses_exclusive_0600_create(monkeypatch, tmp_path):
    key_path = tmp_path / "minted.key"
    real_open = os.open
    seen: dict[str, int] = {}

    def _record_open(path, flags, mode=0o777):
        seen["flags"] = flags
        seen["mode"] = mode
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", _record_open)
    fd = bootstrap._reserve_key_file(str(key_path))
    os.close(fd)
    assert seen["flags"] == (os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    assert seen["mode"] == 0o600


def test_existing_key_file_skips_mint(monkeypatch, tmp_path):
    key_path = tmp_path / "minted.key"
    key_path.write_text("fixed-preexisting-content", encoding="utf-8")

    def _no_mint(**kwargs):
        raise AssertionError("provision must not run when --key-file exists")

    monkeypatch.setenv("BOOTSTRAP_KEY", BOOTSTRAP_SECRET)
    monkeypatch.setattr(bootstrap, "provision", _no_mint)
    monkeypatch.setattr(sys, "argv", _cli_args("--key-file", str(key_path)))
    with pytest.raises(SystemExit, match="already exists"):
        bootstrap.main()
    assert key_path.read_text(encoding="utf-8") == "fixed-preexisting-content"


def test_failed_mint_removes_reserved_key_file(monkeypatch, tmp_path):
    key_path = tmp_path / "minted.key"

    def _fail(**kwargs):
        raise SystemExit("error: fixed-mint-failure")

    monkeypatch.setenv("BOOTSTRAP_KEY", BOOTSTRAP_SECRET)
    monkeypatch.setattr(bootstrap, "provision", _fail)
    monkeypatch.setattr(sys, "argv", _cli_args("--key-file", str(key_path)))
    with pytest.raises(SystemExit, match="fixed-mint-failure"):
        bootstrap.main()
    assert not key_path.exists()


def test_write_key_fd_handles_short_writes_and_fsyncs(monkeypatch, tmp_path):
    key_path = tmp_path / "minted.key"
    fd = bootstrap._reserve_key_file(str(key_path))
    real_write = os.write
    real_fsync = os.fsync
    fsynced: list[int] = []

    def _short_write(target_fd, data):
        if target_fd != fd:
            return real_write(target_fd, data)
        return real_write(target_fd, bytes(data[:1]))

    def _record_fsync(target_fd):
        fsynced.append(target_fd)
        return real_fsync(target_fd)

    monkeypatch.setattr(os, "write", _short_write)
    monkeypatch.setattr(os, "fsync", _record_fsync)
    bootstrap._write_key_fd(fd, str(key_path), FIXED_KEY)
    assert key_path.read_text(encoding="utf-8") == FIXED_KEY
    assert fsynced == [fd]
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_write_key_fd_failure_removes_file_and_closes_fd(monkeypatch, tmp_path):
    key_path = tmp_path / "minted.key"
    fd = bootstrap._reserve_key_file(str(key_path))
    real_write = os.write

    def _fail_write(target_fd, data):
        if target_fd == fd:
            raise OSError("fixed-write-failure")
        return real_write(target_fd, data)

    monkeypatch.setattr(os, "write", _fail_write)
    with pytest.raises(SystemExit, match="fixed-write-failure"):
        bootstrap._write_key_fd(fd, str(key_path), FIXED_KEY)
    assert not key_path.exists()
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(fd)


def test_post_error_omits_response_body():
    class _ErrorResponse:
        status_code = 502
        text = FIXED_BODY_SENTINEL

    class _ErrorClient:
        def post(self, path, json):
            return _ErrorResponse()

    with pytest.raises(SystemExit) as excinfo:
        bootstrap._post(_ErrorClient(), "/v1/api-keys", {"key_name": "fixed-name"})
    message = str(excinfo.value)
    assert "502" in message
    assert FIXED_BODY_SENTINEL not in message


def test_key_only_without_key_file_is_argparse_error(monkeypatch, capsys):
    def _no_mint(**kwargs):
        raise AssertionError("provision must not run without --key-file")

    monkeypatch.setenv("BOOTSTRAP_KEY", BOOTSTRAP_SECRET)
    monkeypatch.setattr(bootstrap, "provision", _no_mint)
    monkeypatch.setattr(sys, "argv", _cli_args("--key-only"))
    with pytest.raises(SystemExit) as excinfo:
        bootstrap.main()
    assert excinfo.value.code == 2
    assert "requires --key-file" in capsys.readouterr().err


def test_bootstrap_doc_creates_config_dir_before_key_file():
    doc = (
        Path(__file__).resolve().parents[1] / "docs" / "partner-api-key-bootstrap.md"
    ).read_text(encoding="utf-8")
    mkdir_at = doc.index("mkdir -p -m 700 ~/.config/amw")
    key_file_at = doc.index("--key-file")
    assert mkdir_at < key_file_at
