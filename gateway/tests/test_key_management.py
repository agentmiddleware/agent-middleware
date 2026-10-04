from __future__ import annotations

import pytest

from app.services.signing_keys import get_signing_key_service


@pytest.mark.anyio
async def test_active_signing_key_signs_and_verifies_payload(clean_database):
    service = get_signing_key_service()
    signature, key_id, payload_hash = await service.sign_payload(
        {"purpose": "trust-test", "amount": "1.0"}
    )
    payload = {
        "purpose": "trust-test",
        "amount": "1.0",
        "alg": "Ed25519",
        "kid": key_id,
        "payload_hash": payload_hash,
    }

    assert await service.verify_payload(
        payload,
        signature=signature,
        key_id=key_id,
    )

    payload["amount"] = "2.0"
    assert not await service.verify_payload(
        payload,
        signature=signature,
        key_id=key_id,
    )
