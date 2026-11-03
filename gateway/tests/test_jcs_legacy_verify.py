"""Gateway verify accepts pre-JCS signatures after the RFC 8785 cutover."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.db.models import ReceiptModel
from app.services.jcs import CanonicalizationError
from app.services.receipts import ReceiptService
from app.services.signing_keys import (
    canonical_json,
    get_signing_key_service,
    legacy_canonical_json,
    payload_hash_matches,
    signing_message_candidates,
)

# Recorded receipt signing golden (repo fixture). Used only as the carrier for
# the soft-hash / legacy-candidate path — not as a new invented body.
_GOLDEN_DENIAL = (
    Path(__file__).parent
    / "fixtures"
    / "receipt_signing_golden"
    / "denial_constraints.json"
)


def _model_from_denial_golden() -> ReceiptModel:
    """Rebuild a ReceiptModel from the recorded denial_constraints golden as-is."""
    recorded = json.loads(_GOLDEN_DENIAL.read_text())
    body = json.loads(recorded["signing_input"])
    created_at = datetime.fromisoformat(body["created_at"])
    constraints = body.get("constraints_evaluated")
    return ReceiptModel(
        receipt_id=body["receipt_id"],
        idempotency_record_id=body.get("idempotency_record_id"),
        dispatch_attempt_id=body.get("dispatch_attempt_id"),
        permit_id=body["permit_id"],
        wallet_id=body["wallet_id"],
        key_id=body.get("key_id"),
        tool=body["tool"],
        request_hash=body["request_hash"],
        response_hash=body.get("response_hash"),
        ledger_entry_id=body.get("ledger_entry_id"),
        credits_authorized=Decimal(body["credits_authorized"]),
        credits_charged=Decimal(body["credits_charged"]),
        outcome=body["outcome"],
        reason_code=body.get("reason_code"),
        audit_event_id=body.get("audit_event_id"),
        approval_id=body.get("approval_id"),
        constraints_evaluated_json=(
            json.dumps(constraints) if constraints is not None else None
        ),
        created_at=created_at,
        signature=recorded["signature_b64_over_test_seed"],
        signature_key_id=body["kid"],
    )


def test_legacy_and_jcs_differ_for_floats_and_non_ascii():
    payload = {"amount": 1.0, "label": "café"}
    jcs = canonical_json(payload)
    legacy = legacy_canonical_json(payload)
    assert jcs != legacy
    assert "1.0" in legacy and '"amount":1' in jcs
    assert r"\u00e9" in legacy  # ensure_ascii escapes
    assert "café" in jcs  # JCS keeps UTF-8


def test_signing_message_candidates_include_both_forms():
    payload = {"amount": 1.0, "label": "café"}
    messages = signing_message_candidates(payload)
    assert len(messages) == 2
    assert canonical_json(payload).encode() in messages
    assert legacy_canonical_json(payload).encode() in messages


def test_payload_hash_matches_accepts_legacy_bytes():
    payload = {"amount": 1.0, "label": "café"}
    legacy_hash = hashlib.sha256(legacy_canonical_json(payload).encode()).hexdigest()
    jcs_hash = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    assert legacy_hash != jcs_hash
    assert payload_hash_matches(payload, legacy_hash)
    assert payload_hash_matches(payload, jcs_hash)


def test_ed25519_verifies_legacy_message_bytes():
    """Simulate a pre-cutover signature over json.dumps bytes."""
    payload = {"amount": 1.0, "label": "café"}
    private = Ed25519PrivateKey.generate()
    message = legacy_canonical_json(payload).encode()
    signature = private.sign(message)
    public = private.public_key()
    verified = False
    for candidate in signing_message_candidates(payload):
        try:
            public.verify(signature, candidate)
            verified = True
            break
        except InvalidSignature:
            continue
    assert verified


def test_signing_message_candidates_rebuild_legacy_payload_hash():
    """Historical rows signed with a legacy-embedded payload_hash must verify.

    ``_verification_payload`` always re-embeds a JCS hash today; candidates must
    rebuild the hash under each serializer or the legacy signature never matches.
    """
    private = Ed25519PrivateKey.generate()
    base = {
        "receipt_id": "rcpt_legacy_float",
        "amount": 1.0,
        "note": "café",
        "alg": "Ed25519",
        "kid": "k1",
    }
    legacy_body = dict(base)
    legacy_body["payload_hash"] = hashlib.sha256(
        legacy_canonical_json(legacy_body).encode()
    ).hexdigest()
    legacy_message = legacy_canonical_json(legacy_body).encode()
    signature = private.sign(legacy_message)

    # Simulate verify rebuilding with a JCS-embedded hash (current helper).
    rebuilt = dict(base)
    rebuilt["payload_hash"] = hashlib.sha256(
        canonical_json(rebuilt).encode()
    ).hexdigest()
    assert rebuilt["payload_hash"] != legacy_body["payload_hash"]

    public = private.public_key()
    matched = False
    for candidate in signing_message_candidates(rebuilt):
        try:
            public.verify(signature, candidate)
            matched = True
            break
        except InvalidSignature:
            continue
    assert matched is True
    assert legacy_message in signing_message_candidates(rebuilt)


def test_audit_style_preset_payload_hash_verifies_as_is():
    """Audit hashes the body first; alg/kid are added without replacing the hash."""
    body = {"event_id": "e1", "ok": True, "amount": 1.0, "note": "café"}
    payload_hash = hashlib.sha256(legacy_canonical_json(body).encode()).hexdigest()
    signed = {
        **body,
        "payload_hash": payload_hash,
        "alg": "Ed25519",
        "kid": "k1",
    }
    private = Ed25519PrivateKey.generate()
    message = legacy_canonical_json(signed).encode()
    signature = private.sign(message)
    public = private.public_key()
    matched = False
    for candidate in signing_message_candidates(signed):
        try:
            public.verify(signature, candidate)
            matched = True
            break
        except InvalidSignature:
            continue
    assert matched is True
    assert message in signing_message_candidates(signed)


def test_jcs_rebuild_error_still_tries_legacy():
    """A JCS CanonicalizationError on rebuild must not skip the legacy path."""
    # NaN is rejected by JCS but accepted by legacy json.dumps(allow_nan=True).
    payload = {
        "v": float("nan"),
        "payload_hash": "0" * 64,
        "alg": "Ed25519",
        "kid": "k1",
    }
    messages = signing_message_candidates(payload)
    assert messages, "expected at least one legacy candidate"
    legacy = legacy_canonical_json(payload).encode()
    assert legacy in messages


def test_receipt_verification_payload_survives_jcs_rejected_constraints(monkeypatch):
    """Soft hash path: JCS failure falls back to legacy without raising.

    Recorded ``denial_constraints`` golden is the carrier (no swapped fields).
    ``sha256_hex`` is stubbed to raise CanonicalizationError so the soft-hash
    branch runs; asserts stay structural (no raise, 64-hex, legacy candidate).
    """
    model = _model_from_denial_golden()

    def _boom(_payload):
        raise CanonicalizationError("forced JCS reject")

    monkeypatch.setattr("app.services.receipts.sha256_hex", _boom)
    payload = ReceiptService._verification_payload(model, include_linkage=True)

    assert len(payload["payload_hash"]) == 64
    assert int(payload["payload_hash"], 16) >= 0
    body = {k: v for k, v in payload.items() if k != "payload_hash"}
    expected = hashlib.sha256(legacy_canonical_json(body).encode("utf-8")).hexdigest()
    assert payload["payload_hash"] == expected
    messages = signing_message_candidates(payload)
    assert messages, "expected at least one legacy candidate after JCS rejection"
    assert legacy_canonical_json(payload).encode() in messages


@pytest.mark.anyio
async def test_verify_model_does_not_raise_on_jcs_rejected_constraints(monkeypatch):
    """verify_model must not surface CanonicalizationError as an HTTP 500.

    Recorded golden carrier unchanged; stub ``sha256_hex`` to force the soft
    path, then monkeypatch verify to assert candidates and return True.
    """
    model = _model_from_denial_golden()

    def _boom(_payload):
        raise CanonicalizationError("forced JCS reject")

    monkeypatch.setattr("app.services.receipts.sha256_hex", _boom)

    async def _ok(payload, **_kwargs):
        assert signing_message_candidates(payload)
        assert len(payload["payload_hash"]) == 64
        return True

    monkeypatch.setattr(get_signing_key_service(), "verify_payload", _ok)
    assert await ReceiptService().verify_model(model) is True
