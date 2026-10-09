"""Focused unit coverage for app/services/api_key_service.py.

Covers the service's main public behavior directly (no HTTP layer):
key generation helpers, create/validate, budgets and expiry, rotation,
revocation, liveness helpers, rotation logs, auto rotation, and emergency
revocation, including edge and error cases.
"""

import hashlib
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlmodel import col

from app.core.time import utc_now
from app.db.database import get_session_factory
from app.db.models import APIKeyModel, WalletModel
from app.schemas.billing import APIKeyStatus
from app.services import api_key_service as api_key_service_module
from app.services.api_key_service import (
    APIKeyService,
    InvalidRotationRequestError,
    KeyNotFoundError,
    WalletNotFoundError,
    generate_api_key,
    mask_key,
)


async def _seed_wallet(wallet_id: str = "wlt-service-test") -> str:
    factory = get_session_factory()
    async with factory() as session:
        session.add(WalletModel(wallet_id=wallet_id, wallet_type="agent"))
        await session.commit()
    return wallet_id


def test_generate_api_key_shape() -> None:
    full_key, key_hash, key_prefix = generate_api_key()
    assert full_key.startswith("b2a_")
    assert key_prefix == full_key[: api_key_service_module.API_KEY_PREFIX_LENGTH]
    assert len(key_prefix) == api_key_service_module.API_KEY_PREFIX_LENGTH
    assert key_hash == hashlib.sha256(full_key.encode()).hexdigest()


def test_generate_api_key_unique() -> None:
    first = generate_api_key()[0]
    second = generate_api_key()[0]
    assert first != second


def test_mask_key_long_and_short() -> None:
    assert mask_key("b2a_abcdefghijklmnop") == "b2a_ab...mnop"
    assert mask_key("short") == "*****"


@pytest.mark.anyio
async def test_create_and_validate_key(clean_database) -> None:
    wallet_id = await _seed_wallet()
    service = APIKeyService()

    created = await service.create_key(wallet_id, key_name="ops")
    assert created["wallet_id"] == wallet_id
    assert created["key_name"] == "ops"
    assert created["status"] == APIKeyStatus.ACTIVE.value

    key = await service.validate_key(created["api_key"])
    assert key is not None
    assert key.key_id == created["key_id"]


@pytest.mark.anyio
async def test_create_key_unknown_wallet_raises(clean_database) -> None:
    with pytest.raises(WalletNotFoundError):
        await APIKeyService().create_key("wlt-missing")


@pytest.mark.anyio
async def test_validate_rejects_blank_short_and_unknown(clean_database) -> None:
    service = APIKeyService()
    assert await service.validate_key("") is None
    assert await service.validate_key("b2a_x") is None
    assert await service.validate_key("b2a_neverissuedsecretvalue") is None


@pytest.mark.anyio
async def test_validate_rejects_expired_key(clean_database) -> None:
    wallet_id = await _seed_wallet()
    service = APIKeyService()
    created = await service.create_key(wallet_id, expires_in_days=1)

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(APIKeyModel).where(col(APIKeyModel.key_id) == created["key_id"])
        )
        stored = result.scalar_one()
        stored.expires_at = utc_now() - timedelta(days=1)
        session.add(stored)
        await session.commit()

    assert await service.validate_key(created["api_key"]) is None


@pytest.mark.anyio
async def test_max_uses_budget_enforced(clean_database) -> None:
    wallet_id = await _seed_wallet()
    service = APIKeyService()
    created = await service.create_key(wallet_id, max_uses=1)

    assert await service.validate_key(created["api_key"]) is not None
    assert await service.validate_key(created["api_key"]) is None


@pytest.mark.anyio
async def test_get_keys_masks_and_counts(clean_database) -> None:
    wallet_id = await _seed_wallet()
    service = APIKeyService()
    created = await service.create_key(wallet_id, key_name="ops")
    await service.revoke_key(wallet_id, created["key_id"])
    await service.create_key(wallet_id, key_name="second")

    overview = await service.get_keys(wallet_id)
    assert overview["wallet_id"] == wallet_id
    assert overview["total_active"] == 1
    assert overview["total_revoked"] == 1
    assert len(overview["keys"]) == 2
    assert all("api_key" not in entry for entry in overview["keys"])
    names = {entry["key_name"] for entry in overview["keys"]}
    assert names == {"ops", "second"}


