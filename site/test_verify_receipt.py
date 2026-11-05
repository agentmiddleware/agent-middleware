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


def test_pre_jcs_float_bundle_verifies_despite_jcs_hash_divergence():
    """A genuinely signed pre-JCS signing_input must verify after the JCS cutover.

    The embedded payload_hash was produced by json.dumps; recomputing it with
    JCS diverges for floats / non-ASCII, but the signature already covers the
    transmitted bytes so the verifier must not reject on that advisory field.
    """
    import base64
    import hashlib
    import json

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    private = Ed25519PrivateKey.generate()
    public_b64 = base64.b64encode(
        private.public_key().public_bytes(
            encoding=Encoding.Raw,
            format=PublicFormat.Raw,
        )
    ).decode()

    def legacy_dumps(payload: dict) -> str:
        return json.dumps(
            payload, separators=(",", ":"), sort_keys=True, ensure_ascii=True
        )

    inner = {
        "receipt_id": "rcpt_pre_jcs_float",
        "amount": 1.0,
        "note": "café",
        "alg": "Ed25519",
        "kid": "legacy-kid",
    }
    inner["payload_hash"] = hashlib.sha256(legacy_dumps(inner).encode()).hexdigest()
    signing_input = legacy_dumps(inner)
    # Confirm JCS digest of the inner tree (minus hash) diverges from stated.
    without_hash = {k: v for k, v in inner.items() if k != "payload_hash"}
    jcs_digest = hashlib.sha256(
        verifier.canonical_json(without_hash).encode()
    ).hexdigest()
    assert jcs_digest != inner["payload_hash"]

    signature = base64.b64encode(private.sign(signing_input.encode())).decode()
    bundle = {
        "receipt_id": "rcpt_pre_jcs_float",
        "kid": "legacy-kid",
        "alg": "Ed25519",
        "canonicalization": "awi-canonical-json/1",
        "signing_input": signing_input,
        "signature": signature,
    }
    keys = {"legacy-kid": base64.b64decode(public_b64)}
    status, reason, receipt_id, key_id, _ = verifier.verify_bundle(bundle, keys)
    assert (status, reason, receipt_id, key_id) == (
        "verified",
        None,
        "rcpt_pre_jcs_float",
        "legacy-kid",
    )
