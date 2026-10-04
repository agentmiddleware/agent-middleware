from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from b2a_sdk import (
    AgentMiddlewareClient,
    AuthenticationError,
    B2AClient,
    DeliveryUncertainError,
    IdempotencyConflictError,
    InsufficientFundsError,
    PermitDeniedError,
    PermitRequest,
    TransportError,
)
from b2a_sdk.x402 import X402Client


def _permit_payload() -> dict:
    now = datetime.now(timezone.utc)
    return {
        "permit_id": "permit-1",
        "issuer_wallet_id": "wallet-1",
        "subject_wallet_id": "wallet-1",
        "subject_key_id": "key-1",
        "scopes": ["tool:partner.search:invoke", "billing:charge"],
        "allowed_tools": ["partner.search"],
        "max_credits": "25",
        "spent_credits": "2",
        "expires_at": (now + timedelta(minutes=30)).isoformat(),
        "nonce": "nonce-1",
        "status": "active",
        "signature": "permit-signature",
        "key_id": "signing-key-1",
        "issued_at": now.isoformat(),
        "revoked_at": None,
    }


def _receipt_payload(outcome: str = "success") -> dict:
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
        "created_at": datetime.now(timezone.utc).isoformat(),
        "signature": "receipt-signature",
        "signature_key_id": "signing-key-1",
    }


def _client(handler) -> AgentMiddlewareClient:
    return AgentMiddlewareClient(
        api_key="test-key",
        base_url="https://gateway.example",
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_default_base_url_is_the_live_canonical_api() -> None:
    client = AgentMiddlewareClient(api_key="test-key")
    try:
        assert client.base_url == "https://api.thisisatest.tech"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_discover_tools_returns_typed_definitions() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/mcp/tools.json"
        assert request.headers["user-agent"] == "b2a-sdk/0.5.0"
        return httpx.Response(
            200,
            json={
                "tools": [
                    {
                        "name": "partner.search",
                        "description": "Search partner data",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                        },
                        "annotations": {
                            "creditsPerCall": 2,
                            "requirePermit": True,
                        },
                    }
                ]
            },
        )

    async with _client(handler) as client:
        tools = await client.discover_tools()

    assert [tool.name for tool in tools] == ["partner.search"]
    assert tools[0].annotations["requirePermit"] is True


@pytest.mark.asyncio
async def test_discover_tools_tolerates_legacy_null_schema() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "tools": [
                    {
                        "name": "legacy.tool",
                        "description": "Legacy metadata",
                        "inputSchema": None,
                        "annotations": None,
                    }
                ]
            },
        )

    async with _client(handler) as client:
        tools = await client.discover_tools()

    assert tools[0].input_schema == {}
    assert tools[0].annotations == {}


