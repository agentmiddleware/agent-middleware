"""Regenerate the receipt test vectors in this directory.

The signing key is derived from the fixed, public test seed bytes(range(32)).
It is NOT a production key and must never be used as one. The signed payloads
come from the gateway's golden fixtures (gateway/tests/fixtures), so a change to
the receipt signing input shows up here as a diff.

The generator refuses to write a suite when the golden directory is empty, when
a fixture signature does not match the test seed, or when an outcome tamper
would be ambiguous. Those failures used to be skipped or asserted, and an
assertion disappears under python -O.

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
# Second fixed seed, used only so one vector can carry a well formed raw key
# that is not the key that signed the receipt.
OTHER_SEED = bytes(range(32, 64))
KID = "sk_golden_0001"
BASE_FIXTURE = "success_legacy"


class VectorError(RuntimeError):
    """The generator cannot produce a trustworthy suite."""


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def signing_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(TEST_SEED)


def _public_raw(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _jwk(public_raw: bytes) -> dict[str, str]:
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": base64.urlsafe_b64encode(public_raw).decode("ascii").rstrip("="),
    }


def key_document(public_raw: bytes) -> dict:
    return {
        "_comment": (
            "Public half of the fixed test seed bytes(range(32)). Test use only."
        ),
        "keys": [
            {
                "kid": KID,
                "alg": "Ed25519",
                "status": "active",
                "public_key_b64": b64(public_raw),
                "jwk": _jwk(public_raw),
            }
        ],
    }


def render_keys(public_raw: bytes | None = None) -> str:
    raw = _public_raw(signing_key()) if public_raw is None else public_raw
    return json.dumps(key_document(raw), indent=2) + "\n"


def tamper_outcome(signing_input: str) -> str:
    """Change the signed outcome without re-serializing the payload.

    Re-serializing would rebuild payload_hash and could hide a byte-level
    edit. The replacement has to hit the one canonical outcome field, so a
    nested copy of the same text cannot be what we tamper by accident.
    """
    try:
        payload = json.loads(signing_input)
    except json.JSONDecodeError as exc:
        raise VectorError("signing input is not JSON") from exc
    if not isinstance(payload, dict):
        raise VectorError("signing input is not a JSON object")
    outcome = payload.get("outcome")
    if not isinstance(outcome, str) or outcome == "":
        raise VectorError("golden signing input has no string outcome to tamper")
    needle = f'"outcome":"{outcome}"'
    count = signing_input.count(needle)
    if count != 1:
        raise VectorError(
            "outcome field is not a single unambiguous canonical string "
            f"(found {count})"
        )
    tampered = signing_input.replace(needle, f'"outcome":"x{outcome}"', 1)
    parsed = json.loads(tampered)
    if parsed.get("outcome") != f"x{outcome}":
        raise VectorError("outcome tamper did not change the signed outcome")
    return tampered


def _with_hash(payload: dict) -> dict:
    body = {key: value for key, value in payload.items() if key != "payload_hash"}
    updated = dict(body)
    updated["payload_hash"] = hashlib.sha256(
        canonical_json(body).encode("utf-8")
    ).hexdigest()
    return updated


def sign_bundle(key: Ed25519PrivateKey, payload: dict, **envelope: object) -> dict:
    """Sign payload. The envelope kid stays the key that actually signed.

    A caller that needs the envelope to disagree passes that field in
    envelope, for example alg="RS256".
    """
    signing_input = canonical_json(payload)
    signature = b64(key.sign(signing_input.encode("utf-8")))
    bundle = {
        "receipt_id": payload["receipt_id"],
        "kid": KID,
        "alg": "Ed25519",
        # Unauthenticated envelope member the gateway's portable receipt
        # endpoint also emits; the signature covers only signing_input.
        # The spec conformance gate requires it on every well-formed bundle.
        "canonicalization": "awi-canonical-json/1",
        "signing_input": signing_input,
        "signature": signature,
    }
    bundle.update(envelope)
    return bundle


def _key_entry(
    *,
    public_key_b64: str | None,
    jwk: dict[str, str] | None,
    status: str = "active",
) -> dict:
    entry: dict = {"kid": KID, "alg": "Ed25519", "status": status}
    if public_key_b64 is not None:
        entry["public_key_b64"] = public_key_b64
    if jwk is not None:
        entry["jwk"] = jwk
    return entry


def cases_for_fixture(path: Path, key: Ed25519PrivateKey) -> list[dict]:
    fixture = json.loads(path.read_text())
    signing_input = fixture["signing_input"]
    raw_signature = key.sign(signing_input.encode("utf-8"))
    signature = b64(raw_signature)
    if signature != fixture.get("signature_b64_over_test_seed"):
        raise VectorError(f"{path.name} signature does not match the fixed test seed")
    payload = json.loads(signing_input)
    if not isinstance(payload, dict):
        raise VectorError(f"{path.name} signing_input is not a JSON object")
    if canonical_json(payload) != signing_input:
        raise VectorError(f"{path.name} signing_input is not canonical JSON")

    bundle = {
        "receipt_id": payload["receipt_id"],
        "kid": payload["kid"],
        "alg": "Ed25519",
        # Same unauthenticated envelope member as sign_bundle: the portable
        # receipt endpoint emits it and the spec conformance gate requires it
        # on every well-formed bundle.
        "canonicalization": "awi-canonical-json/1",
        "signing_input": signing_input,
        "signature": signature,
    }
    name = path.stem
    flipped = bytearray(raw_signature)
    flipped[0] ^= 0x01
    tampered_payload = tamper_outcome(signing_input)
    return [
        {"name": f"{name}.valid", "bundle": bundle, "expect": "verified"},
        {
            "name": f"{name}.bad_signature",
            "bundle": dict(bundle, signature=b64(bytes(flipped))),
            "expect": "invalid",
        },
        {
            "name": f"{name}.tampered_payload",
            "bundle": dict(bundle, signing_input=tampered_payload),
            "expect": "invalid",
        },
        {
            "name": f"{name}.envelope_receipt_id_mismatch",
            "bundle": dict(bundle, receipt_id="rcpt_not_the_signed_one"),
            "expect": "mismatch",
        },
        {
            "name": f"{name}.unknown_key",
            "bundle": dict(bundle),
            "keys": {"keys": []},
            "expect": "unknown_key",
        },
    ]


def _synthetic_cases(
    base_bundle: dict, key: Ed25519PrivateKey, real_raw: bytes
) -> list[dict]:
    payload = json.loads(base_bundle["signing_input"])
    real_b64 = b64(real_raw)
    real_jwk = _jwk(real_raw)
    other_raw = _public_raw(Ed25519PrivateKey.from_private_bytes(OTHER_SEED))
    if other_raw == real_raw or len(other_raw) != 32:
        raise VectorError("the disagreeing test key must not match the signing key")

    kid_mismatch = dict(base_bundle)
    kid_mismatch["kid"] = "sk_envelope_only"
    alg_mismatch = dict(base_bundle)
    alg_mismatch["alg"] = "RS256"
    canon_mismatch = dict(base_bundle)
    canon_mismatch["canonicalization"] = "not-the-contract/9"

    lied = dict(payload)
    lied["payload_hash"] = "0" * 64
    unsupported_alg = _with_hash(dict(payload, alg="RS256"))
    unsupported_canon = _with_hash(dict(payload, canonicalization="other/9"))

    bad_signature = dict(base_bundle)
    bad_signature["signature"] = "!!!!"
    short_signature = dict(base_bundle)
    short_signature["signature"] = b64(b"\x00" * 32)
    bad_json = dict(base_bundle)
    bad_json["signing_input"] = "not json"
    not_object = dict(base_bundle)
    not_object["signing_input"] = "[1,2]"
    missing_kid = dict(base_bundle)
    del missing_kid["kid"]
    missing_input = dict(base_bundle)
    del missing_input["signing_input"]

    return [
        {
            "name": "synthetic.disabled_key",
            "bundle": dict(base_bundle),
            "keys": {
                "keys": [
                    _key_entry(
                        public_key_b64=real_b64,
                        jwk=real_jwk,
                        status="disabled",
                    )
                ]
            },
            "expect": "unknown_key",
        },
        {
            "name": "synthetic.envelope_kid_mismatch",
            "bundle": kid_mismatch,
            "expect": "mismatch",
        },
        {
            "name": "synthetic.envelope_alg_mismatch",
            "bundle": alg_mismatch,
            "expect": "mismatch",
        },
        {
            "name": "synthetic.envelope_canonicalization_mismatch",
            "bundle": canon_mismatch,
            "expect": "mismatch",
        },
        {
            "name": "synthetic.payload_hash_mismatch",
            "bundle": sign_bundle(key, lied),
            "expect": "mismatch",
        },
        {
            "name": "synthetic.jwk_only_key",
            "bundle": dict(base_bundle),
            "keys": {"keys": [_key_entry(public_key_b64=None, jwk=real_jwk)]},
            "expect": "verified",
        },
        {
            "name": "synthetic.raw_key_beats_jwk",
            "bundle": dict(base_bundle),
            "keys": {"keys": [_key_entry(public_key_b64=b64(other_raw), jwk=real_jwk)]},
            "expect": "invalid",
        },
        {
            "name": "synthetic.short_raw_key_uses_jwk",
            "bundle": dict(base_bundle),
            "keys": {"keys": [_key_entry(public_key_b64="AAAA", jwk=real_jwk)]},
            "expect": "verified",
        },
        {
            "name": "synthetic.unsupported_alg",
            "bundle": sign_bundle(key, unsupported_alg, alg="RS256"),
            "expect": "unsupported",
        },
        {
            "name": "synthetic.unsupported_canonicalization",
            "bundle": sign_bundle(key, unsupported_canon, canonicalization="other/9"),
            "expect": "unsupported",
        },
        {
            "name": "synthetic.malformed_signature",
            "bundle": bad_signature,
            "expect": "malformed",
        },
        {
            "name": "synthetic.malformed_signature_length",
            "bundle": short_signature,
            "expect": "malformed",
        },
        {
            "name": "synthetic.malformed_signing_input",
            "bundle": bad_json,
            "expect": "malformed",
        },
        {
            "name": "synthetic.malformed_signing_input_not_object",
            "bundle": not_object,
            "expect": "malformed",
        },
        {
            "name": "synthetic.malformed_missing_kid",
            "bundle": missing_kid,
            "expect": "malformed",
        },
        {
            "name": "synthetic.malformed_missing_signing_input",
            "bundle": missing_input,
            "expect": "malformed",
        },
    ]


def build_cases(
    golden_dir: Path | None = None, key: Ed25519PrivateKey | None = None
) -> list[dict]:
    directory = GOLDEN if golden_dir is None else Path(golden_dir)
    paths = sorted(directory.glob("*.json"))
    if not paths:
        raise VectorError(f"no golden receipt fixtures in {directory}")
    signing = signing_key() if key is None else key
    cases: list[dict] = []
    base_bundle = None
    for path in paths:
        produced = cases_for_fixture(path, signing)
        cases.extend(produced)
        if path.stem == BASE_FIXTURE:
            base_bundle = produced[0]["bundle"]
    if base_bundle is None:
        raise VectorError(
            f"{BASE_FIXTURE} fixture is required to build the synthetic cases"
        )
    cases.extend(_synthetic_cases(base_bundle, signing, _public_raw(signing)))
    cases.append(
        {
            "name": "malformed.no_signature",
            "bundle": {"receipt_id": "r", "kid": KID, "signing_input": "{}"},
            "expect": "malformed",
        }
    )
    return cases


def render_receipts(key: Ed25519PrivateKey | None = None) -> str:
    signing = signing_key() if key is None else key
    document = {
        "_comment": (
            "Receipt verification vectors. Each case is checked against keys.json "
            "unless it carries its own 'keys'. 'expect' is the verifier status. "
            "Synthetic cases cover a revoked key, envelope fields that disagree "
            "with the signed payload, a signed payload_hash that does not match, "
            "an unsupported algorithm, and malformed bundles."
        ),
        "cases": build_cases(key=signing),
    }
    return json.dumps(document, indent=2) + "\n"


def main() -> None:
    key = signing_key()
    # Build both documents before replacing either file, so a failure cannot
    # leave a new key set next to the previous receipt vectors.
    keys_text = render_keys(_public_raw(key))
    receipts_text = render_receipts(key)
    (HERE / "keys.json").write_text(keys_text)
    (HERE / "receipts.json").write_text(receipts_text)
    print(f"wrote {len(json.loads(receipts_text)['cases'])} cases")


if __name__ == "__main__":
    main()
