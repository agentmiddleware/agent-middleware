"""Salted, peppered scrypt API-key hashes with an indexed lookup id."""

import logging
import secrets

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.db.database import get_session_factory
from app.db.models import APIKeyModel, WalletModel
from app.services import api_key_service as svc
from app.services.api_key_service import APIKeyService

# Generated per test run; nothing is hardcoded and nothing is real data.
PEPPER = secrets.token_urlsafe(32)
KEY = "b2a_" + secrets.token_urlsafe(32)


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


async def _seed(key_hash: str, key: str = KEY, key_id: str = "key-scrypt-1") -> None:
    async with get_session_factory()() as session:
        session.add(WalletModel(wallet_id=f"agt-{key_id}", wallet_type="agent"))
        await session.commit()
        session.add(
            APIKeyModel(
                key_id=key_id,
                wallet_id=f"agt-{key_id}",
                key_hash=key_hash,
                key_lookup=svc.api_key_lookup_id(key),
                key_prefix=key[: svc.API_KEY_PREFIX_LENGTH],
            )
        )
        await session.commit()


async def _stored_hash(key_id: str = "key-scrypt-1") -> str:
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(APIKeyModel).where(APIKeyModel.key_id == key_id)
            )
        ).scalar_one()
        return row.key_hash


def test_hash_format_and_parameters(pepper: str) -> None:
    stored = svc.hash_api_key(KEY)
    scheme, n, r, p, salt, derived = stored.split("$")
    assert (scheme, int(n), int(r), int(p)) == ("scrypt", 2**14, 8, 1)
    assert salt and derived and len(stored) <= 255
    assert KEY not in stored and PEPPER not in stored


def test_hashes_are_salted_per_call(pepper: str) -> None:
    first, second = svc.hash_api_key(KEY), svc.hash_api_key(KEY)
    assert first != second
    assert svc._verify_hash(PEPPER.encode(), first, KEY)
    assert svc._verify_hash(PEPPER.encode(), second, KEY)


def test_verify_rejects_wrong_key_pepper_and_garbage(pepper: str) -> None:
    stored = svc.hash_api_key(KEY)
    assert not svc._verify_hash(PEPPER.encode(), stored, KEY + "x")
    assert not svc._verify_hash(b"other-pepper", stored, KEY)
    assert not svc._verify_hash(PEPPER.encode(), "not-a-hash", KEY)
    # A pre-scrypt hex digest never verifies.
    assert not svc._verify_hash(PEPPER.encode(), "a" * 64, KEY)


def test_generate_api_key_lookup_id_is_prefix_only(pepper: str) -> None:
    full_key, stored, prefix = svc.generate_api_key()
    lookup = svc.api_key_lookup_id(full_key)
    assert full_key.startswith(lookup) and len(lookup) == 16
    assert prefix == full_key[:8]
    assert svc._verify_hash(PEPPER.encode(), stored, full_key)


def test_local_without_pepper_uses_public_dev_pepper(no_pepper) -> None:
    full_key, stored, _ = svc.generate_api_key()
    assert svc._verify_hash(svc.DEV_API_KEY_HASH_PEPPER, stored, full_key)


def test_generate_api_key_fails_closed_in_production_without_pepper(
    no_pepper, production
) -> None:
    with pytest.raises(svc.APIKeyHashingUnavailable):
        svc.generate_api_key()


def test_production_never_uses_dev_pepper(no_pepper, production) -> None:
    assert svc._api_key_hash_pepper() is None


def test_startup_guard_refuses_production_without_pepper(no_pepper, production) -> None:
    with pytest.raises(svc.APIKeyHashingUnavailable):
        svc.validate_api_key_hashing_config()


def test_startup_guard_passes_with_pepper(pepper, production) -> None:
    svc.validate_api_key_hashing_config()


def test_pepper_read_from_settings_secret(monkeypatch: pytest.MonkeyPatch) -> None:
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
async def test_row_authenticates(clean_database, pepper: str) -> None:
    stored = svc.hash_api_key(KEY)
    await _seed(stored)
    key = await APIKeyService().validate_key(KEY)
    assert key is not None and key.key_id == "key-scrypt-1"
    assert await _stored_hash() == stored


@pytest.mark.anyio
async def test_near_miss_with_same_lookup_id_rejected(
    clean_database, pepper: str
) -> None:
    await _seed(svc.hash_api_key(KEY))
    assert await APIKeyService().validate_key(KEY) is not None
    # Same lookup id, cached verification of the real key must not help.
    assert await APIKeyService().validate_key(KEY[:-1] + "x") is None