@pytest.mark.asyncio
async def test_create_permit_requires_and_forwards_idempotency_key() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/permits"
        assert request.headers["idempotency-key"] == "permit-key-1"
        body = json.loads(request.content)
        assert body["issuer_wallet_id"] == "wallet-1"
        assert body["max_credits"] == "25"
        return httpx.Response(201, json=_permit_payload())

    request = PermitRequest(
        issuer_wallet_id="wallet-1",
        subject_wallet_id="wallet-1",
        subject_key_id="key-1",
        scopes=["tool:partner.search:invoke", "billing:charge"],
        allowed_tools=["partner.search"],
        max_credits=Decimal("25"),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
    async with _client(handler) as client:
        permit = await client.create_permit(
            request,
            idempotency_key="permit-key-1",
        )
        with pytest.raises(ValueError, match="must not be blank"):
            await client.create_permit(request, idempotency_key="   ")

    assert permit.permit_id == "permit-1"
    assert permit.max_credits == Decimal("25")


@pytest.mark.asyncio
async def test_invoke_tool_returns_typed_receipt_and_governed_context() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/mcp/messages"
        assert request.headers["idempotency-key"] == "invoke-key-1"
        body = json.loads(request.content)
        assert body["method"] == "tools/call"
        assert body["params"]["mcpContext"] == {
            "wallet_id": "wallet-1",
            "permit_id": "permit-1",
            "idempotency_key": "invoke-key-1",
        }
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

    async with _client(handler) as client:
        result = await client.invoke_tool(
            "partner.search",
            {"query": "risk"},
            wallet_id="wallet-1",
            permit_id="permit-1",
            idempotency_key="invoke-key-1",
        )

    assert result.receipt.receipt_id == "receipt-success"
    assert result.receipt.dispatch_attempt_id == "dispatch-1"
    assert result.receipt.credits_charged == Decimal("2")
    assert result.structured_content == {"ok": True}


@pytest.mark.asyncio
async def test_delivery_uncertain_error_carries_signed_receipt_id() -> None:
    uncertain_receipt = _receipt_payload("delivery_uncertain")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "invoke-key-2",
                "error": {
                    "code": -32603,
                    "message": "delivery_uncertain",
                    "data": {"receipt": uncertain_receipt},
                },
            },
        )

    async with _client(handler) as client:
        with pytest.raises(DeliveryUncertainError) as exc_info:
            await client.invoke_tool(
                "partner.search",
                {},
                wallet_id="wallet-1",
                permit_id="permit-1",
                idempotency_key="invoke-key-2",
            )

    assert exc_info.value.receipt_id == "receipt-delivery_uncertain"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "outcome", "error_type"),
    [
        ("permit_tool_not_allowed", "denied", PermitDeniedError),
        ("insufficient_funds", "insufficient_funds", InsufficientFundsError),
        ("idempotency_key_reused", "denied", IdempotencyConflictError),
    ],
)
async def test_governed_jsonrpc_errors_are_typed(
    reason: str,
    outcome: str,
    error_type: type[Exception],
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "invoke-key",
                "error": {
                    "code": -32003,
                    "message": reason,
                    "data": {"receipt": _receipt_payload(outcome)},
                },
            },
        )

    async with _client(handler) as client:
        with pytest.raises(error_type):
            await client.invoke_tool(
                "partner.search",
                {},
                wallet_id="wallet-1",
                permit_id="permit-1",
                idempotency_key="invoke-key",
            )


@pytest.mark.asyncio
async def test_receipt_verification_and_evidence_are_typed() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/receipts/receipt-success":
            return httpx.Response(200, json=_receipt_payload())
        if request.url.path == "/v1/receipts/verify":
            return httpx.Response(
                200,
                json={"valid": True, "reason": None, "receipt": _receipt_payload()},
            )
        if request.url.path == "/v1/evidence/receipt-success":
            return httpx.Response(
                200,
                json={
                    "receipt_id": "receipt-success",
                    "valid": True,
                    "receipt": _receipt_payload(),
                    "permit": _permit_payload(),
                    "ledger_entry": {"entry_id": "ledger-1"},
                    "audit_event": {"event_id": "audit-1"},
                    "verification": {"receipt_signature": "ok"},
                    "dispatch": {"state": "succeeded"},
                },
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    async with _client(handler) as client:
        receipt = await client.get_receipt("receipt-success")
        verification = await client.verify_receipt("receipt-success")
        evidence = await client.get_evidence("receipt-success")

    assert receipt.outcome == "success"
    assert verification.valid is True
    assert verification.receipt is not None
    assert evidence.valid is True
    assert evidence.permit is not None
    assert evidence.dispatch == {"state": "succeeded"}


@pytest.mark.asyncio
async def test_authentication_and_transport_failures_are_typed() -> None:
    async def auth_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "invalid_api_key"})

    async with _client(auth_handler) as client:
        with pytest.raises(AuthenticationError):
            await client.discover_tools()

    async def transport_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with _client(transport_handler) as client:
        with pytest.raises(TransportError):
            await client.discover_tools()


@pytest.mark.asyncio
@pytest.mark.parametrize("client_cls", [AgentMiddlewareClient, X402Client])
async def test_api_key_is_not_kept_as_plain_instance_state(client_cls: type) -> None:
    secret = "sk-live-do-not-log"
    client = client_cls(api_key=secret, base_url="https://gateway.example")
    try:
        # Debug dumps of the object (repr, vars/__dict__) must not carry the
        # secret; the transport's X-API-Key header is the one place it lives.
        assert secret not in repr(client)
        assert secret not in repr(vars(client))
        # Reading the configured key stays backward compatible.
        assert client.api_key == secret
        with pytest.raises(AttributeError):
            client.api_key = "sk-other"
    finally:
        await client._client.aclose()


@pytest.mark.asyncio
async def test_b2a_client_remains_a_deprecated_compatibility_name() -> None:
    with pytest.warns(DeprecationWarning, match="AgentMiddlewareClient"):
        client = B2AClient(
            api_key="test-key",
            base_url="https://gateway.example",
        )
    await client.close()
