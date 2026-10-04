"""Action issuance preserves upstream storage validation before admission."""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.permits import get_permit_service
from app.services.service_registry import get_service_registry
from tests.test_action_permits import BINDING, _action_request
from tests.test_trust_helpers import BOOTSTRAP_HEADERS, provision_agent_wallet


@pytest.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture
def action_registry(monkeypatch, action_permit_route):
    registry = get_service_registry()
    monkeypatch.setattr(registry, "get", AsyncMock(return_value={"fixture": True}))
    monkeypatch.setattr(registry, "get_action_binding", lambda record: BINDING)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "invalid",
    [
        {"max_credits": 0},
        {"max_credits": -1},
        {"max_credits": "1.123456789"},
        {"max_credits": "0.000000001"},
        {"max_credits": "1000000000000"},
        {"nonce": "n" * 65},
    ],
)
async def test_action_storage_bounds_reject_before_issuance_owner(
    client, clean_database, action_registry, invalid
):
    wallets = await provision_agent_wallet(client)
    payload = _action_request(
        issuer_wallet_id=wallets["sponsor_wallet_id"],
        subject_wallet_id=wallets["agent_wallet_id"],
        subject_key_id=wallets["key_id"],
    ).model_dump(mode="json")
    headers = {**BOOTSTRAP_HEADERS, "Idempotency-Key": "action-storage-bound"}
    refused = await client.post(
        "/v1/action-permits", json={**payload, **invalid}, headers=headers
    )
    assert refused.status_code == 422, refused.text
    _, total = await get_permit_service().list_permits(
        wallet_id=wallets["agent_wallet_id"]
    )
    assert total == 0
    retry = await client.post("/v1/action-permits", json=payload, headers=headers)
    assert retry.status_code == 201, retry.text


@pytest.mark.anyio
async def test_action_storage_boundary_persists_with_valid_signature(
    client, clean_database, action_registry
):
    wallets = await provision_agent_wallet(client)
    payload = _action_request(
        issuer_wallet_id=wallets["sponsor_wallet_id"],
        subject_wallet_id=wallets["agent_wallet_id"],
        subject_key_id=wallets["key_id"],
        max_credits="1.12345678",
        nonce="n" * 64,
    ).model_dump(mode="json")
    response = await client.post(
        "/v1/action-permits",
        json=payload,
        headers={**BOOTSTRAP_HEADERS, "Idempotency-Key": "action-storage-boundary"},
    )
    assert response.status_code == 201, response.text
    permit = response.json()
    assert Decimal(permit["max_credits"]) == Decimal("1.12345678")
    assert permit["nonce"] == "n" * 64
    verified = await client.post(
        "/v1/permits/verify",
        json={
            "permit_id": permit["permit_id"],
            "wallet_id": wallets["agent_wallet_id"],
            "key_id": wallets["key_id"],
            "tool": "partner.pay",
            "estimated_credits": "1",
        },
        headers=wallets["agent_headers"],
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["valid"] is True
