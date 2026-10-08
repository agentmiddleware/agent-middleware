"""Behavior the spec vectors must keep true.

The committed JSON is generated. A case that is missing here means a verifier
can ignore key retirement, a disabled key, or a lying envelope and still pass.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import generate
from b2a_sdk.receipt_verifier import key_set_from_document, verify_bundle
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
PERMIT_GOLDEN = ROOT / "gateway" / "tests" / "fixtures" / "permit_signing_golden"
RECEIPT_GOLDEN = ROOT / "gateway" / "tests" / "fixtures" / "receipt_signing_golden"

# Statuses a correct verifier returns for the cases the generator adds on top
# of the per-fixture valid, bad-signature, tamper, and unknown-key rows.
REQUIRED_CASES = {
    "key_status.retired_still_verifies": "verified",
    "key_status.disabled_is_not_usable": "unknown_key",
    "key_form.jwk_only": "verified",
    "key_form.raw_key_beats_disagreeing_jwk": "invalid",
    "signed.payload_hash_mismatch": "mismatch",
    "signed.unsupported_alg": "unsupported",
    "signed.unsupported_canonicalization": "unsupported",
    "envelope.kid_mismatch": "mismatch",
    "envelope.alg_mismatch": "mismatch",
    "envelope.canonicalization_mismatch": "mismatch",
    "malformed.bad_json": "malformed",
    "malformed.short_signature": "malformed",
    "malformed.no_signature": "malformed",
}


def _load_cases() -> list[dict]:
    return json.loads((HERE / "receipts.json").read_text())["cases"]


def _by_name(cases: list[dict]) -> dict[str, dict]:
    found = {case["name"]: case for case in cases}
    assert len(found) == len(cases)
    return found


def test_generator_emits_key_lifecycle_and_envelope_cases():
    _keys, receipts = generate.build_documents()
    found = _by_name(receipts["cases"])
    for name, expect in REQUIRED_CASES.items():
        assert name in found, name
        assert found[name]["expect"] == expect


def test_committed_vectors_match_the_generator():
    keys, receipts = generate.build_documents()
    assert json.loads((HERE / "keys.json").read_text()) == keys
    assert json.loads((HERE / "receipts.json").read_text()) == receipts


def test_reference_verifier_matches_every_committed_case():
    default_keys = key_set_from_document(json.loads((HERE / "keys.json").read_text()))
    for case in _load_cases():
        keys = key_set_from_document(case["keys"]) if "keys" in case else default_keys
        status = verify_bundle(case["bundle"], keys).status.value
        assert status == case["expect"], case["name"]


def test_verified_bundles_name_the_canonicalization():
    """Issued bundles carry the canonicalization the spec example shows."""
    for case in _load_cases():
        if case["expect"] != "verified":
            continue
        assert case["bundle"]["canonicalization"] == generate.CANONICALIZATION


def test_disabled_key_stays_in_the_document_and_is_not_a_verification_key():
    """A cached key set can still list a disabled key. It must not verify."""
    case = _by_name(_load_cases())["key_status.disabled_is_not_usable"]
    kids = [entry["kid"] for entry in case["keys"]["keys"]]
    assert case["bundle"]["kid"] in kids
    disabled = [entry for entry in case["keys"]["keys"] if entry["status"] == "disabled"]
    assert [entry["kid"] for entry in disabled] == [case["bundle"]["kid"]]
    assert case["bundle"]["kid"] not in key_set_from_document(case["keys"])


def test_retired_key_remains_a_verification_key():
    case = _by_name(_load_cases())["key_status.retired_still_verifies"]
    retired = [entry for entry in case["keys"]["keys"] if entry["status"] == "retired"]
    assert len(retired) == 1
    assert retired[0]["kid"] in key_set_from_document(case["keys"])


def test_unsupported_alg_is_inside_the_signed_bytes_not_the_envelope():
    """An envelope algorithm must not decide `unsupported` by itself."""
    case = _by_name(_load_cases())["signed.unsupported_alg"]
    signed = json.loads(case["bundle"]["signing_input"])
    assert signed["alg"] == "RS256"
    assert case["bundle"]["alg"] == "Ed25519"


def test_payload_hash_mismatch_is_a_signature_that_still_verifies():
    """The signature covers the lying hash. The hash check is separate."""
    case = _by_name(_load_cases())["signed.payload_hash_mismatch"]
    signed = json.loads(case["bundle"]["signing_input"])
    assert signed["payload_hash"] != generate.payload_hash(signed)
    public = Ed25519PrivateKey.from_private_bytes(generate.TEST_SEED).public_key()
    public.verify(
        base64.b64decode(case["bundle"]["signature"]),
        case["bundle"]["signing_input"].encode("utf-8"),
    )


def test_outcome_tamper_refuses_a_signing_input_with_no_single_outcome():
    for signing_input in ('{"alg":"Ed25519"}', '{"outcome":"a","note":"outcome":"b"}'):
        try:
            generate.tamper_outcome(signing_input)
        except ValueError:
            continue
        raise AssertionError(signing_input)


def test_signature_flip_changes_only_the_first_byte():
    original = bytes(range(64))
    flipped = generate.flip_signature(original)
    assert flipped[0] == original[0] ^ 0x01
    assert flipped[1:] == original[1:]
    try:
        generate.flip_signature(b"\x00" * 63)
    except ValueError:
        return
    raise AssertionError("a short signature must be refused")


def test_key_set_spec_tells_verifiers_to_keep_retired_keys():
    text = (ROOT / "spec" / "key-set-v0.1.md").read_text()
    assert "`retired`" in text
    assert "`disabled`" in text
    # The old rotation rule told verifiers to leave the previous key active.
    assert "keep the old key" not in text


def test_receipt_spec_treats_a_bad_payload_hash_as_mismatch():
    section = (ROOT / "spec" / "receipt-v0.1.md").read_text().split("## Verifier statuses", 1)[1]
    assert "payload_hash" in section
    assert "envelope" in section.lower() or "Envelope" in section


def test_null_members_stay_in_the_signed_bytes_and_missing_ones_are_omitted():
    denial = json.loads((RECEIPT_GOLDEN / "denial_constraints.json").read_text())
    denial_payload = json.loads(denial["signing_input"])
    assert denial_payload["ledger_entry_id"] is None
    assert denial_payload["response_hash"] is None

    legacy = json.loads((RECEIPT_GOLDEN / "success_legacy.json").read_text())
    legacy_payload = json.loads(legacy["signing_input"])
    assert "dispatch_attempt_id" not in legacy_payload
    assert "idempotency_record_id" not in legacy_payload

    unbound = json.loads((PERMIT_GOLDEN / "active_unbound_key.json").read_text())
    unbound_payload = json.loads(unbound["signing_input"])
    assert "subject_key_id" in unbound_payload
    assert unbound_payload["subject_key_id"] is None


def test_permit_goldens_stay_active_and_match_the_spec_example():
    """Revocation must not rewrite the signed status. The spec example is the minimal golden."""
    spec = (ROOT / "spec" / "permit-v0.1.md").read_text()
    start = spec.index("```json\n") + len("```json\n")
    example = spec[start : spec.index("\n```", start)]
    minimal = json.loads((PERMIT_GOLDEN / "active_minimal.json").read_text())
    assert example == minimal["signing_input"]

    private = Ed25519PrivateKey.from_private_bytes(generate.TEST_SEED)
    public = private.public_key()
    for path in sorted(PERMIT_GOLDEN.glob("*.json")):
        fixture = json.loads(path.read_text())
        payload = json.loads(fixture["signing_input"])
        assert payload["status"] == "active"
        assert generate.canonical_json(payload) == fixture["signing_input"]
        assert payload["payload_hash"] == generate.payload_hash(payload)
        public.verify(
            base64.b64decode(fixture["signature_b64_over_test_seed"]),
            fixture["signing_input"].encode("utf-8"),
        )
