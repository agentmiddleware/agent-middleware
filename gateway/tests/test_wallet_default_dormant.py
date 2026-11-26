"""Omitted wallet IDs use the authenticated wallet on dormant REST routes."""

from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.receipts import get_receipt_service
from tests.test_trust_helpers import (
    BOOTSTRAP_HEADERS,
    create_tool_permit,
    provision_agent_wallet,
)


@pytest.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as test_client:
        yield test_client


@pytest.mark.dormant
@pytest.mark.anyio
async def test_kyc_session_defaults_bound_wallet_and_denies_foreign_wallet(
    client: AsyncClient, clean_database
):
    sponsor = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "KYC wallet default",
            "email": "kyc-default@example.com",
            "require_kyc": True,
        },
        headers=BOOTSTRAP_HEADERS,
    )
    assert sponsor.status_code == 201
    wallet_id = sponsor.json()["wallet_id"]
    key = await client.post(
        "/v1/api-keys", json={"wallet_id": wallet_id}, headers=BOOTSTRAP_HEADERS
    )
    assert key.status_code == 201
    headers = {"X-API-Key": key.json()["api_key"]}
    session = MagicMock(id="vs_wallet_default", url="https://verify.stripe.com/test")

    with patch(
        "app.services.kyc_service.stripe.identity.VerificationSession.create",
        return_value=session,
    ) as create_session:
        created = await client.post(
            "/v1/kyc/sessions",
            json={"return_url": "https://example.com/callback"},
            headers=headers,
        )
        denied = await client.post(
            "/v1/kyc/sessions",
            json={
                "wallet_id": "foreign-wallet",
                "return_url": "https://example.com/callback",
            },
            headers=headers,
        )

    assert created.status_code == 201, created.text
    assert created.json()["wallet_id"] == wallet_id
    assert create_session.call_count == 1
    assert create_session.call_args.kwargs["metadata"]["wallet_id"] == wallet_id
    assert denied.status_code == 403
    assert denied.json()["detail"]["error"] == "wallet_access_denied"


@pytest.mark.dormant
@pytest.mark.anyio
async def test_kyc_session_requires_wallet_for_unbound_admin(
    client: AsyncClient, clean_database
):
    response = await client.post(
        "/v1/kyc/sessions",
        json={"return_url": "https://example.com/callback"},
        headers=BOOTSTRAP_HEADERS,
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "missing_wallet_id"


@pytest.mark.dormant
@pytest.mark.anyio
@pytest.mark.parametrize("omit_first", [True, False])
async def test_x402_settle_default_keeps_replay_identity(
    client: AsyncClient, clean_database, omit_first: bool
):
    owner = await provision_agent_wallet(client)
    wallet_id = owner["agent_wallet_id"]
    permit = await create_tool_permit(
        client,
        wallet_id=wallet_id,
        key_id=owner["key_id"],
        tool_name="x402.payment",
        max_credits=100,
        idem_key="x402-default-permit",
    )
    body = {
        "permit_id": permit["permit_id"],
        "wallet_id": wallet_id,
        "amount": "0.03",
        "pay_to": "0x1111111111111111111111111111111111111111",
        "network": "base",
        "payer": "0x2222222222222222222222222222222222222222",
    }
    omitted = {key: value for key, value in body.items() if key != "wallet_id"}
    headers = {**owner["agent_headers"], "Idempotency-Key": "x402-default-settle"}

    first = await client.post(
        "/v1/x402/settle", json=omitted if omit_first else body, headers=headers
    )
    replay = await client.post(
        "/v1/x402/settle", json=body if omit_first else omitted, headers=headers
    )

    assert first.status_code == replay.status_code == 200, (first.text, replay.text)
    assert first.json()["wallet_id"] == wallet_id
    assert replay.json()["receipt_id"] == first.json()["receipt_id"]
    receipts, total = await get_receipt_service().list_receipts(
        permit_id=permit["permit_id"]
    )
    assert total == len(receipts) == 1


@pytest.mark.dormant
@pytest.mark.anyio
async def test_x402_settle_rejects_unbound_or_foreign_wallet(
    client: AsyncClient, clean_database
):
    owner = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=owner["agent_wallet_id"],
        key_id=owner["key_id"],
        tool_name="x402.payment",
        max_credits=100,
        idem_key="x402-default-denial-permit",
    )
    body = {
        "permit_id": permit["permit_id"],
        "amount": "0.03",
        "pay_to": "0x1111111111111111111111111111111111111111",
        "network": "base",
        "payer": "0x2222222222222222222222222222222222222222",
    }
    missing = await client.post(
        "/v1/x402/settle",
        json=body,
        headers={**BOOTSTRAP_HEADERS, "Idempotency-Key": "x402-missing-wallet"},
    )
    foreign = await client.post(
        "/v1/x402/settle",
        json={**body, "wallet_id": owner["sponsor_wallet_id"]},
        headers={**owner["agent_headers"], "Idempotency-Key": "x402-foreign"},
    )

    assert missing.status_code == 400
    assert missing.json()["detail"]["error"] == "missing_wallet_id"
    assert foreign.status_code == 403
    assert foreign.json()["detail"]["error"] == "wallet_access_denied"
    _, total = await get_receipt_service().list_receipts(permit_id=permit["permit_id"])
    assert total == 0
