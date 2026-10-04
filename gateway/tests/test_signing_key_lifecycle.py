from __future__ import annotations

from decimal import Decimal

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.database import get_session_factory
from app.db.models import SigningKeyModel
from app.main import app
from app.services.audit_log import record_audit_event
from app.services.receipts import get_receipt_service
from app.services.signing_keys import (
    SigningKeyError,
    SigningKeyService,
    get_signing_key_service,
)
from tests.test_trust_helpers import (
    BOOTSTRAP_HEADERS,
    create_tool_permit,
    provision_agent_wallet,
)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
async def test_active_signing_key_metadata_endpoint_excludes_private_key(
    client,
    clean_database,
):
    resp = await client.get("/v1/signing-keys/active", headers=BOOTSTRAP_HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert body["key_id"]
    assert body["alg"] == "Ed25519"
    assert body["public_key_b64"]
    assert body["status"] == "active"
    assert "private_key" not in body
    assert "private_key_b64" not in body


@pytest.mark.anyio
async def test_verify_payload_fails_closed_on_malformed_signature(clean_database):
    """A malformed signature must verify as False, not raise.

    Regression: the base64 decodes were outside the try/except, so a signature
    with bad padding raised binascii.Error (a ValueError) and surfaced as an
    HTTP 500 from the verify endpoints, letting a corrupt row mask tampering.
    """
    service = get_signing_key_service()
    _, key_id, payload_hash = await service.sign_payload({"purpose": "malformed"})
    payload = {
        "purpose": "malformed",
        "alg": "Ed25519",
        "kid": key_id,
        "payload_hash": payload_hash,
    }

    for bad in ("!!!not-base64!!!", "", "YWJj"):  # invalid, empty, valid-b64-wrong
        assert (
            await service.verify_payload(payload, signature=bad, key_id=key_id) is False
        )


@pytest.mark.anyio
async def test_retired_signing_key_metadata_still_verifies_payload(clean_database):
    service = get_signing_key_service()
    signature, key_id, payload_hash = await service.sign_payload(
        {"purpose": "retire-test"}
    )
    await service.retire_key_metadata(key_id)

    payload = {
        "purpose": "retire-test",
        "alg": "Ed25519",
        "kid": key_id,
        "payload_hash": payload_hash,
    }

    assert await service.verify_payload(payload, signature=signature, key_id=key_id)

    factory = get_session_factory()
    async with factory() as session:
        key = await session.get(SigningKeyModel, key_id)
        assert key is not None
        assert key.status == "retired"
        assert key.retired_at is not None


@pytest.mark.anyio
async def test_ephemeral_key_restart_does_not_clobber_prior_public_metadata(
    clean_database,
):
    """A restarted (or second) instance generating a fresh ephemeral key must
    publish its public metadata under its own content-addressed key_id rather
    than overwriting the row the previous key signed under -- so signatures
    made before the restart still verify afterwards."""
    first = SigningKeyService()
    signature, first_key_id, payload_hash = await first.sign_payload(
        {"purpose": "before-restart"}
    )

    # Simulate a process restart: a brand-new instance mints a different
    # ephemeral key and ensures its own active metadata.
    second = SigningKeyService()
    second_key = await second.ensure_active_key()

    assert second_key.key_id != first_key_id
    assert second_key.public_key_b64 != (await first.get_active_key()).public_key_b64

    # The pre-restart signature is still verifiable against its own key_id.
    payload = {
        "purpose": "before-restart",
        "alg": "Ed25519",
        "kid": first_key_id,
        "payload_hash": payload_hash,
    }
    assert await second.verify_payload(
        payload, signature=signature, key_id=first_key_id
    )


@pytest.mark.anyio
async def test_reused_key_id_with_different_public_key_is_rejected_without_clobbering_history(
    client,
    clean_database,
):
    service = get_signing_key_service()
    original_key = await service.ensure_active_key()

    provisioned = await provision_agent_wallet(client)
    old_permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="historical-tool",
        idem_key="permit-before-key-id-mismatch",
    )
    old_receipt = await get_receipt_service().create_receipt(
        permit_id=old_permit["permit_id"],
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool="historical-tool",
        request_payload={"message": "before"},
        response_payload={"message": "after"},
        ledger_entry_id=None,
        credits_authorized=Decimal("2"),
        credits_charged=Decimal("0"),
        outcome="success",
        audit_event_id=None,
    )

    misconfigured = SigningKeyService()
    misconfigured._private_key = Ed25519PrivateKey.generate()
    misconfigured._key_id = original_key.key_id
    assert misconfigured._public_key_b64() != original_key.public_key_b64

    with pytest.raises(
        SigningKeyError,
        match="signing_key_id_public_key_mismatch",
    ):
        await misconfigured.sign_payload({"purpose": "must-not-sign"})

    persisted_key = await service.get_public_key(original_key.key_id)
    assert persisted_key is not None
    assert persisted_key.public_key_b64 == original_key.public_key_b64

    permit_verify = await client.post(
        "/v1/permits/verify",
        json={
            "permit_id": old_permit["permit_id"],
            "wallet_id": provisioned["agent_wallet_id"],
            "tool": "historical-tool",
            "estimated_credits": 1,
        },
        headers=provisioned["agent_headers"],
    )
    assert permit_verify.status_code == 200
    assert permit_verify.json()["valid"] is True

    receipt_verify = await client.post(
        "/v1/receipts/verify",
        json={"receipt_id": old_receipt.receipt_id},
        headers=provisioned["agent_headers"],
    )
    assert receipt_verify.status_code == 200
    assert receipt_verify.json()["valid"] is True


