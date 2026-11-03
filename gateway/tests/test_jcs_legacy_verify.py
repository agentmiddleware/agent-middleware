"""Gateway verify accepts pre-JCS signatures after the RFC 8785 cutover."""

from __future__ import annotations


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
    import hashlib

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
    # dual-path candidates must include the legacy message that verifies
    verified = False
    for candidate in signing_message_candidates(payload):
        try:
            public.verify(signature, candidate)
            verified = True
            break
        except Exception:
            continue
    assert verified
