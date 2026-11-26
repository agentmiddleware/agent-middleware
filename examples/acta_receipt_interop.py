"""Experiment: emit a portable receipt in the ACTA signed-receipt envelope.

Answers `docs/market-research-2026-08.md` §7 question 2 with running code
instead of an estimate. The ACTA format is
draft-farley-acta-signed-receipts (ScopeBlind / protect-mcp): an individual
Internet-Draft with **no IETF standing**: namespaced receipt types over
RFC 8785 (JCS) canonical JSON, signed with Ed25519.

What this demonstrates
----------------------
A signed Agent Middleware portable receipt bundle (the artifact served by
``/v1/receipts/{id}/portable`` and published at ``site/proof/receipt.json``)
can be transcoded into an ACTA envelope as a custom
``agentmiddleware:governed_invoke`` receipt type without changing the trust
plane. The economic claims (ledger entry, idempotency record, permit,
credits) ride as first-class fields the draft's namespacing explicitly
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
import hashlib
import json
import os
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


def _reject_jcs(node: Any) -> None:
    """Reject values that do not have one JSON form in this JCS subset."""
    if isinstance(node, str):
        if any(0xD800 <= ord(char) <= 0xDFFF for char in node):
            raise TranscodeError("lone Unicode surrogates are not valid JSON text")
        return
    if node is None or isinstance(node, bool):
        return
    if isinstance(node, int):
        # Even exactly representable integers such as 2**60 can serialize
        # differently in Python and ECMAScript.
        if abs(node) > 2**53 - 1:
            raise TranscodeError(
                "integers outside the safe JSON range are not supported "
                "by this JCS subset; represent them as strings"
            )
        return
    if isinstance(node, float):
        raise TranscodeError(
            "float values are not supported by this JCS subset; "
            "represent amounts as strings, as the trust plane already does"
        )
    if isinstance(node, dict):
        for key, item in node.items():
            if not isinstance(key, str):
                raise TranscodeError("non-string object keys are not valid JSON")
            _reject_jcs(key)
            _reject_jcs(item)
        return
    if isinstance(node, list):
        for item in node:
            _reject_jcs(item)
        return
    raise TranscodeError(
        f"unsupported JSON value ({type(node).__name__}); "
        "this JCS subset allows strings, booleans, null, safe integers, arrays, and objects"
    )


def _sort_jcs(node: Any) -> Any:
    """Sort object keys by UTF-16 code units, which is the JCS order."""
    if isinstance(node, dict):
        ordered = sorted(node.items(), key=lambda item: item[0].encode("utf-16-be"))
        return {key: _sort_jcs(value) for key, value in ordered}
    if isinstance(node, list):
        return [_sort_jcs(item) for item in node]
    return node


def jcs_canonicalize(value: Any) -> bytes:
    """Minimal RFC 8785 (JCS) canonicalization for the value shapes we emit.

    Object keys are ordered by UTF-16 code units. Floats, lone surrogates,
    and integers outside the safe range are rejected: a signature
    over ambiguous bytes is worse than no signature. Amounts belong in
    strings, as the trust plane already stores them.
    """
    _reject_jcs(value)
    try:
        encoded = json.dumps(
            _sort_jcs(value),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (UnicodeEncodeError, ValueError) as exc:
        raise TranscodeError("value cannot be serialized as unambiguous JSON") from exc
    return encoded


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

    The signature is checked over the raw ``signing_input`` bytes, never
    over a re-serialization, so this function cannot be tricked by a bundle
    whose pretty ``claims`` disagree with what was actually signed.

    The bundle's ``issuer`` field is wrapper metadata *outside* the signed
    bytes. Pass ``expected_issuer``, the issuer bound to the key material in
    the trust-keys snapshot the public key came from, to refuse a bundle
    whose wrapper claims a different issuer. Without it, the issuer is
    carried through as unverified routing metadata; trust is anchored in the
    key, not the label.
    """
    for field in ("signing_input", "signature", "kid", "issuer"):
        if not isinstance(bundle.get(field), str) or not bundle[field]:
            raise TranscodeError(f"portable bundle is missing {field!r}")
    if not isinstance(public_key_b64, str) or not public_key_b64:
        raise TranscodeError("public key is missing")
    if expected_issuer is not None and bundle["issuer"] != expected_issuer:
        raise TranscodeError(
            "bundle issuer does not match the issuer bound to the trusted "
            f"key record: {bundle['issuer']!r} != {expected_issuer!r}"
        )

    # The envelope alg is unsigned. Reading it before the signature check lets
    # one edited byte turn a real receipt into "this verifier cannot do that".
    # Decode and verify first. Compare alg fields only after the signature holds.
    try:
        signature = base64.b64decode(bundle["signature"], validate=True)
    except ValueError as exc:
        raise TranscodeError("portable bundle signature is not valid base64") from exc
    try:
        public_bytes = base64.b64decode(public_key_b64, validate=True)
    except ValueError as exc:
        raise TranscodeError("public key is not valid base64") from exc
    if len(signature) != 64:
        raise TranscodeError("signature is not a 64-byte Ed25519 signature")
    if len(public_bytes) != 32:
        raise TranscodeError("public key is not a 32-byte Ed25519 key")
    try:
        public_key = Ed25519PublicKey.from_public_bytes(public_bytes)
    except ValueError as exc:
        raise TranscodeError("public key is not a 32-byte Ed25519 key") from exc
    try:
        signing_bytes = bundle["signing_input"].encode("utf-8")
    except UnicodeEncodeError as exc:
        raise TranscodeError("signing_input is not valid Unicode text") from exc
    try:
        public_key.verify(signature, signing_bytes)
    except InvalidSignature as exc:
        raise TranscodeError(
            "portable bundle signature does not verify; refusing to transcode "
            "an unverified receipt"
        ) from exc

    try:
        claims = json.loads(bundle["signing_input"])
    except json.JSONDecodeError as exc:
        raise TranscodeError("signing_input is not valid JSON") from exc
    except RecursionError as exc:
        raise TranscodeError("signing_input exceeds the supported JSON nesting depth") from exc
    if not isinstance(claims, dict):
        raise TranscodeError("signing_input is not a JSON object")

    signed_alg = claims.get("alg", "Ed25519")
    if not isinstance(signed_alg, str) or signed_alg != "Ed25519":
        raise TranscodeError(
            f"signed payload declares alg {signed_alg!r}; this verifier only "
            "performs Ed25519"
        )
    envelope_alg = bundle.get("alg")
    if envelope_alg is not None and envelope_alg != signed_alg:
        raise TranscodeError(
            f"bundle alg {envelope_alg!r} does not match the signed alg {signed_alg!r}"
        )
    if claims.get("kid") != bundle["kid"]:
        raise TranscodeError("bundle kid does not match the signed kid claim")
    _reject_bad_payload_hash(claims)

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
    economic linkage, the part no surveyed receipt format defines, as
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
    if "signature" in receipt:
        raise TranscodeError(
            "receipt body already contains a signature field; sign the unsigned body"
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
        signature_bytes = bytes.fromhex(signature_block["sig"])
    except ValueError as exc:
        raise TranscodeError("ACTA signature sig must be hex") from exc
    if len(signature_bytes) != 64:
        raise TranscodeError("ACTA signature must be 64 bytes")
    try:
        public_key.verify(signature_bytes, jcs_canonicalize(body))
    except InvalidSignature as exc:
        raise TranscodeError("ACTA envelope signature does not verify") from exc
    return body


def _reject_bad_payload_hash(claims: dict[str, Any]) -> None:
    """Match the trust-plane payload_hash check. Absent hash means an older receipt.

    This is not JCS. It is the same compact sorted JSON the SDK verifier hashes,
    with non-ASCII escaped. Do not route it through ``jcs_canonicalize``.
    """
    stated = claims.get("payload_hash")
    if not isinstance(stated, str):
        return
    inner = {key: value for key, value in claims.items() if key != "payload_hash"}
    try:
        canonical = json.dumps(inner, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise TranscodeError("payload_hash cannot be checked for this payload") from exc
    computed = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if computed != stated:
        raise TranscodeError("payload_hash does not match the signed payload")


def _public_key_for_kid(keys: dict[str, Any], kid: str) -> str:
    """Pick the Ed25519 public key for ``kid``, skipping disabled records."""
    entries = keys.get("keys") if isinstance(keys, dict) else None
    if not isinstance(entries, list):
        raise TranscodeError("trust-keys document has no keys list")
    saw_disabled = False
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kid") != kid:
            continue
        if entry.get("status") == "disabled":
            saw_disabled = True
            continue
        if entry.get("alg") not in (None, "Ed25519"):
            raise TranscodeError(f"trust key {kid!r} is not an Ed25519 key")
        public_key = entry.get("public_key_b64")
        if not isinstance(public_key, str) or not public_key:
            raise TranscodeError(f"trust key {kid!r} has no public_key_b64")
        return public_key
    if saw_disabled:
        raise TranscodeError(f"trust key {kid!r} is disabled")
    raise TranscodeError(f"no trust key for kid {kid!r}")


def _demo(bundle_path: str) -> int:
    """Verify a portable bundle end-to-end and print the ACTA envelope.

    ``trust-keys.json`` is read from the same directory as the bundle. The
    default path ``site/proof/receipt.json`` therefore uses
    ``site/proof/trust-keys.json`` when the command is run from the repo root.
    """
    bundle_file = os.path.abspath(bundle_path)
    with open(bundle_file, encoding="utf-8") as handle:
        bundle = json.load(handle)
    if not isinstance(bundle, dict):
        raise TranscodeError("portable bundle must be a JSON object")
    kid = bundle.get("kid")
    if not isinstance(kid, str) or not kid:
        raise TranscodeError("portable bundle is missing 'kid'")

    keys_path = os.path.join(os.path.dirname(bundle_file), "trust-keys.json")
    with open(keys_path, encoding="utf-8") as handle:
        keys = json.load(handle)
    if not isinstance(keys, dict) or not isinstance(keys.get("issuer"), str):
        raise TranscodeError("trust-keys document has no issuer")
    key_b64 = _public_key_for_kid(keys, kid)

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
