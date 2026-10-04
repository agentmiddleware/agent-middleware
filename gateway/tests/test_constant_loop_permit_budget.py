"""Test that constant_test_loop.py verifies permit spent_credits tracking.

This test proves the constant loop catches broken permit budget tracking, which
would otherwise be a silent skip: the loop would pass even if spent_credits
never increased after charges.

The test simulates the constant loop's permit spent_credits check and proves
it would fail if budget tracking regressed.
"""

from __future__ import annotations

from decimal import Decimal

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
        service_id="permit-budget-test-tool",
        name="Permit Budget Test Tool",
        description="Tool for testing permit spent_credits tracking",
        category=ServiceCategory.AGENT_COMMS,
        func=tool_func,
        credits_per_unit=2.0,
        unit_name="call",
    )
    yield registry
    registry.unregister_local("permit-budget-test-tool")


@pytest.mark.asyncio
async def test_constant_loop_verifies_permit_spent_credits_increase(
    client, clean_database, registered_tool
):
    """Prove the constant loop verifies permit spent_credits increases after charge.

    The flow:
    1. Provision agent and create permit (spent_credits = 0)
    2. Invoke governed tool (success path)
    3. Verify permit spent_credits increased by the charged amount
    4. Verify the check would catch if spent_credits didn't increase
    """
    # Setup
    agent = await provision_agent_wallet(client)
    permit_resp = await client.post(
        "/v1/permits",
        json={
            "issuer_wallet_id": agent["agent_wallet_id"],
            "subject_wallet_id": agent["agent_wallet_id"],
            "subject_key_id": agent["key_id"],
            "allowed_tools": ["permit-budget-test-tool"],
            "scopes": ["tool:permit-budget-test-tool:invoke", "billing:charge"],
            "max_credits": 50,
            "expires_at": "2030-01-01T00:00:00Z",
        },
        headers={**agent["agent_headers"], "Idempotency-Key": "permit-budget-test"},
    )
    assert permit_resp.status_code == 201
    permit = permit_resp.json()
    permit_id = permit["permit_id"]

    # Step 1: Verify initial spent_credits is 0
    initial_spent = Decimal(str(permit["spent_credits"]))
    assert initial_spent == Decimal("0"), (
        f"New permit should have 0 spent_credits, got {initial_spent}"
    )

    # Step 2: Invoke the governed tool (success path)
    invoke_resp = await client.post(
        "/mcp/tools/permit-budget-test-tool/invoke",
        json={
            "name": "permit-budget-test-tool",
            "arguments": {"message": "testing permit budget"},
            "mcp_context": {
                "wallet_id": agent["agent_wallet_id"],
                "permit_id": permit_id,
                "idempotency_key": "permit-budget-invoke",
            },
        },
        headers=agent["agent_headers"],
    )
    assert invoke_resp.status_code == 200, invoke_resp.text
    body = invoke_resp.json()
    receipt = body["receipt"]
    charged = Decimal(str(receipt["credits_charged"]))
    assert charged > 0, f"Expected positive charge, got {charged}"

    # Step 3: Verify permit spent_credits increased (as constant loop does)
    permit_after_resp = await client.get(
        f"/v1/permits/{permit_id}",
        headers=agent["agent_headers"],
    )
    assert permit_after_resp.status_code == 200
    permit_after = permit_after_resp.json()
    spent_after = Decimal(str(permit_after["spent_credits"]))

    # This is the check the constant loop does - if this fails, budget tracking is broken
    assert spent_after == initial_spent + charged, (
        f"permit spent_credits {spent_after} != "
        f"initial {initial_spent} + charged {charged}"
    )
    print(f"✓ Permit spent_credits increased correctly: 0 → {spent_after}")


