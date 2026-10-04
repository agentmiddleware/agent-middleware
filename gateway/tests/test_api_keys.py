"""
Tests for API Key Rotation Service.
Validates key creation, rotation, revocation, and emergency procedures.
"""

import pytest
from datetime import datetime, timedelta, timezone
from httpx import AsyncClient, ASGITransport
from app.core.config import get_settings
from app.main import app
from app.db.database import get_session_factory
from app.db.models import APIKeyModel


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def api_headers():
    return {"X-API-Key": "test-key"}


@pytest.fixture
async def sponsor_wallet(client, api_headers):
    resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "API Key Test Corp",
            "email": "apikey@test.com",
            "initial_credits": 10000.0,
        },
        headers=api_headers,
    )
    assert resp.status_code == 201
    return resp.json()


@pytest.mark.anyio
async def test_create_api_key(client, api_headers, sponsor_wallet):
    """Test creating a new API key."""
    resp = await client.post(
        "/v1/api-keys",
        json={
            "wallet_id": sponsor_wallet["wallet_id"],
            "key_name": "test_key",
        },
        headers=api_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["wallet_id"] == sponsor_wallet["wallet_id"]
    assert data["api_key"].startswith("b2a_")
    assert len(data["api_key"]) > 20
    assert data["status"] == "active"


@pytest.mark.anyio
async def test_create_api_key_with_expiration(client, api_headers, sponsor_wallet):
    """Test creating an API key with expiration."""
    resp = await client.post(
        "/v1/api-keys",
        json={
            "wallet_id": sponsor_wallet["wallet_id"],
            "key_name": "temp_key",
            "expires_in_days": 30,
        },
        headers=api_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["expires_at"] is not None


@pytest.mark.anyio
async def test_create_api_key_wallet_not_found(client, api_headers):
    """Test creating a key for a non-existent wallet."""
    resp = await client.post(
        "/v1/api-keys",
        json={
            "wallet_id": "nonexistent-wallet",
            "key_name": "test_key",
        },
        headers=api_headers,
    )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_list_api_keys(client, api_headers, sponsor_wallet):
    """Test listing API keys for a wallet."""
    await client.post(
        "/v1/api-keys",
        json={"wallet_id": sponsor_wallet["wallet_id"]},
        headers=api_headers,
    )

    resp = await client.get(
        f"/v1/api-keys/{sponsor_wallet['wallet_id']}",
        headers=api_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["wallet_id"] == sponsor_wallet["wallet_id"]
    assert len(data["keys"]) == 1
    assert data["total_active"] == 1


@pytest.mark.anyio
async def test_rotate_api_key(client, api_headers, sponsor_wallet):
    """Test rotating an API key."""
    create_resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": sponsor_wallet["wallet_id"]},
        headers=api_headers,
    )
    old_key = create_resp.json()

    resp = await client.post(
        "/v1/api-keys/rotate",
        json={
            "wallet_id": sponsor_wallet["wallet_id"],
            "key_id": old_key["key_id"],
            "revoke_old": True,
            "reason": "test_rotation",
        },
        headers=api_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["old_key_id"] == old_key["key_id"]
    assert data["new_key"] is not None
    assert data["new_key"]["api_key"] != old_key["api_key"]
    assert data["rotation_type"] == "manual"
    assert data["revoked_keys"] == [old_key["key_id"]]

    old_key_resp = await client.get(
        f"/v1/billing/wallets/{sponsor_wallet['wallet_id']}",
        headers={"X-API-Key": old_key["api_key"]},
    )
    assert old_key_resp.status_code == 403

    new_key_resp = await client.get(
        f"/v1/billing/wallets/{sponsor_wallet['wallet_id']}",
        headers={"X-API-Key": data["new_key"]["api_key"]},
    )
    assert new_key_resp.status_code == 200


@pytest.mark.anyio
async def test_rotate_api_key_without_revoke(client, api_headers, sponsor_wallet):
    """Test rotating a key without revoking the old one."""
    create_resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": sponsor_wallet["wallet_id"]},
        headers=api_headers,
    )
    old_key = create_resp.json()

    resp = await client.post(
        "/v1/api-keys/rotate",
        json={
            "wallet_id": sponsor_wallet["wallet_id"],
            "key_id": old_key["key_id"],
            "revoke_old": False,
        },
        headers=api_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["revoked_keys"] == []
    assert data["new_key"] is not None

    list_resp = await client.get(
        f"/v1/api-keys/{sponsor_wallet['wallet_id']}",
        headers=api_headers,
    )
    assert list_resp.json()["total_active"] == 2

    old_key_resp = await client.get(
        f"/v1/billing/wallets/{sponsor_wallet['wallet_id']}",
        headers={"X-API-Key": old_key["api_key"]},
    )
    assert old_key_resp.status_code == 200

    new_key_resp = await client.get(
        f"/v1/billing/wallets/{sponsor_wallet['wallet_id']}",
        headers={"X-API-Key": data["new_key"]["api_key"]},
    )
    assert new_key_resp.status_code == 200


@pytest.mark.anyio
async def test_rotate_api_key_rejects_revoke_without_key_id(
    client, api_headers, sponsor_wallet
):
    create_resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": sponsor_wallet["wallet_id"]},
        headers=api_headers,
    )
    old_key = create_resp.json()

    resp = await client.post(
        "/v1/api-keys/rotate",
        json={
            "wallet_id": sponsor_wallet["wallet_id"],
            "revoke_old": True,
        },
        headers=api_headers,
    )
    assert resp.status_code == 422
    assert "key_id is required when revoke_old is true" in str(resp.json()["detail"])

    old_key_resp = await client.get(
        f"/v1/billing/wallets/{sponsor_wallet['wallet_id']}",
        headers={"X-API-Key": old_key["api_key"]},
    )
    assert old_key_resp.status_code == 200

    list_resp = await client.get(
        f"/v1/api-keys/{sponsor_wallet['wallet_id']}",
        headers=api_headers,
    )
    assert list_resp.json()["total_active"] == 1


@pytest.mark.anyio
async def test_rotate_api_key_without_key_id_creates_only(
    client, api_headers, sponsor_wallet
):
    create_resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": sponsor_wallet["wallet_id"]},
        headers=api_headers,
    )
    old_key = create_resp.json()

    resp = await client.post(
        "/v1/api-keys/rotate",
        json={
            "wallet_id": sponsor_wallet["wallet_id"],
            "revoke_old": False,
        },
        headers=api_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["old_key_id"] is None
    assert data["revoked_keys"] == []
    assert data["new_key"] is not None

    old_key_resp = await client.get(
        f"/v1/billing/wallets/{sponsor_wallet['wallet_id']}",
        headers={"X-API-Key": old_key["api_key"]},
    )
    assert old_key_resp.status_code == 200

    new_key_resp = await client.get(
        f"/v1/billing/wallets/{sponsor_wallet['wallet_id']}",
        headers={"X-API-Key": data["new_key"]["api_key"]},
    )
    assert new_key_resp.status_code == 200

    list_resp = await client.get(
        f"/v1/api-keys/{sponsor_wallet['wallet_id']}",
        headers=api_headers,
    )
    assert list_resp.json()["total_active"] == 2


@pytest.mark.anyio
async def test_revoke_api_key(client, api_headers, sponsor_wallet):
    """Test revoking an API key."""
    create_resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": sponsor_wallet["wallet_id"]},
        headers=api_headers,
    )
    key = create_resp.json()

    resp = await client.delete(
        f"/v1/api-keys/{sponsor_wallet['wallet_id']}/{key['key_id']}",
        params={"reason": "test_revocation"},
        headers=api_headers,
    )
    assert resp.status_code == 204

    list_resp = await client.get(
        f"/v1/api-keys/{sponsor_wallet['wallet_id']}",
        headers=api_headers,
    )
    assert list_resp.json()["total_active"] == 0
    assert list_resp.json()["total_revoked"] == 1


@pytest.mark.anyio
async def test_revoke_api_key_not_found(client, api_headers, sponsor_wallet):
    """Test revoking a non-existent API key."""
    resp = await client.delete(
        f"/v1/api-keys/{sponsor_wallet['wallet_id']}/nonexistent-key",
        headers=api_headers,
    )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_emergency_revoke(client, api_headers, sponsor_wallet):
    """Test emergency key revocation."""
    await client.post(
        "/v1/api-keys",
        json={"wallet_id": sponsor_wallet["wallet_id"]},
        headers=api_headers,
    )
    await client.post(
        "/v1/api-keys",
        json={"wallet_id": sponsor_wallet["wallet_id"]},
        headers=api_headers,
    )

    resp = await client.post(
        "/v1/api-keys/emergency-revoke",
        json={
            "wallet_id": sponsor_wallet["wallet_id"],
            "reason": "security_incident",
            "create_new_key": True,
        },
        headers=api_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["revoked_keys"]) == 2
    assert data["new_key"] is not None
    assert data["new_key"]["api_key"].startswith("b2a_")


@pytest.mark.anyio
async def test_get_rotation_logs(client, api_headers, sponsor_wallet):
    """Test getting rotation audit logs."""
    create_resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": sponsor_wallet["wallet_id"]},
        headers=api_headers,
    )
    key = create_resp.json()

    await client.post(
        "/v1/api-keys/rotate",
        json={
            "wallet_id": sponsor_wallet["wallet_id"],
            "key_id": key["key_id"],
            "revoke_old": True,
            "reason": "test_rotation",
        },
        headers=api_headers,
    )

    resp = await client.get(
        f"/v1/api-keys/{sponsor_wallet['wallet_id']}/logs",
        headers=api_headers,
    )
    assert resp.status_code == 200
    logs = resp.json()
    assert len(logs) >= 1
    assert logs[0]["wallet_id"] == sponsor_wallet["wallet_id"]


