"""Regenerate the receipt and permit test vectors in this directory.

The signing keys are derived from fixed, public test seeds. They are NOT
production keys and must never be used as such. The signed payloads come from
the gateway's golden fixtures (gateway/tests/fixtures), so a change to a
signing input shows up here as a diff.

Usage (from the repository root):
    python spec/test-vectors/generate.py
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
RECEIPT_GOLDEN = ROOT / "gateway" / "tests" / "fixtures" / "receipt_signing_golden"
PERMIT_GOLDEN = ROOT / "gateway" / "tests" / "fixtures" / "permit_signing_golden"
TEST_SEED = bytes(range(32))
ROTATION_SEED = bytes(range(32, 64))
DISABLED_SEED = bytes(range(64, 96))
KID = "sk_golden_0001"
ROTATION_KID = "sk_golden_0002"
DISABLED_KID = "sk_golden_0000"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def canonical(payload: Any) -> str:
    """Serialize exactly the way the trust plane signs (see spec/README.md)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def public_entry(key: Ed25519PrivateKey, kid: str, status: str) -> dict[str, Any]:
    raw = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return {
        "kid": kid,
        "alg": "Ed25519",
        "status": status,
        "public_key_b64": b64(raw),
        "jwk": {
            "kty": "OKP",
            "crv": "Ed25519",
            "x": base64.urlsafe_b64encode(raw).decode("ascii").rstrip("="),
        },
    }


