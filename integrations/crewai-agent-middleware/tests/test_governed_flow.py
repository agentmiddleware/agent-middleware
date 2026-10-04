"""Tests for governed permit→invoke→receipt flow in CrewAI wrapper."""

import ast
import json
from datetime import UTC, datetime

import httpx
import pytest

from crewai_b2a import B2AClient, CrewAIB2ATool


def _permit_payload() -> dict:
    return {
        "permit_id": "permit-1",
        "issuer_wallet_id": "wallet-1",
        "subject_wallet_id": "wallet-1",
        "subject_key_id": "key-1",
        "scopes": ["tool:partner.search:invoke", "billing:charge"],
        "allowed_tools": ["partner.search"],
        "max_credits": "100",
        "spent_credits": "0",
        "expires_at": datetime.now(UTC).isoformat(),
        "nonce": "nonce-1",
        "status": "active",
        "signature": "sig-permit-1",
        "key_id": "key-1",
        "issued_at": datetime.now(UTC).isoformat(),
        "revoked_at": None,
    }


def _receipt_payload(outcome: str = "success") -> dict:
    # Mirrors the receipt shape b2a_sdk.models.Receipt.from_dict requires
    # (see b2a_sdk/tests/test_trust_client.py).
    return {
        "receipt_id": f"receipt-{outcome}",
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
        "outcome": outcome,
        "audit_event_id": "audit-1",
        "created_at": datetime.now(UTC).isoformat(),
        "signature": f"sig-{outcome}",
        "signature_key_id": "signing-key-1",
    }


@pytest.mark.asyncio
async def test_call_tool_requires_idempotency_key():
    """Test that idempotency_key is required and must not be blank."""
    permit_called = False
    invoke_called = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal permit_called, invoke_called

        if request.url.path == "/v1/permits":
            permit_called = True
            assert request.headers["idempotency-key"] == "permit-invoke-key-1"
            return httpx.Response(201, json=_permit_payload())

        if request.url.path == "/mcp/messages":
            invoke_called = True
            assert request.headers["idempotency-key"] == "invoke-key-1"
            body = json.loads(request.content)
            assert body["params"]["mcpContext"]["idempotency_key"] == "invoke-key-1"
            assert body["params"]["mcpContext"]["permit_id"] == "permit-1"
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": "invoke-key-1",
                    "result": {
                        "content": [{"type": "text", "text": '{"ok": true}'}],
                        "structuredContent": {"ok": True},
                        "isError": False,
                        "receipt": _receipt_payload(),
                    },
                },
            )

        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    base_client = B2AClient(api_key="test-key", transport=transport)
    tool = CrewAIB2ATool(api_key="test-key", wallet_id="wallet-1")
    tool.client = base_client

    result = await tool._arun(
        operation="call_tool",
        tool_name="partner.search",
        idempotency_key="invoke-key-1",
        permit_idempotency_key="permit-invoke-key-1",
        arguments={"query": "test"},
    )

    assert permit_called
    assert invoke_called
    assert "receipt-success" in result

    result_blank = await tool._arun(
        operation="call_tool",
        tool_name="partner.search",
        idempotency_key="   ",
        permit_idempotency_key="permit-key",
        arguments={},
    )
    assert "Error: idempotency_key is required" in result_blank

    await base_client.close()


