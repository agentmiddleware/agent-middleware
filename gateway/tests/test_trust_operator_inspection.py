from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.audit_log import record_audit_event
from app.services.receipts import get_receipt_service
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


@pytest.mark.anyio
async def test_admin_and_wallet_can_inspect_permits_with_filters(
    client,
    clean_database,
):
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="inspect-tool",
    )

    admin_list = await client.get(
        "/v1/permits",
        params={
            "wallet_id": provisioned["agent_wallet_id"],
            "status": "active",
            "subject_key_id": provisioned["key_id"],
        },
        headers=BOOTSTRAP_HEADERS,
    )
    assert admin_list.status_code == 200
    admin_payload = admin_list.json()
    assert admin_payload["total"] == 1
    assert admin_payload["permits"][0]["permit_id"] == permit["permit_id"]

    wallet_list = await client.get(
        "/v1/permits",
        params={"wallet_id": provisioned["agent_wallet_id"]},
        headers=provisioned["agent_headers"],
    )
    assert wallet_list.status_code == 200
    assert wallet_list.json()["permits"][0]["subject_key_id"] == provisioned["key_id"]

    fetched = await client.get(
        f"/v1/permits/{permit['permit_id']}",
        headers=provisioned["agent_headers"],
    )
    assert fetched.status_code == 200
    assert fetched.json()["permit_id"] == permit["permit_id"]


@pytest.mark.anyio
async def test_wallet_permit_inspection_is_self_scoped_and_rejects_other_wallets(
    client,
    clean_database,
):
    wallet_a = await provision_agent_wallet(client)
    wallet_b = await provision_agent_wallet(client)

    # An unscoped list from a wallet key is its own permits, not everyone's.
    global_resp = await client.get("/v1/permits", headers=wallet_a["agent_headers"])
    assert global_resp.status_code == 200
    assert all(
        permit["subject_wallet_id"] == wallet_a["agent_wallet_id"]
        or permit["issuer_wallet_id"] == wallet_a["agent_wallet_id"]
        for permit in global_resp.json()["permits"]
    )

    other_wallet_resp = await client.get(
        "/v1/permits",
        params={"wallet_id": wallet_b["agent_wallet_id"]},
        headers=wallet_a["agent_headers"],
    )
    assert other_wallet_resp.status_code == 403


@pytest.mark.anyio
async def test_receipt_inspection_filters_and_permit_route_include_audit_ids(
    client,
    clean_database,
):
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="receipt-inspect",
    )
    audit_event = await record_audit_event(
        event="mcp.invoke",
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool="receipt-inspect",
        endpoint="/mcp/messages",
        auth_source="db",
        ok=True,
    )
    receipt = await get_receipt_service().create_receipt(
        permit_id=permit["permit_id"],
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool="receipt-inspect",
        request_payload={"message": "hello"},
        response_payload={"message": "world"},
        ledger_entry_id=None,
        credits_authorized=Decimal("2"),
        credits_charged=Decimal("0"),
        outcome="success",
        audit_event_id=audit_event.event_id,
    )

    wallet_list = await client.get(
        "/v1/receipts",
        params={
            "permit_id": permit["permit_id"],
            "wallet_id": provisioned["agent_wallet_id"],
            "tool": "receipt-inspect",
            "outcome": "success",
        },
        headers=provisioned["agent_headers"],
    )
    assert wallet_list.status_code == 200
    wallet_payload = wallet_list.json()
    assert wallet_payload["total"] == 1
    listed = wallet_payload["receipts"][0]
    assert listed["receipt_id"] == receipt.receipt_id
    assert listed["audit_event_id"] == audit_event.event_id
    assert "ledger_entry_id" in listed

    permit_receipts = await client.get(
        f"/v1/permits/{permit['permit_id']}/receipts",
        headers=provisioned["agent_headers"],
    )
    assert permit_receipts.status_code == 200
    assert permit_receipts.json()["receipts"][0]["receipt_id"] == receipt.receipt_id


@pytest.mark.anyio
async def test_wallet_receipt_inspection_rejects_global_queries(
    client,
    clean_database,
):
    provisioned = await provision_agent_wallet(client)

    response = await client.get("/v1/receipts", headers=provisioned["agent_headers"])

    assert response.status_code == 403


@pytest.mark.anyio
async def test_permit_issuer_can_inspect_subject_receipts(
    client,
    clean_database,
):
    provisioned = await provision_agent_wallet(client)
    sponsor_key_resp = await client.post(
        "/v1/api-keys",
        json={
            "wallet_id": provisioned["sponsor_wallet_id"],
            "key_name": "trust-issuer-inspector",
            "expires_in_days": 30,
        },
        headers=BOOTSTRAP_HEADERS,
    )
    assert sponsor_key_resp.status_code == 201
    sponsor_headers = {"X-API-Key": sponsor_key_resp.json()["api_key"]}

    permit_resp = await client.post(
        "/v1/permits",
        json={
            "issuer_wallet_id": provisioned["sponsor_wallet_id"],
            "subject_wallet_id": provisioned["agent_wallet_id"],
            "subject_key_id": provisioned["key_id"],
            "allowed_tools": ["split-inspect-tool"],
            "scopes": ["tool:split-inspect-tool:invoke", "billing:charge"],
            "max_credits": 50,
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=30)
            ).isoformat(),
        },
        headers={**sponsor_headers, "Idempotency-Key": "split-inspect-permit"},
    )
    assert permit_resp.status_code == 201
    permit = permit_resp.json()

    audit_event = await record_audit_event(
        event="mcp.invoke",
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool="split-inspect-tool",
        endpoint="/mcp/messages",
        auth_source="db",
        ok=True,
    )
    receipt = await get_receipt_service().create_receipt(
        permit_id=permit["permit_id"],
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool="split-inspect-tool",
        request_payload={"message": "hello"},
        response_payload={"message": "world"},
        ledger_entry_id=None,
        credits_authorized=Decimal("2"),
        credits_charged=Decimal("0"),
        outcome="success",
        audit_event_id=audit_event.event_id,
    )

    permit_receipts = await client.get(
        f"/v1/permits/{permit['permit_id']}/receipts",
        headers=sponsor_headers,
    )
    assert permit_receipts.status_code == 200
    assert permit_receipts.json()["receipts"][0]["receipt_id"] == receipt.receipt_id

    filtered_receipts = await client.get(
        "/v1/receipts",
        params={
            "permit_id": permit["permit_id"],
            "wallet_id": provisioned["agent_wallet_id"],
        },
        headers=sponsor_headers,
    )
    assert filtered_receipts.status_code == 200
    assert filtered_receipts.json()["receipts"][0]["receipt_id"] == receipt.receipt_id

    fetched = await client.get(
        f"/v1/receipts/{receipt.receipt_id}",
        headers=sponsor_headers,
    )
    assert fetched.status_code == 200
    assert fetched.json()["receipt_id"] == receipt.receipt_id

    verified = await client.post(
        "/v1/receipts/verify",
        json={"receipt_id": receipt.receipt_id},
        headers=sponsor_headers,
    )
    assert verified.status_code == 200
    assert verified.json()["valid"] is True

    other_wallet = await provision_agent_wallet(client)
    cross_wallet = await client.get(
        "/v1/receipts",
        params={
            "permit_id": permit["permit_id"],
            "wallet_id": other_wallet["agent_wallet_id"],
        },
        headers=sponsor_headers,
    )
    assert cross_wallet.status_code == 403
