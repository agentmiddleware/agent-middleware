"""A stored cross-tenant permit denial replays its signed response."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.database import get_session_factory
from app.db.models import IdempotencyRecordModel
from app.main import app
from app.services.receipts import get_receipt_service
from tests.test_mcp_idempotency_key_validation import counted_tool  # noqa: F401
from tests.test_trust_helpers import (
    BOOTSTRAP_HEADERS,
    create_tool_permit,
    provision_agent_wallet,
)


@pytest.mark.anyio
async def test_foreign_permit_denial_replays_original_403(
    clean_database,
    counted_tool,  # noqa: F811
):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        tenant_a = await provision_agent_wallet(client)
        tenant_b = await provision_agent_wallet(client)
        permit_a = await create_tool_permit(
            client,
            wallet_id=tenant_a["agent_wallet_id"],
            key_id=tenant_a["key_id"],
            tool_name=counted_tool.name,
        )
        key = f"call_sel482_{uuid4().hex}"
        request = {
            "name": counted_tool.name,
            "arguments": {"text": "one"},
            "mcp_context": {
                "wallet_id": tenant_b["agent_wallet_id"],
                "permit_id": permit_a["permit_id"],
                "idempotency_key": key,
            },
        }
        headers = {**tenant_b["agent_headers"], "Idempotency-Key": key}
        path = f"/mcp/tools/{counted_tool.name}/invoke"

        first = await client.post(path, json=request, headers=headers)
        async with get_session_factory()() as session:
            record = (
                await session.execute(
                    select(IdempotencyRecordModel).where(
                        IdempotencyRecordModel.idempotency_key == key
                    )
                )
            ).scalar_one()
        stored = json.loads(record.response_json)
        assert set(stored) >= {"error", "receipt", "details"}
        replay = await client.post(path, json=request, headers=headers)
        other_key = await client.post(
            "/v1/api-keys",
            json={
                "wallet_id": tenant_b["agent_wallet_id"],
                "key_name": "replay-other-key",
                "expires_in_days": 30,
            },
            headers=BOOTSTRAP_HEADERS,
        )
        assert other_key.status_code == 201
        refused_other_key = await client.post(
            path,
            json=request,
            headers={
                "X-API-Key": other_key.json()["api_key"],
                "Idempotency-Key": key,
            },
        )

    assert first.status_code == replay.status_code == 403
    detail = first.json()["detail"]
    assert detail["error"] == "permit_wallet_mismatch"
    assert detail["details"] == {"bound_to": "subject_wallet_id"}
    receipt = detail["receipt"]
    assert receipt["wallet_id"] == tenant_b["agent_wallet_id"]
    assert receipt["outcome"] == "denied"
    assert receipt["credits_charged"] == "0"
    assert set(replay.json()["detail"]) == set(detail)
    assert replay.content == first.content
    assert refused_other_key.status_code == 403
    assert refused_other_key.json()["detail"] == {"error": "permit_wallet_mismatch"}
    assert tenant_a["agent_wallet_id"] not in first.text
    assert counted_tool.effects == []

    valid, reason, _ = await get_receipt_service().verify_receipt(receipt["receipt_id"])
    assert (valid, reason) == (True, None)
