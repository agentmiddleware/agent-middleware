"""Wallet-scoped keys can omit wallet_id on dormant billing routes."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import app
from tests.test_trust_helpers import BOOTSTRAP_HEADERS, provision_agent_wallet


pytestmark = [pytest.mark.dormant, pytest.mark.anyio]


@pytest.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as test_client:
        yield test_client


async def test_dry_run_session_and_charge_default_bound_wallet(
    client: AsyncClient, clean_database
):
    owner = await provision_agent_wallet(client)
    headers = owner["agent_headers"]
    wallet_id = owner["agent_wallet_id"]

    session = await client.post("/v1/billing/dry-run/session", json={}, headers=headers)
    assert session.status_code == 201, session.text
    single = await client.post(
        "/v1/billing/dry-run/charge",
        json={"service": "agent_comms", "units": 1},
        headers=headers,
    )
    in_session = await client.post(
        "/v1/billing/dry-run/charge",
        json={
            "service": "agent_comms",
            "units": 1,
            "dry_run_session_id": session.json()["session_id"],
        },
        headers=headers,
    )

    assert single.status_code == in_session.status_code == 200
    assert session.json()["wallet_id"] == wallet_id
    assert single.json()["wallet_id"] == wallet_id
    assert in_session.json()["wallet_id"] == wallet_id


async def test_unbound_admin_must_name_wallet_for_dormant_billing(
    client: AsyncClient, clean_database
):
    await provision_agent_wallet(client)

    for path, body in (
        ("/v1/billing/dry-run/session", {}),
        ("/v1/billing/dry-run/charge", {"service": "agent_comms"}),
    ):
        response = await client.post(path, json=body, headers=BOOTSTRAP_HEADERS)
        assert response.status_code == 400
        assert response.json()["detail"]["error"] == "missing_wallet_id"
        assert response.json()["code"] == "missing_wallet_id"

    prepare = await client.post(
        "/v1/billing/top-up/prepare",
        params={"amount_fiat": 5},
        headers=BOOTSTRAP_HEADERS,
    )
    assert prepare.status_code == 400
    assert prepare.json()["detail"]["error"] == "missing_wallet_id"
    assert prepare.json()["code"] == "missing_wallet_id"


async def test_top_up_prepare_defaults_sponsor_wallet_and_denies_foreign_wallet(
    client: AsyncClient, clean_database, monkeypatch
):
    owner = await provision_agent_wallet(client)
    sponsor_wallet_id = owner["sponsor_wallet_id"]
    sponsor_key = await client.post(
        "/v1/api-keys",
        json={"wallet_id": sponsor_wallet_id},
        headers=BOOTSTRAP_HEADERS,
    )
    assert sponsor_key.status_code == 201
    headers = {"X-API-Key": sponsor_key.json()["api_key"]}
    monkeypatch.setattr(get_settings(), "KYC_REQUIRED_FOR_TOPUP", False)
    create_intent = AsyncMock(return_value={"wallet_id": sponsor_wallet_id})

    with patch(
        "app.routers.billing.get_stripe_integration",
        return_value=SimpleNamespace(create_top_up_intent=create_intent),
    ):
        created = await client.post(
            "/v1/billing/top-up/prepare",
            params={"amount_fiat": 5},
            headers=headers,
        )
        foreign = await client.post(
            "/v1/billing/top-up/prepare",
            params={"wallet_id": owner["agent_wallet_id"], "amount_fiat": 5},
            headers=headers,
        )

    assert created.status_code == 200, created.text
    assert created.json()["wallet_id"] == sponsor_wallet_id
    create_intent.assert_awaited_once_with(
        wallet_id=sponsor_wallet_id, amount_fiat=Decimal("5.0"), currency="USD"
    )
    assert foreign.status_code == 403
    assert foreign.json()["detail"]["error"] == "wallet_access_denied"