@pytest.mark.anyio
async def test_api_key_response_includes_warning(client, api_headers, sponsor_wallet):
    """Test that key creation response includes security warning."""
    resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": sponsor_wallet["wallet_id"]},
        headers=api_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert "warning" in data
    assert "Store this key securely" in data["warning"]


@pytest.mark.anyio
async def test_db_created_key_authenticates_and_is_wallet_scoped(
    client, api_headers, sponsor_wallet
):
    """DB-created keys authenticate but only for their issuing wallet."""
    other_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Other Corp",
            "email": "other@test.com",
            "initial_credits": 1000.0,
        },
        headers=api_headers,
    )
    other_wallet = other_resp.json()

    key_resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": sponsor_wallet["wallet_id"]},
        headers=api_headers,
    )
    db_key = key_resp.json()["api_key"]
    db_headers = {"X-API-Key": db_key}

    own_resp = await client.get(
        f"/v1/billing/wallets/{sponsor_wallet['wallet_id']}",
        headers=db_headers,
    )
    assert own_resp.status_code == 200

    other_read = await client.get(
        f"/v1/billing/wallets/{other_wallet['wallet_id']}",
        headers=db_headers,
    )
    assert other_read.status_code == 403

    other_keys = await client.get(
        f"/v1/api-keys/{other_wallet['wallet_id']}",
        headers=db_headers,
    )
    assert other_keys.status_code == 403

    own_key_create = await client.post(
        "/v1/api-keys",
        json={"wallet_id": sponsor_wallet["wallet_id"], "key_name": "self_managed"},
        headers=db_headers,
    )
    assert own_key_create.status_code == 201


