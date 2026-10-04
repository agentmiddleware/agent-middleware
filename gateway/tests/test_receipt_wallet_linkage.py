"""Test that receipts always link to the correct wallet.

This test proves receipts cannot be attributed to the wrong wallet - a
fundamental consistency invariant. If the wallet_id linkage broke (receipt
stores wrong wallet, or returns different wallet than was charged), these
tests would catch it.

The gap: Many tests verify permit_id linkage, but few verify wallet_id linkage.
A broken wallet_id field in receipts would be a critical audit/billing failure.
"""

from __future__ import annotations

from decimal import Decimal

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
        service_id="wallet-linkage-test-tool",
        name="Wallet Linkage Test Tool",
        description="Tool for testing receipt wallet_id linkage",
        category=ServiceCategory.AGENT_COMMS,
        func=tool_func,
        credits_per_unit=2.0,
        unit_name="call",
    )
    yield registry
    registry.unregister_local("wallet-linkage-test-tool")


@pytest.mark.asyncio
async def test_success_receipt_links_to_invoking_wallet(
    client, clean_database, registered_tool
):
    """Prove success receipts always link to the wallet that was charged.

    The flow:
    1. Provision agent and permit
    2. Invoke governed tool (success path)
    3. Verify receipt.wallet_id matches the mcpContext.wallet_id sent
    4. Verify receipt.wallet_id matches what's stored in the database
    5. Verify receipt.wallet_id is included in the signed payload
    """
    # Setup
    agent = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=agent["agent_wallet_id"],
        key_id=agent["key_id"],
        tool_name="wallet-linkage-test-tool",
        idem_key="wallet-linkage-permit",
    )

    # Invoke the governed tool (success path)
    invoke_resp = await client.post(
        "/mcp/tools/wallet-linkage-test-tool/invoke",
        json={
            "name": "wallet-linkage-test-tool",
            "arguments": {"message": "testing wallet linkage"},
            "mcp_context": {
                "wallet_id": agent["agent_wallet_id"],
                "permit_id": permit["permit_id"],
                "idempotency_key": "wallet-linkage-invoke",
            },
        },
        headers=agent["agent_headers"],
    )
    assert invoke_resp.status_code == 200, invoke_resp.text
    body = invoke_resp.json()
    receipt = body["receipt"]
    receipt_id = receipt["receipt_id"]

    # Step 1: Verify receipt.wallet_id matches the mcpContext.wallet_id sent
    assert receipt["wallet_id"] == agent["agent_wallet_id"], (
        f"Receipt wallet_id {receipt['wallet_id']} != "
        f"mcpContext wallet_id {agent['agent_wallet_id']}"
    )

    # Step 2: Verify receipt.wallet_id matches what's stored in the database
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(ReceiptModel).where(ReceiptModel.receipt_id == receipt_id)
        )
        model = result.scalar_one()
        assert model.wallet_id == agent["agent_wallet_id"], (
            f"Stored wallet_id {model.wallet_id} != expected {agent['agent_wallet_id']}"
        )

    # Step 3: Verify the receipt signature is valid (includes wallet_id)
    verify_resp = await client.post(
        "/v1/receipts/verify",
        json={"receipt_id": receipt_id},
        headers=agent["agent_headers"],
    )
    assert verify_resp.status_code == 200
    assert verify_resp.json()["valid"] is True, "Receipt signature should be valid"

    # Step 4: Verify wallet_id linkage via GET endpoint
    read_resp = await client.get(
        f"/v1/receipts/{receipt_id}",
        headers=agent["agent_headers"],
    )
    assert read_resp.status_code == 200
    read_receipt = read_resp.json()
    assert read_receipt["wallet_id"] == agent["agent_wallet_id"], (
        f"GET receipt wallet_id {read_receipt['wallet_id']} != "
        f"expected {agent['agent_wallet_id']}"
    )


