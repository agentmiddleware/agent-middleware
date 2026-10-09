"""Pepper coverage for wallet API key hashes.

A server-side pepper (API_KEY_PEPPER) is mixed into the SHA-256 digest so a
database leak alone does not hand out directly attackable hashes. With no
pepper configured the legacy plain digest is kept, and keys issued before
the pepper was configured keep validating until rotation replaces them.
"""

import hashlib

import pytest

from app.core.config import get_settings
from app.db.database import get_session_factory
from app.db.models import APIKeyModel, WalletModel
from app.services import api_key_service as api_key_service_module
from app.services.api_key_service import APIKeyService, generate_api_key, hash_api_key


PEPPER = "test-pepper-value-for-unit-tests"


@pytest.fixture
def peppered_settings(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(get_settings(), "API_KEY_PEPPER", PEPPER)
    return PEPPER


def test_pepper_is_mixed_into_generated_hash(
    peppered_settings: str,
) -> None:
    full_key, key_hash, _ = generate_api_key()
    assert (
        key_hash == hashlib.sha256((peppered_settings + full_key).encode()).hexdigest()
    )
    assert key_hash != hashlib.sha256(full_key.encode()).hexdigest()


def test_empty_pepper_keeps_legacy_digest() -> None:
    assert (
        hash_api_key("b2a_example-key", pepper="")
        == hashlib.sha256(b"b2a_example-key").hexdigest()
    )


@pytest.mark.anyio
async def test_peppered_key_round_trip(clean_database, peppered_settings: str) -> None:
    factory = get_session_factory()
    async with factory() as session:
        session.add(WalletModel(wallet_id="agt-pepper-roundtrip", wallet_type="agent"))
        await session.commit()

    service = APIKeyService()
    created = await service.create_key(
        wallet_id="agt-pepper-roundtrip", key_name="peppered"
    )
    raw_key = created["api_key"]

    validated = await service.validate_key(raw_key)
    assert validated is not None
    assert (
        validated.key_hash
        == hashlib.sha256((peppered_settings + raw_key).encode()).hexdigest()
    )
    assert validated.key_hash != hashlib.sha256(raw_key.encode()).hexdigest()


@pytest.mark.anyio
async def test_legacy_key_still_validates_after_pepper_set(
    clean_database, peppered_settings: str
) -> None:
    del peppered_settings
    raw_key = "b2a_legacy-pre-pepper-secret"
    legacy_hash = hashlib.sha256(raw_key.encode()).hexdigest()

    factory = get_session_factory()
    async with factory() as session:
        session.add(WalletModel(wallet_id="agt-pepper-legacy", wallet_type="agent"))
        await session.commit()
        session.add(
            APIKeyModel(
                key_id="key-pepper-legacy",
                wallet_id="agt-pepper-legacy",
                key_hash=legacy_hash,
                key_prefix=raw_key[: api_key_service_module.API_KEY_PREFIX_LENGTH],
            )
        )
        await session.commit()

    service = APIKeyService()
    validated = await service.validate_key(raw_key)
    assert validated is not None
    assert validated.key_id == "key-pepper-legacy"
    assert await service.validate_key(f"{raw_key[:-1]}x") is None
