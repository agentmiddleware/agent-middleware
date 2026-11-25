"""Permit-term binding, wallet tooling and factory shape (langchain_b2a)."""

from datetime import UTC, datetime

import httpx
import pytest

from langchain_b2a import B2AClient
from langchain_b2a.tools import get_langgraph_tools, get_mcp_tools


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


def _governed_handler(request: httpx.Request) -> httpx.Response:
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
    if request.url.path == "/v1/billing/wallets/wallet-1":
        return httpx.Response(200, json={"balance": "3.25"})
    return httpx.Response(404)


@pytest.mark.asyncio
async def test_permit_key_reused_with_different_tool_is_refused():
    """A permit binds its tool: replaying the permit key for another tool must
    fail locally, never mint a permit whose terms changed under the key."""
    client = B2AClient(api_key="k", transport=httpx.MockTransport(_governed_handler))
    tool = get_mcp_tools(client, wallet_id="wallet-1")[0]
    try:
        await tool.ainvoke(
            {
                "tool_name": "partner.search",
                "idempotency_key": "invoke-1",
                "permit_idempotency_key": "permit-key-1",
                "arguments": {},
            }
        )
        with pytest.raises(ValueError, match="different permit terms"):
            await tool.ainvoke(
                {
                    "tool_name": "other.tool",
                    "idempotency_key": "invoke-2",
                    "permit_idempotency_key": "permit-key-1",
                    "arguments": {},
                }
            )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_factories_return_one_mcp_tool_and_a_wallet_tool():
    client = B2AClient(api_key="k", transport=httpx.MockTransport(_governed_handler))
    try:
        (mcp_tool,) = get_mcp_tools(client, wallet_id="wallet-1")
        assert mcp_tool.name == "mcp_tool_call"
        mcp_tool, wallet_tool = get_langgraph_tools(client, wallet_id="wallet-1")
        assert wallet_tool.name == "wallet_balance"
        assert await wallet_tool.ainvoke({}) == "Balance: 3.25 credits"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_arguments_default_to_empty_object():
    """The model may omit arguments; the call still travels as `{}`."""
    import json

    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/mcp/messages":
            seen.update(json.loads(request.content)["params"])
        return _governed_handler(request)

    client = B2AClient(api_key="k", transport=httpx.MockTransport(handler))
    tool = get_mcp_tools(client, wallet_id="wallet-1")[0]
    try:
        await tool.ainvoke(
            {
                "tool_name": "partner.search",
                "idempotency_key": "invoke-1",
                "permit_idempotency_key": "permit-key-1",
            }
        )
        assert seen["arguments"] == {}
    finally:
        await client.close()
