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
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
GOLDEN = ROOT / "gateway" / "tests" / "fixtures" / "receipt_signing_golden"
TEST_SEED = bytes(range(32))
KID = "sk_golden_0001"
CANONICALIZATION = "awi-canonical-json/1"
_OTHER_KID = "sk_other_0002"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def canonical_json(payload: dict) -> str:
    """awi-canonical-json/1 for objects that are already signer-normalized."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def payload_hash(payload: dict) -> str:
    """Lowercase hex SHA-256 of the canonical payload without payload_hash."""
    inner = {key: value for key, value in payload.items() if key != "payload_hash"}
    return hashlib.sha256(canonical_json(inner).encode("utf-8")).hexdigest()


def require_canonical_signed_input(signing_input: str, source: str) -> dict:
    payload = json.loads(signing_input)
    if not isinstance(payload, dict):
        raise TypeError(f"{source}: signing input is not a JSON object")
    if canonical_json(payload) != signing_input:
        raise ValueError(f"{source}: signing input is not canonical JSON")
    if payload.get("payload_hash") != payload_hash(payload):
        raise ValueError(f"{source}: payload_hash does not match the signed payload")
    if signing_input.count('"outcome":"') != 1:
        raise ValueError(f"{source}: signing input must contain one outcome field")
    return payload


def tamper_outcome(signing_input: str) -> str:
    """Change the outcome value without re-serializing the rest of the bytes."""
    needle = '"outcome":"'
    if signing_input.count(needle) != 1:
        raise ValueError("signing input must contain one outcome field")
    tampered = signing_input.replace(needle, '"outcome":"x', 1)
    json.loads(tampered)
    return tampered


def flip_signature(signature: bytes) -> bytes:
    """Flip one bit so the signature is still 64 bytes and no longer valid."""
    if len(signature) != 64:
        raise ValueError("Ed25519 signature must be 64 bytes")
    flipped = bytearray(signature)
    flipped[0] ^= 0x01
    return bytes(flipped)


def jwk_x(public_raw: bytes) -> str:
    return base64.urlsafe_b64encode(public_raw).decode("ascii").rstrip("=")


def key_entry(public_raw: bytes, *, kid: str = KID, status: str = "active") -> dict:
    return {
        "kid": kid,
        "alg": "Ed25519",
        "status": status,
        "public_key_b64": b64(public_raw),
        "jwk": {"kty": "OKP", "crv": "Ed25519", "x": jwk_x(public_raw)},
    }


def make_bundle(signing_input: str, signature: str) -> dict:
    payload = json.loads(signing_input)
    return {
        "receipt_id": payload["receipt_id"],
        "kid": payload["kid"],
        "alg": "Ed25519",
        "canonicalization": CANONICALIZATION,
        "signing_input": signing_input,
        "signature": signature,
    }


def _other_public(public_raw: bytes) -> bytes:
    other = bytes((byte + 1) % 256 for byte in public_raw)
    if other == public_raw or len(other) != 32:
        raise ValueError("could not build a distinct public key")
    return other


def _extra_cases(
    key: Ed25519PrivateKey,
    public_raw: bytes,
    valid: dict,
    signing_input: str,
    signature_b64: str,
) -> list[dict]:
    """Cases the per-fixture loop does not cover.

    A verifier that only checks a good signature, or that treats every
    published key as usable, still passes the per-fixture rows. These rows
    are the ones that separate those verifiers from the spec.
    """
    other_raw = _other_public(public_raw)
    payload = json.loads(signing_input)

    lying_hash = dict(payload)
    lying_hash["payload_hash"] = "0" * 64
    lying_input = canonical_json(lying_hash)
    lying_signature = b64(key.sign(lying_input.encode("utf-8")))

    unsupported_alg = dict(payload, alg="RS256")
    unsupported_canon = dict(payload, canonicalization="other-json/9")

    jwk_only = key_entry(public_raw)
    del jwk_only["public_key_b64"]

    disagreeing = key_entry(public_raw)
    disagreeing["public_key_b64"] = b64(other_raw)

    retired_keys = {
        "keys": [
            key_entry(public_raw, status="retired"),
            key_entry(other_raw, kid=_OTHER_KID, status="active"),
        ]
    }
    disabled_keys = {
        "keys": [
            key_entry(public_raw, status="disabled"),
            key_entry(other_raw, kid=_OTHER_KID, status="active"),
        ]
    }

    return [
        {
            "name": "key_status.retired_still_verifies",
            "bundle": dict(valid),
            "keys": retired_keys,
            "expect": "verified",
        },
        {
            "name": "key_status.disabled_is_not_usable",
            "bundle": dict(valid),
            "keys": disabled_keys,
            "expect": "unknown_key",
        },
        {
            "name": "key_form.jwk_only",
            "bundle": dict(valid),
            "keys": {"keys": [jwk_only]},
            "expect": "verified",
        },
        {
            "name": "key_form.raw_key_beats_disagreeing_jwk",
            "bundle": dict(valid),
            "keys": {"keys": [disagreeing]},
            "expect": "invalid",
        },
        {
            "name": "signed.payload_hash_mismatch",
            "bundle": make_bundle(lying_input, lying_signature),
            "expect": "mismatch",
        },
        {
            "name": "signed.unsupported_alg",
            "bundle": make_bundle(canonical_json(unsupported_alg), signature_b64),
            "expect": "unsupported",
        },
        {
            "name": "signed.unsupported_canonicalization",
            "bundle": make_bundle(canonical_json(unsupported_canon), signature_b64),
            "expect": "unsupported",
        },
        {
            "name": "envelope.kid_mismatch",
            "bundle": dict(valid, kid="sk_not_the_signed_one"),
            "expect": "mismatch",
        },
        {
            "name": "envelope.alg_mismatch",
            "bundle": dict(valid, alg="RS256"),
            "expect": "mismatch",
        },
        {
            "name": "envelope.canonicalization_mismatch",
            "bundle": dict(valid, canonicalization="other-json/9"),
            "expect": "mismatch",
        },
        {
            "name": "malformed.bad_json",
            "bundle": {
                "receipt_id": "r",
                "kid": KID,
                "alg": "Ed25519",
                "canonicalization": CANONICALIZATION,
                "signing_input": "{",
                "signature": signature_b64,
            },
            "expect": "malformed",
        },
        {
            "name": "malformed.short_signature",
            "bundle": dict(valid, signature=b64(b"\x00" * 32)),
            "expect": "malformed",
        },
    ]


def build_documents() -> tuple[dict, dict]:
    """Return (keys document, receipts document) without writing them."""
    key = Ed25519PrivateKey.from_private_bytes(TEST_SEED)
    public_raw = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    keys = {
        "_comment": (
            "Public half of the fixed test seed bytes(range(32)). Test use only."
        ),
        "keys": [key_entry(public_raw)],
    }

    cases: list[dict] = []
    first: tuple[dict, str, str] | None = None
    for golden in sorted(GOLDEN.glob("*.json")):
        fixture = json.loads(golden.read_text())
        signing_input = fixture["signing_input"]
        require_canonical_signed_input(signing_input, golden.name)
        signature = key.sign(signing_input.encode("utf-8"))
        signature_b64 = b64(signature)
        if signature_b64 != fixture["signature_b64_over_test_seed"]:
            raise ValueError(f"{golden.name}: signature does not match the test seed")
        bundle = make_bundle(signing_input, signature_b64)
        if first is None:
            first = (bundle, signing_input, signature_b64)
        name = golden.stem
        cases.append({"name": f"{name}.valid", "bundle": bundle, "expect": "verified"})
        cases.append(
            {
                "name": f"{name}.bad_signature",
                "bundle": dict(bundle, signature=b64(flip_signature(signature))),
                "expect": "invalid",
            }
        )
        cases.append(
            {
                "name": f"{name}.tampered_payload",
                "bundle": dict(bundle, signing_input=tamper_outcome(signing_input)),
                "expect": "invalid",
            }
        )
        cases.append(
            {
                "name": f"{name}.envelope_receipt_id_mismatch",
                "bundle": dict(bundle, receipt_id="rcpt_not_the_signed_one"),
                "expect": "mismatch",
            }
        )
        cases.append(
            {
                "name": f"{name}.unknown_key",
                "bundle": dict(bundle),
                "keys": {"keys": []},
                "expect": "unknown_key",
            }
        )

    if first is None:
        raise ValueError(f"no receipt fixtures in {GOLDEN}")
    cases.extend(_extra_cases(key, public_raw, *first))
    cases.append(
        {
            "name": "malformed.no_signature",
            "bundle": {"receipt_id": "r", "kid": KID, "signing_input": "{}"},
            "expect": "malformed",
        }
    )
    names = [case["name"] for case in cases]
    if len(names) != len(set(names)):
        raise ValueError("duplicate vector name")

    receipts = {
        "_comment": (
            "Receipt verification vectors. Each case is checked against keys.json "
            "unless it carries its own 'keys'. 'expect' is the verifier status."
        ),
        "cases": cases,
    }
    return keys, receipts


def main() -> None:
    keys, receipts = build_documents()
    (HERE / "keys.json").write_text(json.dumps(keys, indent=2) + "\n")
    (HERE / "receipts.json").write_text(json.dumps(receipts, indent=2) + "\n")
    print(f"wrote {len(receipts['cases'])} cases")


if __name__ == "__main__":
    main()