@pytest.mark.anyio
async def test_revoked_db_key_cannot_authenticate(client, api_headers, sponsor_wallet):
    key_resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": sponsor_wallet["wallet_id"]},
        headers=api_headers,
    )
    key = key_resp.json()

    revoke_resp = await client.delete(
        f"/v1/api-keys/{sponsor_wallet['wallet_id']}/{key['key_id']}",
        headers=api_headers,
    )
    assert revoke_resp.status_code == 204

    resp = await client.get(
        "/v1/billing/pricing",
        headers={"X-API-Key": key["api_key"]},
    )
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_expired_db_key_cannot_authenticate(client, api_headers, sponsor_wallet):
    key_resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": sponsor_wallet["wallet_id"]},
        headers=api_headers,
    )
    key = key_resp.json()

    factory = get_session_factory()
    async with factory() as session:
        db_key = await session.get(APIKeyModel, key["key_id"])
        db_key.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        await session.commit()

    resp = await client.get(
        "/v1/billing/pricing",
        headers={"X-API-Key": key["api_key"]},
    )
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_create_api_key_with_max_uses(client, api_headers, sponsor_wallet):
    """max_uses is persisted and echoed back, not silently dropped."""
    resp = await client.post(
        "/v1/api-keys",
        json={
            "wallet_id": sponsor_wallet["wallet_id"],
            "key_name": "limited_key",
            "max_uses": 3,
        },
        headers=api_headers,
    )
    assert resp.status_code == 201
    assert resp.json()["max_uses"] == 3

    list_resp = await client.get(
        f"/v1/api-keys/{sponsor_wallet['wallet_id']}",
        headers=api_headers,
    )
    assert list_resp.status_code == 200
    (listed,) = list_resp.json()["keys"]
    assert listed["max_uses"] == 3
    assert listed["use_count"] == 0


@pytest.mark.anyio
async def test_max_uses_exhausts_key(client, api_headers, sponsor_wallet):
    """A key minted with max_uses stops authenticating after that many uses."""
    key_resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": sponsor_wallet["wallet_id"], "max_uses": 2},
        headers=api_headers,
    )
    key = key_resp.json()

    for _ in range(2):
        resp = await client.get(
            "/v1/billing/pricing",
            headers={"X-API-Key": key["api_key"]},
        )
        assert resp.status_code == 200

    resp = await client.get(
        "/v1/billing/pricing",
        headers={"X-API-Key": key["api_key"]},
    )
    assert resp.status_code == 403

    list_resp = await client.get(
        f"/v1/api-keys/{sponsor_wallet['wallet_id']}",
        headers=api_headers,
    )
    (listed,) = list_resp.json()["keys"]
    assert listed["use_count"] == 2
    assert listed["max_uses"] == 2


@pytest.mark.anyio
async def test_key_without_max_uses_stays_unlimited(
    client, api_headers, sponsor_wallet
):
    """Omitting max_uses keeps the historical unlimited behavior."""
    key_resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": sponsor_wallet["wallet_id"]},
        headers=api_headers,
    )
    key = key_resp.json()
    assert key["max_uses"] is None

    for _ in range(5):
        resp = await client.get(
            "/v1/billing/pricing",
            headers={"X-API-Key": key["api_key"]},
        )
        assert resp.status_code == 200


@pytest.mark.anyio
async def test_exhausted_key_is_not_live(client, api_headers, sponsor_wallet):
    """Exhaustion kills key liveness, so JWTs minted from the key die with it."""
    from app.services.api_key_service import get_api_key_service

    key_resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": sponsor_wallet["wallet_id"], "max_uses": 1},
        headers=api_headers,
    )
    key = key_resp.json()

    service = get_api_key_service()
    assert await service.is_key_live(key["key_id"]) is True

    resp = await client.get(
        "/v1/billing/pricing",
        headers={"X-API-Key": key["api_key"]},
    )
    assert resp.status_code == 200

    assert await service.is_key_live(key["key_id"]) is False


@pytest.mark.anyio
async def test_rotated_key_inherits_remaining_budget(
    client, api_headers, sponsor_wallet
):
    """Rotation must not widen authority: the new key gets the remaining uses."""
    key_resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": sponsor_wallet["wallet_id"], "max_uses": 5},
        headers=api_headers,
    )
    key = key_resp.json()

    for _ in range(2):
        resp = await client.get(
            "/v1/billing/pricing",
            headers={"X-API-Key": key["api_key"]},
        )
        assert resp.status_code == 200

    rotate_resp = await client.post(
        "/v1/api-keys/rotate",
        json={
            "wallet_id": sponsor_wallet["wallet_id"],
            "key_id": key["key_id"],
            "revoke_old": True,
        },
        headers=api_headers,
    )
    assert rotate_resp.status_code == 200
    new_key = rotate_resp.json()["new_key"]
    assert new_key["max_uses"] == 3

    for _ in range(3):
        resp = await client.get(
            "/v1/billing/pricing",
            headers={"X-API-Key": new_key["api_key"]},
        )
        assert resp.status_code == 200

    resp = await client.get(
        "/v1/billing/pricing",
        headers={"X-API-Key": new_key["api_key"]},
    )
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_rotated_key_inherits_expiry_and_name(
    client, api_headers, sponsor_wallet
):
    """Rotation keeps the original expiry window and the key's name."""
    key_resp = await client.post(
        "/v1/api-keys",
        json={
            "wallet_id": sponsor_wallet["wallet_id"],
            "key_name": "partner-runtime",
            "expires_in_days": 30,
        },
        headers=api_headers,
    )
    key = key_resp.json()

    rotate_resp = await client.post(
        "/v1/api-keys/rotate",
        json={
            "wallet_id": sponsor_wallet["wallet_id"],
            "key_id": key["key_id"],
            "revoke_old": True,
        },
        headers=api_headers,
    )
    new_key = rotate_resp.json()["new_key"]
    assert new_key["key_name"] == "partner-runtime"
    assert new_key["expires_at"] is not None

    old_expiry = datetime.fromisoformat(key["expires_at"]).replace(tzinfo=None)
    new_expiry = datetime.fromisoformat(new_key["expires_at"]).replace(tzinfo=None)
    assert abs((new_expiry - old_expiry).total_seconds()) < 5


