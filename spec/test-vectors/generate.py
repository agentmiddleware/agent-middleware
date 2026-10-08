"""Regenerate the receipt test vectors in this directory.

The signing key is derived from the fixed, public test seed bytes(range(32)).
It is NOT a production key and must never be used as one. The signed payloads
come from the gateway's golden fixtures (gateway/tests/fixtures), so a change to
the receipt signing input shows up here as a diff.

Usage (from the repository root):
    python spec/test-vectors/generate.py
"""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
GOLDEN = ROOT / "gateway" / "tests" / "fixtures" / "receipt_signing_golden"
TEST_SEED = bytes(range(32))
KID = "sk_golden_0001"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def main() -> None:
    key = Ed25519PrivateKey.from_private_bytes(TEST_SEED)
    public_raw = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    keys = {
        "_comment": "Public half of the fixed test seed bytes(range(32)). Test use only.",
        "keys": [
            {
                "kid": KID,
                "alg": "Ed25519",
                "status": "active",
                "public_key_b64": b64(public_raw),
                "jwk": {
                    "kty": "OKP",
                    "crv": "Ed25519",
                    "x": base64.urlsafe_b64encode(public_raw).decode("ascii").rstrip("="),
                },
            }
        ],
    }
    (HERE / "keys.json").write_text(json.dumps(keys, indent=2) + "\n")

    cases = []
    for golden in sorted(GOLDEN.glob("*.json")):
        fixture = json.loads(golden.read_text())
        signing_input = fixture["signing_input"]
        signature = key.sign(signing_input.encode("utf-8"))
        assert b64(signature) == fixture["signature_b64_over_test_seed"], golden.name
        payload = json.loads(signing_input)
        bundle = {
            "receipt_id": payload["receipt_id"],
            "kid": payload["kid"],
            "alg": "Ed25519",
            "signing_input": signing_input,
            "signature": b64(signature),
        }
        name = golden.stem
        cases.append({"name": f"{name}.valid", "bundle": bundle, "expect": "verified"})

        flipped = bytearray(signature)
        flipped[0] ^= 0x01
        bad_sig = dict(bundle, signature=b64(bytes(flipped)))
        cases.append({"name": f"{name}.bad_signature", "bundle": bad_sig, "expect": "invalid"})

        tampered_payload = signing_input.replace('"outcome":"', '"outcome":"x', 1)
        if tampered_payload != signing_input:
            cases.append({
                "name": f"{name}.tampered_payload",
                "bundle": dict(bundle, signing_input=tampered_payload),
                "expect": "invalid",
            })

        cases.append({
            "name": f"{name}.envelope_receipt_id_mismatch",
            "bundle": dict(bundle, receipt_id="rcpt_not_the_signed_one"),
            "expect": "mismatch",
        })

        unknown = copy.deepcopy(bundle)
        cases.append({
            "name": f"{name}.unknown_key",
            "bundle": unknown,
            "keys": {"keys": []},
            "expect": "unknown_key",
        })

    cases.append({
        "name": "malformed.no_signature",
        "bundle": {"receipt_id": "r", "kid": KID, "signing_input": "{}"},
        "expect": "malformed",
    })

    # alg and canonicalization are only trusted after the signature matches.
    # Editing them in an otherwise valid bundle must stay "invalid", not
    # "unsupported". A bundle the test key really signed with a different
    # claim stays "unsupported".
    signed = next(case["bundle"] for case in cases if case["name"].endswith(".valid"))
    flipped_alg = signed["signing_input"].replace('"alg":"Ed25519"', '"alg":"RS256"', 1)
    if flipped_alg == signed["signing_input"]:
        raise SystemExit("vector generator found no alg field to edit")
    cases.append({
        "name": "downgrade.alg_edited",
        "bundle": dict(signed, signing_input=flipped_alg),
        "expect": "invalid",
    })
    flipped_canon = signed["signing_input"].replace(
        '"alg":"Ed25519"',
        '"alg":"Ed25519","canonicalization":"other/9"',
        1,
    )
    cases.append({
        "name": "downgrade.canonicalization_edited",
        "bundle": dict(signed, signing_input=flipped_canon),
        "expect": "invalid",
    })
    declared = json.loads(signed["signing_input"])
    declared["alg"] = "RS256"
    declared_input = json.dumps(
        declared, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    cases.append({
        "name": "downgrade.signer_declared_rs256",
        "bundle": dict(
            signed,
            signing_input=declared_input,
            signature=b64(key.sign(declared_input.encode("utf-8"))),
        ),
        "expect": "unsupported",
    })
    declared_canon = json.loads(signed["signing_input"])
    declared_canon["canonicalization"] = "other/9"
    declared_canon_input = json.dumps(
        declared_canon, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    cases.append({
        "name": "downgrade.signer_declared_canonicalization",
        "bundle": dict(
            signed,
            signing_input=declared_canon_input,
            signature=b64(key.sign(declared_canon_input.encode("utf-8"))),
        ),
        "expect": "unsupported",
    })
    out = {
        "_comment": (
            "Receipt verification vectors. Each case is checked against keys.json "
            "unless it carries its own 'keys'. 'expect' is the verifier status."
        ),
        "cases": cases,
    }
    (HERE / "receipts.json").write_text(json.dumps(out, indent=2) + "\n")
    print(f"wrote {len(cases)} cases")


if __name__ == "__main__":
    main()
