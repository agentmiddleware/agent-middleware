"""Experiment: emit a portable receipt in the ACTA signed-receipt envelope.

Answers `docs/market-research-2026-08.md` §7 question 2 with running code
instead of an estimate. The ACTA format is
draft-farley-acta-signed-receipts (ScopeBlind / protect-mcp): an individual
Internet-Draft with **no IETF standing** — namespaced receipt types over
RFC 8785 (JCS) canonical JSON, signed with Ed25519.

What this demonstrates
----------------------
A signed Agent Middleware portable receipt bundle (the artifact served by
``/v1/receipts/{id}/portable`` and published at ``site/proof/receipt.json``)
can be transcoded into an ACTA envelope as a custom
``agentmiddleware:governed_invoke`` receipt type without changing the trust
plane. The economic claims — ledger entry, idempotency record, permit,
credits — ride as first-class fields the draft's namespacing explicitly
permits, which is precisely the room the research pass found.

What this deliberately does NOT do
----------------------------------
- It does not re-sign with the production service key. The original bundle
  travels inside the envelope untouched, and its Ed25519 signature is
  verified over the exact ``signing_input`` bytes against the bundle's own
  key material, the same check ``b2a-verify-receipt`` performs. The outer
  ACTA signature is applied by whoever transcodes (here: a caller-supplied
  key; in any real adoption: the service signing key, server-side).
- It is not a product surface, an API route, or an SDK export. It is an
  executable answer to "would interop cost a serializer or a redesign?"
  Promoting it is a product decision recorded in the research doc, not here.

Run the demo against the checked-in public proof artifact:

    python3 examples/acta_receipt_interop.py site/proof/receipt.json
"""

from __future__ import annotations

import base64
import json
import sys
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

ACTA_TYPE = "agentmiddleware:governed_invoke"

# Fields the transcoder requires inside the portable bundle's signing_input.
# Missing any of these is a hard error: an economic receipt without its
# ledger linkage is exactly the artifact this experiment exists to avoid.
REQUIRED_CLAIMS = (
    "receipt_id",
    "outcome",
    "tool",
    "created_at",
    "credits_charged",
    "ledger_entry_id",
    "idempotency_record_id",
    "permit_id",
)


class TranscodeError(ValueError):
    """A portable bundle that cannot be honestly represented in ACTA form."""


def jcs_canonicalize(value: Any) -> bytes:
    """Minimal RFC 8785 (JCS) canonicalization for the value shapes we emit.

    The full JCS spec includes IEEE-754 number serialization rules. Every
    field this transcoder produces is a string, bool, None, int, list, or
    dict, so floats are rejected outright rather than risking a
    canonicalization mismatch between implementations. Fail closed: a
    signature over ambiguous bytes is worse than no signature.
    """

    def check(node: Any) -> None:
        """Recursively reject value shapes outside the supported JCS subset."""
        if isinstance(node, float):
            raise TranscodeError(
                "float values are not supported by this JCS subset; "
                "represent amounts as strings, as the trust plane already does"
            )
        if isinstance(node, dict):
            for key, item in node.items():
                if not isinstance(key, str):
                    raise TranscodeError("non-string object keys are not valid JSON")
                check(item)
        elif isinstance(node, list):
            for item in node:
                check(item)

    check(value)
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


@dataclass(frozen=True)
class VerifiedBundle:
    """A portable bundle whose own Ed25519 signature has been checked."""

    claims: dict[str, Any]
    signing_input: str
    signature_b64: str
    kid: str
    issuer: str


def verify_portable_bundle(
    bundle: dict[str, Any], public_key_b64: str, expected_issuer: str | None = None
) -> VerifiedBundle:
    """Verify the inner receipt exactly as the offline SDK verifier does.

    The signature is checked over the raw ``signing_input`` bytes — never
    over a re-serialization — so this function cannot be tricked by a bundle
    whose pretty ``claims`` disagree with what was actually signed.

    The bundle's ``issuer`` field is wrapper metadata *outside* the signed
    bytes. Pass ``expected_issuer`` — the issuer bound to the key material in
    the trust-keys snapshot the public key came from — to refuse a bundle
    whose wrapper claims a different issuer. Without it, the issuer is
    carried through as unverified routing metadata; trust is anchored in the
    key, not the label.
    """
    for field in ("signing_input", "signature", "kid", "issuer"):
        if not isinstance(bundle.get(field), str) or not bundle[field]:
            raise TranscodeError(f"portable bundle is missing {field!r}")
    declared_alg = bundle.get("alg")
    if declared_alg is not None and declared_alg != "Ed25519":
        raise TranscodeError(
            f"bundle declares alg {declared_alg!r}; this verifier only "
            "performs Ed25519 and will not report a misleading result"
        )
    if expected_issuer is not None and bundle["issuer"] != expected_issuer:
        raise TranscodeError(
            "bundle issuer does not match the issuer bound to the trusted "
            f"key record: {bundle['issuer']!r} != {expected_issuer!r}"
        )

    public_key = Ed25519PublicKey.from_public_bytes(
        base64.b64decode(public_key_b64, validate=True)
    )
    try:
        public_key.verify(
            base64.b64decode(bundle["signature"], validate=True),
            bundle["signing_input"].encode("utf-8"),
        )
    except InvalidSignature as exc:
        raise TranscodeError(
            "portable bundle signature does not verify; refusing to transcode "
            "an unverified receipt"
        ) from exc

    claims = json.loads(bundle["signing_input"])
    if not isinstance(claims, dict):
        raise TranscodeError("signing_input is not a JSON object")
    if claims.get("kid") != bundle["kid"]:
        raise TranscodeError("bundle kid does not match the signed kid claim")

    missing = [field for field in REQUIRED_CLAIMS if not claims.get(field)]
    if missing:
        raise TranscodeError(f"signed claims missing required fields: {missing}")

    return VerifiedBundle(
        claims=claims,
        signing_input=bundle["signing_input"],
        signature_b64=bundle["signature"],
        kid=bundle["kid"],
        issuer=bundle["issuer"],
    )


