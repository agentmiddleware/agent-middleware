"""Conformance checks for the published receipt vectors and their generator.

The vectors in this directory are how another implementation proves it
judges receipts the same way the reference verifier does. A gap here
certifies a verifier that skips the check.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
SDK_SRC = REPO / "sdk" / "python" / "src"


def _verifier():
    if str(SDK_SRC) not in sys.path:
        sys.path.insert(0, str(SDK_SRC))
    from b2a_sdk.receipt_verifier import key_set_from_document, verify_bundle

    return key_set_from_document, verify_bundle

# Cases the suite already published before this pass. Their expected status
# must stay put when new cases are added.
ORIGINAL_EXPECTATIONS = {
    "denial_constraints.valid": "verified",
    "denial_constraints.bad_signature": "invalid",
    "denial_constraints.tampered_payload": "invalid",
    "denial_constraints.envelope_receipt_id_mismatch": "mismatch",
    "denial_constraints.unknown_key": "unknown_key",
    "success_action_v1.valid": "verified",
    "success_action_v1.bad_signature": "invalid",
    "success_action_v1.tampered_payload": "invalid",
    "success_action_v1.envelope_receipt_id_mismatch": "mismatch",
    "success_action_v1.unknown_key": "unknown_key",
    "success_approval.valid": "verified",
    "success_approval.bad_signature": "invalid",
    "success_approval.tampered_payload": "invalid",
    "success_approval.envelope_receipt_id_mismatch": "mismatch",
    "success_approval.unknown_key": "unknown_key",
    "success_legacy.valid": "verified",
    "success_legacy.bad_signature": "invalid",
    "success_legacy.tampered_payload": "invalid",
    "success_legacy.envelope_receipt_id_mismatch": "mismatch",
    "success_legacy.unknown_key": "unknown_key",
    "success_legacy_approval.valid": "verified",
    "success_legacy_approval.bad_signature": "invalid",
    "success_legacy_approval.tampered_payload": "invalid",
    "success_legacy_approval.envelope_receipt_id_mismatch": "mismatch",
    "success_legacy_approval.unknown_key": "unknown_key",
    "success_linked.valid": "verified",
    "success_linked.bad_signature": "invalid",
    "success_linked.tampered_payload": "invalid",
    "success_linked.envelope_receipt_id_mismatch": "mismatch",
    "success_linked.unknown_key": "unknown_key",
    "malformed.no_signature": "malformed",
}

# Statuses a verifier has to get right even though no golden fixture produces
# them on its own. Missing any of these leaves a hole in the shared suite.
REQUIRED_EDGES = {
    "synthetic.disabled_key": "unknown_key",
    "synthetic.envelope_kid_mismatch": "mismatch",
    "synthetic.envelope_alg_mismatch": "mismatch",
    "synthetic.envelope_canonicalization_mismatch": "mismatch",
    "synthetic.payload_hash_mismatch": "mismatch",
    "synthetic.jwk_only_key": "verified",
    "synthetic.raw_key_beats_jwk": "invalid",
    "synthetic.short_raw_key_uses_jwk": "verified",
    "synthetic.unsupported_alg": "unsupported",
    "synthetic.unsupported_canonicalization": "unsupported",
    "synthetic.malformed_signature": "malformed",
    "synthetic.malformed_signature_length": "malformed",
    "synthetic.malformed_signing_input": "malformed",
    "synthetic.malformed_signing_input_not_object": "malformed",
    "synthetic.malformed_missing_kid": "malformed",
    "synthetic.malformed_missing_signing_input": "malformed",
}


def load_generate():
    path = HERE / "generate.py"
    spec = importlib.util.spec_from_file_location("receipt_vector_generate", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # importlib sets this from the spec, but pin it so GOLDEN resolves even
    # if a future loader skips that step.
    module.__file__ = str(path)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generated():
    module = load_generate()
    receipts = json.loads(module.render_receipts())
    keys = json.loads(module.render_keys())
    return module, receipts, keys


def _by_name(receipts: dict) -> dict:
    return {case["name"]: case for case in receipts["cases"]}


def test_published_file_keeps_original_cases_and_adds_the_edges():
    published = json.loads((HERE / "receipts.json").read_text())
    found = {case["name"]: case["expect"] for case in published["cases"]}
    for name, expect in ORIGINAL_EXPECTATIONS.items():
        assert found.get(name) == expect, name
    missing = {
        name: expect
        for name, expect in REQUIRED_EDGES.items()
        if found.get(name) != expect
    }
    assert missing == {}


def test_generator_output_matches_the_published_files():
    module = load_generate()
    assert module.render_keys() == (HERE / "keys.json").read_text()
    assert module.render_receipts() == (HERE / "receipts.json").read_text()


def test_reference_verifier_agrees_with_every_generated_case(generated):
    key_set_from_document, verify_bundle = _verifier()
    _module, receipts, keys = generated
    default_keys = key_set_from_document(keys)
    for case in receipts["cases"]:
        case_keys = (
            key_set_from_document(case["keys"]) if "keys" in case else default_keys
        )
        status = verify_bundle(case["bundle"], case_keys).status.value
        assert status == case["expect"], case["name"]


def test_disabled_key_still_holds_the_real_bytes_but_does_not_verify(generated):
    """A cached key document must not keep a revoked key working."""
    key_set_from_document, verify_bundle = _verifier()
    _module, receipts, keys = generated
    case = _by_name(receipts)["synthetic.disabled_key"]
    published_raw = keys["keys"][0]["public_key_b64"]
    entry = case["keys"]["keys"][0]
    assert entry["status"] == "disabled"
    assert entry["kid"] == keys["keys"][0]["kid"]
    assert entry["public_key_b64"] == published_raw
    assert entry["jwk"] == keys["keys"][0]["jwk"]
    assert key_set_from_document(case["keys"]) == {}

    active = key_set_from_document(keys)
    assert verify_bundle(case["bundle"], active).status.value == "verified"
    assert verify_bundle(case["bundle"], {}).status.value == "unknown_key"


def test_wrong_raw_key_is_not_rescued_by_a_matching_jwk(generated):
    key_set_from_document, verify_bundle = _verifier()
    _module, receipts, _keys = generated
    case = _by_name(receipts)["synthetic.raw_key_beats_jwk"]
    judged = verify_bundle(case["bundle"], key_set_from_document(case["keys"]))
    assert judged.status.value == "invalid"

    entry = dict(case["keys"]["keys"][0])
    del entry["public_key_b64"]
    jwk_only = key_set_from_document({"keys": [entry]})
    assert jwk_only, "the JWK mirror on this case has to be the real signing key"
    rescued = verify_bundle(case["bundle"], jwk_only)
    assert rescued.status.value == "verified"


def test_payload_hash_lie_and_kid_downgrade_are_rejected(generated):
    key_set_from_document, verify_bundle = _verifier()
    _module, receipts, keys = generated
    active = key_set_from_document(keys)
    by_name = _by_name(receipts)

    lied = verify_bundle(by_name["synthetic.payload_hash_mismatch"]["bundle"], active)
    assert lied.status.value == "mismatch"

def test_outcome_tamper_only_prefixes_the_signed_outcome(generated):
    module, receipts, _keys = generated
    by_name = _by_name(receipts)
    for name, case in by_name.items():
        if not name.endswith(".tampered_payload"):
            continue
        valid = by_name[name.replace(".tampered_payload", ".valid")]
        original = valid["bundle"]["signing_input"]
        tampered = case["bundle"]["signing_input"]
        assert tampered == module.tamper_outcome(original)
        assert case["bundle"]["signature"] == valid["bundle"]["signature"]
        before = json.loads(original)
        after = json.loads(tampered)
        assert after["outcome"] == "x" + before["outcome"]
        after["outcome"] = before["outcome"]
        assert after == before


def test_outcome_tamper_refuses_a_payload_with_no_string_outcome():
    module = load_generate()
    with pytest.raises(module.VectorError, match="no string outcome"):
        module.tamper_outcome('{"outcome":null,"kid":"sk_golden_0001"}')


def test_outcome_tamper_refuses_an_ambiguous_outcome():
    module = load_generate()
    signing = '{"nested":{"outcome":"success"},"outcome":"success"}'
    with pytest.raises(module.VectorError, match="not a single"):
        module.tamper_outcome(signing)


def test_empty_fixture_directory_is_refused(tmp_path):
    module = load_generate()
    with pytest.raises(module.VectorError, match="no golden"):
        module.build_cases(tmp_path)
    with pytest.raises(module.VectorError, match="no golden"):
        module.build_cases(tmp_path / "missing")


def test_fixture_signature_mismatch_is_refused(tmp_path):
    module = load_generate()
    source = json.loads((module.GOLDEN / "success_legacy.json").read_text())
    source["signature_b64_over_test_seed"] = "AAAA"
    path = tmp_path / "success_legacy.json"
    path.write_text(json.dumps(source))
    with pytest.raises(module.VectorError, match="does not match"):
        module.cases_for_fixture(path, module.signing_key())
