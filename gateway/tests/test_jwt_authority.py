"""Synthetic, offline authority attenuation regressions against the pinned tree."""

import base64

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from app.core.auth import AuthContext, get_auth_context
from app.core.config import get_settings
from app.core.jwt import get_jwt_service
from app.core.scopes import require_scope
from app.db.database import get_session_factory
from app.db.models import WalletModel, APIKeyModel
from app.routers import auth, api_keys
from app.services.api_key_service import get_api_key_service


@pytest.fixture
async def client(clean_database, monkeypatch):
    monkeypatch.setattr(
        get_settings(),
        "TRUST_SIGNING_PRIVATE_KEY_B64",
        base64.b64encode(bytes(range(32))).decode(),
    )
    app = FastAPI()
    app.include_router(auth.router)  # Explicit dormant-surface opt-in.
    app.include_router(api_keys.router)

    @app.get("/guarded")
    @require_scope("billing:charge")
    async def guarded(auth: AuthContext = Depends(get_auth_context)):
        return {"allowed": True}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def seed(wallet_id, max_uses=None):
    async with get_session_factory()() as session:
        session.add(WalletModel(wallet_id=wallet_id, wallet_type="agent"))
        await session.commit()
    return await get_api_key_service().create_key(wallet_id, max_uses=max_uses)


async def exchange(client, key, scopes):
    response = await client.post(
        "/v1/auth/token", json={"api_key": key["api_key"], "scopes": scopes}
    )
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize("scopes", [["billing:read"], [], ["custom:read"]])
async def test_refresh_preserves_requested_scopes(client, scopes):
    key = await seed("qa_refresh_scope")
    tokens = await exchange(client, key, scopes)
    before = await client.get(
        "/guarded", headers={"Authorization": "Bearer " + tokens["access_token"]}
    )
    assert before.status_code == 403
    refreshed = await client.post(
        "/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert refreshed.status_code == 200
    payload = get_jwt_service().verify_access_token(refreshed.json()["access_token"])
    assert payload.scopes == scopes
    again = await client.post(
        "/v1/auth/refresh", json={"refresh_token": refreshed.json()["refresh_token"]}
    )
    assert again.status_code == 200
    assert (
        get_jwt_service().verify_access_token(again.json()["access_token"]).scopes
        == scopes
    )


async def test_scoped_jwt_cannot_mint_unrestricted_api_key(client):
    key = await seed("qa_key_scope")
    tokens = await exchange(client, key, ["billing:read"])
    bearer = {"Authorization": "Bearer " + tokens["access_token"]}
    denied = await client.get("/guarded", headers=bearer)
    assert denied.status_code == 403
    created = await client.post(
        "/v1/api-keys",
        headers=bearer,
        json={"wallet_id": "qa_key_scope", "key_name": "synthetic-repro"},
    )
    if created.status_code == 201:
        escalated = await client.get(
            "/guarded", headers={"X-API-Key": created.json()["api_key"]}
        )
        assert escalated.status_code == 200
    assert created.status_code == 403


async def test_jwt_authentication_consumes_origin_key_budget(client):
    key = await seed("qa_jwt_budget", max_uses=2)
    tokens = await exchange(client, key, ["billing:charge"])
    bearer = {"Authorization": "Bearer " + tokens["access_token"]}
    statuses = [
        (await client.get("/guarded", headers=bearer)).status_code for _ in range(5)
    ]
    async with get_session_factory()() as session:
        row = await session.get(APIKeyModel, key["key_id"])
        use_count = row.use_count
    assert statuses == [200, 401, 401, 401, 401], {
        "statuses": statuses,
        "max_uses": 2,
        "use_count": use_count,
    }


async def test_raw_key_budget_control(client):
    key = await seed("qa_raw_budget_control", max_uses=2)
    statuses = [
        (
            await client.get("/guarded", headers={"X-API-Key": key["api_key"]})
        ).status_code
        for _ in range(3)
    ]
    assert statuses == [200, 200, 403]


async def test_revoked_jwt_control(client):
    key = await seed("qa_revoked_jwt_control")
    tokens = await exchange(client, key, ["billing:charge"])
    await get_api_key_service().revoke_key("qa_revoked_jwt_control", key["key_id"])
    response = await client.get(
        "/guarded", headers={"Authorization": "Bearer " + tokens["access_token"]}
    )
    assert response.status_code == 401


async def test_cross_wallet_key_creation_control(client):
    key = await seed("qa_tenant_control")
    await seed("qa_other_tenant_control")
    tokens = await exchange(client, key, ["billing:read"])
    response = await client.post(
        "/v1/api-keys",
        headers={"Authorization": "Bearer " + tokens["access_token"]},
        json={"wallet_id": "qa_other_tenant_control"},
    )
    assert response.status_code == 403


async def test_bearer_rate_bucket_ignores_unauthenticated_key_header(client):
    from app.core.rate_limiter import RateLimitMiddleware

    key = await seed("qa_rate_scope")
    tokens = await exchange(client, key, ["billing:charge"])
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, requests_per_minute=2)

    @app.get("/protected")
    async def protected(auth: AuthContext = Depends(get_auth_context)):
        return {"accepted": True}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        statuses = [
            (
                await c.get(
                    "/protected",
                    headers={
                        "Authorization": "Bearer " + tokens["access_token"],
                        "X-API-Key": f"not-authenticating-{i}",
                    },
                )
            ).status_code
            for i in range(6)
        ]
    assert statuses == [200, 200, 429, 429, 429, 429]