def to_acta_receipt(verified: VerifiedBundle, issuer_id: str) -> dict[str, Any]:
    """Map a verified portable receipt into an unsigned ACTA receipt body.

    Field choices follow the draft's conventions where one exists
    (``type``, ``issued_at``, ``issuer_id``, ``tool_name``) and carry the
    economic linkage — the part no surveyed receipt format defines — as
    plainly named custom fields. The untouched original bundle is embedded
    so cryptographic trust never depends on the transcoder. Its ``issuer``
    field is wrapper metadata, trustworthy only when the caller verified it
    against the key record (``expected_issuer``); the signature covers
    ``signing_input`` alone.
    """
    claims = verified.claims
    return {
        "type": ACTA_TYPE,
        "issued_at": claims["created_at"],
        "issuer_id": issuer_id,
        "tool_name": claims["tool"],
        "outcome": claims["outcome"],
        "credits_charged": claims["credits_charged"],
        "receipt_id": claims["receipt_id"],
        "permit_id": claims["permit_id"],
        "ledger_entry_id": claims["ledger_entry_id"],
        "idempotency_record_id": claims["idempotency_record_id"],
        "request_hash": claims.get("request_hash"),
        "response_hash": claims.get("response_hash"),
        "original_bundle": {
            "alg": "Ed25519",
            "issuer": verified.issuer,
            "kid": verified.kid,
            "signature": verified.signature_b64,
            "signing_input": verified.signing_input,
        },
    }


def sign_acta_receipt(
    receipt: dict[str, Any], private_key: Ed25519PrivateKey, kid: str
) -> dict[str, Any]:
    """Attach the draft's signature block: Ed25519 over JCS bytes, hex."""
    if receipt.get("issuer_id") != kid:
        raise TranscodeError(
            "the draft requires issuer_id to match the signing kid; "
            f"got issuer_id={receipt.get('issuer_id')!r} kid={kid!r}"
        )
    signature = private_key.sign(jcs_canonicalize(receipt))
    return {
        **receipt,
        "signature": {"alg": "Ed25519", "kid": kid, "sig": signature.hex()},
    }


def verify_acta_receipt(
    signed: dict[str, Any], public_key: Ed25519PublicKey
) -> dict[str, Any]:
    """Verify the outer envelope and return the receipt body."""
    signature_block = signed.get("signature")
    if not isinstance(signature_block, dict):
        raise TranscodeError("ACTA receipt has no signature block")
    if signature_block.get("alg") != "Ed25519":
        raise TranscodeError(
            "ACTA signature alg must be Ed25519; refusing "
            f"{signature_block.get('alg')!r}"
        )
    if not isinstance(signature_block.get("sig"), str):
        raise TranscodeError("ACTA signature sig must be a hex string")
    body = {key: value for key, value in signed.items() if key != "signature"}
    issuer_id = body.get("issuer_id")
    kid = signature_block.get("kid")
    if not isinstance(issuer_id, str) or not issuer_id:
        raise TranscodeError("ACTA receipt issuer_id must be a non-empty string")
    if not isinstance(kid, str) or not kid:
        raise TranscodeError("ACTA signature kid must be a non-empty string")
    if issuer_id != kid:
        raise TranscodeError("issuer_id does not match the signature kid")
    try:
        public_key.verify(bytes.fromhex(signature_block["sig"]), jcs_canonicalize(body))
    except (InvalidSignature, ValueError) as exc:
        raise TranscodeError("ACTA envelope signature does not verify") from exc
    return body


def _demo(bundle_path: str) -> int:
    """Verify a portable bundle end-to-end and print the ACTA envelope."""
    with open(bundle_path, "r", encoding="utf-8") as handle:
        bundle = json.load(handle)

    # The public proof bundle's key snapshot ships beside it.
    with open("site/proof/trust-keys.json", "r", encoding="utf-8") as handle:
        keys = json.load(handle)
    key_b64 = next(
        entry["public_key_b64"]
        for entry in keys["keys"]
        if entry["kid"] == bundle["kid"]
    )

    verified = verify_portable_bundle(bundle, key_b64, expected_issuer=keys["issuer"])
    print(f"inner signature: VERIFIED (kid={verified.kid})")

    transcoder_key = Ed25519PrivateKey.generate()
    signed = sign_acta_receipt(
        to_acta_receipt(verified, issuer_id="demo-transcoder-key"),
        transcoder_key,
        kid="demo-transcoder-key",
    )
    body = verify_acta_receipt(signed, transcoder_key.public_key())
    print(f"outer envelope:  VERIFIED (type={body['type']})")
    print(json.dumps(signed, indent=2)[:800])
    return 0


if __name__ == "__main__":
    raise SystemExit(
        _demo(sys.argv[1] if len(sys.argv) > 1 else "site/proof/receipt.json")
    )
