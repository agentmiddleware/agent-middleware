"""Behavior the spec vectors must keep true.

The committed JSON is generated. A case that is missing here means a verifier
can ignore key retirement, a disabled key, or a lying envelope and still pass.

Case names follow the generator's synthetic.* vocabulary; the expectations
are the contract independent verifiers must match.
"""

from __future__ import annotations

import json
from pathlib import Path

import generate
from b2a_sdk.receipt_verifier import key_set_from_document, verify_bundle

HERE = Path(__file__).resolve().parent

# Statuses a correct verifier returns for the cases the generator adds on top
# of the per-fixture valid, bad-signature, tamper, and unknown-key rows.
REQUIRED_CASES = {
    "synthetic.retired_key_still_verifies": "verified",
    "synthetic.disabled_key": "unknown_key",
    "synthetic.jwk_only_key": "verified",
    "synthetic.raw_key_beats_jwk": "invalid",
    "synthetic.payload_hash_mismatch": "mismatch",
    "synthetic.unsupported_alg": "unsupported",
    "synthetic.unsupported_canonicalization": "unsupported",
    "synthetic.envelope_kid_mismatch": "mismatch",
    "synthetic.envelope_alg_mismatch": "mismatch",
    "synthetic.envelope_canonicalization_mismatch": "mismatch",
    "synthetic.malformed_signing_input": "malformed",
    "synthetic.malformed_signature_length": "malformed",
    "synthetic.malformed_missing_signature": "malformed",
}


def _load_cases() -> list[dict]:
    return json.loads((HERE / "receipts.json").read_text())["cases"]


def _by_name(cases: list[dict]) -> dict[str, dict]:
    found = {case["name"]: case for case in cases}
    assert len(found) == len(cases)
    return found


def test_generator_emits_key_lifecycle_and_envelope_cases():
    receipts = json.loads(generate.render_receipts())
    found = _by_name(receipts["cases"])
    for name, expect in REQUIRED_CASES.items():
        assert name in found, name
        assert found[name]["expect"] == expect


def test_committed_vectors_match_the_generator():
    assert json.loads((HERE / "keys.json").read_text()) == json.loads(
        generate.render_keys()
    )
    assert json.loads((HERE / "receipts.json").read_text()) == json.loads(
        generate.render_receipts()
    )


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
    case = _by_name(_load_cases())["synthetic.disabled_key"]
    kids = [entry["kid"] for entry in case["keys"]["keys"]]
    assert case["bundle"]["kid"] in kids
    disabled = [entry for entry in case["keys"]["keys"] if entry["status"] == "disabled"]
    assert [entry["kid"] for entry in disabled] == [case["bundle"]["kid"]]
    assert case["bundle"]["kid"] not in key_set_from_document(case["keys"])


def test_retired_key_remains_a_verification_key():
    case = _by_name(_load_cases())["synthetic.retired_key_still_verifies"]
    retired = [entry for entry in case["keys"]["keys"] if entry["status"] == "retired"]
    assert len(retired) == 1
    assert retired[0]["kid"] in key_set_from_document(case["keys"])


def test_unsupported_alg_is_inside_the_signed_bytes_not_the_envelope():
    """An envelope algorithm must not decide `unsupported` by itself."""
    case = _by_name(_load_cases())["synthetic.unsupported_alg"]
    signed = json.loads(case["bundle"]["signing_input"])
    assert signed["alg"] == "RS256"
    assert case["bundle"]["alg"] == "Ed25519"


def test_payload_hash_mismatch_is_a_signature_that_still_verifies():
    """The signature covers the lying hash. The hash check is separate."""
    case = _by_name(_load_cases())["synthetic.payload_hash_mismatch"]
    signed = json.loads(case["bundle"]["signing_input"])
    assert signed["payload_hash"] != generate.payload_hash(signed)