@pytest.mark.parametrize("kind", ["own_rotation", "sibling_rotation", "emergency"])
async def test_jwt_cannot_replace_keys_through_alternate_routes(client, kind):
    key = await seed("qa_alternate_mint")
    sibling = await get_api_key_service().create_key("qa_alternate_mint")
    tokens = await exchange(client, key, ["billing:read"])
    if kind == "emergency":
        path = "/v1/api-keys/emergency-revoke"
        body = {
            "wallet_id": "qa_alternate_mint",
            "reason": "synthetic",
            "create_new_key": True,
        }
    else:
        path = "/v1/api-keys/rotate"
        body = {
            "wallet_id": "qa_alternate_mint",
            "key_id": key["key_id"] if kind == "own_rotation" else sibling["key_id"],
            "revoke_old": True,
        }
    response = await client.post(
        path, json=body, headers={"Authorization": "Bearer " + tokens["access_token"]}
    )
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "jwt_cannot_mint_api_key"
    assert await get_api_key_service().is_key_live(key["key_id"])
    assert await get_api_key_service().is_key_live(sibling["key_id"])


async def test_concurrent_jwts_share_one_remaining_origin_use(client):
    import asyncio

    key = await seed("qa_concurrent_origin", max_uses=2)
    tokens = await exchange(client, key, ["billing:charge"])
    responses = await asyncio.gather(
        *[
            client.get(
                "/guarded",
                headers={"Authorization": "Bearer " + tokens["access_token"]},
            )
            for _ in range(8)
        ]
    )
    assert sorted(r.status_code for r in responses) == [200] + [401] * 7
    async with get_session_factory()() as session:
        row = await session.get(APIKeyModel, key["key_id"])
        assert row.use_count == 2


async def test_jwt_origin_wallet_binding_and_expiry(client):
    from datetime import timedelta
    from app.core.time import utc_now

    key = await seed("qa_bound_wallet")
    wrong = get_jwt_service().create_access_token(
        wallet_id="qa_other_wallet", key_id=key["key_id"], scopes=["billing:charge"]
    )
    assert (
        await client.get("/guarded", headers={"Authorization": "Bearer " + wrong})
    ).status_code == 401
    tokens = await exchange(client, key, ["billing:charge"])
    async with get_session_factory()() as session:
        row = await session.get(APIKeyModel, key["key_id"])
        row.expires_at = utc_now() - timedelta(seconds=1)
        await session.commit()
    assert (
        await client.get(
            "/guarded", headers={"Authorization": "Bearer " + tokens["access_token"]}
        )
    ).status_code == 401


async def test_refresh_missing_signed_scope_binding_fails_closed(client):
    import jwt
    from app.core.jwt import JWT_ALGORITHM

    key = await seed("qa_legacy_refresh")
    tokens = await exchange(client, key, ["billing:read"])
    payload = jwt.decode(tokens["refresh_token"], options={"verify_signature": False})
    del payload["scopes"]
    private_key, _ = get_jwt_service()._load_keys()
    legacy = jwt.encode(payload, private_key, algorithm=JWT_ALGORITHM)
    response = await client.post("/v1/auth/refresh", json={"refresh_token": legacy})
    assert response.status_code == 401
    assert response.json()["detail"]["error"] == "invalid_refresh_token"


@pytest.mark.parametrize("header", ["Authorization", "X-API-Key"])
async def test_rotated_jwts_share_the_origin_rate_limit(client, header):
    from app.core.rate_limiter import RateLimitMiddleware

    key = await seed("qa_rotated_rate")
    first = await exchange(client, key, ["billing:read"])
    second = await exchange(client, key, ["billing:read"])
    tokens = [first["access_token"], second["access_token"]]
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, requests_per_minute=2)

    @app.get("/protected")
    async def protected(auth: AuthContext = Depends(get_auth_context)):
        return {"accepted": True}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        statuses = []
        for index in range(4):
            token = tokens[index % 2]
            headers = {
                header: "Bearer " + token if header == "Authorization" else token
            }
            if header == "Authorization":
                headers["X-API-Key"] = (
                    "test-key"  # Ignored headers cannot activate the test bypass.
                )
            statuses.append((await http.get("/protected", headers=headers)).status_code)
    assert statuses == [200, 200, 429, 429]