@pytest.mark.anyio
async def test_rotate_without_key_id_stays_unbounded(
    client, api_headers, sponsor_wallet
):
    """Creating an extra key via rotate (no key_id) keeps historical behavior."""
    rotate_resp = await client.post(
        "/v1/api-keys/rotate",
        json={"wallet_id": sponsor_wallet["wallet_id"]},
        headers=api_headers,
    )
    assert rotate_resp.status_code == 200
    new_key = rotate_resp.json()["new_key"]
    assert new_key["max_uses"] is None
    assert new_key["expires_at"] is None


@pytest.mark.anyio
async def test_rotating_exhausted_key_yields_dead_key(
    client, api_headers, sponsor_wallet
):
    """A spent budget cannot be rotated back to life."""
    key_resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": sponsor_wallet["wallet_id"], "max_uses": 1},
        headers=api_headers,
    )
    key = key_resp.json()

    resp = await client.get(
        "/v1/billing/pricing",
        headers={"X-API-Key": key["api_key"]},
    )
    assert resp.status_code == 200

    rotate_resp = await client.post(
        "/v1/api-keys/rotate",
        json={
            "wallet_id": sponsor_wallet["wallet_id"],
            "key_id": key["key_id"],
            "revoke_old": True,
        },
        headers=api_headers,
    )
    new_key = rotate_resp.json()["new_key"]
    assert new_key["max_uses"] == 0

    resp = await client.get(
        "/v1/billing/pricing",
        headers={"X-API-Key": new_key["api_key"]},
    )
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_emergency_key_inherits_loosest_live_bounds(
    client, api_headers, sponsor_wallet
):
    """Emergency replacement gets the widest authority the wallet already had."""
    wallet_id = sponsor_wallet["wallet_id"]
    await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id, "max_uses": 2},
        headers=api_headers,
    )
    big_resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id, "max_uses": 10},
        headers=api_headers,
    )
    resp = await client.get(
        "/v1/billing/pricing",
        headers={"X-API-Key": big_resp.json()["api_key"]},
    )
    assert resp.status_code == 200

    revoke_resp = await client.post(
        "/v1/api-keys/emergency-revoke",
        json={"wallet_id": wallet_id, "reason": "test_incident"},
        headers=api_headers,
    )
    assert revoke_resp.status_code == 200
    new_key = revoke_resp.json()["new_key"]
    assert new_key["max_uses"] == 9


@pytest.mark.anyio
async def test_emergency_key_unbounded_when_any_live_key_is(
    client, api_headers, sponsor_wallet
):
    """An unlimited live key means the replacement is not an escalation."""
    wallet_id = sponsor_wallet["wallet_id"]
    await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id, "max_uses": 2},
        headers=api_headers,
    )
    await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id},
        headers=api_headers,
    )

    revoke_resp = await client.post(
        "/v1/api-keys/emergency-revoke",
        json={"wallet_id": wallet_id, "reason": "test_incident"},
        headers=api_headers,
    )
    new_key = revoke_resp.json()["new_key"]
    assert new_key["max_uses"] is None
    assert new_key["expires_at"] is None


@pytest.mark.anyio
async def test_emergency_revoke_with_own_last_use_stays_bounded(
    client, api_headers, sponsor_wallet
):
    """Spending a key's final use to call emergency-revoke must not launder it.

    Authenticating the emergency-revoke request itself consumes the sole
    key's last use, so the replacement must inherit a zero budget — not an
    unbounded one (the caller was never a bootstrap admin).
    """
    wallet_id = sponsor_wallet["wallet_id"]
    key_resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id, "max_uses": 1},
        headers=api_headers,
    )
    key = key_resp.json()

    revoke_resp = await client.post(
        "/v1/api-keys/emergency-revoke",
        json={"wallet_id": wallet_id, "reason": "self_service_attempt"},
        headers={"X-API-Key": key["api_key"]},
    )
    assert revoke_resp.status_code == 200
    new_key = revoke_resp.json()["new_key"]
    assert new_key["max_uses"] == 0

    resp = await client.get(
        "/v1/billing/pricing",
        headers={"X-API-Key": new_key["api_key"]},
    )
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_rotate_capped_key_requires_revoke_old(
    client, api_headers, sponsor_wallet
):
    """Non-revoking rotation of a budgeted key would double the budget — 422."""
    key_resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": sponsor_wallet["wallet_id"], "max_uses": 3},
        headers=api_headers,
    )
    key = key_resp.json()

    resp = await client.post(
        "/v1/api-keys/rotate",
        json={
            "wallet_id": sponsor_wallet["wallet_id"],
            "key_id": key["key_id"],
            "revoke_old": False,
        },
        headers=api_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_rotation_request"

    list_resp = await client.get(
        f"/v1/api-keys/{sponsor_wallet['wallet_id']}",
        headers=api_headers,
    )
    assert list_resp.json()["total_active"] == 1


@pytest.mark.anyio
async def test_emergency_key_inherits_latest_expiry(
    client, api_headers, sponsor_wallet
):
    """All active keys expire → the replacement uses the latest expiry."""
    wallet_id = sponsor_wallet["wallet_id"]
    await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id, "expires_in_days": 10},
        headers=api_headers,
    )
    long_resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id, "expires_in_days": 30},
        headers=api_headers,
    )

    revoke_resp = await client.post(
        "/v1/api-keys/emergency-revoke",
        json={"wallet_id": wallet_id, "reason": "test_incident"},
        headers=api_headers,
    )
    new_key = revoke_resp.json()["new_key"]
    assert new_key["expires_at"] is not None

    latest = datetime.fromisoformat(long_resp.json()["expires_at"]).replace(tzinfo=None)
    inherited = datetime.fromisoformat(new_key["expires_at"]).replace(tzinfo=None)
    assert abs((inherited - latest).total_seconds()) < 5


