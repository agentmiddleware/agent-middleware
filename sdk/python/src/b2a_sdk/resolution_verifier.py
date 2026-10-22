"""Offline verification of signed receipt resolutions and wallet chains.

The caller supplies the resolution record, the original portable receipt,
and keys previously obtained from ``/.well-known/trust-keys.json``. No network
or credential is used here. Key distribution and issuer identity remain the
caller's trust decision.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .receipt_verifier import (
    CANONICALIZATION,
    VerificationKey,
    VerificationStatus,
    canonical_json,
    verify_bundle,
    verify_ed25519,
)

__all__ = ["ResolutionVerificationResult", "verify_resolution", "verify_resolution_chain"]

_VERSION = "resolution-v0.1"
_OUTCOMES = {"confirmed_success", "confirmed_not_delivered"}
_SIGNED_FIELDS = {
    "alg",
    "created_at",
    "kid",
    "outcome",
    "payload_hash",
    "prev_hash",
    "receipt_hash",
    "receipt_id",
    "resolution_id",
    "sequence",
    "version",
    "wallet_id",
}
_MIRRORED_FIELDS = _SIGNED_FIELDS - {"version"}


@dataclass(frozen=True)
class ResolutionVerificationResult:
    """Verdict for a record or a supplied complete wallet chain.

    ``claims`` contains only verified signed fields. For a chain it contains
    the record associated with the supplied receipt. ``resolution_hash`` is
    the verified record hash, or the chain head when verifying a chain.
    """

    status: VerificationStatus
    reason: str | None = None
    claims: dict[str, Any] = field(default_factory=dict)
    resolution_hash: str | None = None
    records_verified: int = 0

    @property
    def ok(self) -> bool:
        return self.status is VerificationStatus.VERIFIED

    @property
    def is_rejected(self) -> bool:
        return self.status in (VerificationStatus.INVALID, VerificationStatus.MISMATCH)


def _fail(status: VerificationStatus, reason: str) -> ResolutionVerificationResult:
    return ResolutionVerificationResult(status=status, reason=reason)


def _decode_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str | bytes):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError, ValueError):
            return None
    return value if isinstance(value, dict) else None


def _is_hash(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _valid_utc_time(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.utcoffset() == timedelta(0)


def _verify_record(
    record: Any,
    key_set: dict[str, bytes | VerificationKey],
) -> ResolutionVerificationResult:
    envelope = _decode_object(record)
    if envelope is None:
        return _fail(VerificationStatus.MALFORMED, "resolution must be a JSON object")

    signing_input = envelope.get("signing_input")
    signature_b64 = envelope.get("signature")
    if not isinstance(signing_input, str) or not signing_input:
        return _fail(VerificationStatus.MALFORMED, "resolution has no signing_input")
    if not isinstance(signature_b64, str) or not signature_b64:
        return _fail(VerificationStatus.MALFORMED, "resolution has no signature")
    try:
        signing_bytes = signing_input.encode("utf-8")
        claims = json.loads(signing_input)
    except (UnicodeEncodeError, json.JSONDecodeError, RecursionError, ValueError):
        return _fail(VerificationStatus.MALFORMED, "resolution signing_input is not valid JSON")
    if not isinstance(claims, dict):
        return _fail(VerificationStatus.MALFORMED, "resolution signing_input is not an object")

    kid = claims.get("kid")
    if not isinstance(kid, str) or not kid:
        return _fail(VerificationStatus.MALFORMED, "signed resolution has no kid")
    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except (binascii.Error, ValueError):
        return _fail(VerificationStatus.MALFORMED, "resolution signature is not valid base64")
    key = key_set.get(kid)
    if key is None:
        return _fail(VerificationStatus.UNKNOWN_KEY, "resolution signing key is unavailable")
    if isinstance(key, VerificationKey):
        return _fail(VerificationStatus.UNSUPPORTED, "resolution requires an Ed25519 key")
    if not isinstance(key, bytes) or len(key) != 32 or len(signature) != 64:
        return _fail(VerificationStatus.MALFORMED, "resolution key or signature has wrong length")
    if not verify_ed25519(key, signature, signing_bytes):
        return _fail(VerificationStatus.INVALID, "resolution signature does not verify")

    if claims.get("alg") != "Ed25519" or claims.get("version") != _VERSION:
        return _fail(VerificationStatus.UNSUPPORTED, "unsupported signed resolution format")
    if set(claims) != _SIGNED_FIELDS:
        return _fail(VerificationStatus.MALFORMED, "signed resolution fields are incomplete")
    for name in ("resolution_id", "receipt_id", "wallet_id", "kid"):
        if not isinstance(claims[name], str) or not claims[name]:
            return _fail(VerificationStatus.MALFORMED, f"signed resolution has invalid {name}")
        try:
            claims[name].encode("utf-8")
        except UnicodeEncodeError:
            return _fail(VerificationStatus.MALFORMED, f"signed resolution has invalid {name}")
    sequence = claims["sequence"]
    if type(sequence) is not int or sequence < 1:
        return _fail(VerificationStatus.MALFORMED, "signed resolution has invalid sequence")
    if not _is_hash(claims["receipt_hash"]) or not _is_hash(claims["payload_hash"]):
        return _fail(VerificationStatus.MALFORMED, "signed resolution has invalid hash")
    prev_hash = claims["prev_hash"]
    if (sequence == 1 and prev_hash is not None) or (sequence > 1 and not _is_hash(prev_hash)):
        return _fail(VerificationStatus.MALFORMED, "signed resolution has invalid prev_hash")
    if not isinstance(claims["outcome"], str) or claims["outcome"] not in _OUTCOMES:
        return _fail(VerificationStatus.MALFORMED, "signed resolution has invalid outcome")
    if not _valid_utc_time(claims["created_at"]):
        return _fail(VerificationStatus.MALFORMED, "signed resolution has invalid created_at")

    # Resolution v0.1 uses strings, one integer, and null only, so its JSON
    # can be checked against the gateway's canonical bytes without the numeric
    # round-trip ambiguity that older receipt payloads may carry.
    if canonical_json(claims).encode("utf-8") != signing_bytes:
        return _fail(VerificationStatus.MISMATCH, "resolution signing_input is not canonical")
    without_hash = {name: value for name, value in claims.items() if name != "payload_hash"}
    expected_payload_hash = hashlib.sha256(canonical_json(without_hash).encode("utf-8")).hexdigest()
    if claims["payload_hash"] != expected_payload_hash:
        return _fail(VerificationStatus.MISMATCH, "resolution payload_hash disagrees")

    resolution_hash = hashlib.sha256(signing_bytes).hexdigest()
    if envelope.get("canonicalization") != CANONICALIZATION:
        return _fail(VerificationStatus.MISMATCH, "resolution canonicalization disagrees")
    if envelope.get("resolution_hash") != resolution_hash:
        return _fail(VerificationStatus.MISMATCH, "resolution_hash disagrees with signing_input")
    for name in _MIRRORED_FIELDS:
        if name not in envelope:
            return _fail(VerificationStatus.MALFORMED, f"resolution envelope has no {name}")
        if type(envelope[name]) is not type(claims[name]) or envelope[name] != claims[name]:
            return _fail(VerificationStatus.MISMATCH, f"resolution envelope {name} disagrees")
    if "version" in envelope and envelope["version"] != claims["version"]:
        return _fail(VerificationStatus.MISMATCH, "resolution envelope version disagrees")

    return ResolutionVerificationResult(
        status=VerificationStatus.VERIFIED,
        claims=claims,
        resolution_hash=resolution_hash,
        records_verified=1,
    )


def _verified_receipt(
    receipt_bundle: Any,
    key_set: dict[str, bytes | VerificationKey],
) -> tuple[dict[str, Any], str] | ResolutionVerificationResult:
    bundle = _decode_object(receipt_bundle)
    if bundle is None:
        return _fail(VerificationStatus.MALFORMED, "receipt bundle must be a JSON object")
    receipt_result = verify_bundle(bundle, key_set)
    if not receipt_result.ok:
        return _fail(receipt_result.status, f"receipt: {receipt_result.reason}")
    signing_input = bundle["signing_input"]
    return receipt_result.claims, hashlib.sha256(signing_input.encode("utf-8")).hexdigest()


def verify_resolution(
    record: Any,
    receipt_bundle: Any,
    key_set: dict[str, bytes | VerificationKey],
) -> ResolutionVerificationResult:
    """Verify one resolution, its original receipt, and their exact-byte link."""
    receipt = _verified_receipt(receipt_bundle, key_set)
    if isinstance(receipt, ResolutionVerificationResult):
        return receipt
    receipt_claims, receipt_hash = receipt
    verified = _verify_record(record, key_set)
    if not verified.ok:
        return verified
    claims = verified.claims
    if (
        claims["receipt_id"] != receipt_claims.get("receipt_id")
        or claims["wallet_id"] != receipt_claims.get("wallet_id")
        or claims["receipt_hash"] != receipt_hash
    ):
        return _fail(VerificationStatus.MISMATCH, "resolution points to a different receipt")
    return verified


def verify_resolution_chain(
    records: Any,
    receipt_bundle: Any,
    key_set: dict[str, bytes | VerificationKey],
) -> ResolutionVerificationResult:
    """Verify a complete, ordered wallet chain from sequence 1.

    The supplied receipt must occur exactly once. Other chain records are
    signature checked, but their original receipts are not supplied here.
    A party withholding a valid competing fork or later records cannot be
    detected by an offline verifier; compare an independently witnessed head
    if completeness matters.
    """
    if not isinstance(records, list) or not records:
        return _fail(VerificationStatus.MALFORMED, "chain must be a nonempty list")
    receipt = _verified_receipt(receipt_bundle, key_set)
    if isinstance(receipt, ResolutionVerificationResult):
        return receipt
    receipt_claims, receipt_hash = receipt

    wallet_id: str | None = None
    previous_hash: str | None = None
    seen_receipt_ids: set[str] = set()
    seen_resolution_ids: set[str] = set()
    target: dict[str, Any] | None = None
    for sequence, record in enumerate(records, start=1):
        verified = _verify_record(record, key_set)
        if not verified.ok:
            return verified
        claims = verified.claims
        if wallet_id is None:
            wallet_id = claims["wallet_id"]
        if claims["wallet_id"] != wallet_id:
            return _fail(VerificationStatus.MISMATCH, "chain contains another wallet")
        if claims["sequence"] != sequence or claims["prev_hash"] != previous_hash:
            return _fail(VerificationStatus.MISMATCH, "chain is forked, reordered, or incomplete")
        if claims["receipt_id"] in seen_receipt_ids:
            return _fail(VerificationStatus.MISMATCH, "chain repeats a receipt")
        if claims["resolution_id"] in seen_resolution_ids:
            return _fail(VerificationStatus.MISMATCH, "chain repeats a resolution")
        seen_receipt_ids.add(claims["receipt_id"])
        seen_resolution_ids.add(claims["resolution_id"])
        previous_hash = verified.resolution_hash
        if claims["receipt_id"] == receipt_claims.get("receipt_id"):
            if claims["receipt_hash"] != receipt_hash or claims["wallet_id"] != receipt_claims.get(
                "wallet_id"
            ):
                return _fail(
                    VerificationStatus.MISMATCH, "resolution points to a different receipt"
                )
            target = claims
    if target is None:
        return _fail(VerificationStatus.MISMATCH, "chain has no resolution for supplied receipt")
    return ResolutionVerificationResult(
        status=VerificationStatus.VERIFIED,
        claims=target,
        resolution_hash=previous_hash,
        records_verified=len(records),
    )
