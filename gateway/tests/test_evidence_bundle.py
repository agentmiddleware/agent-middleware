from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.service_registry import get_service_registry
from tests.test_trust_helpers import (
    BOOTSTRAP_HEADERS,
    create_tool_permit,
    provision_agent_wallet,
)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _invoke_governed_tool(client, provisioned, tool_name="evidence-echo"):
    registry = get_service_registry()
    registry.register_local(
        service_id=tool_name,
        name="Evidence Echo",
        description="Evidence bundle test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=lambda message="ok": {"message": message},
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
        resp = await client.post(
            "/mcp/messages",
            json={
                "jsonrpc": "2.0",
                "id": "evidence-call-1",
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": {"message": "hi"},
                    "mcpContext": {
                        "wallet_id": provisioned["agent_wallet_id"],
                        "permit_id": permit["permit_id"],
                        "idempotency_key": "evidence-invoke-1",
                    },
                },
            },
            headers=provisioned["agent_headers"],
        )
        assert resp.status_code == 200, resp.text
        return permit, resp.json()["result"]["receipt"]
    finally:
        registry.unregister_local(tool_name)


@pytest.mark.anyio
async def test_evidence_bundle_returns_flat_verified_artifact(client, clean_database):
    provisioned = await provision_agent_wallet(client)
    permit, receipt = await _invoke_governed_tool(client, provisioned)

    resp = await client.get(
        f"/v1/evidence/{receipt['receipt_id']}",
        headers=provisioned["agent_headers"],
    )
    assert resp.status_code == 200, resp.text
    bundle = resp.json()

    assert bundle["receipt_id"] == receipt["receipt_id"]
    assert bundle["valid"] is True
    assert bundle["permit"]["permit_id"] == permit["permit_id"]
    assert bundle["ledger_entry"]["entry_id"] == receipt["ledger_entry_id"]
    assert bundle["audit_event"] is not None

    verification = bundle["verification"]
    assert verification["receipt_signature"] == "ok"
    assert verification["permit_signature"] == "ok"
    assert verification["audit_chain"] == "ok"
    assert verification["request_hash"] == "ok"


@pytest.mark.anyio
async def test_evidence_bundle_for_denied_receipt(client, clean_database):
    provisioned = await provision_agent_wallet(client)
    registry = get_service_registry()
    registry.register_local(
        service_id="ev-allowed",
        name="Allowed",
        description="permitted tool",
        category=ServiceCategory.AGENT_COMMS,
        func=lambda: {"ok": True},
        credits_per_unit=2.0,
        unit_name="call",
    )
    registry.register_local(
        service_id="ev-blocked",
        name="Blocked",
        description="out-of-scope tool",
        category=ServiceCategory.AGENT_COMMS,
        func=lambda: {"ok": True},
        credits_per_unit=2.0,
        unit_name="call",
    )
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name="ev-allowed",
        )
        denied = await client.post(
            "/mcp/messages",
            json={
                "jsonrpc": "2.0",
                "id": "ev-denied",
                "method": "tools/call",
                "params": {
                    "name": "ev-blocked",
                    "arguments": {},
                    "mcpContext": {
                        "wallet_id": provisioned["agent_wallet_id"],
                        "permit_id": permit["permit_id"],
                        "idempotency_key": "ev-denied-1",
                    },
                },
            },
            headers=provisioned["agent_headers"],
        )
        receipt = denied.json()["error"]["data"]["receipt"]
        assert receipt["outcome"] == "denied"

        resp = await client.get(
            f"/v1/evidence/{receipt['receipt_id']}",
            headers=provisioned["agent_headers"],
        )
        assert resp.status_code == 200, resp.text
        bundle = resp.json()
        # A denied receipt is genuinely signed; the bundle exposes that while
        # the absence of a charge leaves no ledger entry.
        assert bundle["verification"]["receipt_signature"] == "ok"
        assert bundle["ledger_entry"] is None
        # Out-of-scope tool means the receipt does not satisfy permit scope,
        # so the overall bundle is not "valid".
        assert bundle["valid"] is False
    finally:
        registry.unregister_local("ev-allowed")
        registry.unregister_local("ev-blocked")


