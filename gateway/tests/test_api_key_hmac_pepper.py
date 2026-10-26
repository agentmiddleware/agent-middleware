"""HMAC-SHA256 peppered API-key digests; no unkeyed fallback."""

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


@pytest.fixture
def pepper(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv(svc.API_KEY_HASH_PEPPER_ENV, PEPPER)
    return PEPPER


@pytest.fixture
def no_pepper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(svc.API_KEY_HASH_PEPPER_ENV, raising=False)
    monkeypatch.setattr(svc, "_configured_pepper", lambda: None, raising=True)


@pytest.fixture
def production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "_dev_pepper_allowed", lambda: False)


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


def test_generate_api_key_uses_hmac_with_configured_pepper(pepper: str) -> None:
    full_key, key_hash, _ = svc.generate_api_key()
    assert key_hash == _hmac(pepper, full_key)


def test_local_without_pepper_uses_public_dev_pepper(no_pepper) -> None:
    full_key, key_hash, _ = svc.generate_api_key()
    assert key_hash == _hmac(svc.DEV_API_KEY_HASH_PEPPER.decode(), full_key)


def test_generate_api_key_fails_closed_in_production_without_pepper(
    no_pepper, production
) -> None:
    with pytest.raises(svc.APIKeyHashingUnavailable) as exc:
        svc.generate_api_key()
    assert PEPPER not in str(exc.value)


def test_production_never_uses_dev_pepper(no_pepper, production) -> None:
    assert svc._api_key_hash_pepper() is None


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
async def test_near_miss_rejected(clean_database, pepper: str) -> None:
    await _seed(_hmac(pepper, KEY))
    assert await APIKeyService().validate_key(KEY[:-1] + "x") is None


@pytest.mark.anyio
async def test_row_hashed_under_other_pepper_is_rejected(
    clean_database, pepper: str
) -> None:
    # Old rows (any digest not made with the current pepper) stop working:
    # there is no fallback lookup and nothing is rewritten.
    await _seed(_hmac("other-pepper", KEY))
    assert await APIKeyService().validate_key(KEY) is None
    assert await APIKeyService().validate_key(KEY, consume=False) is None
    assert await _stored_hash() == _hmac("other-pepper", KEY)


@pytest.mark.anyio
async def test_local_without_pepper_authenticates_dev_pepper_rows(
    clean_database, no_pepper
) -> None:
    await _seed(_hmac(svc.DEV_API_KEY_HASH_PEPPER.decode(), KEY))
    assert await APIKeyService().validate_key(KEY) is not None


@pytest.mark.anyio
async def test_production_without_pepper_rejects_all_keys(
    clean_database, no_pepper, production
) -> None:
    await _seed(_hmac(svc.DEV_API_KEY_HASH_PEPPER.decode(), KEY))
    assert await APIKeyService().validate_key(KEY) is None


@pytest.mark.anyio
async def test_auth_logs_never_contain_pepper_or_key(
    clean_database, pepper: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    await _seed(_hmac(pepper, KEY))
    assert await APIKeyService().validate_key(KEY) is not None
    assert await APIKeyService().validate_key(KEY[:-1] + "x") is None
    assert PEPPER not in caplog.text and KEY not in caplog.text
