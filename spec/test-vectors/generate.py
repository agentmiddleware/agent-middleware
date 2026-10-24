"""Regenerate the receipt and permit test vectors in this directory.

The signing key is derived from the fixed, public test seed bytes(range(32)).
It is NOT a production key and must never be used as one. The signed payloads
come from the gateway's golden fixtures (gateway/tests/fixtures), so a change to
a signed payload shows up here as a diff.

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

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
GOLDEN = ROOT / "gateway" / "tests" / "fixtures" / "receipt_signing_golden"
PERMIT_GOLDEN = ROOT / "gateway" / "tests" / "fixtures" / "permit_signing_golden"
TEST_SEED = bytes(range(32))
# Second fixed seed, used only so one vector can carry a well formed raw key
# that is not the key that signed the receipt.
OTHER_SEED = bytes(range(32, 64))
DISABLED_SEED = bytes(range(64, 96))
KID = "sk_golden_0001"
ROTATION_KID = "sk_golden_0002"
DISABLED_KID = "sk_golden_0000"
# Envelope member the gateway's portable receipt endpoint also emits
# (PortableReceiptResponse.canonicalization). The signature covers only
# signing_input, so this member is unauthenticated envelope data.
CANONICALIZATION = "awi-canonical-json/1"
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


def _public_entry(key: Ed25519PrivateKey, kid: str, status: str) -> dict:
    raw = _public_raw(key)
    return {
        "kid": kid,
        "alg": "Ed25519",
        "status": status,
        "public_key_b64": b64(raw),
        "jwk": _jwk(raw),
    }


def key_document(public_raw: bytes) -> dict:
    return {
        "_comment": "Public half of the fixed test seed bytes(range(32)). Test use only.",
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


def payload_hash(payload: dict) -> str:
    """Lowercase hex SHA-256 of the canonical payload without payload_hash."""
    inner = {key: value for key, value in payload.items() if key != "payload_hash"}
    return hashlib.sha256(canonical_json(inner).encode("utf-8")).hexdigest()


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
        "canonicalization": CANONICALIZATION,
        "signing_input": signing_input,
        "signature": signature,
    }
    bundle.update(envelope)
    return bundle


def rekeyed_signing_input(
    signing_input: str,
    key: Ed25519PrivateKey,
    *,
    kid: str | None = None,
    alg: str | None = None,
    canonicalization: str | None = None,
) -> tuple[str, str]:
    """Re-sign an existing golden payload with one changed signed field."""
    payload = json.loads(signing_input)
    if kid is not None:
        payload["kid"] = kid
    if alg is not None:
        payload["alg"] = alg
    if canonicalization is not None:
        payload["canonicalization"] = canonicalization
    revised = canonical_json(_with_hash(payload))
    signature = key.sign(revised.encode("utf-8"))
    key.public_key().verify(signature, revised.encode("utf-8"))
    return revised, b64(signature)


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
        "canonicalization": CANONICALIZATION,
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

    # Downgrade pinning: editing alg or canonicalization in the signed text
    # without re-signing is tampering (invalid), never a capability gap
    # (unsupported). Only a claim the test key really signed is unsupported;
    # the signer-declared variants are the synthetic.unsupported_* cases.
    edited_alg_input = base_bundle["signing_input"].replace(
        '"alg":"Ed25519"', '"alg":"RS256"', 1
    )
    if edited_alg_input == base_bundle["signing_input"]:
        raise VectorError("golden signing input has no alg field to edit")
    edited_canon_input = base_bundle["signing_input"].replace(
        '"alg":"Ed25519"', '"alg":"Ed25519","canonicalization":"other/9"', 1
    )
    if edited_canon_input == base_bundle["signing_input"]:
        raise VectorError("golden signing input has no anchor for canonicalization")

    lied = dict(payload)
    lied["payload_hash"] = "0" * 64
    # Numbers Python renders one way and other languages another (1.0, 0.0,
    # 4e-05, integers above 2^53): the gateway signs such payloads inside
    # constraints_evaluated.jev_risk_guard, so one vector carries them and
    # must still verify without any re-serialization.
    float_payload = _with_hash(
        dict(
            payload,
            constraints_evaluated={
                "jev_risk_guard": {
                    "latency_ms": 0.0,
                    "injected_instructions": 1.0,
                    "tiny": 4e-05,
                    "big": 9007199254740993,
                }
            },
        )
    )
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
            "name": "synthetic.retired_key_still_verifies",
            "bundle": dict(base_bundle),
            # Retirement is not revocation: receipts signed before retirement
            # must still verify, or rotation would falsify its own history.
            "keys": {
                "keys": [
                    _key_entry(
                        public_key_b64=real_b64,
                        jwk=real_jwk,
                        status="retired",
                    )
                ]
            },
            "expect": "verified",
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
            # payload_hash is advisory: the signature covers the lying hash,
            # so this still verifies. Editing the hash without re-signing is
            # what fails, as invalid.
            "name": "synthetic.payload_hash_mismatch",
            "bundle": sign_bundle(key, lied),
            "expect": "verified",
        },
        {
            "name": "synthetic.float_payload_verifies",
            "bundle": sign_bundle(key, float_payload),
            "expect": "verified",
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
            # The envelope keeps the real algorithm: `unsupported` must come from the
            # signed bytes, never from unauthenticated envelope data.
            "bundle": sign_bundle(key, unsupported_alg),
            "expect": "unsupported",
        },
        {
            "name": "synthetic.unsupported_canonicalization",
            "bundle": sign_bundle(key, unsupported_canon, canonicalization="other/9"),
            "expect": "unsupported",
        },
        {
            "name": "downgrade.alg_edited",
            "bundle": dict(base_bundle, signing_input=edited_alg_input),
            "expect": "invalid",
        },
        {
            "name": "downgrade.canonicalization_edited",
            "bundle": dict(base_bundle, signing_input=edited_canon_input),
            "expect": "invalid",
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
            "name": "synthetic.malformed_missing_signature",
            "bundle": {
                "receipt_id": "rcpt-no-sig",
                "kid": KID,
                "alg": "Ed25519",
                "canonicalization": CANONICALIZATION,
                "signing_input": "{}",
            },
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


def _p256_cases(base_bundle: dict) -> list[dict]:
    """Fixed, public test key for the dormant KMS receipt algorithm."""
    key = ec.derive_private_key(1, ec.SECP256R1())
    kid = "sk_p256_test"
    algorithm = "ECDSA-P256-SHA256"
    payload = _with_hash(
        dict(json.loads(base_bundle["signing_input"]), kid=kid, alg=algorithm)
    )
    signing_input = canonical_json(payload)
    signature = key.sign(
        signing_input.encode("utf-8"),
        ec.ECDSA(hashes.SHA256(), deterministic_signing=True),
    )
    bundle = {
        "receipt_id": payload["receipt_id"],
        "kid": kid,
        "alg": algorithm,
        "canonicalization": CANONICALIZATION,
        "signing_input": signing_input,
        "signature": b64(signature),
    }
    keys = {
        "keys": [
            {
                "kid": kid,
                "alg": algorithm,
                "status": "active",
                "public_key_b64": b64(
                    key.public_key().public_bytes(
                        Encoding.DER, PublicFormat.SubjectPublicKeyInfo
                    )
                ),
            }
        ]
    }
    return [
        {"name": "p256.valid", "bundle": bundle, "keys": keys, "expect": "unsupported"},
        {
            "name": "p256.tampered_payload",
            "bundle": dict(bundle, signing_input=tamper_outcome(signing_input)),
            "keys": keys,
            "expect": "invalid",
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
    cases.extend(_p256_cases(base_bundle))
    disabled_key = Ed25519PrivateKey.from_private_bytes(DISABLED_SEED)
    revised, signature = rekeyed_signing_input(
        base_bundle["signing_input"], disabled_key, kid=DISABLED_KID
    )
    cases.append(
        {
            "name": "rotation.disabled_key_unknown",
            "bundle": dict(
                base_bundle,
                kid=DISABLED_KID,
                signing_input=revised,
                signature=signature,
            ),
            "keys": {
                "keys": [
                    _public_entry(signing, KID, "active"),
                    _public_entry(disabled_key, DISABLED_KID, "disabled"),
                ]
            },
            "expect": "unknown_key",
        }
    )
    for name, changes in (
        ("unsupported.future_alg", {"alg": "Ed448"}),
        (
            "unsupported.future_canonicalization",
            {"canonicalization": "awi-canonical-json/99"},
        ),
    ):
        revised, signature = rekeyed_signing_input(
            base_bundle["signing_input"], signing, **changes
        )
        cases.append(
            {
                "name": name,
                "bundle": dict(base_bundle, signing_input=revised, signature=signature),
                "expect": "unsupported",
            }
        )
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
            "with the signed payload, an advisory payload_hash that does not match "
            "recomputation (still verified when signed), a float-bearing payload "
            "that verifies without re-serialization, a genuine P-256 signature "
            "that is unsupported by receipt v0.1, "
            "and malformed bundles."
        ),
        "cases": build_cases(key=signing),
    }
    return json.dumps(document, indent=2) + "\n"


def render_rotation() -> str:
    """Keep the existing rotated receipt case outside the single-key corpus."""
    fixture = json.loads((GOLDEN / f"{BASE_FIXTURE}.json").read_text())
    original_signature = b64(
        signing_key().sign(fixture["signing_input"].encode("utf-8"))
    )
    if original_signature != fixture.get("signature_b64_over_test_seed"):
        raise VectorError(
            f"{BASE_FIXTURE} signature does not match the fixed test seed"
        )
    rotation_key = Ed25519PrivateKey.from_private_bytes(OTHER_SEED)
    revised, signature = rekeyed_signing_input(
        fixture["signing_input"], rotation_key, kid=ROTATION_KID
    )
    receipt_id = json.loads(revised)["receipt_id"]
    document = {
        "_comment": "Rotated receipt case from a golden receipt fixture; test use only.",
        "cases": [
            {
                "name": "rotation.successor_key_valid",
                "bundle": {
                    "receipt_id": receipt_id,
                    "kid": ROTATION_KID,
                    "alg": "Ed25519",
                    "canonicalization": CANONICALIZATION,
                    "signing_input": revised,
                    "signature": signature,
                },
                "keys": {
                    "keys": [
                        _public_entry(signing_key(), KID, "active"),
                        _public_entry(rotation_key, ROTATION_KID, "active"),
                    ]
                },
                "expect": "verified",
            }
        ],
    }
    return json.dumps(document, indent=2) + "\n"


def build_permit_cases(
    golden_dir: Path | None = None, key: Ed25519PrivateKey | None = None
) -> list[dict]:
    directory = PERMIT_GOLDEN if golden_dir is None else Path(golden_dir)
    paths = sorted(directory.glob("*.json"))
    if not paths:
        raise VectorError(f"no golden permit fixtures in {directory}")
    signing = signing_key() if key is None else key
    cases: list[dict] = []
    for path in paths:
        fixture = json.loads(path.read_text())
        signing_input = fixture["signing_input"]
        signature = signing.sign(signing_input.encode("utf-8"))
        if b64(signature) != fixture.get("signature_b64_over_test_seed"):
            raise VectorError(
                f"{path.name} signature does not match the fixed test seed"
            )
        payload = json.loads(signing_input)
        if not isinstance(payload, dict) or canonical_json(payload) != signing_input:
            raise VectorError(f"{path.name} signing_input is not canonical JSON")
        bundle = {
            "permit_id": payload["permit_id"],
            "kid": payload["kid"],
            "alg": "Ed25519",
            "signing_input": signing_input,
            "signature": b64(signature),
        }
        flipped = bytearray(signature)
        flipped[0] ^= 0x01
        tampered = signing_input.replace('"status":"active"', '"status":"suspended"', 1)
        if tampered == signing_input:
            raise VectorError(f"{path.name} has no active status to tamper")
        cases.extend(
            [
                {"name": f"{path.stem}.valid", "bundle": bundle, "expect": "verified"},
                {
                    "name": f"{path.stem}.bad_signature",
                    "bundle": dict(bundle, signature=b64(bytes(flipped))),
                    "expect": "invalid",
                },
                {
                    "name": f"{path.stem}.tampered_payload",
                    "bundle": dict(bundle, signing_input=tampered),
                    "expect": "invalid",
                },
                {
                    "name": f"{path.stem}.envelope_permit_id_mismatch",
                    "bundle": dict(bundle, permit_id="pmt_not_the_signed_one"),
                    "expect": "mismatch",
                },
                {
                    "name": f"{path.stem}.unknown_key",
                    "bundle": dict(bundle),
                    "keys": {"keys": []},
                    "expect": "unknown_key",
                },
            ]
        )
    source = cases[0]["bundle"]
    future_input, future_signature = rekeyed_signing_input(
        source["signing_input"], signing, alg="Ed448"
    )
    cases.extend(
        [
            {
                "name": "unsupported.future_alg",
                "bundle": dict(
                    source, signing_input=future_input, signature=future_signature
                ),
                "expect": "unsupported",
            },
            {
                "name": "malformed.no_signature",
                "bundle": {"permit_id": "p", "kid": KID, "signing_input": "{}"},
                "expect": "malformed",
            },
        ]
    )
    return cases


def render_permits(key: Ed25519PrivateKey | None = None) -> str:
    document = {
        "_comment": (
            "Permit verification vectors from the gateway's golden permit fixtures. "
            "Each case uses keys.json unless it carries its own 'keys'."
        ),
        "cases": build_permit_cases(key=key),
    }
    return json.dumps(document, indent=2) + "\n"


def main() -> None:
    key = signing_key()
    # Build all documents before replacing any file.
    keys_text = render_keys(_public_raw(key))
    receipts_text = render_receipts(key)
    rotation_text = render_rotation()
    permits_text = render_permits(key)
    (HERE / "keys.json").write_text(keys_text)
    (HERE / "receipts.json").write_text(receipts_text)
    (HERE / "rotation.json").write_text(rotation_text)
    (HERE / "permits.json").write_text(permits_text)
    print(
        f"wrote {len(json.loads(receipts_text)['cases'])} receipt cases, "
        f"{len(json.loads(permits_text)['cases'])} permit cases"
    )


if __name__ == "__main__":
    main()
