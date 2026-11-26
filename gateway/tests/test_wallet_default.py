"""Wallet-scoped API keys can omit their own wallet ID on key management calls."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.service_registry import get_service_registry
from tests.test_trust_helpers import BOOTSTRAP_HEADERS


@pytest.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as test_client:
        yield test_client


async def _sponsor_wallet(client: AsyncClient, name: str) -> str:
    response = await client.post(
        "/v1/billing/wallets/sponsor",
        json={"sponsor_name": name, "email": f"{name}@example.com"},
        headers=BOOTSTRAP_HEADERS,
    )
    assert response.status_code == 201
    return response.json()["wallet_id"]


async def _wallet_key(client: AsyncClient, wallet_id: str) -> dict:
    response = await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id},
        headers=BOOTSTRAP_HEADERS,
    )
    assert response.status_code == 201
    return response.json()


@pytest.fixture
def registered_tool():
    registry = get_service_registry()
    registry.register_local(
        service_id="wallet-default-quote",
        name="Wallet default quote",
        description="Quote wallet default test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=lambda: {"ok": True},
        credits_per_unit=1.0,
        unit_name="call",
    )
    yield
    registry.unregister_local("wallet-default-quote")


@pytest.mark.anyio
async def test_bound_key_defaults_wallet_id_for_create_and_idempotent_replay(
    client: AsyncClient, clean_database
):
    wallet_id = await _sponsor_wallet(client, "default-create")
    bound_key = await _wallet_key(client, wallet_id)
    headers = {
        "X-API-Key": bound_key["api_key"],
        "Idempotency-Key": "default-create-replay",
    }

    created = await client.post(
        "/v1/api-keys", json={"key_name": "defaulted"}, headers=headers
    )
    replay = await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id, "key_name": "defaulted"},
        headers=headers,
    )

    assert created.status_code == replay.status_code == 201
    assert created.json()["wallet_id"] == wallet_id
    assert replay.json()["key_id"] == created.json()["key_id"]
    assert replay.json()["api_key"] is None


@pytest.mark.anyio
async def test_bound_key_defaults_wallet_id_for_rotation(
    client: AsyncClient, clean_database
):
    wallet_id = await _sponsor_wallet(client, "default-rotate")
    bound_key = await _wallet_key(client, wallet_id)
    headers = {
        "X-API-Key": bound_key["api_key"],
        "Idempotency-Key": "default-rotate-replay",
    }

    response = await client.post(
        "/v1/api-keys/rotate",
        json={"key_id": bound_key["key_id"], "revoke_old": False},
        headers=headers,
    )
    replay = await client.post(
        "/v1/api-keys/rotate",
        json={
            "wallet_id": wallet_id,
            "key_id": bound_key["key_id"],
            "revoke_old": False,
        },
        headers=headers,
    )

    assert response.status_code == replay.status_code == 200
    assert response.json()["wallet_id"] == wallet_id
    assert response.json()["new_key"]["wallet_id"] == wallet_id
    assert replay.json()["rotation_id"] == response.json()["rotation_id"]
    assert replay.json()["new_key"]["api_key"] is None


@pytest.mark.anyio
async def test_bound_key_defaults_wallet_id_for_emergency_revoke(
    client: AsyncClient, clean_database
):
    wallet_id = await _sponsor_wallet(client, "default-revoke")
    bound_key = await _wallet_key(client, wallet_id)

    response = await client.post(
        "/v1/api-keys/emergency-revoke",
        json={"create_new_key": False},
        headers={"X-API-Key": bound_key["api_key"]},
    )

    assert response.status_code == 200
    assert response.json()["wallet_id"] == wallet_id
    assert bound_key["key_id"] in response.json()["revoked_keys"]


@pytest.mark.anyio
async def test_unbound_admin_must_name_wallet_when_multiple_exist(
    client: AsyncClient, clean_database
):
    await _sponsor_wallet(client, "first-admin-wallet")
    await _sponsor_wallet(client, "second-admin-wallet")

    for path, body in (
        ("/v1/api-keys", {}),
        ("/v1/api-keys/rotate", {}),
        ("/v1/api-keys/emergency-revoke", {"create_new_key": False}),
    ):
        response = await client.post(path, json=body, headers=BOOTSTRAP_HEADERS)
        assert response.status_code == 400
        assert response.json()["detail"]["error"] == "missing_wallet_id"
        assert response.json()["code"] == "missing_wallet_id"


@pytest.mark.anyio
async def test_explicit_foreign_wallet_still_denied(
    client: AsyncClient, clean_database
):
    own_wallet = await _sponsor_wallet(client, "own-wallet")
    foreign_wallet = await _sponsor_wallet(client, "foreign-wallet")
    bound_key = await _wallet_key(client, own_wallet)

    headers = {"X-API-Key": bound_key["api_key"]}
    responses = (
        await client.post(
            "/v1/api-keys", json={"wallet_id": foreign_wallet}, headers=headers
        ),
        await client.get(
            "/v1/receipts", params={"wallet_id": foreign_wallet}, headers=headers
        ),
    )
    for response in responses:
        assert response.status_code == 403
        assert response.json()["detail"]["error"] == "wallet_access_denied"


@pytest.mark.anyio
async def test_bound_key_defaults_wallet_id_for_quote(
    client: AsyncClient, clean_database, registered_tool
):
    wallet_id = await _sponsor_wallet(client, "default-quote")
    bound_key = await _wallet_key(client, wallet_id)

    response = await client.post(
        "/v1/quotes",
        json={"tool": "wallet-default-quote"},
        headers={"X-API-Key": bound_key["api_key"]},
    )

    assert response.status_code == 201
    assert response.json()["wallet_id"] == wallet_id


@pytest.mark.anyio
async def test_unbound_admin_must_name_wallet_for_quote(
    client: AsyncClient, clean_database, registered_tool
):
    await _sponsor_wallet(client, "first-quote-wallet")
    await _sponsor_wallet(client, "second-quote-wallet")

    response = await client.post(
        "/v1/quotes",
        json={"tool": "wallet-default-quote"},
        headers=BOOTSTRAP_HEADERS,
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "missing_wallet_id"
    assert response.json()["code"] == "missing_wallet_id"


@pytest.mark.anyio
async def test_quote_explicit_foreign_wallet_still_denied(
    client: AsyncClient, clean_database, registered_tool
):
    own_wallet = await _sponsor_wallet(client, "own-quote-wallet")
    foreign_wallet = await _sponsor_wallet(client, "foreign-quote-wallet")
    bound_key = await _wallet_key(client, own_wallet)

    response = await client.post(
        "/v1/quotes",
        json={"wallet_id": foreign_wallet, "tool": "wallet-default-quote"},
        headers={"X-API-Key": bound_key["api_key"]},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "wallet_access_denied"


@pytest.mark.anyio
async def test_unbound_admin_must_name_wallet_for_permit_verification(
    client: AsyncClient, clean_database
):
    await _sponsor_wallet(client, "first-verify-wallet")
    await _sponsor_wallet(client, "second-verify-wallet")

    response = await client.post(
        "/v1/permits/verify",
        json={"permit_id": "permit-missing", "tool": "example"},
        headers=BOOTSTRAP_HEADERS,
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "missing_wallet_id"
    assert response.json()["code"] == "missing_wallet_id"
