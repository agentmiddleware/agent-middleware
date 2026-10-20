"""Behavior tests for the standard MCP retry example.

The example is a client, not the gateway. These tests stay offline: they check
the config, the receipt reader, and the retry decision. They do not open a
socket and they do not read a database URL.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from mcp.types import CallToolResult
from typing_extensions import Self

HERE = Path(__file__).resolve().parent
MODULE_PATH = HERE / "call_with_retry.py"
CONFIG_PATH = HERE / "mcp.json"

_spec = importlib.util.spec_from_file_location(
    "mcp_client_call_with_retry", MODULE_PATH
)
assert _spec is not None and _spec.loader is not None
client = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = client
_spec.loader.exec_module(client)

API_KEY = "amw_dev_example_key"
RECEIPT_KEY = "io.agentmiddleware/receipt"


def _receipt(receipt_id: str, outcome: str = "success") -> dict:
    return {"receipt_id": receipt_id, "outcome": outcome}


def _sdk_result(
    receipt_id: str, *, outcome: str = "success", is_error: bool = False
) -> dict:
    """Shape produced by CallToolResult.model_dump (field name meta, plus receipt)."""
    receipt = _receipt(receipt_id, outcome)
    return {
        "meta": {RECEIPT_KEY: dict(receipt)},
        "receipt": dict(receipt),
        "isError": is_error,
        "content": [],
    }


def _wire_result(receipt_id: str, *, outcome: str = "success") -> dict:
    """Shape of the JSON result body on POST /mcp (alias _meta, plus receipt)."""
    receipt = _receipt(receipt_id, outcome)
    return {
        "_meta": {RECEIPT_KEY: dict(receipt)},
        "receipt": dict(receipt),
        "isError": False,
        "content": [],
    }


def test_client_config_authenticates_without_pinning_one_action() -> None:
    """A connection header is sent on every request, including every tool call.

    One Idempotency-Key there makes the second distinct action a replay or a
    conflict, and an unset placeholder becomes a blank key the gateway refuses
    before any effect. The API key stays. The retry script supplies a per-run key.
    """
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    server = config["mcpServers"]["agent-middleware"]
    assert server["type"] == "http"
    assert server["url"] == "http://127.0.0.1:8000/mcp"
    headers = server["headers"]
    assert headers["X-API-Key"] == "${AGENT_API_KEY}"
    folded = {name.lower(): value for name, value in headers.items()}
    assert "idempotency-key" not in folded
    assert "idempotency" not in json.dumps(headers).lower()


def test_receipt_id_reads_sdk_dump_and_wire_result() -> None:
    assert client.receipt_id(_sdk_result("rcpt-sdk")) == "rcpt-sdk"
    assert client._outcome(_sdk_result("rcpt-sdk")) == "success"
    assert client.receipt_id(_wire_result("rcpt-wire")) == "rcpt-wire"
    assert client._outcome(_wire_result("rcpt-wire", outcome="denied")) == "denied"


def test_receipt_reader_accepts_official_mcp_result_dump() -> None:
    receipt = _receipt("rcpt-sdk")
    result = CallToolResult.model_validate(
        {
            "content": [],
            "isError": False,
            "_meta": {RECEIPT_KEY: receipt},
            "receipt": receipt,
        }
    ).model_dump(mode="json")
    assert client.retry_failure(result, result) is None


def test_receipt_id_reads_top_level_receipt_when_meta_is_absent() -> None:
    result = {"isError": False, "receipt": _receipt("rcpt-top")}
    assert client.receipt_id(result) == "rcpt-top"
    assert client._outcome(result) == "success"


def test_receipt_reader_does_not_crash_on_a_non_dict_receipt() -> None:
    for result in (
        {"_meta": {RECEIPT_KEY: "rcpt-not-an-object"}, "isError": False},
        {"meta": "nope", "isError": False},
        {"receipt": "rcpt-not-an-object", "isError": False},
    ):
        assert client.receipt_id(result) is None
        assert client._outcome(result) is None


def test_receipt_reader_refuses_disagreeing_copies() -> None:
    disagreed_ids = {
        "meta": {RECEIPT_KEY: _receipt("rcpt-a")},
        "_meta": {RECEIPT_KEY: _receipt("rcpt-b")},
    }
    assert client.receipt_id(disagreed_ids) is None

    disagreed_outcomes = {
        "meta": {RECEIPT_KEY: _receipt("rcpt-1", "success")},
        "receipt": _receipt("rcpt-1", "denied"),
    }
    assert client.receipt_id(disagreed_outcomes) is None
    assert client._outcome(disagreed_outcomes) is None

    missing_outcome = {
        "meta": {RECEIPT_KEY: _receipt("rcpt-1")},
        "receipt": {"receipt_id": "rcpt-1"},
    }
    assert client.receipt_id(missing_outcome) is None
    assert client._outcome(missing_outcome) is None


def test_matching_copies_still_return_the_one_receipt_id() -> None:
    result = {
        "meta": {RECEIPT_KEY: _receipt("rcpt-same")},
        "_meta": {RECEIPT_KEY: _receipt("rcpt-same")},
        "receipt": _receipt("rcpt-same"),
    }
    assert client.receipt_id(result) == "rcpt-same"
    assert client._outcome(result) == "success"


class _Session:
    def __init__(self, payload: dict, seen: dict) -> None:
        self._payload = payload
        self._seen = seen

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def initialize(self) -> None:
        self._seen["initialized"] = True

    async def call_tool(self, name: str, arguments: dict) -> CallToolResult:
        self._seen["tool"] = name
        self._seen["arguments"] = arguments
        return CallToolResult.model_validate(self._payload)


def test_call_once_sends_key_headers_and_does_not_put_the_secret_in_the_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict = {}

    class _Client:
        def __init__(self, url: str, headers: dict | None = None) -> None:
            seen["url"] = url
            seen["headers"] = dict(headers or {})

        async def __aenter__(self) -> tuple:
            return (None, None, None)

        async def __aexit__(self, *exc: object) -> bool:
            return False

    monkeypatch.setattr(client, "streamablehttp_client", _Client)
    monkeypatch.setattr(
        client,
        "ClientSession",
        lambda read, write: _Session(_sdk_result("rcpt-1"), seen),
    )

    payload = asyncio.run(client.call_once(API_KEY, "retry-key-1"))

    assert payload["isError"] is False
    assert seen["initialized"] is True
    assert seen["url"] == client.URL
    assert API_KEY not in seen["url"]
    assert seen["headers"] == {
        "X-API-Key": API_KEY,
        "Idempotency-Key": "retry-key-1",
    }
    assert seen["tool"] == client.TOOL
    assert seen["arguments"] == {"text": "hello from an MCP client"}


@pytest.mark.parametrize(
    "actual_receipt",
    [
        _receipt("rcpt-other"),
        _receipt("rcpt-good", "denied"),
        {"receipt_id": "rcpt-good"},
        "malformed",
    ],
    ids=["conflicting-id", "denied", "missing-outcome", "malformed"],
)
def test_call_once_keeps_wire_meta_when_an_extra_meta_claims_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    actual_receipt: dict | str,
) -> None:
    """The MCP model must not let an extra meta overwrite the wire _meta."""
    good = _receipt("rcpt-good")
    wire = {
        "content": [],
        "isError": False,
        "_meta": {RECEIPT_KEY: actual_receipt},
        "meta": {RECEIPT_KEY: good},
        "receipt": good,
    }

    class _Client:
        async def __aenter__(self) -> tuple:
            return (None, None, None)

        async def __aexit__(self, *exc: object) -> bool:
            return False

    monkeypatch.setattr(
        client, "streamablehttp_client", lambda *args, **kwargs: _Client()
    )
    monkeypatch.setattr(client, "ClientSession", lambda read, write: _Session(wire, {}))

    result = asyncio.run(client.call_once(API_KEY, "retry-key-1"))
    assert result["_meta"][RECEIPT_KEY] == actual_receipt
    assert client.retry_failure(result, result) is not None

    _patch_main(monkeypatch, [result, result])
    with pytest.raises(SystemExit):
        asyncio.run(client.main())
    assert "receipt:" not in capsys.readouterr().out


def test_call_once_refuses_a_blank_or_short_api_key_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("client connected")

    monkeypatch.setattr(client, "streamablehttp_client", _boom)
    for api_key in ("", "   ", "short", "bad\nkey-value"):
        with pytest.raises(SystemExit):
            asyncio.run(client.call_once(api_key, "retry-key-1"))


def test_call_once_refuses_an_unusable_idempotency_key_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("client connected")

    monkeypatch.setattr(client, "streamablehttp_client", _boom)
    for key in ("", "   ", "x" * 129, "bad\nkey", "bad\x7fkey"):
        with pytest.raises(SystemExit):
            asyncio.run(client.call_once(API_KEY, key))


def _patch_main(
    monkeypatch: pytest.MonkeyPatch,
    results: list[dict],
) -> list[str]:
    keys: list[str] = []

    async def _fake(api_key: str, idempotency_key: str) -> dict:
        assert api_key == API_KEY
        keys.append(idempotency_key)
        return results[len(keys) - 1]

    monkeypatch.setattr(client, "call_once", _fake)
    monkeypatch.setenv("AGENT_API_KEY", API_KEY)
    return keys


def test_main_reuses_one_generated_key_and_prints_it_before_calling(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("AMW_IDEMPOTENCY_KEY", raising=False)
    printed_before_call: list[str] = []
    sent: list[str] = []

    async def _fake(api_key: str, idempotency_key: str) -> dict:
        printed_before_call.append(capsys.readouterr().out)
        sent.append(idempotency_key)
        return _sdk_result("rcpt-1")

    monkeypatch.setattr(client, "call_once", _fake)
    monkeypatch.setenv("AGENT_API_KEY", API_KEY)

    asyncio.run(client.main())
    first_out = printed_before_call[0]
    assert sent[0] == sent[1]
    assert f"idempotency key: {sent[0]}" in first_out
    assert API_KEY not in first_out
    assert API_KEY not in capsys.readouterr().out


def test_main_uses_the_env_key_for_both_calls(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    keys = _patch_main(
        monkeypatch,
        [_sdk_result("rcpt-1"), _sdk_result("rcpt-1")],
    )
    monkeypatch.setenv("AMW_IDEMPOTENCY_KEY", "kept-for-a-lost-response")
    asyncio.run(client.main())
    assert keys == ["kept-for-a-lost-response", "kept-for-a-lost-response"]
    output = capsys.readouterr().out
    assert "idempotency key: kept-for-a-lost-response" in output
    assert "first receipt: rcpt-1 outcome: success" in output
    assert "retry receipt: rcpt-1 outcome: success" in output


def test_main_mints_a_fresh_key_per_process_when_env_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AMW_IDEMPOTENCY_KEY", raising=False)
    first = _patch_main(monkeypatch, [_sdk_result("rcpt-1"), _sdk_result("rcpt-1")])
    asyncio.run(client.main())
    second = _patch_main(monkeypatch, [_sdk_result("rcpt-2"), _sdk_result("rcpt-2")])
    asyncio.run(client.main())
    assert first[0] == first[1]
    assert second[0] == second[1]
    assert first[0] != second[0]
    assert first[0].startswith("mcp-example-")
    assert len(first[0]) <= 128


def test_main_exits_when_the_retry_is_not_the_same_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_main(monkeypatch, [_sdk_result("rcpt-1"), _sdk_result("rcpt-2")])
    monkeypatch.delenv("AMW_IDEMPOTENCY_KEY", raising=False)
    with pytest.raises(SystemExit) as raised:
        asyncio.run(client.main())
    assert raised.value.code not in (0, None)
    assert "different receipt" in str(raised.value)


def test_main_exits_when_only_the_retry_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_main(
        monkeypatch,
        [_sdk_result("rcpt-1"), _sdk_result("rcpt-1", is_error=True)],
    )
    monkeypatch.delenv("AMW_IDEMPOTENCY_KEY", raising=False)
    with pytest.raises(SystemExit) as raised:
        asyncio.run(client.main())
    assert "error" in str(raised.value).lower()


def test_main_exits_when_outcomes_differ(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_main(
        monkeypatch,
        [
            _sdk_result("rcpt-1", outcome="success"),
            _sdk_result("rcpt-1", outcome="denied"),
        ],
    )
    monkeypatch.delenv("AMW_IDEMPOTENCY_KEY", raising=False)
    with pytest.raises(SystemExit) as raised:
        asyncio.run(client.main())
    assert "outcome" in str(raised.value).lower()


def test_main_exits_when_the_tool_call_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    failed = _sdk_result("rcpt-1", is_error=True)
    _patch_main(monkeypatch, [failed, failed])
    monkeypatch.delenv("AMW_IDEMPOTENCY_KEY", raising=False)
    with pytest.raises(SystemExit) as raised:
        asyncio.run(client.main())
    assert "error" in str(raised.value).lower()


def test_main_exits_when_the_receipt_id_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = {"meta": {}, "isError": False, "content": []}
    _patch_main(monkeypatch, [empty, empty])
    monkeypatch.delenv("AMW_IDEMPOTENCY_KEY", raising=False)
    with pytest.raises(SystemExit) as raised:
        asyncio.run(client.main())
    assert "receipt" in str(raised.value).lower()


@pytest.mark.parametrize(
    "result",
    [
        _sdk_result("rcpt-1", outcome="denied"),
        {"meta": {RECEIPT_KEY: {"receipt_id": "rcpt-1"}}, "isError": False},
        _sdk_result("   "),
    ],
    ids=["denied", "missing-outcome", "blank-receipt-id"],
)
def test_main_refuses_matching_receipts_without_successful_nonblank_claims(
    monkeypatch: pytest.MonkeyPatch, result: dict
) -> None:
    _patch_main(monkeypatch, [result, result])
    monkeypatch.delenv("AMW_IDEMPOTENCY_KEY", raising=False)
    with pytest.raises(SystemExit) as raised:
        asyncio.run(client.main())
    assert raised.value.code not in (0, None)


def test_main_refuses_a_blank_api_key_without_calling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def _fake(api_key: str, idempotency_key: str) -> dict:
        nonlocal called
        called = True
        return _sdk_result("rcpt-1")

    monkeypatch.setattr(client, "call_once", _fake)
    monkeypatch.setenv("AGENT_API_KEY", "   ")
    with pytest.raises(SystemExit):
        asyncio.run(client.main())
    assert called is False


def test_main_refuses_a_bad_saved_idempotency_key_without_calling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def _fake(api_key: str, idempotency_key: str) -> dict:
        nonlocal called
        called = True
        return _sdk_result("rcpt-1")

    monkeypatch.setattr(client, "call_once", _fake)
    monkeypatch.setenv("AGENT_API_KEY", API_KEY)
    monkeypatch.setenv("AMW_IDEMPOTENCY_KEY", "bad\nkey")
    with pytest.raises(SystemExit):
        asyncio.run(client.main())
    assert called is False


@pytest.mark.parametrize(
    "results",
    [
        [_sdk_result("rcpt-1", outcome="denied")] * 2,
        [{"meta": {RECEIPT_KEY: {"receipt_id": "rcpt-1"}}}] * 2,
        [_sdk_result("   ")] * 2,
        [_sdk_result("rcpt-1"), _sdk_result("rcpt-2")],
        [{"meta": "malformed"}] * 2,
        [
            {
                "meta": {RECEIPT_KEY: _receipt("rcpt-1")},
                "receipt": {"receipt_id": "rcpt-1"},
            }
        ]
        * 2,
    ],
    ids=[
        "denied",
        "missing-outcome",
        "blank-id",
        "changed-id",
        "malformed",
        "missing-copy-outcome",
    ],
)
def test_failed_pair_does_not_print_a_successful_receipt(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    results: list[dict],
) -> None:
    _patch_main(monkeypatch, results)
    monkeypatch.delenv("AMW_IDEMPOTENCY_KEY", raising=False)
    with pytest.raises(SystemExit):
        asyncio.run(client.main())
    output = capsys.readouterr().out
    assert output.startswith("idempotency key: mcp-example-")
    assert "receipt:" not in output
    assert "outcome: success" not in output


@pytest.mark.parametrize("failure_call", [1, 2])
def test_transport_failure_does_not_print_a_successful_receipt(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure_call: int,
) -> None:
    calls = 0

    async def _fake(api_key: str, idempotency_key: str) -> dict:
        nonlocal calls
        calls += 1
        if calls == failure_call:
            raise OSError("transport failed")
        return _sdk_result("rcpt-1")

    monkeypatch.setattr(client, "call_once", _fake)
    monkeypatch.setenv("AGENT_API_KEY", API_KEY)
    monkeypatch.delenv("AMW_IDEMPOTENCY_KEY", raising=False)
    with pytest.raises(OSError, match="transport failed"):
        asyncio.run(client.main())
    output = capsys.readouterr().out
    assert output.startswith("idempotency key: mcp-example-")
    assert "receipt:" not in output
    assert "outcome: success" not in output
