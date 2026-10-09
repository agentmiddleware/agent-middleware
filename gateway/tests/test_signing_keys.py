"""Focused unit coverage for app.services.signing_keys.

Covers the pure helpers (canonical_json, sha256_hex), the
validate_signing_key_configuration edges, and the sign/verify
round trip on ephemeral keys, including error and fail-closed cases.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.core.config import Settings
from app.services.signing_keys import (
    RECEIPT_CANONICALIZATION,
    SigningKeyError,
    SigningKeyService,
    canonical_json,
    get_signing_key_service,
    sha256_hex,
    validate_signing_key_configuration,
)


def _ephemeral_service() -> SigningKeyService:
    return SigningKeyService()


def test_canonical_json_sorts_keys_with_compact_separators():
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_canonical_json_normalizes_nested_dicts_and_lists():
    payload = {"z": [{"b": 1, "a": 2}], "a": {"d": 4, "c": 3}}
    assert canonical_json(payload) == '{"a":{"c":3,"d":4},"z":[{"a":2,"b":1}]}'


def test_canonical_json_normalizes_decimal_to_plain_string():
    assert canonical_json({"v": Decimal("2.0")}) == '{"v":"2"}'
    assert canonical_json({"v": Decimal("2.50")}) == '{"v":"2.5"}'


def test_canonical_json_treats_naive_datetime_as_utc():
    naive = datetime(2024, 1, 2, 3, 4, 5)
    aware = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert canonical_json({"t": naive}) == canonical_json({"t": aware})
    assert "+00:00" in canonical_json({"t": naive})


def test_sha256_hex_dict_input_hashes_canonical_bytes():
    payload = {"b": 1, "a": 2}
    expected = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    assert sha256_hex(payload) == expected


def test_sha256_hex_str_and_bytes_agree():
    assert sha256_hex("hello") == sha256_hex(b"hello")


def test_receipt_canonicalization_contract_name_is_pinned():
    assert RECEIPT_CANONICALIZATION == "awi-canonical-json/1"


def test_whitespace_only_key_with_trust_disabled_is_ephemeral():
    settings = Settings(
        TRUST_MODE_ENABLED=False,
        TRUST_SIGNING_PRIVATE_KEY_B64="   ",
    )
    assert validate_signing_key_configuration(settings) == "ephemeral"


def test_whitespace_only_key_with_trust_enabled_is_required():
    settings = Settings(
        TRUST_MODE_ENABLED=True,
        TRUST_SIGNING_PRIVATE_KEY_B64="   ",
    )
    with pytest.raises(SigningKeyError, match="trust_signing_private_key_required"):
        validate_signing_key_configuration(settings)


def test_get_signing_key_service_returns_singleton():
    assert get_signing_key_service() is get_signing_key_service()


@pytest.mark.anyio
async def test_sign_and_verify_round_trip(clean_database):
    service = _ephemeral_service()
    signature, key_id, payload_hash = await service.sign_payload(
        {"purpose": "round-trip"}
    )
    payload = {
        "purpose": "round-trip",
        "alg": "Ed25519",
        "kid": key_id,
        "payload_hash": payload_hash,
    }
    assert (
        await service.verify_payload(payload, signature=signature, key_id=key_id)
        is True
    )


@pytest.mark.anyio
async def test_verify_rejects_tampered_payload(clean_database):
    service = _ephemeral_service()
    signature, key_id, payload_hash = await service.sign_payload({"amount": "1"})
    tampered = {
        "amount": "2",
        "alg": "Ed25519",
        "kid": key_id,
        "payload_hash": payload_hash,
    }
    assert (
        await service.verify_payload(tampered, signature=signature, key_id=key_id)
        is False
    )


@pytest.mark.anyio
async def test_verify_unknown_key_id_is_false(clean_database):
    service = _ephemeral_service()
    signature, key_id, payload_hash = await service.sign_payload(
        {"purpose": "unknown-key"}
    )
    payload = {
        "purpose": "unknown-key",
        "alg": "Ed25519",
        "kid": key_id,
        "payload_hash": payload_hash,
    }
    assert (
        await service.verify_payload(
            payload, signature=signature, key_id="key-id-that-was-never-created"
        )
        is False
    )


@pytest.mark.anyio
async def test_verify_disabled_key_is_false(clean_database):
    from app.db.database import get_session_factory
    from app.db.models import SigningKeyModel

    service = _ephemeral_service()
    signature, key_id, payload_hash = await service.sign_payload(
        {"purpose": "disabled"}
    )
    factory = get_session_factory()
    async with factory() as session:
        persisted = await session.get(SigningKeyModel, key_id)
        assert persisted is not None
        persisted.status = "disabled"
        session.add(persisted)
        await session.commit()
    payload = {
        "purpose": "disabled",
        "alg": "Ed25519",
        "kid": key_id,
        "payload_hash": payload_hash,
    }
    assert (
        await service.verify_payload(payload, signature=signature, key_id=key_id)
        is False
    )


def test_sign_with_key_id_preserves_caller_fields_without_db():
    service = _ephemeral_service()
    payload = {"purpose": "preset", "alg": "custom", "kid": "caller-kid"}
    signature, key_id, payload_hash = service.sign_payload_with_key_id(
        payload, "caller-kid"
    )
    assert key_id == "caller-kid"
    assert payload_hash == sha256_hex(
        {"purpose": "preset", "alg": "custom", "kid": "caller-kid"}
    )
    assert base64.b64decode(signature, validate=True)


@pytest.mark.anyio
async def test_rotate_rejects_blank_key_id(clean_database):
    service = _ephemeral_service()
    with pytest.raises(SigningKeyError, match="signing_key_id_required"):
        await service.rotate_active_key_metadata("   ")


@pytest.mark.anyio
async def test_retire_unknown_key_raises_not_found(clean_database):
    service = _ephemeral_service()
    with pytest.raises(SigningKeyError, match="signing_key_not_found"):
        await service.retire_key_metadata("key-id-that-was-never-created")