@pytest.mark.anyio
async def test_emergency_key_never_expires_if_any_active_key_does_not(
    client, api_headers, sponsor_wallet
):
    """One non-expiring active key → the replacement does not expire either."""
    wallet_id = sponsor_wallet["wallet_id"]
    await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id, "expires_in_days": 10},
        headers=api_headers,
    )
    await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id},
        headers=api_headers,
    )

    revoke_resp = await client.post(
        "/v1/api-keys/emergency-revoke",
        json={"wallet_id": wallet_id, "reason": "test_incident"},
        headers=api_headers,
    )
    assert revoke_resp.json()["new_key"]["expires_at"] is None


@pytest.mark.anyio
async def test_emergency_key_bounds_come_from_one_donor_credential(
    client, api_headers, sponsor_wallet
):
    """Bounds must not mix across keys into authority nothing possessed.

    A 1-use never-expiring key plus a 99-use short-lived key must not
    combine into a 99-use never-expiring replacement: both bounds come
    from the single donor with the largest remaining budget.
    """
    wallet_id = sponsor_wallet["wallet_id"]
    await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id, "max_uses": 1},
        headers=api_headers,
    )
    big_resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id, "max_uses": 99, "expires_in_days": 1},
        headers=api_headers,
    )

    revoke_resp = await client.post(
        "/v1/api-keys/emergency-revoke",
        json={"wallet_id": wallet_id, "reason": "test_incident"},
        headers=api_headers,
    )
    new_key = revoke_resp.json()["new_key"]
    assert new_key["max_uses"] == 99
    assert new_key["expires_at"] is not None

    donor_expiry = datetime.fromisoformat(big_resp.json()["expires_at"]).replace(
        tzinfo=None
    )
    inherited = datetime.fromisoformat(new_key["expires_at"]).replace(tzinfo=None)
    assert abs((inherited - donor_expiry).total_seconds()) < 5


@pytest.mark.anyio
async def test_rotate_revoked_key_is_rejected(client, api_headers, sponsor_wallet):
    """A revoked key must not be rotatable back into an active credential."""
    wallet_id = sponsor_wallet["wallet_id"]
    key_resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id},
        headers=api_headers,
    )
    key = key_resp.json()

    revoke_resp = await client.delete(
        f"/v1/api-keys/{wallet_id}/{key['key_id']}",
        headers=api_headers,
    )
    assert revoke_resp.status_code == 204

    resp = await client.post(
        "/v1/api-keys/rotate",
        json={
            "wallet_id": wallet_id,
            "key_id": key["key_id"],
            "revoke_old": True,
        },
        headers=api_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_rotation_request"

    list_resp = await client.get(
        f"/v1/api-keys/{wallet_id}",
        headers=api_headers,
    )
    assert list_resp.json()["total_active"] == 0


@pytest.mark.anyio
async def test_emergency_revoked_keys_cannot_be_rotated_back(
    client, api_headers, sponsor_wallet
):
    """Emergency revocation must not be undone by rotating a revoked key."""
    wallet_id = sponsor_wallet["wallet_id"]
    key_resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id},
        headers=api_headers,
    )
    key = key_resp.json()

    revoke_resp = await client.post(
        "/v1/api-keys/emergency-revoke",
        json={
            "wallet_id": wallet_id,
            "reason": "test_incident",
            "create_new_key": False,
        },
        headers=api_headers,
    )
    assert revoke_resp.status_code == 200
    assert key["key_id"] in revoke_resp.json()["revoked_keys"]

    resp = await client.post(
        "/v1/api-keys/rotate",
        json={
            "wallet_id": wallet_id,
            "key_id": key["key_id"],
            "revoke_old": True,
        },
        headers=api_headers,
    )
    assert resp.status_code == 422


async def _second_sponsor_wallet(client, api_headers) -> dict:
    resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "API Key Other Corp",
            "email": "apikey-other@test.com",
            "initial_credits": 1000.0,
        },
        headers=api_headers,
    )
    assert resp.status_code == 201
    return resp.json()


async def _key_ids(client, api_headers, wallet_id: str) -> list[str]:
    resp = await client.get(f"/v1/api-keys/{wallet_id}", headers=api_headers)
    assert resp.status_code == 200
    return sorted(k["key_id"] for k in resp.json()["keys"])