class _IdempotentGateway:
    """Mock gateway that enforces idempotency the way the real server does.

    The first request under a key does the work (creates the permit, or charges
    the call) and stores its response with a hash of the request. An identical
    replay gets the stored response without new work; the same key with a
    changed request gets the server's conflict (HTTP 409 for permits, a
    JSON-RPC ``idempotency_key_reused`` error for MCP calls).
    """

    def __init__(self) -> None:
        self.permit_requests = 0
        self.charges = 0
        # (Idempotency-Key header, mcpContext.idempotency_key) per MCP request.
        self.invoke_keys: list[tuple[str, str]] = []
        self._permits: dict[str, tuple[str, dict]] = {}
        self._invocations: dict[str, tuple[str, dict]] = {}

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        key = request.headers["idempotency-key"]

        if request.url.path == "/v1/permits":
            self.permit_requests += 1
            request_hash = json.dumps(body, sort_keys=True)
            stored = self._permits.setdefault(key, (request_hash, _permit_payload()))
            if stored[0] != request_hash:
                return httpx.Response(409, json={"detail": "idempotency_key_reused"})
            return httpx.Response(201, json=stored[1])

        if request.url.path == "/mcp/messages":
            params = body["params"]
            self.invoke_keys.append((key, params["mcpContext"]["idempotency_key"]))
            request_hash = json.dumps(
                {"name": params["name"], "arguments": params["arguments"]},
                sort_keys=True,
            )
            if key not in self._invocations:
                self.charges += 1
                receipt = _receipt_payload()
                receipt["receipt_id"] = f"receipt-charge-{self.charges}"
                self._invocations[key] = (
                    request_hash,
                    {
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {
                            "content": [{"type": "text", "text": '{"ok": true}'}],
                            "structuredContent": {"ok": True},
                            "isError": False,
                            "receipt": receipt,
                        },
                    },
                )
            stored_hash, stored_response = self._invocations[key]
            if stored_hash != request_hash:
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "error": {"code": -32603, "message": "idempotency_key_reused"},
                    },
                )
            return httpx.Response(200, json=stored_response)

        return httpx.Response(404)


@pytest.mark.asyncio
async def test_identical_replay_forwards_same_key_and_charges_once():
    """An identical replay reuses the permit and key, so the call is charged once."""
    gateway = _IdempotentGateway()
    base_client = B2AClient(api_key="test-key", transport=httpx.MockTransport(gateway))
    tool = CrewAIB2ATool(api_key="test-key", wallet_id="wallet-1")
    tool.client = base_client

    call = {
        "operation": "call_tool",
        "tool_name": "partner.search",
        "idempotency_key": "replay-key",
        "permit_idempotency_key": "permit-replay-key",
        "arguments": {"query": "same"},
    }
    first = await tool._arun(**call)
    replay = await tool._arun(**call)

    assert gateway.invoke_keys == [("replay-key", "replay-key")] * 2
    assert gateway.permit_requests == 1
    assert gateway.charges == 1
    assert ast.literal_eval(first)["receipt_id"] == "receipt-charge-1"
    assert ast.literal_eval(replay)["receipt_id"] == "receipt-charge-1"

    await base_client.close()


@pytest.mark.asyncio
async def test_replay_with_changed_arguments_returns_idempotency_conflict():
    """Reusing an invocation key with changed arguments is a conflict, not a replay."""
    gateway = _IdempotentGateway()
    base_client = B2AClient(api_key="test-key", transport=httpx.MockTransport(gateway))
    tool = CrewAIB2ATool(api_key="test-key", wallet_id="wallet-1")
    tool.client = base_client

    first = await tool._arun(
        operation="call_tool",
        tool_name="partner.search",
        idempotency_key="replay-key",
        permit_idempotency_key="permit-replay-key",
        arguments={"query": "first"},
    )
    conflict = await tool._arun(
        operation="call_tool",
        tool_name="partner.search",
        idempotency_key="replay-key",
        permit_idempotency_key="permit-replay-key",
        arguments={"query": "second"},
    )

    assert gateway.invoke_keys == [("replay-key", "replay-key")] * 2
    assert gateway.permit_requests == 1
    assert gateway.charges == 1
    assert ast.literal_eval(first)["receipt_id"] == "receipt-charge-1"
    assert conflict == "Error: idempotency_key_reused"

    await base_client.close()


@pytest.mark.asyncio
async def test_signed_receipt_returned():
    """Test that signed receipts are returned with signature and credits charged."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/permits":
            return httpx.Response(201, json=_permit_payload())

        if request.url.path == "/mcp/messages":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": "signed-key",
                    "result": {
                        "content": [{"type": "text", "text": '{"status": "ok"}'}],
                        "structuredContent": {"status": "ok"},
                        "isError": False,
                        "receipt": _receipt_payload(),
                    },
                },
            )

        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    base_client = B2AClient(api_key="test-key", transport=transport)
    tool = CrewAIB2ATool(api_key="test-key", wallet_id="wallet-1")
    tool.client = base_client

    result = await tool._arun(
        operation="call_tool",
        tool_name="partner.search",
        idempotency_key="signed-key",
        permit_idempotency_key="permit-signed-key",
        arguments={},
    )

    assert "receipt_id" in result
    assert "signature" in result
    assert "credits_charged" in result
    assert "sig-success" in result
    assert "'2'" in result

    await base_client.close()