def rekeyed_signing_input(
    signing_input: str,
    signing_key: Ed25519PrivateKey,
    *,
    kid: str | None = None,
    alg: str | None = None,
    extra: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Return ``(signing_input, signature_b64)`` for an edited payload.

    The ``payload_hash`` is recomputed over the edited payload so the only
    thing a verifier can object to is the edited field itself (a new key id,
    a future algorithm, ...), never a stale hash.
    """
    payload = json.loads(signing_input)
    if kid is not None:
        payload["kid"] = kid
    if alg is not None:
        payload["alg"] = alg
    if extra:
        payload.update(extra)
    payload.pop("payload_hash", None)
    payload["payload_hash"] = hashlib.sha256(
        canonical(payload).encode("utf-8")
    ).hexdigest()
    revised = canonical(payload)
    signature = signing_key.sign(revised.encode("utf-8"))
    # Self-check: a vector with a broken signature would teach verifiers the
    # wrong lesson, so prove each one before writing it.
    signing_key.public_key().verify(signature, revised.encode("utf-8"))
    return revised, b64(signature)


def main() -> None:
    key = Ed25519PrivateKey.from_private_bytes(TEST_SEED)
    rotation_key = Ed25519PrivateKey.from_private_bytes(ROTATION_SEED)
    disabled_key = Ed25519PrivateKey.from_private_bytes(DISABLED_SEED)
    keys = {
        "_comment": (
            "Public halves of fixed test seeds bytes(range(32)) (active), "
            "bytes(range(32, 64)) (active rotation successor), and "
            "bytes(range(64, 96)) (disabled). Test use only."
        ),
        "keys": [
            public_entry(key, KID, "active"),
            public_entry(rotation_key, ROTATION_KID, "active"),
            public_entry(disabled_key, DISABLED_KID, "disabled"),
        ],
    }
    (HERE / "keys.json").write_text(json.dumps(keys, indent=2) + "\n")

    receipt_cases = []
    for golden in sorted(RECEIPT_GOLDEN.glob("*.json")):
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
        receipt_cases.append(
            {"name": f"{name}.valid", "bundle": bundle, "expect": "verified"}
        )

        flipped = bytearray(signature)
        flipped[0] ^= 0x01
        bad_sig = dict(bundle, signature=b64(bytes(flipped)))
        receipt_cases.append(
            {"name": f"{name}.bad_signature", "bundle": bad_sig, "expect": "invalid"}
        )

        tampered_payload = signing_input.replace('"outcome":"', '"outcome":"x', 1)
        if tampered_payload != signing_input:
            receipt_cases.append(
                {
                    "name": f"{name}.tampered_payload",
                    "bundle": dict(bundle, signing_input=tampered_payload),
                    "expect": "invalid",
                }
            )

        receipt_cases.append(
            {
                "name": f"{name}.envelope_receipt_id_mismatch",
                "bundle": dict(bundle, receipt_id="rcpt_not_the_signed_one"),
                "expect": "mismatch",
            }
        )

        receipt_cases.append(
            {
                "name": f"{name}.unknown_key",
                "bundle": copy.deepcopy(bundle),
                "keys": {"keys": []},
                "expect": "unknown_key",
            }
        )

    first_receipt = json.loads(sorted(RECEIPT_GOLDEN.glob("*.json"))[0].read_text())[
        "signing_input"
    ]

    rotated_input, rotated_sig = rekeyed_signing_input(
        first_receipt, rotation_key, kid=ROTATION_KID
    )
    rotated_payload = json.loads(rotated_input)
    receipt_cases.append(
        {
            "name": "rotation.successor_key_valid",
            "bundle": {
                "receipt_id": rotated_payload["receipt_id"],
                "kid": ROTATION_KID,
                "alg": "Ed25519",
                "signing_input": rotated_input,
                "signature": rotated_sig,
            },
            "expect": "verified",
        }
    )

    retired_input, retired_sig = rekeyed_signing_input(
        first_receipt, disabled_key, kid=DISABLED_KID
    )
    retired_payload = json.loads(retired_input)
    receipt_cases.append(
        {
            "name": "rotation.disabled_key_unknown",
            "bundle": {
                "receipt_id": retired_payload["receipt_id"],
                "kid": DISABLED_KID,
                "alg": "Ed25519",
                "signing_input": retired_input,
                "signature": retired_sig,
            },
            "expect": "unknown_key",
        }
    )

    future_alg_input, future_alg_sig = rekeyed_signing_input(
        first_receipt, key, alg="Ed448"
    )
    future_alg_payload = json.loads(future_alg_input)
    receipt_cases.append(
        {
            "name": "unsupported.future_alg",
            "bundle": {
                "receipt_id": future_alg_payload["receipt_id"],
                "kid": KID,
                "alg": "Ed25519",
                "signing_input": future_alg_input,
                "signature": future_alg_sig,
            },
            "expect": "unsupported",
        }
    )

    future_canon_input, future_canon_sig = rekeyed_signing_input(
        first_receipt, key, extra={"canonicalization": "awi-canonical-json/99"}
    )
    future_canon_payload = json.loads(future_canon_input)
    receipt_cases.append(
        {
            "name": "unsupported.future_canonicalization",
            "bundle": {
                "receipt_id": future_canon_payload["receipt_id"],
                "kid": KID,
                "alg": "Ed25519",
                "signing_input": future_canon_input,
                "signature": future_canon_sig,
            },
            "expect": "unsupported",
        }
    )

    receipt_cases.append(
        {
            "name": "malformed.no_signature",
            "bundle": {"receipt_id": "r", "kid": KID, "signing_input": "{}"},
            "expect": "malformed",
        }
    )
    receipts_out = {
        "_comment": (
            "Receipt verification vectors. Each case is checked against keys.json "
            "unless it carries its own 'keys'. 'expect' is the verifier status."
        ),
        "cases": receipt_cases,
    }
    (HERE / "receipts.json").write_text(json.dumps(receipts_out, indent=2) + "\n")

    permit_cases = []
    for golden in sorted(PERMIT_GOLDEN.glob("*.json")):
        fixture = json.loads(golden.read_text())
        signing_input = fixture["signing_input"]
        signature = key.sign(signing_input.encode("utf-8"))
        assert b64(signature) == fixture["signature_b64_over_test_seed"], golden.name
        payload = json.loads(signing_input)
        bundle = {
            "permit_id": payload["permit_id"],
            "kid": payload["kid"],
            "alg": "Ed25519",
            "signing_input": signing_input,
            "signature": b64(signature),
        }
        name = golden.stem
        permit_cases.append(
            {"name": f"{name}.valid", "bundle": bundle, "expect": "verified"}
        )

        flipped = bytearray(signature)
        flipped[0] ^= 0x01
        permit_cases.append(
            {
                "name": f"{name}.bad_signature",
                "bundle": dict(bundle, signature=b64(bytes(flipped))),
                "expect": "invalid",
            }
        )

        tampered_payload = signing_input.replace(
            '"status":"active"', '"status":"suspended"', 1
        )
        assert tampered_payload != signing_input, golden.name
        permit_cases.append(
            {
                "name": f"{name}.tampered_payload",
                "bundle": dict(bundle, signing_input=tampered_payload),
                "expect": "invalid",
            }
        )

        permit_cases.append(
            {
                "name": f"{name}.envelope_permit_id_mismatch",
                "bundle": dict(bundle, permit_id="pmt_not_the_signed_one"),
                "expect": "mismatch",
            }
        )

        permit_cases.append(
            {
                "name": f"{name}.unknown_key",
                "bundle": copy.deepcopy(bundle),
                "keys": {"keys": []},
                "expect": "unknown_key",
            }
        )

    first_permit = json.loads(sorted(PERMIT_GOLDEN.glob("*.json"))[0].read_text())[
        "signing_input"
    ]
    future_permit_input, future_permit_sig = rekeyed_signing_input(
        first_permit, key, alg="Ed448"
    )
    future_permit_payload = json.loads(future_permit_input)
    permit_cases.append(
        {
            "name": "unsupported.future_alg",
            "bundle": {
                "permit_id": future_permit_payload["permit_id"],
                "kid": KID,
                "alg": "Ed25519",
                "signing_input": future_permit_input,
                "signature": future_permit_sig,
            },
            "expect": "unsupported",
        }
    )
    permit_cases.append(
        {
            "name": "malformed.no_signature",
            "bundle": {"permit_id": "p", "kid": KID, "signing_input": "{}"},
            "expect": "malformed",
        }
    )
    permits_out = {
        "_comment": (
            "Permit verification vectors, built the same way as receipts.json "
            "from the gateway's permit golden fixtures. Each case is checked "
            "against keys.json unless it carries its own 'keys'."
        ),
        "cases": permit_cases,
    }
    (HERE / "permits.json").write_text(json.dumps(permits_out, indent=2) + "\n")
    print(f"wrote {len(receipt_cases)} receipt cases, {len(permit_cases)} permit cases")


if __name__ == "__main__":
    main()
