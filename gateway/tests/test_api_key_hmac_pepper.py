"""HMAC-SHA256 peppered API-key digests with legacy SHA-256 fallback."""

import hashlib
import hmac

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.db.database import get_session_factory
from app.db.models import APIKeyModel, WalletModel
from app.services import api_key_service as svc
from app.services.api_key_service import APIKeyService

PEPPER = "test-pepper-not-a-real-secret"
KEY = "b2a_hmac-pepper-regression-key-0001"


def _hmac(pepper: str, key: str) -> str:
    return hmac.new(pepper.encode(), key.encode(), hashlib.sha256).hexdigest()


def _legacy(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


@pytest.fixture
def pepper(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv(svc.API_KEY_HASH_PEPPER_ENV, PEPPER)
    return PEPPER


@pytest.fixture
def no_pepper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(svc.API_KEY_HASH_PEPPER_ENV, raising=False)
    monkeypatch.setattr(svc, "_api_key_hash_pepper", lambda: None, raising=True)


@pytest.fixture
def production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "_legacy_api_key_hashing_allowed", lambda: False)


async def _seed(key_hash: str, key: str = KEY, key_id: str = "key-hmac-1") -> None:
    async with get_session_factory()() as session:
        session.add(WalletModel(wallet_id=f"agt-{key_id}", wallet_type="agent"))
        await session.commit()
        session.add(
            APIKeyModel(
                key_id=key_id,
                wallet_id=f"agt-{key_id}",
                key_hash=key_hash,
                key_prefix=key[: svc.API_KEY_PREFIX_LENGTH],
            )
        )
        await session.commit()


async def _stored_hash(key_id: str = "key-hmac-1") -> str:
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(APIKeyModel).where(APIKeyModel.key_id == key_id)
            )
        ).scalar_one()
        return row.key_hash


def test_generate_api_key_uses_hmac_when_pepper_set(pepper: str) -> None:
    full_key, key_hash, _ = svc.generate_api_key()
    assert key_hash == _hmac(pepper, full_key)
    assert key_hash != _legacy(full_key)


def test_generate_api_key_legacy_only_in_local_without_pepper(no_pepper) -> None:
    full_key, key_hash, _ = svc.generate_api_key()
    assert key_hash == _legacy(full_key)


def test_generate_api_key_fails_closed_in_production_without_pepper(
    no_pepper, production
) -> None:
    with pytest.raises(svc.APIKeyHashingUnavailable) as exc:
        svc.generate_api_key()
    assert PEPPER not in str(exc.value)


def test_startup_guard_refuses_production_without_pepper(no_pepper, production) -> None:
    with pytest.raises(svc.APIKeyHashingUnavailable):
        svc.validate_api_key_hashing_config()


def test_startup_guard_passes_with_pepper(pepper, production) -> None:
    svc.validate_api_key_hashing_config()


def test_pepper_read_from_settings_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(svc.API_KEY_HASH_PEPPER_ENV, raising=False)
    get_settings.cache_clear()
    try:
        monkeypatch.setenv(svc.API_KEY_HASH_PEPPER_ENV, PEPPER)
        settings = get_settings()
        assert settings.API_KEY_HASH_PEPPER.get_secret_value() == PEPPER
        assert PEPPER not in repr(settings)
    finally:
        monkeypatch.delenv(svc.API_KEY_HASH_PEPPER_ENV, raising=False)
        get_settings.cache_clear()


@pytest.mark.anyio
async def test_hmac_row_authenticates(clean_database, pepper: str) -> None:
    await _seed(_hmac(pepper, KEY))
    key = await APIKeyService().validate_key(KEY)
    assert key is not None and key.key_id == "key-hmac-1"
    assert await _stored_hash() == _hmac(pepper, KEY)


@pytest.mark.anyio
async def test_legacy_row_authenticates_and_is_rehashed(
    clean_database, pepper: str, caplog: pytest.LogCaptureFixture
) -> None:
    await _seed(_legacy(KEY))
    caplog.set_level("DEBUG")
    key = await APIKeyService().validate_key(KEY)
    assert key is not None
    assert await _stored_hash() == _hmac(pepper, KEY)
    # Second auth goes through the HMAC path.
    assert await APIKeyService().validate_key(KEY) is not None
    assert PEPPER not in caplog.text and KEY not in caplog.text


@pytest.mark.anyio
async def test_legacy_row_not_rehashed_when_validation_fails(
    clean_database, pepper: str
) -> None:
    await _seed(_legacy(KEY))
    near_miss = KEY[:-1] + "x"
    assert await APIKeyService().validate_key(near_miss) is None
    assert await _stored_hash() == _legacy(KEY)


@pytest.mark.anyio
async def test_preflight_does_not_rehash(clean_database, pepper: str) -> None:
    await _seed(_legacy(KEY))
    assert await APIKeyService().validate_key(KEY, consume=False) is not None
    assert await _stored_hash() == _legacy(KEY)


@pytest.mark.anyio
async def test_hmac_row_rejected_with_wrong_pepper(
    clean_database, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed(_hmac("other-pepper", KEY))
    monkeypatch.setenv(svc.API_KEY_HASH_PEPPER_ENV, PEPPER)
    assert await APIKeyService().validate_key(KEY) is None


@pytest.mark.anyio
async def test_legacy_row_works_locally_without_pepper(
    clean_database, no_pepper
) -> None:
    await _seed(_legacy(KEY))
    assert await APIKeyService().validate_key(KEY) is not None
    assert await _stored_hash() == _legacy(KEY)


@pytest.mark.anyio
async def test_production_without_pepper_rejects_all_keys(
    clean_database, no_pepper, production
) -> None:
    await _seed(_legacy(KEY))
    assert await APIKeyService().validate_key(KEY) is None


@pytest.mark.anyio
async def test_rehash_failure_keeps_legacy_row_working(
    clean_database, pepper: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed(_legacy(KEY))

    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    real = APIKeyService._rehash_legacy_key

    async def failing(session, key, *, legacy_hash, peppered_hash):
        monkeypatch.setattr(session, "execute", boom)
        await real(session, key, legacy_hash=legacy_hash, peppered_hash=peppered_hash)

    monkeypatch.setattr(APIKeyService, "_rehash_legacy_key", staticmethod(failing))
    assert await APIKeyService().validate_key(KEY) is not None
    assert await _stored_hash() == _legacy(KEY)
