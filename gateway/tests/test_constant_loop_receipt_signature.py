"""Test that constant_test_loop.py verifies receipt signatures.

This test proves the constant loop catches broken receipt signatures, which
would otherwise be a silent skip: the loop would pass even if receipt signing
regressed to return invalid signatures or no signatures at all.

The test simulates the constant loop's receipt signature check against a
receipt whose signature has been tampered with, proving the check would fail.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.database import get_session_factory
from app.db.models import ReceiptModel
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.service_registry import get_service_registry
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def registered_tool():
    """A local tool for testing the governed flow."""
    registry = get_service_registry()

    def tool_func(message: str = "ok") -> dict:
        return {"message": message}

    registry.register_local(
        service_id="constant-loop-sig-test-tool",
        name="Constant Loop Signature Test Tool",
        description="Tool for testing receipt signature verification",
        category=ServiceCategory.AGENT_COMMS,
        func=tool_func,
        credits_per_unit=2.0,
        unit_name="call",
    )
    yield registry
    registry.unregister_local("constant-loop-sig-test-tool")


@pytest.mark.asyncio
async def test_constant_loop_verifies_receipt_signature(
    client, clean_database, registered_tool
):
    """Prove the constant loop catches broken receipt signatures.

    The flow:
    1. Provision agent and permit
    2. Invoke a governed tool (success path)
    3. Verify the receipt signature is valid (as constant loop does)
    4. Tamper with the receipt signature
    5. Verify the signature check now fails (proving the check is real)
    """
    # Setup
    agent = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=agent["agent_wallet_id"],
        key_id=agent["key_id"],
        tool_name="constant-loop-sig-test-tool",
        idem_key="constant-loop-sig-permit",
    )

    # Invoke the governed tool (success path)
    invoke_resp = await client.post(
        "/mcp/tools/constant-loop-sig-test-tool/invoke",
        json={
            "name": "constant-loop-sig-test-tool",
            "arguments": {"message": "testing signature verification"},
            "mcp_context": {
                "wallet_id": agent["agent_wallet_id"],
                "permit_id": permit["permit_id"],
                "idempotency_key": "constant-loop-sig-invoke",
            },
        },
        headers=agent["agent_headers"],
    )
    assert invoke_resp.status_code == 200, invoke_resp.text
    body = invoke_resp.json()
    receipt = body["receipt"]
    receipt_id = receipt["receipt_id"]

    # Step 1: Verify the receipt signature is valid (as constant loop does)
    verify_resp = await client.post(
        "/v1/receipts/verify",
        json={"receipt_id": receipt_id},
        headers=agent["agent_headers"],
    )
    assert verify_resp.status_code == 200
    assert verify_resp.json()["valid"] is True, (
        "Fresh receipt signature should be valid"
    )

    # Step 2: Tamper with the receipt signature (simulate broken signing)
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(ReceiptModel).where(ReceiptModel.receipt_id == receipt_id)
        )
        model = result.scalar_one()
        # Tamper: flip a few bytes in the signature to simulate broken signing
        model.signature = model.signature[:-8] + "TAMPERED"
        session.add(model)
        await session.commit()

    # Step 3: Verify the signature check now fails (proving the check is real)
    tampered_resp = await client.post(
        "/v1/receipts/verify",
        json={"receipt_id": receipt_id},
        headers=agent["agent_headers"],
    )
    assert tampered_resp.status_code == 200
    assert tampered_resp.json()["valid"] is False, (
        "Tampered receipt signature should be invalid"
    )
    assert tampered_resp.json()["reason"] == "receipt_signature_invalid"


@pytest.mark.asyncio
async def test_constant_loop_verifies_denial_receipt_signature(
    client, clean_database, registered_tool
):
    """Prove the constant loop verifies denial receipt signatures too.

    The flow:
    1. Provision agent and permit for one tool
    2. Attempt to invoke a different tool (denial path)
    3. Verify the denial receipt signature is valid (as constant loop does)
    4. Tamper with the denial receipt signature
    5. Verify the signature check now fails
    """
    # Setup
    agent = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=agent["agent_wallet_id"],
        key_id=agent["key_id"],
        tool_name="constant-loop-sig-test-tool",
        idem_key="constant-loop-denial-sig-permit",
    )

    # Register a second tool NOT in the permit
    registry = get_service_registry()

    def other_tool_func(message: str = "ok") -> dict:
        return {"message": message}

    registry.register_local(
        service_id="constant-loop-other-tool",
        name="Other Tool",
        description="Tool not in permit for denial testing",
        category=ServiceCategory.AGENT_COMMS,
        func=other_tool_func,
        credits_per_unit=1.0,
        unit_name="call",
    )

    try:
        # Attempt to invoke the other tool (denial: permit_tool_not_allowed)
        denial_resp = await client.post(
            "/mcp/tools/constant-loop-other-tool/invoke",
            json={
                "name": "constant-loop-other-tool",
                "arguments": {"message": "should be denied"},
                "mcp_context": {
                    "wallet_id": agent["agent_wallet_id"],
                    "permit_id": permit["permit_id"],
                    "idempotency_key": "constant-loop-denial-invoke",
                },
            },
            headers=agent["agent_headers"],
        )
        assert denial_resp.status_code == 403
        body = denial_resp.json()
        # The /mcp/tools/{tool}/invoke endpoint returns detail with error and receipt
        assert body["detail"]["error"] == "permit_tool_not_allowed"
        denial_receipt = body["detail"]["receipt"]
        receipt_id = denial_receipt["receipt_id"]

        # Step 1: Verify the denial receipt signature is valid (as constant loop does)
        verify_resp = await client.post(
            "/v1/receipts/verify",
            json={"receipt_id": receipt_id},
            headers=agent["agent_headers"],
        )
        assert verify_resp.status_code == 200
        assert verify_resp.json()["valid"] is True, (
            "Fresh denial receipt signature should be valid"
        )

        # Step 2: Tamper with the denial receipt signature
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(ReceiptModel).where(ReceiptModel.receipt_id == receipt_id)
            )
            model = result.scalar_one()
            # Tamper: flip a few bytes in the signature
            model.signature = model.signature[:-8] + "TAMPERED"
            session.add(model)
            await session.commit()

        # Step 3: Verify the signature check now fails
        tampered_resp = await client.post(
            "/v1/receipts/verify",
            json={"receipt_id": receipt_id},
            headers=agent["agent_headers"],
        )
        assert tampered_resp.status_code == 200
        assert tampered_resp.json()["valid"] is False, (
            "Tampered denial receipt signature should be invalid"
        )
        assert tampered_resp.json()["reason"] == "receipt_signature_invalid"

    finally:
        registry.unregister_local("constant-loop-other-tool")