@pytest.mark.anyio
async def test_row_hashed_under_other_pepper_is_rejected(
    clean_database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(svc.API_KEY_HASH_PEPPER_ENV, "other-pepper")
    await _seed(svc.hash_api_key(KEY))
    assert await APIKeyService().validate_key(KEY) is not None  # now cached
    monkeypatch.setenv(svc.API_KEY_HASH_PEPPER_ENV, PEPPER)
    assert await APIKeyService().validate_key(KEY) is None
    assert await APIKeyService().validate_key(KEY, consume=False) is None


@pytest.mark.anyio
async def test_old_rows_without_lookup_id_never_authenticate(
    clean_database, pepper: str
) -> None:
    async with get_session_factory()() as session:
        session.add(WalletModel(wallet_id="agt-old", wallet_type="agent"))
        await session.commit()
        session.add(
            APIKeyModel(
                key_id="key-old",
                wallet_id="agt-old",
                key_hash=svc.hash_api_key(KEY),
                key_lookup=None,
                key_prefix=KEY[: svc.API_KEY_PREFIX_LENGTH],
            )
        )
        await session.commit()
    assert await APIKeyService().validate_key(KEY) is None


@pytest.mark.anyio
async def test_cache_hit_still_honours_revocation(clean_database, pepper: str) -> None:
    await _seed(svc.hash_api_key(KEY))
    service = APIKeyService()
    assert await service.validate_key(KEY) is not None
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(APIKeyModel).where(APIKeyModel.key_id == "key-scrypt-1")
            )
        ).scalar_one()
        row.status = "revoked"
        await session.commit()
    assert await service.validate_key(KEY) is None


@pytest.mark.anyio
async def test_cache_skips_kdf_on_repeat(
    clean_database, pepper: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed(svc.hash_api_key(KEY))
    calls = []
    real = svc._verify_hash

    def counting(*args):
        calls.append(1)
        return real(*args)

    monkeypatch.setattr(svc, "_verify_hash", counting)
    service = APIKeyService()
    assert await service.validate_key(KEY) is not None
    assert await service.validate_key(KEY) is not None
    assert len(calls) == 1


def test_cache_is_bounded_and_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = svc._VerifiedKeyCache(max_entries=2, ttl_seconds=10)
    clock = [100.0]
    monkeypatch.setattr(svc.time, "monotonic", lambda: clock[0])
    for i in range(3):
        cache.remember(f"h{i}", b"p", f"k{i}")
    assert not cache.hit("h0", b"p", "k0")
    assert cache.hit("h2", b"p", "k2")
    assert not cache.hit("h2", b"q", "k2")
    assert not cache.hit("h2", b"p", "k1")
    clock[0] += 11
    assert not cache.hit("h2", b"p", "k2")


@pytest.mark.anyio
async def test_local_without_pepper_authenticates_dev_pepper_rows(
    clean_database, no_pepper
) -> None:
    await _seed(svc.hash_api_key(KEY))
    assert await APIKeyService().validate_key(KEY) is not None


@pytest.mark.anyio
async def test_production_without_pepper_rejects_all_keys(
    clean_database, no_pepper, production, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(svc, "_dev_pepper_allowed", lambda: True)
    await _seed(svc.hash_api_key(KEY))
    monkeypatch.setattr(svc, "_dev_pepper_allowed", lambda: False)
    assert await APIKeyService().validate_key(KEY) is None


@pytest.mark.anyio
async def test_auth_logs_never_contain_pepper_or_key(
    clean_database, pepper: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    await _seed(svc.hash_api_key(KEY))
    assert await APIKeyService().validate_key(KEY) is not None
    assert await APIKeyService().validate_key(KEY[:-1] + "x") is None
    assert PEPPER not in caplog.text and KEY not in caplog.text


def test_agent_comms_owner_digest_is_peppered_scrypt(pepper: str, monkeypatch) -> None:
    from app.services import agent_comms

    digest = agent_comms._digest(KEY)
    assert digest.startswith("scrypt:") and KEY not in digest
    assert digest == agent_comms._digest(KEY)  # deterministic for equality
    monkeypatch.setenv(svc.API_KEY_HASH_PEPPER_ENV, "other-pepper")
    assert agent_comms._digest(KEY) != digest


def test_agent_comms_digest_fails_closed_without_pepper(no_pepper, production) -> None:
    from app.services import agent_comms

    with pytest.raises(svc.APIKeyHashingUnavailable):
        agent_comms._digest(KEY)


def test_agent_comms_retired_sha256_owner_matches_nobody(pepper: str) -> None:
    from types import SimpleNamespace

    from app.services import agent_comms

    agent = SimpleNamespace(owner_key="sha256:" + "0" * 64)
    auth = SimpleNamespace(wallet_id=None, source="api_key", raw_key=KEY)
    assert not agent_comms.caller_owns_agent(agent, auth)