@pytest.mark.asyncio
async def test_denial_receipt_links_to_invoking_wallet(
    client, clean_database, registered_tool
):
    """Prove denial receipts always link to the wallet that was denied.

    The flow:
    1. Provision agent and permit for one tool
    2. Attempt to invoke a different tool (denial path)
    3. Verify denial receipt.wallet_id matches the mcpContext.wallet_id sent
    4. Verify denial receipt.wallet_id matches what's stored in the database
    """
    # Setup
    agent = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=agent["agent_wallet_id"],
        key_id=agent["key_id"],
        tool_name="wallet-linkage-test-tool",
        idem_key="wallet-linkage-denial-permit",
    )

    # Register a second tool NOT in the permit
    registry = get_service_registry()

    def other_tool_func(message: str = "ok") -> dict:
        return {"message": message}

    registry.register_local(
        service_id="wallet-linkage-other-tool",
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
            "/mcp/tools/wallet-linkage-other-tool/invoke",
            json={
                "name": "wallet-linkage-other-tool",
                "arguments": {"message": "should be denied"},
                "mcp_context": {
                    "wallet_id": agent["agent_wallet_id"],
                    "permit_id": permit["permit_id"],
                    "idempotency_key": "wallet-linkage-denial-invoke",
                },
            },
            headers=agent["agent_headers"],
        )
        assert denial_resp.status_code == 403
        body = denial_resp.json()
        assert body["detail"]["error"] == "permit_tool_not_allowed"
        denial_receipt = body["detail"]["receipt"]
        receipt_id = denial_receipt["receipt_id"]

        # Step 1: Verify denial receipt.wallet_id matches the mcpContext.wallet_id sent
        assert denial_receipt["wallet_id"] == agent["agent_wallet_id"], (
            f"Denial receipt wallet_id {denial_receipt['wallet_id']} != "
            f"mcpContext wallet_id {agent['agent_wallet_id']}"
        )

        # Step 2: Verify denial receipt.wallet_id matches what's stored in the database
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(ReceiptModel).where(ReceiptModel.receipt_id == receipt_id)
            )
            model = result.scalar_one()
            assert model.wallet_id == agent["agent_wallet_id"], (
                f"Stored denial wallet_id {model.wallet_id} != "
                f"expected {agent['agent_wallet_id']}"
            )

        # Step 3: Verify denial charged 0 credits (denials are free)
        assert Decimal(str(denial_receipt["credits_charged"])) == Decimal("0"), (
            "Denial should charge 0 credits"
        )

    finally:
        registry.unregister_local("wallet-linkage-other-tool")


@pytest.mark.asyncio
async def test_receipt_wallet_linkage_across_multiple_wallets(
    client, clean_database, registered_tool
):
    """Prove each wallet's receipts link to the correct wallet.

    This test creates two separate wallets, invokes the same tool from each,
    and verifies that each receipt links to the correct invoking wallet.
    """
    # Setup wallet 1
    agent1 = await provision_agent_wallet(client)
    permit1 = await create_tool_permit(
        client,
        wallet_id=agent1["agent_wallet_id"],
        key_id=agent1["key_id"],
        tool_name="wallet-linkage-test-tool",
        idem_key="wallet-linkage-permit-1",
    )

    # Setup wallet 2
    agent2 = await provision_agent_wallet(client)
    permit2 = await create_tool_permit(
        client,
        wallet_id=agent2["agent_wallet_id"],
        key_id=agent2["key_id"],
        tool_name="wallet-linkage-test-tool",
        idem_key="wallet-linkage-permit-2",
    )

    # Invoke from wallet 1
    invoke1_resp = await client.post(
        "/mcp/tools/wallet-linkage-test-tool/invoke",
        json={
            "name": "wallet-linkage-test-tool",
            "arguments": {"message": "from wallet 1"},
            "mcp_context": {
                "wallet_id": agent1["agent_wallet_id"],
                "permit_id": permit1["permit_id"],
                "idempotency_key": "wallet-1-invoke",
            },
        },
        headers=agent1["agent_headers"],
    )
    assert invoke1_resp.status_code == 200
    receipt1 = invoke1_resp.json()["receipt"]

    # Invoke from wallet 2
    invoke2_resp = await client.post(
        "/mcp/tools/wallet-linkage-test-tool/invoke",
        json={
            "name": "wallet-linkage-test-tool",
            "arguments": {"message": "from wallet 2"},
            "mcp_context": {
                "wallet_id": agent2["agent_wallet_id"],
                "permit_id": permit2["permit_id"],
                "idempotency_key": "wallet-2-invoke",
            },
        },
        headers=agent2["agent_headers"],
    )
    assert invoke2_resp.status_code == 200
    receipt2 = invoke2_resp.json()["receipt"]

    # Verify each receipt links to the correct wallet
    assert receipt1["wallet_id"] == agent1["agent_wallet_id"], (
        f"Receipt 1 wallet_id {receipt1['wallet_id']} != "
        f"agent 1 wallet_id {agent1['agent_wallet_id']}"
    )
    assert receipt2["wallet_id"] == agent2["agent_wallet_id"], (
        f"Receipt 2 wallet_id {receipt2['wallet_id']} != "
        f"agent 2 wallet_id {agent2['agent_wallet_id']}"
    )

    # Verify receipts don't cross-link (receipt 1 doesn't have wallet 2's id)
    assert receipt1["wallet_id"] != agent2["agent_wallet_id"], (
        "Receipt 1 should not link to wallet 2"
    )
    assert receipt2["wallet_id"] != agent1["agent_wallet_id"], (
        "Receipt 2 should not link to wallet 1"
    )
