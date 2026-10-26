"""Permit-term binding, discovery shape and the sync bridge (crewai_b2a).

`crewai` itself is a heavy dependency (chromadb and friends). When it is not
installed these tests fall back to a minimal `BaseTool` stub so the wrapper's
own logic, permit caching and error strings are still exercised; in CI the
real `crewai` is used.
"""

import sys
import types
from datetime import UTC, datetime

import httpx
import pytest

try:
    import crewai  # noqa: F401
except ImportError:
    _crewai = types.ModuleType("crewai")
    _tools_mod = types.ModuleType("crewai.tools")

    class BaseTool:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

    _tools_mod.BaseTool = BaseTool
    _crewai.tools = _tools_mod
    sys.modules["crewai"] = _crewai
    sys.modules["crewai.tools"] = _tools_mod

from crewai_b2a import B2AClient, CrewAIB2ATool


def _permit_payload() -> dict:
    now = datetime.now(UTC).isoformat()
    return {
        "permit_id": "permit-1",
        "issuer_wallet_id": "wallet-1",
        "subject_wallet_id": "wallet-1",
        "subject_key_id": "key-1",
        "scopes": ["tool:partner.search:invoke", "billing:charge"],
        "allowed_tools": ["partner.search"],
        "max_credits": "100",
        "spent_credits": "0",
        "expires_at": now,
        "nonce": "nonce-1",
        "status": "active",
        "signature": "sig-permit-1",
        "key_id": "key-1",
        "issued_at": now,
        "revoked_at": None,
    }


def _receipt_payload() -> dict:
    now = datetime.now(UTC).isoformat()
    return {
        "receipt_id": "receipt-1",
        "permit_id": "permit-1",
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
        "created_at": now,
        "signature": "sig-1",
        "signature_key_id": "signing-key-1",
    }


async def _handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/v1/permits":
        return httpx.Response(201, json=_permit_payload())
    if request.url.path == "/mcp/messages":
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "x",
                "result": {
                    "content": [{"type": "text", "text": '{"ok": true}'}],
                    "structuredContent": {"ok": True},
                    "isError": False,
                    "receipt": _receipt_payload(),
                },
            },
        )
    if request.url.path == "/mcp/tools.json":
        return httpx.Response(
            200,
            json={
                "tools": [
                    {
                        "name": "partner.search",
                        "description": "Search partners",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                        },
                    }
                ]
            },
        )
    if request.url.path == "/v1/billing/wallets/wallet-1":
        return httpx.Response(200, json={"balance": "4.5"})
    return httpx.Response(404)


def _tool():
    base = B2AClient(api_key="k", transport=httpx.MockTransport(_handler))
    # A borrowed client stays on the caller's loop, so sync calls need their
    # own runner-loop client from sync_client_factory.
    tool = CrewAIB2ATool(
        api_key="k",
        wallet_id="wallet-1",
        sync_client_factory=lambda: B2AClient(
            api_key="k", transport=httpx.MockTransport(_handler)
        ),
    )
    tool.client = base
    return base, tool


@pytest.mark.asyncio
async def test_permit_key_reused_with_different_tool_is_refused():
    base, tool = _tool()
    try:
        first = await tool._arun(
            operation="call_tool",
            tool_name="partner.search",
            idempotency_key="invoke-1",
            permit_idempotency_key="permit-key-1",
            arguments={},
        )
        assert "receipt-1" in first
        conflict = await tool._arun(
            operation="call_tool",
            tool_name="other.tool",
            idempotency_key="invoke-2",
            permit_idempotency_key="permit-key-1",
            arguments={},
        )
        assert (
            conflict
            == "Error: permit_idempotency_key reused with different permit terms"
        )
    finally:
        await base.close()


@pytest.mark.asyncio
async def test_discover_reports_each_tool_input_schema():
    """Discovery must carry the input schema (like the AutoGen wrapper does):
    without it the agent cannot shape the arguments it sends."""
    base, tool = _tool()
    try:
        result = await tool._arun(operation="discover_tools")
        assert "partner.search" in result
        assert "input_schema" in result
        assert "query" in result
    finally:
        await base.close()


@pytest.fixture()
def clean_event_loop():
    """A sync caller with no running or leftover loop, like a CrewAI worker."""
    import asyncio

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def test_sync_run_balance_and_unknown_operation(clean_event_loop):
    base, tool = _tool()
    try:
        assert tool._run(operation="balance") == "Balance: 4.5 credits"
        assert tool._run(operation="nope") == "Unknown operation: nope"
    finally:
        tool.close()
        clean_event_loop.run_until_complete(base.close())


@pytest.mark.asyncio
async def test_sync_run_inside_a_running_loop_executes():
    base, tool = _tool()
    try:
        assert tool._run(operation="balance") == "Balance: 4.5 credits"
    finally:
        tool.close()
        await base.close()


def test_permit_caches_do_not_leak_between_instances():
    """One agent's cached permit must never authorize another agent's call."""
    _, first = _tool()
    _, second = _tool()
    first._permit_cache["k"] = "permit-1"
    first._permit_requests["k"] = "request-1"
    assert second._permit_cache == {}
    assert second._permit_requests == {}
