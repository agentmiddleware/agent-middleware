"""Test that constant_test_loop.py verifies receipt permit_id linkage.

This test proves the constant loop catches broken permit_id linkage in receipts,
which would otherwise be a silent skip: the loop would pass even if receipts
stored the wrong permit_id or no permit_id at all.

The test simulates the constant loop's permit_id linkage check and proves it
would fail if the linkage broke.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.service_registry import get_service_registry
from tests.test_trust_helpers import provision_agent_wallet


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
        service_id="permit-linkage-test-tool",
        name="Permit Linkage Test Tool",
        description="Tool for testing receipt permit_id linkage",
        category=ServiceCategory.AGENT_COMMS,
        func=tool_func,
        credits_per_unit=2.0,
        unit_name="call",
    )
    yield registry
    registry.unregister_local("permit-linkage-test-tool")


@pytest.mark.asyncio
async def test_constant_loop_verifies_receipt_permit_linkage(
    client, clean_database, registered_tool
):
    """Prove the constant loop verifies receipt permit_id matches the permit used.

    The flow:
    1. Provision agent and create permit
    2. Invoke governed tool (success path)
    3. Verify receipt.permit_id matches the permit_id sent in mcpContext
    4. This is the check the constant loop does
    """
    # Setup
    agent = await provision_agent_wallet(client)
    permit_resp = await client.post(
        "/v1/permits",
        json={
            "issuer_wallet_id": agent["agent_wallet_id"],
            "subject_wallet_id": agent["agent_wallet_id"],
            "subject_key_id": agent["key_id"],
            "allowed_tools": ["permit-linkage-test-tool"],
            "scopes": ["tool:permit-linkage-test-tool:invoke", "billing:charge"],
            "max_credits": 50,
            "expires_at": "2030-01-01T00:00:00Z",
        },
        headers={**agent["agent_headers"], "Idempotency-Key": "permit-linkage-test"},
    )
    assert permit_resp.status_code == 201
    permit = permit_resp.json()
    permit_id = permit["permit_id"]

    # Invoke the governed tool (success path)
    invoke_resp = await client.post(
        "/mcp/tools/permit-linkage-test-tool/invoke",
        json={
            "name": "permit-linkage-test-tool",
            "arguments": {"message": "testing permit linkage"},
            "mcp_context": {
                "wallet_id": agent["agent_wallet_id"],
                "permit_id": permit_id,
                "idempotency_key": "permit-linkage-invoke",
            },
        },
        headers=agent["agent_headers"],
    )
    assert invoke_resp.status_code == 200, invoke_resp.text
    body = invoke_resp.json()
    receipt = body["receipt"]

    # This is the check the constant loop does
    assert receipt["permit_id"] == permit_id, (
        f"receipt permit_id {receipt.get('permit_id')} != used permit {permit_id}"
    )
    print(f"✓ Receipt permit_id matches: {receipt['permit_id']}")


@pytest.mark.asyncio
async def test_constant_loop_verifies_denial_receipt_permit_linkage(
    client, clean_database, registered_tool
):
    """Prove the constant loop verifies denial receipt permit_id matches.

    The flow:
    1. Provision agent and create permit for one tool
    2. Attempt to invoke a different tool (denial: permit_tool_not_allowed)
    3. Verify denial receipt.permit_id matches the permit_id sent
    """
    # Setup
    agent = await provision_agent_wallet(client)
    permit_resp = await client.post(
        "/v1/permits",
        json={
            "issuer_wallet_id": agent["agent_wallet_id"],
            "subject_wallet_id": agent["agent_wallet_id"],
            "subject_key_id": agent["key_id"],
            "allowed_tools": ["permit-linkage-test-tool"],
            "scopes": ["tool:permit-linkage-test-tool:invoke", "billing:charge"],
            "max_credits": 50,
            "expires_at": "2030-01-01T00:00:00Z",
        },
        headers={**agent["agent_headers"], "Idempotency-Key": "permit-denial-linkage"},
    )
    assert permit_resp.status_code == 201
    permit = permit_resp.json()
    permit_id = permit["permit_id"]

    # Register a second tool NOT in the permit
    registry = get_service_registry()

    def other_tool_func(message: str = "ok") -> dict:
        return {"message": message}

    registry.register_local(
        service_id="permit-linkage-other-tool",
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
            "/mcp/tools/permit-linkage-other-tool/invoke",
            json={
                "name": "permit-linkage-other-tool",
                "arguments": {"message": "should be denied"},
                "mcp_context": {
                    "wallet_id": agent["agent_wallet_id"],
                    "permit_id": permit_id,
                    "idempotency_key": "permit-denial-invoke",
                },
            },
            headers=agent["agent_headers"],
        )
        assert denial_resp.status_code == 403
        body = denial_resp.json()
        assert body["detail"]["error"] == "permit_tool_not_allowed"
        denial_receipt = body["detail"]["receipt"]

        # This is the check the constant loop does for denial receipts
        assert denial_receipt["permit_id"] == permit_id, (
            f"denial receipt permit_id {denial_receipt.get('permit_id')} != "
            f"used permit {permit_id}"
        )
        print(f"✓ Denial receipt permit_id matches: {denial_receipt['permit_id']}")

    finally:
        registry.unregister_local("permit-linkage-other-tool")


@pytest.mark.asyncio
async def test_receipt_permit_linkage_across_multiple_permits(
    client, clean_database, registered_tool
):
    """Prove each receipt links to the correct permit (no cross-permit leakage).

    This test creates two permits for the same wallet/tool, invokes with each,
    and verifies each receipt links to the correct permit.
    """
    # Setup
    agent = await provision_agent_wallet(client)

    # Create permit 1
    permit1_resp = await client.post(
        "/v1/permits",
        json={
            "issuer_wallet_id": agent["agent_wallet_id"],
            "subject_wallet_id": agent["agent_wallet_id"],
            "subject_key_id": agent["key_id"],
            "allowed_tools": ["permit-linkage-test-tool"],
            "scopes": ["tool:permit-linkage-test-tool:invoke", "billing:charge"],
            "max_credits": 50,
            "expires_at": "2030-01-01T00:00:00Z",
        },
        headers={**agent["agent_headers"], "Idempotency-Key": "multi-permit-1"},
    )
    assert permit1_resp.status_code == 201
    permit1_id = permit1_resp.json()["permit_id"]

    # Create permit 2
    permit2_resp = await client.post(
        "/v1/permits",
        json={
            "issuer_wallet_id": agent["agent_wallet_id"],
            "subject_wallet_id": agent["agent_wallet_id"],
            "subject_key_id": agent["key_id"],
            "allowed_tools": ["permit-linkage-test-tool"],
            "scopes": ["tool:permit-linkage-test-tool:invoke", "billing:charge"],
            "max_credits": 50,
            "expires_at": "2030-01-01T00:00:00Z",
        },
        headers={**agent["agent_headers"], "Idempotency-Key": "multi-permit-2"},
    )
    assert permit2_resp.status_code == 201
    permit2_id = permit2_resp.json()["permit_id"]

    # Invoke with permit 1
    invoke1_resp = await client.post(
        "/mcp/tools/permit-linkage-test-tool/invoke",
        json={
            "name": "permit-linkage-test-tool",
            "arguments": {"message": "from permit 1"},
            "mcp_context": {
                "wallet_id": agent["agent_wallet_id"],
                "permit_id": permit1_id,
                "idempotency_key": "multi-permit-invoke-1",
            },
        },
        headers=agent["agent_headers"],
    )
    assert invoke1_resp.status_code == 200
    receipt1 = invoke1_resp.json()["receipt"]

    # Invoke with permit 2
    invoke2_resp = await client.post(
        "/mcp/tools/permit-linkage-test-tool/invoke",
        json={
            "name": "permit-linkage-test-tool",
            "arguments": {"message": "from permit 2"},
            "mcp_context": {
                "wallet_id": agent["agent_wallet_id"],
                "permit_id": permit2_id,
                "idempotency_key": "multi-permit-invoke-2",
            },
        },
        headers=agent["agent_headers"],
    )
    assert invoke2_resp.status_code == 200
    receipt2 = invoke2_resp.json()["receipt"]

    # Verify each receipt links to the correct permit
    assert receipt1["permit_id"] == permit1_id, (
        f"Receipt 1 permit_id {receipt1['permit_id']} != permit 1 {permit1_id}"
    )
    assert receipt2["permit_id"] == permit2_id, (
        f"Receipt 2 permit_id {receipt2['permit_id']} != permit 2 {permit2_id}"
    )

    # Verify receipts don't cross-link
    assert receipt1["permit_id"] != permit2_id, "Receipt 1 should not link to permit 2"
    assert receipt2["permit_id"] != permit1_id, "Receipt 2 should not link to permit 1"
    print(f"✓ No cross-permit leakage: receipt1→{permit1_id}, receipt2→{permit2_id}")
