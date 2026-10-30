"""Local checks for the published standalone receipt verifier."""

import importlib.util
import json
from pathlib import Path

import pytest

PROOF = Path(__file__).resolve().parent / "proof"
spec = importlib.util.spec_from_file_location(
    "site_verify_receipt", PROOF / "verify_receipt.py"
)
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


@pytest.mark.parametrize("claim", ["alg", "canonicalization"])
def test_edited_unsupported_claim_is_invalid_before_capability_verdict(claim):
    bundle = json.loads((PROOF / "receipt.json").read_text(encoding="utf-8"))
    keys = verifier.key_set_from_document(
        json.loads((PROOF / "trust-keys.json").read_text(encoding="utf-8"))
    )
    assert verifier.verify_bundle(bundle, keys)[0] == "verified"

    claims = json.loads(bundle["signing_input"])
    claims[claim] = "unsupported"
    bundle["signing_input"] = verifier.canonical_json(claims)
    assert verifier.verify_bundle(bundle, keys)[0] == "invalid"


@pytest.mark.parametrize("input_name", ["bundle", "keys"])
def test_deep_input_json_is_undetermined(tmp_path, input_name):
    deeply_nested = tmp_path / "deep.json"
    deeply_nested.write_text("[" * 1100 + "0" + "]" * 1100, encoding="utf-8")
    paths = {
        "bundle": PROOF / "receipt.json",
        "keys": PROOF / "trust-keys.json",
    }
    paths[input_name] = deeply_nested
    assert (
        verifier.main(["--bundle", str(paths["bundle"]), "--keys", str(paths["keys"])])
        == verifier.EXIT_UNDETERMINED
    )
