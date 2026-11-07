"""Schemas, discovery, balance and permit-term binding (autogen_b2a).

`autogen` is only needed for `register_b2a_tools` (covered by
test_register_tools.py with the real dependency). These tests fall back to a
minimal stub so the wrapper's own logic runs anywhere; in CI the real
`autogen` is used.
"""

import sys
import types
from datetime import UTC, datetime

import httpx
import pytest

try:
    import autogen  # noqa: F401
except ImportError:
    _autogen = types.ModuleType("autogen")
    _agentchat = types.ModuleType("autogen.agentchat")
    _conversable = types.ModuleType("autogen.agentchat.conversable_agent")

    class ConversableAgent:
        pass

    _conversable.ConversableAgent = ConversableAgent
    _agentchat.conversable_agent = _conversable
    _autogen.agentchat = _agentchat
    sys.modules["autogen"] = _autogen
    sys.modules["autogen.agentchat"] = _agentchat
    sys.modules["autogen.agentchat.conversable_agent"] = _conversable

from autogen_b2a import B2AClient, B2AFunctionTool


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
                        "inputSchema": {"type": "object"},
                    }
                ]
            },
        )
    if request.url.path == "/v1/billing/wallets/wallet-1":
        return httpx.Response(200, json={"balance": "9.5"})
    return httpx.Response(404)


def _tool():
    base = B2AClient(api_key="k", transport=httpx.MockTransport(_handler))
    tool = B2AFunctionTool(api_key="k", wallet_id="wallet-1")
    tool.client = base
    return base, tool


@pytest.mark.asyncio
async def test_permit_key_reused_with_different_terms_is_refused():
    base, tool = _tool()
    try:
        await tool.call_mcp_tool(
            tool_name="partner.search",
            idempotency_key="invoke-1",
            permit_idempotency_key="permit-key-1",
            arguments={},
        )
        with pytest.raises(ValueError, match="different permit terms"):
            await tool.call_mcp_tool(
                tool_name="other.tool",
                idempotency_key="invoke-2",
                permit_idempotency_key="permit-key-1",
                arguments={},
            )
        tool.permit_budget = tool.permit_budget + 1
        with pytest.raises(ValueError, match="different permit terms"):
            await tool.call_mcp_tool(
                tool_name="partner.search",
                idempotency_key="invoke-3",
                permit_idempotency_key="permit-key-1",
                arguments={},
            )
    finally:
        await base.close()


@pytest.mark.asyncio
async def test_discover_and_balance_forward_the_wallet():
    base, tool = _tool()
    try:
        assert await tool.discover_tools() == [
            {
                "name": "partner.search",
                "description": "Search partners",
                "input_schema": {"type": "object"},
            }
        ]
        assert await tool.get_wallet_balance() == "9.5"
    finally:
        await base.close()


def test_function_schemas_require_both_idempotency_keys():
    tool = B2AFunctionTool(api_key="k", wallet_id="wallet-1")
    by_name = {
        s["function"]["name"]: s["function"] for s in tool.get_function_schemas()
    }
    assert set(by_name) == {"discover_tools", "call_mcp_tool", "get_wallet_balance"}
    required = by_name["call_mcp_tool"]["parameters"]["required"]
    assert required == ["tool_name", "idempotency_key", "permit_idempotency_key"]