@pytest.mark.anyio
async def test_disabled_signing_key_cannot_be_reactivated_by_signing(
    clean_database,
):
    service = get_signing_key_service()
    key = await service.ensure_active_key()

    factory = get_session_factory()
    async with factory() as session:
        persisted = await session.get(SigningKeyModel, key.key_id)
        assert persisted is not None
        persisted.status = "disabled"
        session.add(persisted)
        await session.commit()

    with pytest.raises(SigningKeyError, match="signing_key_disabled"):
        await service.sign_payload({"purpose": "must-not-reactivate"})

    persisted = await service.get_public_key(key.key_id)
    assert persisted is not None
    assert persisted.status == "disabled"


@pytest.mark.anyio
async def test_rotation_cannot_reactivate_disabled_target_key(clean_database):
    service = get_signing_key_service()
    current = await service.ensure_active_key()
    disabled_key_id = "disabled-rotation-target"

    factory = get_session_factory()
    async with factory() as session:
        session.add(
            SigningKeyModel(
                key_id=disabled_key_id,
                public_key_b64=current.public_key_b64,
                status="disabled",
                activated_at=current.activated_at,
            )
        )
        await session.commit()

    with pytest.raises(SigningKeyError, match="signing_key_disabled"):
        await service.rotate_active_key_metadata(disabled_key_id)

    persisted_current = await service.get_public_key(current.key_id)
    persisted_target = await service.get_public_key(disabled_key_id)
    assert persisted_current is not None
    assert persisted_current.status == "active"
    assert persisted_target is not None
    assert persisted_target.status == "disabled"


@pytest.mark.anyio
async def test_rotation_rejects_existing_key_id_bound_to_different_public_key(
    clean_database,
):
    first = SigningKeyService()
    first._private_key = Ed25519PrivateKey.generate()
    first._key_id = "first-signing-key"
    first_key = await first.ensure_active_key()

    second = SigningKeyService()
    second._private_key = Ed25519PrivateKey.generate()
    second._key_id = "second-signing-key"
    second_key = await second.ensure_active_key()

    with pytest.raises(
        SigningKeyError,
        match="signing_key_id_public_key_mismatch",
    ):
        await second.rotate_active_key_metadata(first_key.key_id)

    persisted_first = await first.get_public_key(first_key.key_id)
    persisted_second = await second.get_public_key(second_key.key_id)
    assert persisted_first is not None
    assert persisted_first.public_key_b64 == first_key.public_key_b64
    assert persisted_first.status == "active"
    assert persisted_second is not None
    assert persisted_second.public_key_b64 == second_key.public_key_b64
    assert persisted_second.status == "active"


