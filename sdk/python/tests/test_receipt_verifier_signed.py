"""Signed tamper cases for the offline receipt verifier.

Every test here signs with a throwaway Ed25519 key, so the suite needs no
gateway, no database, and no network: just the SDK plus ``cryptography``.
Each tamper test mutates either the signed bytes or the envelope of a bundle
that verifies untouched, then asserts the verifier rejects it with the right
status. A test that passes here means an outside party holding the same two
JSON documents reaches the same conclusion.

Money movement, identity binding, and envelope relabelling get priority:
those are the forgeries that would misattribute a charge or a tool call.
"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from b2a_sdk import verify_cli
from b2a_sdk.receipt_verifier import (
    VerificationError,
    VerificationStatus,
    canonical_json,
    key_set_from_document,
    verify_bundle,
    verify_ed25519,
)


def _local_signer(kid="local-test-key"):
    """Return (kid, key_set, sign) for a throwaway Ed25519 key."""
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    private_key = Ed25519PrivateKey.generate()
    raw_public = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    def sign(signing_input: str) -> str:
        return base64.b64encode(private_key.sign(signing_input.encode("utf-8"))).decode()

    return kid, {kid: raw_public}, sign


def _payload(**overrides):
    body = {
        "receipt_id": "rcpt-tamper",
        "kid": "local-test-key",
        "permit_id": "permit-1",
        "wallet_id": "wallet-1",
        "key_id": "key-1",
        "tool": "partner.echo",
        "idempotency_record_id": "idem-1",
        "dispatch_attempt_id": "dispatch-1",
        "ledger_entry_id": "ledger-1",
        "audit_event_id": "audit-1",
        "request_hash": "a" * 64,
        "response_hash": "b" * 64,
        "outcome": "success",
        "credits_authorized": "2",
        "credits_charged": "2",
        "created_at": "2026-08-11T00:00:00Z",
    }
    body.update(overrides)
    inner = {k: v for k, v in body.items() if k != "payload_hash"}
    body["payload_hash"] = hashlib.sha256(canonical_json(inner).encode("utf-8")).hexdigest()
    return body


def _signed_bundle(payload, *, kid, sign, **overrides):
    signing_input = canonical_json(payload)
    bundle = {
        "receipt_id": payload["receipt_id"],
        "issuer": "https://api.example.com",
        "alg": "Ed25519",
        "kid": kid,
        "canonicalization": "awi-canonical-json/1",
        "signing_input": signing_input,
        "signature": sign(signing_input),
    }
    bundle.update(overrides)
    return bundle


def _verified_bundle(**payload_overrides):
    kid, key_set, sign = _local_signer()
    payload = _payload(kid=kid, **payload_overrides)
    bundle = _signed_bundle(payload, kid=kid, sign=sign)
    result = verify_bundle(bundle, key_set)
    assert result.status is VerificationStatus.VERIFIED, result.reason
    return bundle, key_set


def _retarget(bundle, old, new):
    """Edit the signed bytes, proving the edit bites."""
    edited = dict(bundle, signing_input=bundle["signing_input"].replace(old, new, 1))
    assert new in edited["signing_input"]
    assert edited["signing_input"] != bundle["signing_input"]
    return edited


def test_flipping_credits_charged_is_rejected_as_tampering():
    bundle, key_set = _verified_bundle()

    result = verify_bundle(
        _retarget(bundle, '"credits_charged":"2"', '"credits_charged":"0"'), key_set
    )

    assert result.status is VerificationStatus.INVALID
    assert result.is_tampered
    assert result.is_rejected


def test_flipping_credits_authorized_is_rejected_as_tampering():
    bundle, key_set = _verified_bundle()

    result = verify_bundle(
        _retarget(bundle, '"credits_authorized":"2"', '"credits_authorized":"200"'), key_set
    )

    assert result.status is VerificationStatus.INVALID
    assert result.is_tampered


def test_flipping_outcome_success_to_denied_is_rejected():
    """Outcome decides whether money moved; rewriting it is forgery."""
    bundle, key_set = _verified_bundle()

    result = verify_bundle(_retarget(bundle, '"outcome":"success"', '"outcome":"denied"'), key_set)

    assert result.status is VerificationStatus.INVALID
    assert result.is_tampered


@pytest.mark.parametrize(
    "field,old,new",
    [
        ("tool", "partner.echo", "partner.delete"),
        ("wallet_id", "wallet-1", "wallet-2"),
        ("key_id", "key-1", "key-2"),
        ("permit_id", "permit-1", "permit-2"),
        ("ledger_entry_id", "ledger-1", "ledger-9"),
        ("idempotency_record_id", "idem-1", "idem-9"),
        ("dispatch_attempt_id", "dispatch-1", "dispatch-9"),
        ("audit_event_id", "audit-1", "audit-9"),
        ("request_hash", "a" * 64, "c" * 64),
    ],
)
def test_relabeling_signed_identity_fields_is_rejected(field, old, new):
    bundle, key_set = _verified_bundle()

    result = verify_bundle(_retarget(bundle, f'"{field}":"{old}"', f'"{field}":"{new}"'), key_set)

    assert result.status is VerificationStatus.INVALID, field
    assert result.is_tampered, field


def test_reserialized_bytes_with_identical_json_are_rejected():
    """The signature covers the bytes verbatim, never a re-serialization."""
    bundle, key_set = _verified_bundle()
    reserialized = json.dumps(json.loads(bundle["signing_input"]), indent=2, sort_keys=True)
    assert json.loads(reserialized) == json.loads(bundle["signing_input"])
    assert reserialized != bundle["signing_input"]

    result = verify_bundle(dict(bundle, signing_input=reserialized), key_set)

    assert result.status is VerificationStatus.INVALID
    assert result.is_tampered


def test_unicode_escape_flip_is_rejected():
    bundle, key_set = _verified_bundle(tool="caf\u00e9-echo")
    assert "\\u00e9" in bundle["signing_input"]

    result = verify_bundle(_retarget(bundle, "\\u00e9", "\u00e9"), key_set)

    assert result.status is VerificationStatus.INVALID
    assert result.is_tampered


@pytest.mark.parametrize("index", [0, -1])
def test_single_bit_signature_flip_is_rejected(index):
    bundle, key_set = _verified_bundle()
    raw = bytearray(base64.b64decode(bundle["signature"]))
    raw[index] ^= 0x01

    result = verify_bundle(dict(bundle, signature=base64.b64encode(bytes(raw)).decode()), key_set)

    assert result.status is VerificationStatus.INVALID
    assert result.is_tampered


def test_truncated_signing_input_is_not_verified():
    bundle, key_set = _verified_bundle()

    result = verify_bundle(dict(bundle, signing_input=bundle["signing_input"][:-1]), key_set)

    assert result.status is VerificationStatus.MALFORMED
    assert not result.ok
    assert not result.is_tampered


def test_envelope_receipt_id_relabel_is_a_mismatch_with_signed_id_reported():
    bundle, key_set = _verified_bundle()

    result = verify_bundle(dict(bundle, receipt_id="rcpt_not_the_signed_one"), key_set)

    assert result.status is VerificationStatus.MISMATCH
    assert result.is_rejected
    assert not result.is_tampered
    assert result.receipt_id == "rcpt-tamper"


def test_signed_under_a_key_the_payload_does_not_name_is_a_mismatch():
    kid, key_set, sign = _local_signer()
    payload = _payload(kid="a-different-key")
    bundle = _signed_bundle(payload, kid=kid, sign=sign)

    result = verify_bundle(bundle, key_set)

    assert result.status is VerificationStatus.MISMATCH
    assert result.is_rejected
    assert not result.is_tampered


def test_unknown_signed_kid_with_unknown_envelope_is_unknown_key():
    bundle, key_set = _verified_bundle()
    assert "no-such-key" not in key_set

    result = verify_bundle(
        _retarget(bundle, '"kid":"local-test-key"', '"kid":"no-such-key"'),
        {"other-key": b"\x00" * 32},
    )

    assert result.status is VerificationStatus.UNKNOWN_KEY
    assert not result.is_tampered
    assert not result.is_rejected


def test_rejected_bundle_carries_no_claims_or_receipt_id():
    bundle, key_set = _verified_bundle()

    result = verify_bundle(_retarget(bundle, '"outcome":"success"', '"outcome":"denied"'), key_set)

    assert result.status is VerificationStatus.INVALID
    assert result.claims == {}
    assert result.receipt_id is None


def test_matching_expected_issuer_still_verifies():
    bundle, key_set = _verified_bundle()

    result = verify_bundle(bundle, key_set, expected_issuer="https://api.example.com")

    assert result.status is VerificationStatus.VERIFIED
    assert result.ok


def test_payload_without_payload_hash_verifies():
    kid, key_set, sign = _local_signer()
    payload = _payload(kid=kid)
    del payload["payload_hash"]

    result = verify_bundle(_signed_bundle(payload, kid=kid, sign=sign), key_set)

    assert result.status is VerificationStatus.VERIFIED, result.reason


def test_signed_payload_hash_is_advisory():
    """A wrong but signed payload_hash still verifies; the signature covers it."""
    kid, key_set, sign = _local_signer()
    payload = _payload(kid=kid)
    payload["payload_hash"] = "0" * 64

    result = verify_bundle(_signed_bundle(payload, kid=kid, sign=sign), key_set)

    assert result.status is VerificationStatus.VERIFIED, result.reason


def test_editing_payload_hash_without_resigning_is_invalid():
    bundle, key_set = _verified_bundle()
    good = json.loads(bundle["signing_input"])["payload_hash"]

    result = verify_bundle(_retarget(bundle, good, "0" * 64), key_set)

    assert result.status is VerificationStatus.INVALID
    assert result.is_tampered


def test_float_bearing_payload_verifies_without_reserialization():
    """Floats the gateway signs (1.0, 0.0) verify; numbers are never rechecked."""
    kid, key_set, sign = _local_signer()
    payload = _payload(kid=kid)
    del payload["payload_hash"]
    payload["constraints_evaluated"] = {
        "jev_risk_guard": {"latency_ms": 0.0, "injected_instructions": 1.0}
    }
    inner = {k: v for k, v in payload.items() if k != "payload_hash"}
    payload["payload_hash"] = hashlib.sha256(canonical_json(inner).encode("utf-8")).hexdigest()
    bundle = _signed_bundle(payload, kid=kid, sign=sign)
    assert '"latency_ms":0.0' in bundle["signing_input"]
    assert '"injected_instructions":1.0' in bundle["signing_input"]

    result = verify_bundle(bundle, key_set)

    assert result.status is VerificationStatus.VERIFIED, result.reason


def test_extra_envelope_fields_are_ignored():
    bundle, key_set = _verified_bundle()
    bundle = {**bundle, "keys_url": "/.well-known/trust-keys.json", "future_field": {"n": 1}}

    result = verify_bundle(bundle, key_set)

    assert result.status is VerificationStatus.VERIFIED, result.reason


def test_bundle_accepted_as_bytes():
    bundle, key_set = _verified_bundle()

    result = verify_bundle(json.dumps(bundle).encode("utf-8"), key_set)

    assert result.status is VerificationStatus.VERIFIED, result.reason


def test_verify_ed25519_accepts_genuine_and_rejects_altered_bytes():
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    private_key = Ed25519PrivateKey.generate()
    raw_public = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    message = b'{"receipt_id":"rcpt-1"}'
    signature = private_key.sign(message)

    assert verify_ed25519(raw_public, signature, message) is True
    assert verify_ed25519(raw_public, signature, message + b" ") is False


def test_key_set_accepts_key_id_mirror():
    document = {"keys": [{"key_id": "mirror", "public_key_b64": "A" * 43 + "="}]}

    assert set(key_set_from_document(document)) == {"mirror"}


def test_key_set_empty_raw_falls_back_to_jwk_mirror():
    document = {
        "keys": [
            {
                "kid": "jwk-fallback",
                "alg": "Ed25519",
                "public_key_b64": "",
                "jwk": {"kty": "OKP", "crv": "Ed25519", "x": "B" * 43},
            }
        ]
    }

    keys = key_set_from_document(document)

    assert set(keys) == {"jwk-fallback"}
    assert len(keys["jwk-fallback"]) == 32


@pytest.mark.parametrize(
    "entry",
    [
        {"kid": "wrong-alg", "alg": "RS256", "public_key_b64": "A" * 43 + "="},
        {
            "kid": "wrong-crv",
            "alg": "Ed25519",
            "jwk": {"kty": "OKP", "crv": "P-256", "x": "B" * 43},
        },
    ],
    ids=["non_ed25519_alg", "non_ed25519_crv"],
)
def test_key_set_drops_keys_it_cannot_verify_with(entry):
    keys = key_set_from_document({"keys": [entry]})

    assert keys == {}


def test_key_set_rejects_a_non_object_document():
    with pytest.raises(VerificationError):
        key_set_from_document(["not", "a", "dict"])


def test_genuine_p256_receipt_is_unsupported_only_after_signature_verification():
    """The dormant KMS algorithm is recognizable without accepting it as v0.1."""
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private_key = ec.generate_private_key(ec.SECP256R1())
    public_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    kid = "p256-test-key"
    algorithm = "ECDSA-P256-SHA256"
    signing_input = canonical_json(_payload(kid=kid, alg=algorithm))
    signature = private_key.sign(signing_input.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    bundle = {
        "kid": kid,
        "alg": algorithm,
        "signing_input": signing_input,
        "signature": base64.b64encode(signature).decode(),
    }
    document = {
        "keys": [
            {
                "kid": kid,
                "alg": algorithm,
                "status": "active",
                "public_key_b64": base64.b64encode(public_der).decode(),
            }
        ]
    }
    keys = key_set_from_document(document)

    assert kid in keys
    assert verify_bundle(bundle, {}).status is VerificationStatus.UNKNOWN_KEY
    genuine = verify_bundle(bundle, keys)
    assert genuine.status is VerificationStatus.UNSUPPORTED
    assert not genuine.is_tampered

    ed_claim = canonical_json(_payload(kid=kid, alg="Ed25519"))
    ed_claim_signature = private_key.sign(ed_claim.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    mislabeled = dict(
        bundle,
        alg="Ed25519",
        signing_input=ed_claim,
        signature=base64.b64encode(ed_claim_signature).decode(),
    )
    mismatched_algorithm = verify_bundle(mislabeled, keys)
    assert mismatched_algorithm.status is VerificationStatus.UNSUPPORTED
    assert "verification key algorithm" in (mismatched_algorithm.reason or "")

    altered = dict(
        bundle, signing_input=signing_input.replace('"outcome":"success"', '"outcome":"denied"')
    )
    assert verify_bundle(altered, keys).status is VerificationStatus.INVALID

    other_key = ec.generate_private_key(ec.SECP256R1())
    wrong_signature = other_key.sign(signing_input.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    assert (
        verify_bundle(
            dict(bundle, signature=base64.b64encode(wrong_signature).decode()), keys
        ).status
        is VerificationStatus.INVALID
    )
    assert (
        verify_bundle(dict(bundle, signature=base64.b64encode(b"\x30\x00").decode()), keys).status
        is VerificationStatus.MALFORMED
    )

    document["keys"][0]["status"] = "disabled"
    assert (
        verify_bundle(bundle, key_set_from_document(document)).status
        is VerificationStatus.UNKNOWN_KEY
    )
    document["keys"][0]["status"] = "active"
    document["keys"][0]["public_key_b64"] = base64.b64encode(b"not DER").decode()
    assert (
        verify_bundle(bundle, key_set_from_document(document)).status
        is VerificationStatus.MALFORMED
    )
    document["keys"][0]["public_key_b64"] = "!!!!"
    assert (
        verify_bundle(bundle, key_set_from_document(document)).status
        is VerificationStatus.MALFORMED
    )


def _write_cli_inputs(tmp_path, bundle, key_set):
    bundle_path = tmp_path / "bundle.json"
    keys_path = tmp_path / "trust-keys.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    keys_path.write_text(
        json.dumps(
            {
                "keys": [
                    {
                        "kid": kid,
                        "alg": "Ed25519",
                        "status": "active",
                        "public_key_b64": base64.b64encode(raw).decode(),
                    }
                    for kid, raw in key_set.items()
                ]
            }
        ),
        encoding="utf-8",
    )
    return str(bundle_path), str(keys_path)


def test_cli_envelope_mismatch_exits_1_not_2(tmp_path, capsys):
    """A caught relabelling is a verdict on the bundle, not an outage."""
    bundle, key_set = _verified_bundle()
    bundle_path, keys_path = _write_cli_inputs(
        tmp_path, dict(bundle, receipt_id="rcpt_not_the_signed_one"), key_set
    )

    assert verify_cli.main(["--bundle", bundle_path, "--keys", keys_path]) == 1
    assert "MISMATCH" in capsys.readouterr().err


def test_cli_malformed_bundle_exits_2(tmp_path, capsys):
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text("{oops", encoding="utf-8")
    keys_path = tmp_path / "trust-keys.json"
    keys_path.write_text(json.dumps({"keys": []}), encoding="utf-8")

    assert verify_cli.main(["--bundle", str(bundle_path), "--keys", str(keys_path)]) == 2


def test_cli_json_output_for_rejected_bundle(tmp_path, capsys):
    bundle, key_set = _verified_bundle()
    forged = _retarget(bundle, '"outcome":"success"', '"outcome":"denied"')
    bundle_path, keys_path = _write_cli_inputs(tmp_path, forged, key_set)

    assert verify_cli.main(["--bundle", bundle_path, "--keys", keys_path, "--json"]) == 1

    emitted = json.loads(capsys.readouterr().out)
    assert emitted["ok"] is False
    assert emitted["status"] == "invalid"
    assert emitted["claims"] == {}