@pytest.mark.anyio
async def test_get_keys_unknown_wallet_raises(clean_database) -> None:
    with pytest.raises(WalletNotFoundError):
        await APIKeyService().get_keys("wlt-missing")


@pytest.mark.anyio
async def test_rotate_key_with_revoke_old(clean_database) -> None:
    wallet_id = await _seed_wallet()
    service = APIKeyService()
    created = await service.create_key(wallet_id)

    rotation = await service.rotate_key(
        wallet_id, key_id=created["key_id"], revoke_old=True
    )
    assert rotation["old_key_id"] == created["key_id"]
    assert rotation["revoked_keys"] == [created["key_id"]]

    assert await service.validate_key(created["api_key"]) is None
    assert await service.validate_key(rotation["new_key"]["api_key"]) is not None


@pytest.mark.anyio
async def test_rotate_key_revoke_old_requires_key_id(clean_database) -> None:
    wallet_id = await _seed_wallet()
    with pytest.raises(InvalidRotationRequestError):
        await APIKeyService().rotate_key(wallet_id, revoke_old=True)


@pytest.mark.anyio
async def test_rotate_revoked_key_raises(clean_database) -> None:
    wallet_id = await _seed_wallet()
    service = APIKeyService()
    created = await service.create_key(wallet_id)
    await service.revoke_key(wallet_id, created["key_id"])

    with pytest.raises(InvalidRotationRequestError):
        await service.rotate_key(wallet_id, key_id=created["key_id"])


@pytest.mark.anyio
async def test_revoke_key_then_unknown_revoke_raises(clean_database) -> None:
    wallet_id = await _seed_wallet()
    service = APIKeyService()
    created = await service.create_key(wallet_id)

    assert await service.revoke_key(wallet_id, created["key_id"]) is True
    assert await service.validate_key(created["api_key"]) is None

    with pytest.raises(KeyNotFoundError):
        await service.revoke_key(wallet_id, "key-missing")


@pytest.mark.anyio
async def test_liveness_helpers(clean_database) -> None:
    wallet_id = await _seed_wallet()
    service = APIKeyService()
    assert await service.has_live_key(wallet_id) is False
    assert await service.is_key_live("key-missing") is False

    created = await service.create_key(wallet_id)
    assert await service.has_live_key(wallet_id) is True
    assert await service.is_key_live(created["key_id"]) is True

    await service.revoke_key(wallet_id, created["key_id"])
    assert await service.has_live_key(wallet_id) is False
    assert await service.is_key_live(created["key_id"]) is False


@pytest.mark.anyio
async def test_rotation_logs_record_rotation(clean_database) -> None:
    wallet_id = await _seed_wallet()
    service = APIKeyService()
    created = await service.create_key(wallet_id)
    await service.rotate_key(wallet_id, key_id=created["key_id"], revoke_old=True)

    logs = await service.get_rotation_logs(wallet_id)
    assert len(logs) == 1
    assert logs[0]["old_key_id"] == created["key_id"]


@pytest.mark.anyio
async def test_auto_rotate_needs_live_key(clean_database) -> None:
    wallet_id = await _seed_wallet()
    service = APIKeyService()
    with pytest.raises(InvalidRotationRequestError):
        await service.auto_rotate_on_suspicious_activity(wallet_id, "anomaly")

    created = await service.create_key(wallet_id)
    result = await service.auto_rotate_on_suspicious_activity(wallet_id, "anomaly")
    assert result["old_key_id"] == created["key_id"]
    assert await service.validate_key(created["api_key"]) is None


@pytest.mark.anyio
async def test_emergency_revocation_replaces_keys(clean_database) -> None:
    wallet_id = await _seed_wallet()
    service = APIKeyService()
    created = await service.create_key(wallet_id)

    result = await service.emergency_revocation(wallet_id, create_new_key=True)
    assert created["key_id"] in result["revoked_keys"]
    assert result["new_key"] is not None
    assert await service.validate_key(created["api_key"]) is None
    assert await service.validate_key(result["new_key"]["api_key"]) is not None
