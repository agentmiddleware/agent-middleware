"""Sync bridge tests for the governed CrewAI wrapper.

CrewAI drives every tool through the synchronous ``_run`` entry point, so
these tests exercise ``_run`` directly (the existing governed-flow tests only
cover ``_arun``). They fail on the old ``asyncio.get_event_loop().
run_until_complete`` bridge: calling ``_run`` from inside a running event
loop returned ``"Error: This event loop is already running"``, and every
call emitted a deprecation warning about there being no current event loop.
"""

import json
import warnings
from datetime import UTC, datetime

import httpx
import pytest

# isort: split
from crewai_b2a import B2AClient, CrewAIB2ATool
from crewai_b2a import tool as tool_module


def _permit_payload() -> dict:
    return {
        "permit_id": "permit-sync-1",
        "issuer_wallet_id": "wallet-1",
        "subject_wallet_id": "wallet-1",
        "subject_key_id": "key-1",
        "scopes": ["tool:partner.search:invoke", "billing:charge"],
        "allowed_tools": ["partner.search"],
        "max_credits": "100",
        "spent_credits": "0",
        "expires_at": datetime.now(UTC).isoformat(),
        "nonce": "nonce-sync-1",
        "status": "active",
        "signature": "sig-permit-sync-1",
        "key_id": "key-1",
        "issued_at": datetime.now(UTC).isoformat(),
        "revoked_at": None,
    }


def _receipt_payload(receipt_id: str) -> dict:
    return {
        "receipt_id": receipt_id,
        "permit_id": "permit-sync-1",
        "wallet_id": "wallet-1",
        "key_id": "key-1",
        "tool": "partner.search",
        "request_hash": "request-hash",
        "response_hash": "response-hash",
        "ledger_entry_id": "ledger-1",
        "dispatch_attempt_id": "dispatch-1",
        "credits_authorized": "2",
        "credits_charged": "2",
        "outcome": "success",
        "audit_event_id": "audit-1",
        "created_at": datetime.now(UTC).isoformat(),
        "signature": "sig-sync",
        "signature_key_id": "signing-key-1",
    }


class _SyncGateway:
    """Counting mock gateway: one charge per fresh invoke key."""

    def __init__(self) -> None:
        self.permit_requests = 0
        self.charges = 0
        self.seen_keys: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/permits":
            self.permit_requests += 1
            return httpx.Response(201, json=_permit_payload())
        if request.url.path == "/mcp/messages":
            body = json.loads(request.content)
            key = body["params"]["mcpContext"]["idempotency_key"]
            self.seen_keys.append(key)
            self.charges += 1
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "content": [{"type": "text", "text": '{"ok": true}'}],
                        "structuredContent": {"ok": True},
                        "isError": False,
                        "receipt": _receipt_payload(f"receipt-sync-{self.charges}"),
                    },
                },
            )
        return httpx.Response(404)


@pytest.fixture
def sync_tool(monkeypatch):
    """A tool whose per-drive sync clients talk to the counting mock gateway."""
    gateway = _SyncGateway()
    seen: dict = {}

    def _factory(**kwargs):
        seen.update(kwargs)
        return B2AClient(transport=httpx.MockTransport(gateway), **kwargs)

    monkeypatch.setattr(tool_module, "B2AClient", _factory)
    tool = CrewAIB2ATool(api_key="test-key", wallet_id="wallet-1")
    return tool, gateway, seen


def _call(tool, invoke_key: str, permit_key: str):
    return tool._run(
        operation="call_tool",
        tool_name="partner.search",
        idempotency_key=invoke_key,
        permit_idempotency_key=permit_key,
        arguments={"query": "test"},
    )


def test_sync_run_repeated_calls_each_succeed(sync_tool):
    """Repeated synchronous calls each complete the governed flow."""
    tool, gateway, _ = sync_tool

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        first = _call(tool, "sync-key-1", "sync-permit-1")
        second = _call(tool, "sync-key-2", "sync-permit-2")

    assert "receipt-sync-1" in first
    assert "receipt-sync-2" in second
    assert gateway.charges == 2
    assert "Error:" not in first
    assert "Error:" not in second
    assert [
        str(w.message)
        for w in caught
        if "event loop" in str(w.message).lower() or "never awaited" in str(w.message)
    ] == []


def test_sync_run_emits_no_event_loop_warnings(sync_tool):
    """The sync bridge must not touch the deprecated get_event_loop path."""
    tool, _, _ = sync_tool

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = _call(tool, "sync-key-warn", "sync-permit-warn")

    assert "receipt-sync-1" in result
    loop_warnings = [
        str(w.message)
        for w in caught
        if "event loop" in str(w.message).lower() or "never awaited" in str(w.message)
    ]
    assert loop_warnings == []


@pytest.mark.asyncio
async def test_sync_run_inside_running_loop_succeeds(sync_tool):
    """Calling the sync entry point from async code must work, not error."""
    tool, gateway, _ = sync_tool

    # Old bridge returned "Error: This event loop is already running" here.
    result = _call(tool, "sync-key-loop", "sync-permit-loop")

    assert "receipt-sync-1" in result
    assert gateway.charges == 1


@pytest.mark.asyncio
async def test_sync_and_async_paths_return_same_receipt_shape(sync_tool):
    """Both entry points run the same governed flow and receipt format."""
    tool, _, _ = sync_tool
    tool.client = B2AClient(
        api_key="test-key",
        transport=httpx.MockTransport(_SyncGateway()),
    )

    sync_result = _call(tool, "shape-sync-key", "shape-sync-permit")
    async_result = await tool._arun(
        operation="call_tool",
        tool_name="partner.search",
        idempotency_key="shape-async-key",
        permit_idempotency_key="shape-async-permit",
        arguments={"query": "test"},
    )

    for result in (sync_result, async_result):
        assert "receipt_id" in result
        assert "credits_charged" in result
        assert "signature" in result

    await tool.client.close()


def test_sync_client_carries_tool_credentials(sync_tool):
    """Each sync drive builds its client from the tool's own credentials."""
    tool, _, seen = sync_tool

    _call(tool, "sync-key-creds", "sync-permit-creds")

    assert seen["api_key"] == "test-key"
    assert seen["base_url"] == tool.base_url


def test_sync_run_reports_missing_keys_without_network(sync_tool):
    """Blank idempotency keys fail fast, before any client or loop exists."""
    tool, gateway, _ = sync_tool

    result = tool._run(
        operation="call_tool",
        tool_name="partner.search",
        idempotency_key="   ",
        permit_idempotency_key="permit-key",
        arguments={},
    )

    assert "Error: idempotency_key is required" in result
    assert gateway.permit_requests == 0
    assert gateway.charges == 0


def test_run_blocking_reraises_caller_errors():
    """Driver failures reach the caller instead of hanging or returning None."""

    async def _boom():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        tool_module.run_blocking(_boom)


def test_run_blocking_returns_value_without_running_loop():
    async def _answer():
        return "ok"

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert tool_module.run_blocking(_answer) == "ok"
    assert [
        str(w.message) for w in caught if "event loop" in str(w.message).lower()
    ] == []


@pytest.mark.asyncio
async def test_run_blocking_returns_value_inside_running_loop():
    async def _answer():
        return "ok"

    assert tool_module.run_blocking(_answer) == "ok"
