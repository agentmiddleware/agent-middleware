"""Archived PR #840's distinct API-key usage reporting route."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import app
from app.core.api_key_usage import record_api_key_usage_event


@pytest.fixture
async def client(clean_database):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def admin_key():
    settings = get_settings()
    return settings.VALID_API_KEYS.split(",")[0].strip()


async def create_test_wallet_and_key(tenant=None):
    """Use the PR #840 wallet and key fixture data."""
    from app.core.dependencies import get_agent_money
    from app.services.api_key_service import get_api_key_service

    wallet = await get_agent_money().create_sponsor_wallet(
        sponsor_name="Test Sponsor",
        email="test@example.com",
        initial_credits=1000.0,
        tenant=tenant,
    )
    key = await get_api_key_service().create_key(
        wallet_id=wallet.wallet_id,
        key_name="test_key",
        tenant=tenant,
    )
    return key


@pytest.mark.asyncio
async def test_admin_can_access(client, admin_key):
    response = await client.get(
        "/v1/api-keys/usage/daily", headers={"X-API-Key": admin_key}
    )
    assert response.status_code == 200
    assert isinstance(response.json(), list)


@pytest.mark.asyncio
async def test_demo_and_non_admin_keys_forbidden(client):
    assert (await client.get("/v1/api-keys/usage/daily")).status_code == 401
    demo = await create_test_wallet_and_key(tenant="demo")
    normal = await create_test_wallet_and_key()
    for key in (demo, normal):
        response = await client.get(
            "/v1/api-keys/usage/daily", headers={"X-API-Key": key["api_key"]}
        )
        assert response.status_code == 403


@pytest.mark.asyncio
async def test_filter_and_aggregated_fields(client, admin_key):
    key = await create_test_wallet_and_key()
    await record_api_key_usage_event(
        key_id=key["key_id"],
        tenant=None,
        method="POST",
        endpoint="/v1/permits",
        status_code=200,
        latency_ms=25,
    )
    response = await client.get(
        "/v1/api-keys/usage/daily",
        params={"key_id": key["key_id"]},
        headers={"X-API-Key": admin_key},
    )
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["key_id"] == key["key_id"]
    assert rows[0]["calls"] == 1
    assert {
        "day",
        "count_4xx",
        "count_5xx",
        "p50_latency_ms",
        "p95_latency_ms",
        "first_call",
        "last_call",
    } <= rows[0].keys()