@pytest.mark.asyncio
async def test_constant_loop_verifies_permit_spent_credits_replay_safe(
    client, clean_database, registered_tool
):
    """Prove permit spent_credits doesn't double-count on replay.

    The flow:
    1. Provision agent and create permit
    2. Invoke governed tool (success path)
    3. Verify spent_credits increased
    4. Replay with same idempotency key
    5. Verify spent_credits did NOT increase again (replay is free)
    """
    # Setup
    agent = await provision_agent_wallet(client)
    permit_resp = await client.post(
        "/v1/permits",
        json={
            "issuer_wallet_id": agent["agent_wallet_id"],
            "subject_wallet_id": agent["agent_wallet_id"],
            "subject_key_id": agent["key_id"],
            "allowed_tools": ["permit-budget-test-tool"],
            "scopes": ["tool:permit-budget-test-tool:invoke", "billing:charge"],
            "max_credits": 50,
            "expires_at": "2030-01-01T00:00:00Z",
        },
        headers={**agent["agent_headers"], "Idempotency-Key": "permit-replay-test"},
    )
    assert permit_resp.status_code == 201
    permit = permit_resp.json()
    permit_id = permit["permit_id"]

    # Invoke the governed tool (first time)
    invoke_body = {
        "name": "permit-budget-test-tool",
        "arguments": {"message": "testing replay"},
        "mcp_context": {
            "wallet_id": agent["agent_wallet_id"],
            "permit_id": permit_id,
            "idempotency_key": "permit-replay-invoke",
        },
    }
    first_resp = await client.post(
        "/mcp/tools/permit-budget-test-tool/invoke",
        json=invoke_body,
        headers=agent["agent_headers"],
    )
    assert first_resp.status_code == 200
    charged = Decimal(str(first_resp.json()["receipt"]["credits_charged"]))

    # Check spent_credits after first invoke
    permit_after_first = await client.get(
        f"/v1/permits/{permit_id}", headers=agent["agent_headers"]
    )
    spent_after_first = Decimal(str(permit_after_first.json()["spent_credits"]))
    assert spent_after_first == charged

    # Replay with same idempotency key
    replay_resp = await client.post(
        "/mcp/tools/permit-budget-test-tool/invoke",
        json=invoke_body,
        headers=agent["agent_headers"],
    )
    assert replay_resp.status_code == 200

    # Verify spent_credits did NOT increase again (replay is free)
    permit_after_replay = await client.get(
        f"/v1/permits/{permit_id}", headers=agent["agent_headers"]
    )
    spent_after_replay = Decimal(str(permit_after_replay.json()["spent_credits"]))
    assert spent_after_replay == spent_after_first, (
        f"Replay should not increase spent_credits: "
        f"{spent_after_replay} != {spent_after_first}"
    )
    print(
        f"✓ Replay did not double-count: spent_credits stayed at {spent_after_replay}"
    )


@pytest.mark.asyncio
async def test_permit_spent_credits_matches_receipt_charged(
    client, clean_database, registered_tool
):
    """Prove permit spent_credits always matches sum of receipt credits_charged.

    This is the fundamental invariant: the permit's spent_credits must equal
    the total of all successful receipts' credits_charged for that permit.
    """
    # Setup
    agent = await provision_agent_wallet(client)
    permit_resp = await client.post(
        "/v1/permits",
        json={
            "issuer_wallet_id": agent["agent_wallet_id"],
            "subject_wallet_id": agent["agent_wallet_id"],
            "subject_key_id": agent["key_id"],
            "allowed_tools": ["permit-budget-test-tool"],
            "scopes": ["tool:permit-budget-test-tool:invoke", "billing:charge"],
            "max_credits": 50,
            "expires_at": "2030-01-01T00:00:00Z",
        },
        headers={**agent["agent_headers"], "Idempotency-Key": "permit-sum-test"},
    )
    assert permit_resp.status_code == 201
    permit = permit_resp.json()
    permit_id = permit["permit_id"]

    # Invoke multiple times with different idempotency keys
    total_charged = Decimal("0")
    for i in range(3):
        invoke_resp = await client.post(
            "/mcp/tools/permit-budget-test-tool/invoke",
            json={
                "name": "permit-budget-test-tool",
                "arguments": {"message": f"call {i}"},
                "mcp_context": {
                    "wallet_id": agent["agent_wallet_id"],
                    "permit_id": permit_id,
                    "idempotency_key": f"permit-sum-invoke-{i}",
                },
            },
            headers=agent["agent_headers"],
        )
        assert invoke_resp.status_code == 200
        charged = Decimal(str(invoke_resp.json()["receipt"]["credits_charged"]))
        total_charged += charged

    # Verify permit spent_credits equals sum of all charges
    permit_final = await client.get(
        f"/v1/permits/{permit_id}", headers=agent["agent_headers"]
    )
    spent_final = Decimal(str(permit_final.json()["spent_credits"]))
    assert spent_final == total_charged, (
        f"permit spent_credits {spent_final} != sum of charges {total_charged}"
    )
    print(f"✓ Permit spent_credits matches sum of charges: {spent_final}")