@pytest.mark.anyio
async def test_evidence_bundle_for_refunded_receipt(client, clean_database):
    provisioned = await provision_agent_wallet(client)
    registry = get_service_registry()

    def boom():
        raise RuntimeError("tool exploded")

    registry.register_local(
        service_id="ev-failing",
        name="Failing",
        description="raises at runtime",
        category=ServiceCategory.AGENT_COMMS,
        func=boom,
        credits_per_unit=2.0,
        unit_name="call",
    )
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name="ev-failing",
        )
        failed = await client.post(
            "/mcp/messages",
            json={
                "jsonrpc": "2.0",
                "id": "ev-failed",
                "method": "tools/call",
                "params": {
                    "name": "ev-failing",
                    "arguments": {},
                    "mcpContext": {
                        "wallet_id": provisioned["agent_wallet_id"],
                        "permit_id": permit["permit_id"],
                        "idempotency_key": "ev-failed-1",
                    },
                },
            },
            headers=provisioned["agent_headers"],
        )
        receipt = failed.json()["error"]["data"]["receipt"]
        assert receipt["outcome"] == "failed_refunded"

        resp = await client.get(
            f"/v1/evidence/{receipt['receipt_id']}",
            headers=provisioned["agent_headers"],
        )
        assert resp.status_code == 200, resp.text
        bundle = resp.json()
        assert bundle["verification"]["receipt_signature"] == "ok"
        assert bundle["verification"]["audit_chain"] == "ok"
        # The original debit is linked and matched by a refund entry, so the
        # ledger linkage still holds and the bundle verifies.
        assert bundle["ledger_entry"] is not None
        assert bundle["valid"] is True
    finally:
        registry.unregister_local("ev-failing")


@pytest.mark.anyio
async def test_evidence_bundle_unknown_receipt_returns_404(client, clean_database):
    resp = await client.get(
        "/v1/evidence/rcpt-does-not-exist",
        headers=BOOTSTRAP_HEADERS,
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "receipt_not_found"


@pytest.mark.anyio
async def test_evidence_bundle_denies_cross_wallet_access(client, clean_database):
    owner = await provision_agent_wallet(client)
    _, receipt = await _invoke_governed_tool(client, owner)

    # A second, unrelated agent wallet must not read the owner's evidence.
    other_sponsor = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Other Sponsor",
            "email": "other@example.com",
            "initial_credits": 5000,
            "require_kyc": False,
        },
        headers=BOOTSTRAP_HEADERS,
    )
    other_sponsor_id = other_sponsor.json()["wallet_id"]
    other_agent = await client.post(
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": other_sponsor_id,
            "agent_id": "other-agent",
            "budget_credits": 500,
            "daily_limit": 250,
        },
        headers=BOOTSTRAP_HEADERS,
    )
    other_agent_id = other_agent.json()["wallet_id"]
    other_key = await client.post(
        "/v1/api-keys",
        json={
            "wallet_id": other_agent_id,
            "key_name": "other-runtime",
            "expires_in_days": 30,
        },
        headers=BOOTSTRAP_HEADERS,
    )
    other_headers = {"X-API-Key": other_key.json()["api_key"]}

    resp = await client.get(
        f"/v1/evidence/{receipt['receipt_id']}",
        headers=other_headers,
    )
    assert resp.status_code == 403


# The evidence bundle is the buyer-facing artifact an auditor verifies, so its
# read endpoint must fail closed for unauthenticated callers before any receipt
# lookup. The credential decides the status, not whether the receipt exists: a
# missing or malformed key is 401, a well-formed but unknown key is 403 — and an
# unknown receipt id must not 404 for an anonymous caller (that would leak
# existence). See app/core/auth.get_auth_context.
@pytest.mark.anyio
@pytest.mark.parametrize(
    "headers,expected_status",
    (
        ({}, 401),
        ({"X-API-Key": "short"}, 401),
        ({"X-API-Key": "not-a-real-key"}, 403),
    ),
)
async def test_evidence_bundle_requires_authentication(
    client, clean_database, headers, expected_status
):
    resp = await client.get("/v1/evidence/rcpt-does-not-exist", headers=headers)
    assert resp.status_code == expected_status, resp.text
    assert resp.status_code != 404, resp.text