async def _expire_key(key_id: str) -> None:
    factory = get_session_factory()
    async with factory() as session:
        db_key = await session.get(APIKeyModel, key_id)
        db_key.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        await session.commit()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "bounds",
    [
        {"max_uses": 3},
        {"expires_in_days": 1},
        {"max_uses": 3, "expires_in_days": 1},
    ],
)
async def test_bounded_wallet_key_cannot_mint_key_via_create(
    client, api_headers, sponsor_wallet, bounds
):
    """A capped or expiring key must not launder itself into a fresh key.

    POST /v1/api-keys applies only the caller-supplied bounds, so a bounded
    wallet key could otherwise mint an unlimited, never-expiring sibling.
    The refusal holds even when the request asks for bounds of its own.
    """
    wallet_id = sponsor_wallet["wallet_id"]
    key_resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id, **bounds},
        headers=api_headers,
    )
    assert key_resp.status_code == 201
    bounded = key_resp.json()

    for body in (
        {"wallet_id": wallet_id, "key_name": "laundered"},
        {"wallet_id": wallet_id, "max_uses": 1, "expires_in_days": 1},
    ):
        resp = await client.post(
            "/v1/api-keys",
            json=body,
            headers={"X-API-Key": bounded["api_key"]},
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "bounded_key_cannot_mint"

    assert await _key_ids(client, api_headers, wallet_id) == [bounded["key_id"]]


@pytest.mark.anyio
async def test_bounded_wallet_key_cannot_mint_key_via_rotate_without_key_id(
    client, api_headers, sponsor_wallet
):
    """rotate with key_id=None is a create; a bounded caller is refused."""
    wallet_id = sponsor_wallet["wallet_id"]
    key_resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id, "max_uses": 3, "expires_in_days": 1},
        headers=api_headers,
    )
    bounded = key_resp.json()

    resp = await client.post(
        "/v1/api-keys/rotate",
        json={"wallet_id": wallet_id},
        headers={"X-API-Key": bounded["api_key"]},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "bounded_key_cannot_mint"

    assert await _key_ids(client, api_headers, wallet_id) == [bounded["key_id"]]
    logs = await client.get(f"/v1/api-keys/{wallet_id}/logs", headers=api_headers)
    assert logs.json() == []


@pytest.mark.anyio
async def test_bounded_wallet_key_cross_tenant_mint_is_still_wallet_denied(
    client, api_headers, sponsor_wallet
):
    """Wallet scoping is checked first: another tenant's wallet stays 403."""
    other_wallet = await _second_sponsor_wallet(client, api_headers)
    key_resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": sponsor_wallet["wallet_id"], "max_uses": 3},
        headers=api_headers,
    )
    bounded = key_resp.json()

    for path in ("/v1/api-keys", "/v1/api-keys/rotate"):
        resp = await client.post(
            path,
            json={"wallet_id": other_wallet["wallet_id"]},
            headers={"X-API-Key": bounded["api_key"]},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "wallet_access_denied"

    assert await _key_ids(client, api_headers, other_wallet["wallet_id"]) == []


@pytest.mark.anyio
async def test_bounded_wallet_key_rotating_itself_still_inherits_budget(
    client, api_headers, sponsor_wallet
):
    """Rotate WITH key_id keeps inheriting the remaining bounds."""
    wallet_id = sponsor_wallet["wallet_id"]
    key_resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id, "max_uses": 5, "expires_in_days": 1},
        headers=api_headers,
    )
    bounded = key_resp.json()

    resp = await client.post(
        "/v1/api-keys/rotate",
        json={
            "wallet_id": wallet_id,
            "key_id": bounded["key_id"],
            "revoke_old": True,
        },
        headers={"X-API-Key": bounded["api_key"]},
    )
    assert resp.status_code == 200, resp.text
    new_key = resp.json()["new_key"]
    # Authenticating the rotate request itself spent one of the five uses.
    assert new_key["max_uses"] == 4
    assert new_key["expires_at"] is not None

    # A bounded caller may rotate only its own key: any other key_id, real
    # or not, is refused before lookup, so it cannot probe for key ids.
    unknown = await client.post(
        "/v1/api-keys/rotate",
        json={"wallet_id": wallet_id, "key_id": "key_missing", "revoke_old": True},
        headers={"X-API-Key": new_key["api_key"]},
    )
    assert unknown.status_code == 403
    assert unknown.json()["detail"]["error"] == "bounded_key_cannot_mint"


