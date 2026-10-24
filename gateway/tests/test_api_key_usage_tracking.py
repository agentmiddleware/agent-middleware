"""Archived PR #840's distinct API-key usage reporting route."""

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.auth import AuthContext, get_auth_context
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


@pytest.mark.asyncio
async def test_admin_can_access(client, admin_key):
    response = await client.get(
        "/v1/api-keys/usage/daily", headers={"X-API-Key": admin_key}
    )
    assert response.status_code == 200
    assert isinstance(response.json(), list)


@pytest.mark.parametrize("tenant", ["demo", None])
@pytest.mark.asyncio
async def test_demo_and_non_admin_keys_forbidden(client, tenant):
    assert (await client.get("/v1/api-keys/usage/daily")).status_code == 401
    original = app.dependency_overrides.get(get_auth_context)
    try:
        app.dependency_overrides[get_auth_context] = lambda: AuthContext(
            source="db", raw_key="", tenant=tenant
        )
        response = await client.get("/v1/api-keys/usage/daily")
        assert response.status_code == 403
    finally:
        if original is None:
            app.dependency_overrides.pop(get_auth_context, None)
        else:
            app.dependency_overrides[get_auth_context] = original


@pytest.mark.asyncio
async def test_filter_and_aggregated_fields(client, admin_key):
    key_id = str(uuid4())
    await record_api_key_usage_event(
        key_id=key_id,
        tenant=None,
        method="POST",
        endpoint="/v1/permits",
        status_code=200,
        latency_ms=25,
    )
    response = await client.get(
        "/v1/api-keys/usage/daily",
        params={"key_id": key_id},
        headers={"X-API-Key": admin_key},
    )
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["key_id"] == key_id
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
