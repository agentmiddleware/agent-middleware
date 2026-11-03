"""Gateway verify accepts pre-JCS signatures after the RFC 8785 cutover."""

from __future__ import annotations

import hashlib
import json

import pytest

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.services.signing_keys import (
    canonical_json,
    legacy_canonical_json,
    payload_hash_matches,
    signing_message_candidates,
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


def test_receipt_verification_payload_survives_jcs_rejected_constraints():
    """Service-level: JCS-only sha256_hex must not abort receipt verify rebuild.

    A historical constraints_evaluated value with a non-exact int (outside the
    IEEE-754 exact range) is rejected by JCS. Reconstructing the verification
    payload must fall through to the legacy serializer so verify does not 500.
    """
    from datetime import datetime, timezone
    from decimal import Decimal

    from app.db.models import ReceiptModel
    from app.services.jcs import CanonicalizationError
    from app.services.receipts import ReceiptService
    from app.services.signing_keys import sha256_hex

    non_exact = 9007199254740993  # 2**53 + 1; JCS rejects, legacy accepts
    try:
        sha256_hex({"n": non_exact})
        raise AssertionError("expected JCS to reject non-exact int")
    except CanonicalizationError:
        pass

    model = ReceiptModel(
        receipt_id="rcpt_jcs_fallback_0001",
        idempotency_record_id=None,
        dispatch_attempt_id=None,
        permit_id="pmt_jcs_fallback_0001",
        wallet_id="agt-jcs-fallback-0001",
        key_id="key_jcs_fallback_0001",
        tool="jcs-fallback-tool",
        request_hash="a" * 64,
        response_hash=None,
        ledger_entry_id=None,
        credits_authorized=Decimal("1"),
        credits_charged=Decimal("0"),
        outcome="denied",
        reason_code="permit_budget_exceeded",
        audit_event_id=None,
        approval_id=None,
        constraints_evaluated_json=json.dumps({"n": non_exact}),
        created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        signature="dGVzdA==",
        signature_key_id="sk_jcs_fallback_0001",
    )
    payload = ReceiptService._verification_payload(model, include_linkage=True)
    assert payload["constraints_evaluated"]["n"] == non_exact
    assert len(payload["payload_hash"]) == 64
    messages = signing_message_candidates(payload)
    assert messages, "expected at least one legacy candidate after JCS rejection"
    legacy = legacy_canonical_json(payload).encode()
    assert legacy in messages


@pytest.mark.anyio
async def test_verify_model_does_not_raise_on_jcs_rejected_constraints(monkeypatch):
    """verify_model must not surface CanonicalizationError as an HTTP 500."""
    from datetime import datetime, timezone
    from decimal import Decimal

    from app.db.models import ReceiptModel
    from app.services.receipts import ReceiptService
    from app.services.signing_keys import get_signing_key_service

    non_exact = 9007199254740993
    model = ReceiptModel(
        receipt_id="rcpt_jcs_verify_0001",
        idempotency_record_id=None,
        dispatch_attempt_id=None,
        permit_id="pmt_jcs_verify_0001",
        wallet_id="agt-jcs-verify-0001",
        key_id="key_jcs_verify_0001",
        tool="jcs-verify-tool",
        request_hash="c" * 64,
        response_hash=None,
        ledger_entry_id=None,
        credits_authorized=Decimal("1"),
        credits_charged=Decimal("0"),
        outcome="denied",
        reason_code="permit_budget_exceeded",
        audit_event_id=None,
        approval_id=None,
        constraints_evaluated_json=json.dumps({"n": non_exact}),
        created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        signature="dGVzdA==",
        signature_key_id="sk_jcs_verify_0001",
    )

    async def _ok(payload, **_kwargs):
        # Rebuild already happened; payload must be usable for candidates.
        assert signing_message_candidates(payload)
        return True

    monkeypatch.setattr(get_signing_key_service(), "verify_payload", _ok)
    assert await ReceiptService().verify_model(model) is True