@pytest.mark.anyio
@pytest.mark.parametrize("revoke_old", [False, True])
async def test_bounded_wallet_key_cannot_rotate_an_unbounded_sibling(
    client, api_headers, sponsor_wallet, revoke_old
):
    """Rotating another key inherits THAT key's bounds.

    A capped workload key naming an unbounded sibling's key_id would mint a
    key with no bounds at all, so a bounded caller may rotate only itself.
    """
    wallet_id = sponsor_wallet["wallet_id"]
    sibling = (
        await client.post(
            "/v1/api-keys",
            json={"wallet_id": wallet_id, "key_name": "management"},
            headers=api_headers,
        )
    ).json()
    bounded = (
        await client.post(
            "/v1/api-keys",
            json={"wallet_id": wallet_id, "max_uses": 3, "expires_in_days": 1},
            headers=api_headers,
        )
    ).json()

    resp = await client.post(
        "/v1/api-keys/rotate",
        json={
            "wallet_id": wallet_id,
            "key_id": sibling["key_id"],
            "revoke_old": revoke_old,
        },
        headers={"X-API-Key": bounded["api_key"]},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "bounded_key_cannot_mint"

    assert await _key_ids(client, api_headers, wallet_id) == sorted(
        [sibling["key_id"], bounded["key_id"]]
    )
    still_live = await client.get(
        f"/v1/api-keys/{wallet_id}", headers={"X-API-Key": sibling["api_key"]}
    )
    assert still_live.status_code == 200


@pytest.mark.anyio
async def test_unbounded_wallet_key_rotating_a_bounded_sibling_inherits_its_bounds(
    client, api_headers, sponsor_wallet
):
    """Control: an unbounded caller may rotate any key of its wallet."""
    wallet_id = sponsor_wallet["wallet_id"]
    manager = (
        await client.post(
            "/v1/api-keys",
            json={"wallet_id": wallet_id, "key_name": "management"},
            headers=api_headers,
        )
    ).json()
    workload = (
        await client.post(
            "/v1/api-keys",
            json={"wallet_id": wallet_id, "max_uses": 3, "expires_in_days": 1},
            headers=api_headers,
        )
    ).json()

    resp = await client.post(
        "/v1/api-keys/rotate",
        json={
            "wallet_id": wallet_id,
            "key_id": workload["key_id"],
            "revoke_old": True,
        },
        headers={"X-API-Key": manager["api_key"]},
    )
    assert resp.status_code == 200, resp.text
    new_key = resp.json()["new_key"]
    assert new_key["max_uses"] == 3
    assert new_key["expires_at"] is not None


@pytest.mark.anyio
async def test_bounded_caller_emergency_key_takes_only_its_own_bounds(
    client, api_headers, sponsor_wallet
):
    """Emergency revoke must not hand a capped caller a sibling's authority.

    The wallet-wide donor rule picks the loosest live key, so with an
    unbounded sibling present a capped caller used to get an unbounded
    emergency key. A wallet-scoped caller's replacement now inherits only its
    own remaining bounds.
    """
    wallet_id = sponsor_wallet["wallet_id"]
    sibling = (
        await client.post(
            "/v1/api-keys",
            json={"wallet_id": wallet_id, "key_name": "management"},
            headers=api_headers,
        )
    ).json()
    bounded = (
        await client.post(
            "/v1/api-keys",
            json={"wallet_id": wallet_id, "max_uses": 5, "expires_in_days": 1},
            headers=api_headers,
        )
    ).json()

    resp = await client.post(
        "/v1/api-keys/emergency-revoke",
        json={"wallet_id": wallet_id, "reason": "incident", "create_new_key": True},
        headers={"X-API-Key": bounded["api_key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert sorted(body["revoked_keys"]) == sorted(
        [sibling["key_id"], bounded["key_id"]]
    )
    new_key = body["new_key"]
    # Authenticating the emergency request spent one of the five uses.
    assert new_key["max_uses"] == 4
    assert new_key["expires_at"] is not None


@pytest.mark.anyio
async def test_admin_emergency_key_keeps_wallet_wide_donor_rule(
    client, api_headers, sponsor_wallet
):
    """Control: a bootstrap admin still gets the loosest live key's bounds."""
    wallet_id = sponsor_wallet["wallet_id"]
    await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id, "key_name": "management"},
        headers=api_headers,
    )
    await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id, "max_uses": 5, "expires_in_days": 1},
        headers=api_headers,
    )

    resp = await client.post(
        "/v1/api-keys/emergency-revoke",
        json={"wallet_id": wallet_id, "reason": "incident", "create_new_key": True},
        headers=api_headers,
    )
    assert resp.status_code == 200, resp.text
    new_key = resp.json()["new_key"]
    assert new_key["max_uses"] is None
    assert new_key["expires_at"] is None


@pytest.mark.anyio
async def test_unbounded_wallet_key_can_still_mint_keys(
    client, api_headers, sponsor_wallet
):
    """Control: an unbounded wallet key keeps self-service key management."""
    wallet_id = sponsor_wallet["wallet_id"]
    key_resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id},
        headers=api_headers,
    )
    unbounded = {"X-API-Key": key_resp.json()["api_key"]}

    created = await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id, "key_name": "self_managed"},
        headers=unbounded,
    )
    assert created.status_code == 201

    rotated = await client.post(
        "/v1/api-keys/rotate",
        json={"wallet_id": wallet_id},
        headers=unbounded,
    )
    assert rotated.status_code == 200
    assert rotated.json()["old_key_id"] is None


# 32 raw bytes, strict base64 — same non-secret test material CI uses.
TEST_SIGNING_KEY = "dGVzdC1zaWduaW5nLWtleS1tYXRlcmlhbC0zMmJ5dGU="


@pytest.fixture
def signing_key(monkeypatch):
    monkeypatch.setenv("TRUST_SIGNING_PRIVATE_KEY_B64", TEST_SIGNING_KEY)
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


@pytest.mark.anyio
async def test_jwt_from_bounded_key_cannot_mint_key(
    client, api_headers, sponsor_wallet, signing_key
):
    """A bearer token carries its originating key's bounds into the check."""
    wallet_id = sponsor_wallet["wallet_id"]
    key_resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id, "max_uses": 5},
        headers=api_headers,
    )
    bounded = key_resp.json()
    token_resp = await client.post(
        "/v1/auth/token", json={"api_key": bounded["api_key"]}
    )
    assert token_resp.status_code == 200, token_resp.text
    bearer = {"Authorization": f"Bearer {token_resp.json()['access_token']}"}

    for path in ("/v1/api-keys", "/v1/api-keys/rotate"):
        resp = await client.post(path, json={"wallet_id": wallet_id}, headers=bearer)
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "jwt_cannot_mint_api_key"

    assert await _key_ids(client, api_headers, wallet_id) == [bounded["key_id"]]


@pytest.mark.anyio
async def test_list_total_active_counts_only_live_keys(
    client, api_headers, sponsor_wallet
):
    """Expired or exhausted keys keep status=active but cannot authenticate."""
    wallet_id = sponsor_wallet["wallet_id"]
    live = await client.post(
        "/v1/api-keys", json={"wallet_id": wallet_id}, headers=api_headers
    )
    expired = await client.post(
        "/v1/api-keys", json={"wallet_id": wallet_id}, headers=api_headers
    )
    exhausted = await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id, "max_uses": 1},
        headers=api_headers,
    )
    revoked = await client.post(
        "/v1/api-keys", json={"wallet_id": wallet_id}, headers=api_headers
    )

    await _expire_key(expired.json()["key_id"])
    spend = await client.get(
        "/v1/billing/pricing", headers={"X-API-Key": exhausted.json()["api_key"]}
    )
    assert spend.status_code == 200
    revoke = await client.delete(
        f"/v1/api-keys/{wallet_id}/{revoked.json()['key_id']}", headers=api_headers
    )
    assert revoke.status_code == 204

    resp = await client.get(f"/v1/api-keys/{wallet_id}", headers=api_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["keys"]) == 4
    assert data["total_active"] == 1
    assert data["total_revoked"] == 1
    statuses = {k["key_id"]: k["status"] for k in data["keys"]}
    assert statuses[live.json()["key_id"]] == "active"


