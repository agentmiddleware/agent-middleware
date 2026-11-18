"""Test revoked permit denial behavior and receipt generation.

This test suite verifies the design partner's claim that a revoked permit:
1. Is denied before any charge or budget movement
2. Does not dispatch to the tool
3. Returns a specific denial error code
4. Produces a signed receipt
5. The receipt is offline-verifiable with the Python SDK
6. A tampered receipt is rejected by the SDK verifier
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.database import get_session_factory
from app.db.models import PermitModel, ReceiptModel
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.service_registry import get_service_registry
from tests.test_trust_helpers import (
    create_tool_permit,
    provision_agent_wallet,
)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _register_test_tool(tool_name: str) -> None:
    """Register a simple test tool for permit validation."""
    get_service_registry().register_local(
        service_id=tool_name,
        name=tool_name,
        description="test tool for revoked permit checks",
        category=ServiceCategory.AGENT_COMMS,
        func=lambda message="ok": {"message": message, "dispatched": True},
        credits_per_unit=2.0,
        unit_name="call",
    )


def _mcp_call(wallet_id: str, permit_id: str, tool_name: str, idem: str) -> dict:
    """Create an MCP tools/call request payload."""
    return {
        "jsonrpc": "2.0",
        "id": idem,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": {"message": "test"},
            "mcpContext": {
                "wallet_id": wallet_id,
                "permit_id": permit_id,
                "idempotency_key": idem,
            },
        },
    }


@pytest.mark.anyio
async def test_revoked_permit_denied_without_budget_movement(client, clean_database):
    """A revoked permit is denied without reserving or charging credits."""
    provisioned = await provision_agent_wallet(client)
    tool_name = "revoked-no-charge-tool"
    _register_test_tool(tool_name)
    
    try:
        # Create and then revoke a permit
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
        )
        
        revoke_resp = await client.post(
            f"/v1/permits/{permit['permit_id']}/revoke",
            headers={"X-API-Key": "test-key"},
        )
        assert revoke_resp.status_code == 200
        
        # Verify the permit is revoked
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(PermitModel).where(PermitModel.permit_id == permit["permit_id"])
            )
            model = result.scalar_one()
            assert model.status == "revoked"
            spent_before = model.spent_credits
        
        # Try to invoke with the revoked permit
        call_resp = await client.post(
            "/mcp/messages",
            json=_mcp_call(
                provisioned["agent_wallet_id"],
                permit["permit_id"],
                tool_name,
                "revoked-call-1",
            ),
            headers=provisioned["agent_headers"],
        )
        assert call_resp.status_code == 200
        error = call_resp.json()["error"]
        assert error["message"] == "permit_revoked"
        
        # Verify no budget movement occurred
        async with factory() as session:
            result = await session.execute(
                select(PermitModel).where(PermitModel.permit_id == permit["permit_id"])
            )
            model = result.scalar_one()
            assert model.spent_credits == spent_before
        
        # Verify no ledger charge
        ledger_resp = await client.get(
            f"/v1/billing/ledger/{provisioned['agent_wallet_id']}",
            headers=provisioned["agent_headers"],
        )
        assert ledger_resp.status_code == 200
        debits = [
            e for e in ledger_resp.json()["entries"]
            if e["service_category"] == "agent_comms" and tool_name in e.get("description", "")
        ]
        assert len(debits) == 0
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_revoked_permit_not_dispatched(client, clean_database):
    """A revoked permit denial does not dispatch to the upstream tool."""
    provisioned = await provision_agent_wallet(client)
    tool_name = "revoked-no-dispatch-tool"
    
    dispatched = []
    
    def tracking_tool(message="ok"):
        dispatched.append(message)
        return {"message": message, "dispatched": True}
    
    get_service_registry().register_local(
        service_id=tool_name,
        name=tool_name,
        description="tool that tracks dispatch calls",
        category=ServiceCategory.AGENT_COMMS,
        func=tracking_tool,
        credits_per_unit=2.0,
        unit_name="call",
    )
    
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
        )
        
        # Revoke the permit
        revoke_resp = await client.post(
            f"/v1/permits/{permit['permit_id']}/revoke",
            headers={"X-API-Key": "test-key"},
        )
        assert revoke_resp.status_code == 200
        
        # Try to invoke
        call_resp = await client.post(
            "/mcp/messages",
            json=_mcp_call(
                provisioned["agent_wallet_id"],
                permit["permit_id"],
                tool_name,
                "revoked-call-2",
            ),
            headers=provisioned["agent_headers"],
        )
        assert call_resp.status_code == 200
        assert call_resp.json()["error"]["message"] == "permit_revoked"
        
        # Verify the tool was never invoked
        assert len(dispatched) == 0
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_revoked_permit_returns_denial_code(client, clean_database):
    """A revoked permit returns the specific 'permit_revoked' error code."""
    provisioned = await provision_agent_wallet(client)
    tool_name = "revoked-error-code-tool"
    _register_test_tool(tool_name)
    
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
        )
        
        # Revoke the permit
        await client.post(
            f"/v1/permits/{permit['permit_id']}/revoke",
            headers={"X-API-Key": "test-key"},
        )
        
        # Invoke and check error code
        call_resp = await client.post(
            "/mcp/messages",
            json=_mcp_call(
                provisioned["agent_wallet_id"],
                permit["permit_id"],
                tool_name,
                "revoked-call-3",
            ),
            headers=provisioned["agent_headers"],
        )
        assert call_resp.status_code == 200
        error = call_resp.json()["error"]
        assert error["message"] == "permit_revoked"
        assert error["code"] == -32003  # ToolPermissionDenied error code
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_revoked_permit_produces_receipt(client, clean_database):
    """A revoked permit denial produces a signed receipt."""
    provisioned = await provision_agent_wallet(client)
    tool_name = "revoked-receipt-tool"
    _register_test_tool(tool_name)
    
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
        )
        
        # Revoke the permit
        await client.post(
            f"/v1/permits/{permit['permit_id']}/revoke",
            headers={"X-API-Key": "test-key"},
        )
        
        # Invoke with revoked permit
        call_resp = await client.post(
            "/mcp/messages",
            json=_mcp_call(
                provisioned["agent_wallet_id"],
                permit["permit_id"],
                tool_name,
                "revoked-call-4",
            ),
            headers=provisioned["agent_headers"],
        )
        assert call_resp.status_code == 200
        error = call_resp.json()["error"]
        assert error["message"] == "permit_revoked"
        
        # Verify a receipt exists in the response
        assert "receipt" in error["data"]
        receipt = error["data"]["receipt"]
        assert receipt["outcome"] == "denied"
        assert receipt["reason_code"] == "permit_revoked"
        assert receipt["credits_charged"] == "0"
        assert receipt["ledger_entry_id"] is None
        assert "signature" in receipt
        assert receipt["signature"] is not None
        
        # Verify the receipt is persisted
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(ReceiptModel).where(
                    ReceiptModel.permit_id == permit["permit_id"]
                )
            )
            stored_receipt = result.scalar_one()
            assert stored_receipt.outcome == "denied"
            assert stored_receipt.signature is not None
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.xfail(strict=True, reason="Receipt in MCP error response is not in portable bundle format with signing_input")
@pytest.mark.anyio
async def test_revoked_permit_receipt_inline_not_verifiable(client, clean_database):
    """Receipt in MCP error response lacks signing_input for offline verification."""
    provisioned = await provision_agent_wallet(client)
    tool_name = "revoked-inline-verify-tool"
    _register_test_tool(tool_name)
    
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
        )
        
        # Revoke and invoke
        await client.post(
            f"/v1/permits/{permit['permit_id']}/revoke",
            headers={"X-API-Key": "test-key"},
        )
        
        call_resp = await client.post(
            "/mcp/messages",
            json=_mcp_call(
                provisioned["agent_wallet_id"],
                permit["permit_id"],
                tool_name,
                "revoked-call-5",
            ),
            headers=provisioned["agent_headers"],
        )
        assert call_resp.status_code == 200
        receipt = call_resp.json()["error"]["data"]["receipt"]
        
        # Get the public keys for verification
        keys_resp = await client.get("/.well-known/trust-keys.json")
        assert keys_resp.status_code == 200
        keys_doc = keys_resp.json()
        
        # Try to verify with the Python SDK
        from b2a_sdk.receipt_verifier import verify_bundle, key_set_from_document
        
        key_set = key_set_from_document(keys_doc)
        result = verify_bundle(receipt, key_set)
        
        # The inline receipt lacks signing_input so verification fails
        assert result.ok, f"Verification failed: {result.status} - {result.reason}"
        assert result.claims["outcome"] == "denied"
        assert result.claims["reason_code"] == "permit_revoked"
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_portable_denial_receipt_offline_verifiable(client, clean_database):
    """Portable bundle from GET /v1/receipts/{id}/portable is offline-verifiable."""
    provisioned = await provision_agent_wallet(client)
    tool_name = "revoked-portable-verify-tool"
    _register_test_tool(tool_name)
    
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
        )
        
        # Revoke and invoke to get a denial receipt
        await client.post(
            f"/v1/permits/{permit['permit_id']}/revoke",
            headers={"X-API-Key": "test-key"},
        )
        
        call_resp = await client.post(
            "/mcp/messages",
            json=_mcp_call(
                provisioned["agent_wallet_id"],
                permit["permit_id"],
                tool_name,
                "revoked-call-portable-1",
            ),
            headers=provisioned["agent_headers"],
        )
        assert call_resp.status_code == 200
        receipt_id = call_resp.json()["error"]["data"]["receipt"]["receipt_id"]
        
        # Fetch the portable bundle
        portable_resp = await client.get(
            f"/v1/receipts/{receipt_id}/portable",
            headers=provisioned["agent_headers"],
        )
        assert portable_resp.status_code == 200
        portable_bundle = portable_resp.json()
        
        # Get the public keys for verification
        keys_resp = await client.get("/.well-known/trust-keys.json")
        assert keys_resp.status_code == 200
        keys_doc = keys_resp.json()
        
        # Verify with the Python SDK
        from b2a_sdk.receipt_verifier import verify_bundle, key_set_from_document
        
        key_set = key_set_from_document(keys_doc)
        result = verify_bundle(portable_bundle, key_set)
        
        # The portable bundle should verify offline
        assert result.ok, f"Verification failed: {result.status} - {result.reason}"
        assert result.claims["outcome"] == "denied"
        assert result.claims["reason_code"] == "permit_revoked"
        assert result.receipt_id == receipt_id
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_tampered_portable_denial_receipt_rejected(client, clean_database):
    """A tampered portable denial receipt bundle is rejected by offline verification."""
    provisioned = await provision_agent_wallet(client)
    tool_name = "revoked-portable-tamper-tool"
    _register_test_tool(tool_name)
    
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
        )
        
        # Revoke and invoke
        await client.post(
            f"/v1/permits/{permit['permit_id']}/revoke",
            headers={"X-API-Key": "test-key"},
        )
        
        call_resp = await client.post(
            "/mcp/messages",
            json=_mcp_call(
                provisioned["agent_wallet_id"],
                permit["permit_id"],
                tool_name,
                "revoked-call-portable-2",
            ),
            headers=provisioned["agent_headers"],
        )
        assert call_resp.status_code == 200
        receipt_id = call_resp.json()["error"]["data"]["receipt"]["receipt_id"]
        
        # Fetch the portable bundle
        portable_resp = await client.get(
            f"/v1/receipts/{receipt_id}/portable",
            headers=provisioned["agent_headers"],
        )
        assert portable_resp.status_code == 200
        portable_bundle = portable_resp.json()
        
        # Get the public keys
        keys_resp = await client.get("/.well-known/trust-keys.json")
        assert keys_resp.status_code == 200
        keys_doc = keys_resp.json()
        
        # Tamper with the portable bundle
        from b2a_sdk.receipt_verifier import verify_bundle, key_set_from_document
        import copy
        
        key_set = key_set_from_document(keys_doc)
        
        # First verify the untampered bundle works
        result = verify_bundle(portable_bundle, key_set)
        assert result.ok, "Untampered bundle should verify"
        
        # Now tamper with fields in the signing_input (the canonical signed payload)
        tampered = copy.deepcopy(portable_bundle)
        
        # Tamper with a field in signing_input - change the receipt_id
        import json
        signing_input = json.loads(tampered["signing_input"])
        signing_input["receipt_id"] = "rcpt-tampered-000000000000"
        tampered["signing_input"] = json.dumps(signing_input, separators=(",", ":"), sort_keys=True)
        
        tampered_result = verify_bundle(tampered, key_set)
        # Verification should fail because signature doesn't match tampered signing_input
        assert tampered_result.is_rejected or tampered_result.is_tampered, (
            f"Tampered bundle should be rejected, got: {tampered_result.status} - {tampered_result.reason}"
        )
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.xfail(strict=True, reason="Receipt in MCP error response is not in portable bundle format with signing_input")
@pytest.mark.anyio
async def test_tampered_inline_denial_receipt_rejected(client, clean_database):
    """A tampered denial receipt is rejected by offline verification."""
    provisioned = await provision_agent_wallet(client)
    tool_name = "revoked-tamper-tool"
    _register_test_tool(tool_name)
    
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
        )
        
        # Revoke and invoke
        await client.post(
            f"/v1/permits/{permit['permit_id']}/revoke",
            headers={"X-API-Key": "test-key"},
        )
        
        call_resp = await client.post(
            "/mcp/messages",
            json=_mcp_call(
                provisioned["agent_wallet_id"],
                permit["permit_id"],
                tool_name,
                "revoked-call-6",
            ),
            headers=provisioned["agent_headers"],
        )
        assert call_resp.status_code == 200
        receipt = call_resp.json()["error"]["data"]["receipt"]
        
        # Get the public keys for verification
        keys_resp = await client.get("/.well-known/trust-keys.json")
        assert keys_resp.status_code == 200
        keys_doc = keys_resp.json()
        
        # Tamper with the receipt by changing outcome in the signed payload
        from b2a_sdk.receipt_verifier import verify_bundle, key_set_from_document
        import copy
        
        key_set = key_set_from_document(keys_doc)
        tampered = copy.deepcopy(receipt)
        # Tamper with a field that should be in the signed payload
        tampered["outcome"] = "success"  # Change denial to success
        
        result = verify_bundle(tampered, key_set)
        # Verification should fail because the signature no longer matches
        assert result.is_rejected or result.is_tampered, (
            f"Tampered receipt should be rejected, got: {result.status}"
        )
    finally:
        get_service_registry().unregister_local(tool_name)
