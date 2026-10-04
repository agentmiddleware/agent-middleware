from __future__ import annotations

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


async def _create_receipted_action(
    client: AsyncClient,
    *,
    provisioned: dict,
    tool_name: str,
    idem_key: str,
) -> tuple[dict, str]:
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name=tool_name,
        idem_key=idem_key,
    )
    audit_event = await record_audit_event(
        event="mcp.invoke",
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool=tool_name,
        endpoint="/mcp/messages",
        auth_source="db",
        policy_decision_id=f"pol-{tool_name}",
        request_id=f"req-{tool_name}",
        ok=True,
        metadata={"permit_id": permit["permit_id"]},
    )
    receipt = await get_receipt_service().create_receipt(
        permit_id=permit["permit_id"],
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool=tool_name,
        request_payload={"message": "hello"},
        response_payload={"message": "world"},
        ledger_entry_id=None,
        credits_authorized=Decimal("2"),
        credits_charged=Decimal("0"),
        outcome="success",
        audit_event_id=audit_event.event_id,
    )
    return permit, receipt.receipt_id


@pytest.mark.anyio
async def test_agent_can_inspect_own_trust_ledger_without_wallet_query(
    client,
    clean_database,
):
    provisioned = await provision_agent_wallet(client)
    permit, receipt_id = await _create_receipted_action(
        client,
        provisioned=provisioned,
        tool_name="self-ledger-tool",
        idem_key="self-ledger-permit",
    )

    permits = await client.get(
        "/v1/me/permits",
        params={"status": "active"},
        headers=provisioned["agent_headers"],
    )
    assert permits.status_code == 200
    permits_body = permits.json()
    assert permits_body["total"] == 1
    assert permits_body["permits"][0]["permit_id"] == permit["permit_id"]
    assert permits_body["permits"][0]["subject_key_id"] == provisioned["key_id"]

    receipts = await client.get(
        "/v1/me/receipts",
        params={"tool": "self-ledger-tool", "outcome": "success"},
        headers=provisioned["agent_headers"],
    )
    assert receipts.status_code == 200
    receipts_body = receipts.json()
    assert receipts_body["total"] == 1
    assert receipts_body["receipts"][0]["receipt_id"] == receipt_id
    assert receipts_body["receipts"][0]["permit_id"] == permit["permit_id"]

    audit = await client.get(
        "/v1/me/audit/events",
        params={"event": "mcp.invoke", "tool": "self-ledger-tool"},
        headers=provisioned["agent_headers"],
    )
    assert audit.status_code == 200
    audit_body = audit.json()
    assert audit_body["total"] == 1
    assert audit_body["events"][0]["wallet_id"] == provisioned["agent_wallet_id"]
    assert audit_body["events"][0]["key_id"] == provisioned["key_id"]
    assert audit_body["events"][0]["metadata"]["permit_id"] == permit["permit_id"]


@pytest.mark.anyio
async def test_agent_self_trust_ledger_excludes_other_wallet_records(
    client,
    clean_database,
):
    wallet_a = await provision_agent_wallet(client)
    wallet_b = await provision_agent_wallet(client)
    permit_a, receipt_a = await _create_receipted_action(
        client,
        provisioned=wallet_a,
        tool_name="self-ledger-a",
        idem_key="self-ledger-a-permit",
    )
    permit_b, receipt_b = await _create_receipted_action(
        client,
        provisioned=wallet_b,
        tool_name="self-ledger-b",
        idem_key="self-ledger-b-permit",
    )

    a_permits = await client.get("/v1/me/permits", headers=wallet_a["agent_headers"])
    assert a_permits.status_code == 200
    a_permit_ids = {permit["permit_id"] for permit in a_permits.json()["permits"]}
    assert permit_a["permit_id"] in a_permit_ids
    assert permit_b["permit_id"] not in a_permit_ids

    a_receipts = await client.get("/v1/me/receipts", headers=wallet_a["agent_headers"])
    assert a_receipts.status_code == 200
    a_receipt_ids = {receipt["receipt_id"] for receipt in a_receipts.json()["receipts"]}
    assert receipt_a in a_receipt_ids
    assert receipt_b not in a_receipt_ids

    a_audit = await client.get(
        "/v1/me/audit/events",
        headers=wallet_a["agent_headers"],
    )
    assert a_audit.status_code == 200
    assert {event["wallet_id"] for event in a_audit.json()["events"]} == {
        wallet_a["agent_wallet_id"]
    }


@pytest.mark.anyio
async def test_bootstrap_key_cannot_use_me_trust_ledger(
    client,
    clean_database,
):
    for path in ("/v1/me/permits", "/v1/me/receipts", "/v1/me/audit/events"):
        response = await client.get(path, headers=BOOTSTRAP_HEADERS)
        assert response.status_code == 403
        assert response.json()["detail"]["error"] == "wallet_key_required"