@pytest.mark.anyio
@pytest.mark.parametrize("dead_state", ["revoked", "expired"])
async def test_auto_rotate_without_live_key_refuses_instead_of_minting(
    client, api_headers, sponsor_wallet, dead_state
):
    """No live key to contain means nothing to rotate, not a fresh key."""
    from app.services.api_key_service import (
        InvalidRotationRequestError,
        get_api_key_service,
    )

    wallet_id = sponsor_wallet["wallet_id"]
    dead = await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id, "max_uses": 3},
        headers=api_headers,
    )
    dead_key_id = dead.json()["key_id"]
    if dead_state == "revoked":
        resp = await client.delete(
            f"/v1/api-keys/{wallet_id}/{dead_key_id}", headers=api_headers
        )
        assert resp.status_code == 204
    else:
        await _expire_key(dead_key_id)

    with pytest.raises(InvalidRotationRequestError):
        await get_api_key_service().auto_rotate_on_suspicious_activity(
            wallet_id=wallet_id, reason="test_suspicious"
        )

    assert await _key_ids(client, api_headers, wallet_id) == [dead_key_id]


@pytest.mark.anyio
async def test_auto_rotate_targets_live_key_not_expired_sibling(
    client, api_headers, sponsor_wallet
):
    """The suspect credential rotated is one that could still authenticate."""
    from app.services.api_key_service import get_api_key_service

    wallet_id = sponsor_wallet["wallet_id"]
    expired = await client.post(
        "/v1/api-keys", json={"wallet_id": wallet_id}, headers=api_headers
    )
    await _expire_key(expired.json()["key_id"])
    live = await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id, "max_uses": 4},
        headers=api_headers,
    )

    result = await get_api_key_service().auto_rotate_on_suspicious_activity(
        wallet_id=wallet_id, reason="test_suspicious"
    )
    assert result["old_key_id"] == live.json()["key_id"]
    assert result["revoked_keys"] == [live.json()["key_id"]]
    assert result["new_key"]["max_uses"] == 4


@pytest.mark.anyio
async def test_create_api_key_replays_on_idempotency_key(
    client, api_headers, sponsor_wallet
):
    """A retried create with the same Idempotency-Key must not mint a second key.

    The replay carries the first key's metadata but ``api_key: null``: keys
    are stored hashed by design, so the replay record never holds the secret.
    """
    payload = {"wallet_id": sponsor_wallet["wallet_id"], "key_name": "idem_key"}
    headers = {**api_headers, "Idempotency-Key": "create-key-idem-1"}
    first = await client.post("/v1/api-keys", json=payload, headers=headers)
    assert first.status_code == 201, first.text
    assert first.json()["api_key"]
    replay = await client.post("/v1/api-keys", json=payload, headers=headers)
    assert replay.status_code == 201, replay.text
    assert replay.json()["key_id"] == first.json()["key_id"]
    assert replay.json()["api_key"] is None

    listed = await client.get(
        f"/v1/api-keys/{sponsor_wallet['wallet_id']}", headers=api_headers
    )
    assert listed.status_code == 200
    assert [k["key_id"] for k in listed.json()["keys"]] == [first.json()["key_id"]]

    conflict = await client.post(
        "/v1/api-keys", json={**payload, "key_name": "other"}, headers=headers
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error"] == "idempotency_key_reused"


@pytest.mark.anyio
async def test_create_api_key_unknown_wallet_with_idempotency_key_is_404(
    client, api_headers
):
    headers = {**api_headers, "Idempotency-Key": "create-key-orphan-1"}
    payload = {"wallet_id": "nonexistent-wallet", "key_name": "k"}
    first = await client.post("/v1/api-keys", json=payload, headers=headers)
    assert first.status_code == 404, first.text
    assert first.json()["detail"]["error"] == "wallet_not_found"
    replay = await client.post("/v1/api-keys", json=payload, headers=headers)
    assert replay.status_code == 404
    assert replay.json() == first.json()


@pytest.mark.anyio
async def test_rotate_api_key_replays_on_idempotency_key(
    client, api_headers, sponsor_wallet
):
    """A retried rotate must not mint another key or 404 on the revoked key_id."""
    wallet_id = sponsor_wallet["wallet_id"]
    created = await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id, "key_name": "to_rotate"},
        headers=api_headers,
    )
    assert created.status_code == 201
    old_key_id = created.json()["key_id"]

    payload = {
        "wallet_id": wallet_id,
        "key_id": old_key_id,
        "revoke_old": True,
        "reason": "idem",
    }
    headers = {**api_headers, "Idempotency-Key": "rotate-key-idem-1"}
    first = await client.post("/v1/api-keys/rotate", json=payload, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["new_key"]["api_key"]
    replay = await client.post("/v1/api-keys/rotate", json=payload, headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["rotation_id"] == first.json()["rotation_id"]
    assert replay.json()["new_key"]["key_id"] == first.json()["new_key"]["key_id"]
    assert replay.json()["new_key"]["api_key"] is None

    listed = await client.get(f"/v1/api-keys/{wallet_id}", headers=api_headers)
    active = [k["key_id"] for k in listed.json()["keys"] if k["status"] == "active"]
    assert active == [first.json()["new_key"]["key_id"]]
