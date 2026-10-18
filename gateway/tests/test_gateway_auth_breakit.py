"""Adversarial break-it tests for gateway auth and API key routes/services.

Local test setups only. Covers revoked keys, wrong signing keys, missing
and malformed credentials, cross-wallet IDOR, garbage inputs, token type
confusion, and max_uses races.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient

from app.core.auth import get_auth_context
from app.core.config import get_settings
from app.core.jwt import JWT_AUDIENCE, JWT_ISSUER
from app.db.database import get_session_factory
from app.db.models import APIKeyModel
from app.main import app
from tests.test_trust_helpers import BOOTSTRAP_HEADERS, provision_agent_wallet
from sqlalchemy import select
from sqlmodel import col

pytestmark = pytest.mark.dormant

# 32 raw bytes, strict base64. Same non-secret test material CI uses.
TEST_SIGNING_KEY = "dGVzdC1zaWduaW5nLWtleS1tYXRlcmlhbC0zMmJ5dGU="


@pytest.fixture
def signing_key(monkeypatch):
    monkeypatch.setenv("TRUST_SIGNING_PRIVATE_KEY_B64", TEST_SIGNING_KEY)
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _decode_test_private_key() -> Ed25519PrivateKey:
    raw = base64.b64decode(TEST_SIGNING_KEY)
    return Ed25519PrivateKey.from_private_bytes(raw)


async def _expire_key_now(key_id: str) -> None:
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(APIKeyModel).where(col(APIKeyModel.key_id) == key_id)
        )
        key = result.scalar_one()
        key.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            seconds=10
        )
        session.add(key)
        await session.commit()


@pytest.mark.anyio
async def test_revoked_key_rejected_everywhere(client, clean_database, signing_key):
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    agent_headers = provisioned["agent_headers"]

    listed = await client.get(f"/v1/api-keys/{wallet_id}", headers=agent_headers)
    assert listed.status_code == 200
    key_id = listed.json()["keys"][0]["key_id"]

    revoke = await client.delete(
        f"/v1/api-keys/{wallet_id}/{key_id}",
        headers=BOOTSTRAP_HEADERS,
    )
    assert revoke.status_code == 204

    after = await client.get(f"/v1/api-keys/{wallet_id}", headers=agent_headers)
    assert after.status_code == 403

    mint = await client.post(
        "/v1/auth/token",
        json={"api_key": agent_headers["X-API-Key"]},
    )
    assert mint.status_code == 401


@pytest.mark.anyio
async def test_expired_key_rejected(client, clean_database, signing_key):
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    agent_headers = provisioned["agent_headers"]

    listed = await client.get(f"/v1/api-keys/{wallet_id}", headers=agent_headers)
    assert listed.status_code == 200
    await _expire_key_now(listed.json()["keys"][0]["key_id"])

    after = await client.get(f"/v1/api-keys/{wallet_id}", headers=agent_headers)
    assert after.status_code == 403

    mint = await client.post(
        "/v1/auth/token",
        json={"api_key": agent_headers["X-API-Key"]},
    )
    assert mint.status_code == 401


@pytest.mark.anyio
async def test_max_uses_budget_survives_parallel_use(client, clean_database):
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]

    created = await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id, "key_name": "capped", "max_uses": 5},
        headers=BOOTSTRAP_HEADERS,
    )
    assert created.status_code == 201
    capped = {"X-API-Key": created.json()["api_key"]}

    async def one_call(local_client):
        resp = await local_client.get(f"/v1/api-keys/{wallet_id}", headers=capped)
        return resp.status_code

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c2:
        results = await asyncio.gather(*[one_call(c2) for _ in range(20)])

    assert results.count(200) == 5
    assert results.count(403) == 15


@pytest.mark.anyio
async def test_jwt_signed_by_wrong_key_rejected(signing_key):
    other = Ed25519PrivateKey.generate()
    now = datetime.now(timezone.utc)
    token = pyjwt.encode(
        {
            "sub": "wallet_x",
            "key_id": "key_x",
            "scopes": [],
            "iat": now,
            "exp": now + timedelta(minutes=15),
            "iss": JWT_ISSUER,
            "aud": JWT_AUDIENCE,
            "jti": "jwt-wrong-key",
            "type": "access",
        },
        other,
        algorithm="EdDSA",
    )
    with pytest.raises(Exception):
        await get_auth_context(api_key=None, authorization=f"Bearer {token}")


@pytest.mark.anyio
async def test_tampered_jwt_rejected(client, clean_database, signing_key):
    provisioned = await provision_agent_wallet(client)
    mint = await client.post(
        "/v1/auth/token",
        json={"api_key": provisioned["agent_headers"]["X-API-Key"]},
    )
    assert mint.status_code == 200
    access = mint.json()["access_token"]
    tampered = access[:-2] + ("AA" if not access.endswith("AA") else "BB")

    resp = await client.get(
        "/v1/me/authority", headers={"Authorization": f"Bearer {tampered}"}
    )
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_missing_credentials_rejected(client, clean_database):
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]

    assert (await client.get(f"/v1/api-keys/{wallet_id}")).status_code == 401
    assert (
        await client.post(
            "/v1/api-keys",
            json={"wallet_id": wallet_id},
        )
    ).status_code in (401, 422)
    assert (
        await client.post("/v1/api-keys/rotate", json={"wallet_id": wallet_id})
    ).status_code in (401, 422)


@pytest.mark.anyio
async def test_malformed_bearer_never_falls_back_to_api_key(
    client, clean_database, signing_key
):
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    good_key = provisioned["agent_headers"]["X-API-Key"]

    bad_authorizations = [
        "Basic xyz",
        "Bearer",
        "Bearer ",
        "Bearer not-a-jwt",
        "Bearer  spaced  token",
        "bearer lower",
    ]
    for bad in bad_authorizations:
        resp = await client.get(
            f"/v1/api-keys/{wallet_id}",
            headers={"X-API-Key": good_key, "Authorization": bad},
        )
        assert resp.status_code == 401, bad

    resp = await client.get(
        f"/v1/api-keys/{wallet_id}",
        headers={
            "X-API-Key": good_key,
            "Authorization": f"Bearer {good_key}",
        },
    )
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_cross_wallet_idor_blocked(client, clean_database, signing_key):
    first = await provision_agent_wallet(client)
    second = await provision_agent_wallet(client)
    a_headers = first["agent_headers"]
    b_wallet = second["agent_wallet_id"]

    assert (
        await client.get(f"/v1/api-keys/{b_wallet}", headers=a_headers)
    ).status_code == 403

    assert (
        await client.post(
            "/v1/api-keys", json={"wallet_id": b_wallet}, headers=a_headers
        )
    ).status_code == 403

    assert (
        await client.post(
            "/v1/api-keys/rotate",
            json={"wallet_id": b_wallet},
            headers=a_headers,
        )
    ).status_code == 403

    assert (
        await client.get(f"/v1/api-keys/{b_wallet}/logs", headers=a_headers)
    ).status_code == 403

    before = await client.get(f"/v1/api-keys/{b_wallet}", headers=BOOTSTRAP_HEADERS)
    assert before.status_code == 200
    before_count = len(before.json()["keys"])
    victim_key_id = before.json()["keys"][0]["key_id"]

    assert (
        await client.delete(
            f"/v1/api-keys/{b_wallet}/{victim_key_id}", headers=a_headers
        )
    ).status_code == 403

    after = await client.get(f"/v1/api-keys/{b_wallet}", headers=BOOTSTRAP_HEADERS)
    assert len(after.json()["keys"]) == before_count

    assert (
        await client.post(
            "/v1/api-keys/emergency-revoke",
            json={"wallet_id": b_wallet, "create_new_key": False},
            headers=a_headers,
        )
    ).status_code == 403


@pytest.mark.anyio
async def test_garbage_api_keys_never_500(client, clean_database):
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]

    garbage = [
        "",
        "   ",
        "short",
        "x" * 7,
        "' OR '1'='1",
        "b2a_" + "A" * 10000,
        "\x00\x01\x02b2a_fake",
        "Bearer ",
        ".".join(["a", "b", "c"]),
    ]
    for bad in garbage:
        resp = await client.get(f"/v1/api-keys/{wallet_id}", headers={"X-API-Key": bad})
        assert resp.status_code in (401, 403), (bad, resp.status_code)

    # Non-ASCII material cannot travel in a real header (httpx refuses to
    # send it), so exercise it at the unit level: it must be refused, and
    # must never raise a 500-class error out of the auth layer.
    from fastapi import HTTPException as _HTTPException

    for bad in ("b2a_caf\xe9key", "clé secrète"):
        try:
            await get_auth_context(api_key=bad)
        except _HTTPException as exc:
            assert exc.status_code in (401, 403)
        else:
            raise AssertionError(bad)


@pytest.mark.anyio
async def test_access_refresh_type_swap_rejected(client, clean_database, signing_key):
    provisioned = await provision_agent_wallet(client)
    mint = await client.post(
        "/v1/auth/token",
        json={"api_key": provisioned["agent_headers"]["X-API-Key"]},
    )
    assert mint.status_code == 200
    tokens = mint.json()

    swap = await client.post(
        "/v1/auth/refresh", json={"refresh_token": tokens["access_token"]}
    )
    assert swap.status_code == 401

    as_bearer = await client.get(
        "/v1/me/authority",
        headers={"Authorization": f"Bearer {tokens['refresh_token']}"},
    )
    assert as_bearer.status_code == 401


@pytest.mark.anyio
async def test_expired_jwt_rejected(signing_key):
    private = _decode_test_private_key()
    now = datetime.now(timezone.utc)
    token = pyjwt.encode(
        {
            "sub": "wallet_x",
            "key_id": "key_x",
            "scopes": [],
            "iat": now - timedelta(hours=2),
            "exp": now - timedelta(hours=1),
            "iss": JWT_ISSUER,
            "aud": JWT_AUDIENCE,
            "jti": "jwt-expired",
            "type": "access",
        },
        private,
        algorithm="EdDSA",
    )
    with pytest.raises(Exception):
        await get_auth_context(api_key=None, authorization=f"Bearer {token}")


@pytest.mark.anyio
async def test_key_expiring_mid_validation_consumes_nothing(
    client, clean_database, monkeypatch
):
    """A use budget key that times out between the liveness read and the
    guarded increment must not authenticate and must not spend budget."""
    import app.services.api_key_service as key_module

    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]

    created = await key_module.get_api_key_service().create_key(
        wallet_id=wallet_id, key_name="boundary", max_uses=5
    )
    expiring_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
        minutes=30
    )
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(APIKeyModel).where(col(APIKeyModel.key_id) == created["key_id"])
        )
        row = result.scalar_one()
        row.expires_at = expiring_at
        session.add(row)
        await session.commit()

    before = expiring_at - timedelta(minutes=1)
    after = expiring_at + timedelta(minutes=1)
    ticks = [before, after]

    def scripted_clock():
        return ticks.pop(0) if len(ticks) > 1 else ticks[0]

    monkeypatch.setattr(key_module, "utc_now", scripted_clock)

    assert (
        await key_module.get_api_key_service().validate_key(created["api_key"]) is None
    )

    async with factory() as session:
        result = await session.execute(
            select(APIKeyModel).where(col(APIKeyModel.key_id) == created["key_id"])
        )
        assert result.scalar_one().use_count == 0


@pytest.mark.anyio
async def test_jwt_cannot_mint_api_keys(client, clean_database, signing_key):
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    mint = await client.post(
        "/v1/auth/token",
        json={"api_key": provisioned["agent_headers"]["X-API-Key"]},
    )
    assert mint.status_code == 200
    jwt_headers = {"Authorization": f"Bearer {mint.json()['access_token']}"}

    resp = await client.post(
        "/v1/api-keys", json={"wallet_id": wallet_id}, headers=jwt_headers
    )
    assert resp.status_code == 403
