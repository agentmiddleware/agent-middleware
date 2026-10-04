"""Focused regression coverage for DB-backed API-key digest comparison."""

import hashlib

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.database import get_session_factory
from app.db.models import APIKeyModel, WalletModel
from app.main import app
from app.services import api_key_service as api_key_service_module
from app.services.api_key_service import APIKeyService


VALID_API_KEY = "b2a_same-prefix-authenticated-secret"
NEAR_MISS_API_KEY = f"{VALID_API_KEY[:-1]}x"


@pytest.mark.anyio
async def test_db_key_digest_comparison_is_constant_time_and_near_miss_is_rejected(
    clean_database, monkeypatch: pytest.MonkeyPatch
) -> None:
    valid_hash = hashlib.sha256(VALID_API_KEY.encode()).hexdigest()
    near_miss_hash = hashlib.sha256(NEAR_MISS_API_KEY.encode()).hexdigest()
    assert (
        VALID_API_KEY[: api_key_service_module.API_KEY_PREFIX_LENGTH]
        == NEAR_MISS_API_KEY[: api_key_service_module.API_KEY_PREFIX_LENGTH]
    )

    factory = get_session_factory()
    async with factory() as session:
        session.add(
            WalletModel(
                wallet_id="agt-constant-time-key",
                wallet_type="agent",
            )
        )
        await session.commit()
        session.add(
            APIKeyModel(
                key_id="key-constant-time",
                wallet_id="agt-constant-time-key",
                key_hash=valid_hash,
                key_prefix=VALID_API_KEY[
                    : api_key_service_module.API_KEY_PREFIX_LENGTH
                ],
            )
        )
        await session.commit()

    comparisons: list[tuple[str, str]] = []
    real_compare_digest = api_key_service_module.hmac.compare_digest

    def record_compare_digest(stored_digest: str, supplied_digest: str) -> bool:
        comparisons.append((stored_digest, supplied_digest))
        return real_compare_digest(stored_digest, supplied_digest)

    monkeypatch.setattr(
        api_key_service_module.hmac,
        "compare_digest",
        record_compare_digest,
    )

    service = APIKeyService()
    valid = await service.validate_key(VALID_API_KEY)
    near_miss = await service.validate_key(NEAR_MISS_API_KEY)

    assert valid is not None
    assert valid.key_id == "key-constant-time"
    assert near_miss is None
    # The lookup is by the full digest, so a same-prefix near miss selects no
    # stored row at all; the selected row is still re-checked in constant time.
    assert comparisons == [(valid_hash, valid_hash)]
    assert all(near_miss_hash not in comparison for comparison in comparisons)
    assert all(
        len(digest) == hashlib.sha256().digest_size * 2
        for comparison in comparisons
        for digest in comparison
    )


# key_prefix is "b2a_" plus only four random characters and is not unique, so
# two live keys sharing it are a birthday-paradox certainty at a few thousand
# keys — and a tenant that can mint keys can grind one out on purpose.
SHARED_PREFIX = "b2a_AAAA"
SAME_PREFIX_KEYS = {
    "agt-same-prefix-a": ("key-same-prefix-a", f"{SHARED_PREFIX}first-secret"),
    "agt-same-prefix-b": ("key-same-prefix-b", f"{SHARED_PREFIX}second-secret"),
}
UNISSUED_SAME_PREFIX_KEY = f"{SHARED_PREFIX}never-issued-secret"


async def _seed_same_prefix_keys() -> None:
    factory = get_session_factory()
    async with factory() as session:
        for wallet_id in SAME_PREFIX_KEYS:
            session.add(WalletModel(wallet_id=wallet_id, wallet_type="agent"))
        await session.commit()
        for wallet_id, (key_id, api_key) in SAME_PREFIX_KEYS.items():
            assert (
                api_key[: api_key_service_module.API_KEY_PREFIX_LENGTH] == SHARED_PREFIX
            )
            session.add(
                APIKeyModel(
                    key_id=key_id,
                    wallet_id=wallet_id,
                    key_hash=hashlib.sha256(api_key.encode()).hexdigest(),
                    key_prefix=SHARED_PREFIX,
                )
            )
        await session.commit()


@pytest.mark.anyio
async def test_same_prefix_active_keys_each_validate_as_themselves(
    clean_database,
) -> None:
    await _seed_same_prefix_keys()

    service = APIKeyService()
    for wallet_id, (key_id, api_key) in SAME_PREFIX_KEYS.items():
        key = await service.validate_key(api_key)
        assert key is not None
        assert (key.key_id, key.wallet_id) == (key_id, wallet_id)

    assert await service.validate_key(UNISSUED_SAME_PREFIX_KEY) is None


@pytest.mark.anyio
async def test_same_prefix_keys_authenticate_over_http_without_500(
    clean_database,
) -> None:
    await _seed_same_prefix_keys()
    (_, first_key), (_, second_key) = SAME_PREFIX_KEYS.values()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for api_key in (first_key, second_key):
            resp = await client.get(
                "/v1/billing/pricing", headers={"X-API-Key": api_key}
            )
            assert resp.status_code == 200, resp.text

        unknown = await client.get(
            "/v1/billing/pricing",
            headers={"X-API-Key": UNISSUED_SAME_PREFIX_KEY},
        )
        assert unknown.status_code == 403
        assert unknown.json()["detail"]["error"] == "invalid_api_key"

        # Sharing a prefix must not blur tenants: each key stays scoped to
        # its own wallet.
        own = await client.get(
            "/v1/api-keys/agt-same-prefix-a", headers={"X-API-Key": first_key}
        )
        assert own.status_code == 200
        cross = await client.get(
            "/v1/api-keys/agt-same-prefix-b", headers={"X-API-Key": first_key}
        )
        assert cross.status_code == 403
        assert cross.json()["detail"]["error"] == "wallet_access_denied"
