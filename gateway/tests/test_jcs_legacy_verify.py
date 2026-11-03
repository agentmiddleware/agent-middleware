"""Gateway verify accepts pre-JCS signatures after the RFC 8785 cutover."""

from __future__ import annotations

import hashlib

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