@pytest.mark.anyio
async def test_rotated_metadata_preserves_historical_trust_artifact_verification(
    client,
    clean_database,
):
    service = get_signing_key_service()
    original_key_id = service._settings.TRUST_SIGNING_KEY_ID
    await service.rotate_active_key_metadata(original_key_id)

    provisioned = await provision_agent_wallet(client)
    old_permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="historical-tool",
        idem_key="permit-before-signing-key-rotation",
    )
    old_receipt = await get_receipt_service().create_receipt(
        permit_id=old_permit["permit_id"],
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool="historical-tool",
        request_payload={"message": "before"},
        response_payload={"message": "after"},
        ledger_entry_id=None,
        credits_authorized=Decimal("2"),
        credits_charged=Decimal("0"),
        outcome="success",
        audit_event_id=None,
    )
    audit_event = await record_audit_event(
        event="trust.rotation.before",
        wallet_id=provisioned["agent_wallet_id"],
        tool="historical-tool",
        key_id=provisioned["key_id"],
        metadata={"phase": "before"},
    )

    rotated_key_id = "test-rotated-ed25519"
    try:
        rotated_key = await service.rotate_active_key_metadata(rotated_key_id)
        assert rotated_key.key_id == rotated_key_id

        active_resp = await client.get(
            "/v1/signing-keys/active",
            headers=BOOTSTRAP_HEADERS,
        )
        assert active_resp.status_code == 200
        assert active_resp.json()["key_id"] == rotated_key_id

        new_permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name="current-tool",
            idem_key="permit-after-signing-key-rotation",
        )
        assert new_permit["key_id"] == rotated_key_id

        permit_verify = await client.post(
            "/v1/permits/verify",
            json={
                "permit_id": old_permit["permit_id"],
                "wallet_id": provisioned["agent_wallet_id"],
                "tool": "historical-tool",
                "estimated_credits": 1,
            },
            headers=provisioned["agent_headers"],
        )
        assert permit_verify.status_code == 200
        assert permit_verify.json()["valid"] is True

        receipt_verify = await client.post(
            "/v1/receipts/verify",
            json={"receipt_id": old_receipt.receipt_id},
            headers=provisioned["agent_headers"],
        )
        assert receipt_verify.status_code == 200
        assert receipt_verify.json()["valid"] is True

        audit_verify = await client.post(
            "/v1/audit/verify-chain",
            json={"wallet_id": provisioned["agent_wallet_id"]},
            headers=provisioned["agent_headers"],
        )
        assert audit_verify.status_code == 200
        assert audit_verify.json()["valid"] is True

        old_key_resp = await client.get(
            f"/v1/signing-keys/{old_permit['key_id']}",
            headers=BOOTSTRAP_HEADERS,
        )
        assert old_key_resp.status_code == 200
        assert old_key_resp.json()["status"] == "retired"
        assert old_key_resp.json()["retired_at"] is not None
        assert audit_event.signature_key_id == old_permit["key_id"]

        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(SigningKeyModel).where(
                    SigningKeyModel.key_id.in_([old_permit["key_id"], rotated_key_id])
                )
            )
            statuses = {key.key_id: key.status for key in result.scalars().all()}
        assert statuses[old_permit["key_id"]] == "retired"
        assert statuses[rotated_key_id] == "active"
    finally:
        await service.rotate_active_key_metadata(original_key_id)


# The signing-key metadata endpoints are the surface a verifier trusts to learn
# which public key signed a receipt. They must fail closed for every
# unauthenticated or unrecognized caller, before any key lookup runs. The
# credential decides the status, never the target: a missing or malformed key is
# 401, a well-formed but unknown key is 403 (see app/core/auth.get_auth_context).
_UNAUTHORIZED_CREDENTIALS = (
    ({}, 401),
    ({"X-API-Key": "short"}, 401),
    ({"X-API-Key": "not-a-real-key"}, 403),
)


@pytest.mark.anyio
@pytest.mark.parametrize("headers,expected_status", _UNAUTHORIZED_CREDENTIALS)
async def test_active_signing_key_requires_authentication(
    client, clean_database, headers, expected_status
):
    resp = await client.get("/v1/signing-keys/active", headers=headers)
    assert resp.status_code == expected_status, resp.text
    # A refused caller learns nothing about the active key.
    assert "public_key_b64" not in resp.text


@pytest.mark.anyio
@pytest.mark.parametrize("headers,expected_status", _UNAUTHORIZED_CREDENTIALS)
async def test_signing_key_by_id_requires_authentication(
    client, clean_database, headers, expected_status
):
    # Auth is enforced before the key lookup: an unknown key_id with no valid
    # credential is a credential failure (401/403), never a 404 that would
    # confirm or deny the id's existence to an anonymous caller.
    resp = await client.get("/v1/signing-keys/key-does-not-exist", headers=headers)
    assert resp.status_code == expected_status, resp.text
