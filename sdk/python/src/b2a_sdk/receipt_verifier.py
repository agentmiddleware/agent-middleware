"""Offline verification of trust-plane receipts.

A receipt is portable evidence when someone can check its signature using a
public key they trust. Everything in this module therefore runs with no
network, no database, and no credential: it takes a portable receipt bundle
plus a caller-supplied key set and answers whether the signature holds. Key
distribution and issuer identity remain the caller's trust decision.

Two rules keep that answer meaningful:

1. Every field this module reports is read out of the *signed bytes*. Nothing
   is taken from the enclosing bundle, which is unauthenticated envelope data.
   A bundle claiming ``receipt_id: X`` around a payload for ``Y`` is rejected.
2. Failure is never silently "false". An unreachable key set, an unknown key,
   and a bad signature are different situations, and a caller that cannot tell
   them apart will eventually read an outage as fraud. See
   :class:`VerificationStatus`.

Typical use::

    from b2a_sdk.receipt_verifier import verify_bundle

    result = verify_bundle(bundle, key_set)
    if result.ok:
        print(result.claims["tool"], result.claims["credits_charged"])

The only dependency beyond the standard library is ``cryptography``, installed
via the ``verify`` extra::

    pip install "b2a-sdk[verify]"
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "CANONICALIZATION",
    "VerificationError",
    "VerificationResult",
    "VerificationStatus",
    "canonical_json",
    "key_set_from_document",
    "verify_bundle",
    "verify_ed25519",
]

# The canonicalization contract these rules implement. A bundle declaring a
# different version is refused rather than verified under rules it was not
# signed with.
CANONICALIZATION = "awi-canonical-json/1"

_ED25519_RAW_LEN = 32
_ED25519_SIGNATURE_LEN = 64


class VerificationStatus(str, Enum):
    """Why verification concluded what it did.

    ``VERIFIED`` and ``INVALID`` are cryptographic claims. ``MISMATCH`` means
    the envelope, signed payload, or caller expectation disagrees despite no
    demonstrated signature failure. The remaining statuses describe missing
    input/capability and must not be reported as evidence of tampering.
    """

    VERIFIED = "verified"
    INVALID = "invalid"
    MALFORMED = "malformed"
    UNKNOWN_KEY = "unknown_key"
    # Kept for SDK compatibility. The current raw ``kid -> bytes`` key-set
    # representation cannot carry lifecycle metadata, so disabled keys are
    # filtered before verification and resolve as UNKNOWN_KEY instead.
    KEY_REVOKED = "key_revoked"
    MISMATCH = "mismatch"
    UNSUPPORTED = "unsupported"


class VerificationError(Exception):
    """Raised only for programming errors, never for a failed verification."""


@dataclass(frozen=True)
class VerificationResult:
    """Outcome of checking one receipt bundle."""

    status: VerificationStatus
    reason: str | None = None
    receipt_id: str | None = None
    key_id: str | None = None
    #: Fields parsed out of the signed bytes. Populated only when ``ok``.
    claims: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True only when the signature verified over the signed bytes."""
        return self.status is VerificationStatus.VERIFIED

    @property
    def is_tampered(self) -> bool:
        """True only for a bundle that is well-formed but does not verify.

        Distinct from :attr:`ok` being False, which also covers "cannot tell".

        Deliberately narrow: this is a claim that a *signature failed*. It does
        not cover ``MISMATCH``, because ``MISMATCH`` also carries caller-
        expectation failures such as ``expected_issuer`` -- and reporting "you
        asked for a different issuer" as forgery would be the same category
        error, in the opposite direction, as reporting a missing key as
        tampering. Use :attr:`is_rejected` to decide whether to accept a
        bundle; use this only to assert that the cryptography failed.
        """
        return self.status is VerificationStatus.INVALID

    @property
    def is_rejected(self) -> bool:
        """True when the bundle must not be accepted as evidence.

        The verifier reached a verdict and the verdict is no. Covers a failed
        signature (``INVALID``) and a bundle that does not hold together
        (``MISMATCH`` -- the signature verified but the envelope, the signed
        payload, or an explicit caller expectation disagree).

        This is the property most callers want. It is distinct from ``not ok``,
        which additionally sweeps in ``UNKNOWN_KEY``, ``MALFORMED`` and
        ``UNSUPPORTED`` -- the "I cannot judge this" statuses, where the right
        response is to fetch keys or upgrade, not to allege fraud.
        """
        return self.status in (
            VerificationStatus.INVALID,
            VerificationStatus.MISMATCH,
        )


def canonical_json(payload: Any) -> str:
    """Serialize already-decoded JSON the way the trust plane signs it.

    The rules are: keys sorted at every level, no insignificant whitespace,
    and non-ASCII escaped. Numbers and timestamps are already strings by the
    time they reach a signed payload, so no numeric formatting rules are
    needed here — which is what makes this reimplementable in any language.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _b64decode(value: str, *, urlsafe: bool = False) -> bytes | None:
    """Strict base64 decode that returns None instead of raising."""
    if not isinstance(value, str):
        return None
    try:
        if urlsafe:
            # JWK members omit padding; restore it before decoding.
            padded = value + "=" * (-len(value) % 4)
            return base64.urlsafe_b64decode(padded)
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None


def _contains_invalid_unicode(value: Any) -> bool:
    """Return True when decoded JSON contains a lone Unicode surrogate."""
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            try:
                current.encode("utf-8")
            except UnicodeEncodeError:
                return True
        elif isinstance(current, dict):
            pending.extend(current.keys())
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return False


def key_set_from_document(document: Any) -> dict[str, bytes]:
    """Extract usable Ed25519 public keys from a ``trust-keys.json`` document.

    Accepts either the ``public_key_b64`` form or the JWK mirror. Keys the
    issuing plane marks ``disabled`` are dropped, matching the plane's own
    refusal to verify against them — a revoked key must not keep validating
    receipts just because a cached copy of the document still lists it.
    """
    if not isinstance(document, dict):
        raise VerificationError("key set document must be a JSON object")

    entries = document.get("keys")
    if not isinstance(entries, list):
        raise VerificationError("key set document has no 'keys' list")

    keys: dict[str, bytes] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        kid = entry.get("kid") or entry.get("key_id")
        if not isinstance(kid, str) or not kid:
            continue
        if entry.get("status") == "disabled":
            continue
        if entry.get("alg") not in (None, "Ed25519"):
            continue

        # Fall through to the JWK mirror whenever the raw form is absent,
        # unreadable, or the wrong length — an empty string decodes cleanly to
        # zero bytes, so "decoded without error" is not enough to accept it.
        raw = _b64decode(entry.get("public_key_b64", ""))
        if raw is None or len(raw) != _ED25519_RAW_LEN:
            jwk = entry.get("jwk")
            if isinstance(jwk, dict) and jwk.get("crv") == "Ed25519":
                raw = _b64decode(jwk.get("x", ""), urlsafe=True)
        if raw is None or len(raw) != _ED25519_RAW_LEN:
            continue
        keys[kid] = raw
    return keys


def _load_verifier(raw_public_key: bytes):
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise VerificationError(
            "Ed25519 verification requires the 'cryptography' package. "
            'Install it with: pip install "b2a-sdk[verify]"'
        ) from exc
    return Ed25519PublicKey.from_public_bytes(raw_public_key)


def _verify_ed25519_signature(
    raw_public_key: bytes,
    signature: bytes,
    signing_bytes: bytes,
) -> bool:
    """Return False only for cryptography's explicit bad-signature result."""
    try:
        from cryptography.exceptions import InvalidSignature
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise VerificationError(
            "Ed25519 verification requires the 'cryptography' package. "
            'Install it with: pip install "b2a-sdk[verify]"'
        ) from exc

    try:
        _load_verifier(raw_public_key).verify(signature, signing_bytes)
    except VerificationError:
        raise
    except InvalidSignature:
        return False
    except Exception as exc:
        # A backend/programming failure is not a cryptographic negative. Let
        # the caller report verifier failure rather than mislabel evidence as
        # forged.
        raise VerificationError("Ed25519 verifier failed unexpectedly") from exc
    return True


def verify_ed25519(
    raw_public_key: bytes,
    signature: bytes,
    signing_bytes: bytes,
) -> bool:
    """Verify one raw Ed25519 signature over ``signing_bytes``.

    Public seam for other SDK modules (e.g. the edge client's local permit
    validator) that need the same primitive :func:`verify_bundle` is built on,
    without reaching into this module's private helpers. Delegates to the
    private implementation with identical signature and behavior: returns
    ``False`` only for cryptography's explicit bad-signature result, and
    raises :class:`VerificationError` when the verifier itself is unavailable
    (``cryptography`` not installed) or fails unexpectedly — a verifier
    failure is never reported as a cryptographic negative.
    """
    return _verify_ed25519_signature(raw_public_key, signature, signing_bytes)


def _fail(
    status: VerificationStatus,
    reason: str,
    *,
    receipt_id: str | None = None,
    key_id: str | None = None,
) -> VerificationResult:
    return VerificationResult(
        status=status,
        reason=reason,
        receipt_id=receipt_id,
        key_id=key_id,
    )


def verify_bundle(
    bundle: Any,
    key_set: dict[str, bytes],
    *,
    expected_issuer: str | None = None,
) -> VerificationResult:
    """Verify a portable receipt bundle against published public keys.

    Args:
        bundle: The object returned by ``GET /v1/receipts/{id}/portable``,
            either as a dict or a JSON string.
        key_set: Mapping of key id to raw 32-byte Ed25519 public key, as
            produced by :func:`key_set_from_document`.
        expected_issuer: When given, the unauthenticated envelope's ``issuer``
            label must match. This catches accidental relabeling but does not
            authenticate the issuer; that requires a trusted public key.

    Returns:
        A :class:`VerificationResult`. Inspect ``status`` rather than only
        ``ok`` when the difference between "invalid" and "cannot tell"
        matters.
    """
    if isinstance(bundle, str | bytes):
        try:
            bundle = json.loads(bundle)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError, ValueError):
            return _fail(VerificationStatus.MALFORMED, "bundle is not valid JSON")
    if not isinstance(bundle, dict):
        return _fail(VerificationStatus.MALFORMED, "bundle must be a JSON object")

    signing_input = bundle.get("signing_input")
    signature_b64 = bundle.get("signature")
    envelope_key_id = bundle.get("kid")
    if not isinstance(signing_input, str) or not signing_input:
        return _fail(VerificationStatus.MALFORMED, "bundle has no signing_input")
    if not isinstance(signature_b64, str) or not signature_b64:
        return _fail(VerificationStatus.MALFORMED, "bundle has no signature")
    if not isinstance(envelope_key_id, str) or not envelope_key_id:
        return _fail(VerificationStatus.MALFORMED, "bundle has no kid")

    if expected_issuer is not None and bundle.get("issuer") != expected_issuer:
        return _fail(
            VerificationStatus.MISMATCH,
            f"issuer mismatch: bundle claims {bundle.get('issuer')!r}",
            key_id=envelope_key_id,
        )

    # Parse the signed bytes before anything else that can decide an outcome.
    #
    # Ordering is the whole security property here. The envelope is
    # unauthenticated: anyone in the transport path can edit it. So the
    # capability gates below (algorithm, canonicalization) and the key
    # selection must read the *signed* payload, never the envelope. An earlier
    # version read `alg` and `canonicalization` from the envelope and
    # short-circuited to UNSUPPORTED before the signature was ever checked,
    # which let one edited byte turn a genuine receipt into "your verifier is
    # too old" -- an attacker-chosen status, and the exact integrity/capability
    # inversion this taxonomy exists to prevent. Envelope values are still
    # checked, but afterwards, and disagreement is MISMATCH (a finding about
    # the bundle) rather than UNSUPPORTED (a statement about this verifier).
    try:
        payload = json.loads(signing_input)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError, ValueError):
        return _fail(
            VerificationStatus.MALFORMED,
            "signing_input is not valid JSON",
            key_id=envelope_key_id,
        )
    if not isinstance(payload, dict):
        return _fail(
            VerificationStatus.MALFORMED,
            "signing_input is not a JSON object",
            key_id=envelope_key_id,
        )
    if _contains_invalid_unicode(payload):
        return _fail(
            VerificationStatus.MALFORMED,
            "signing_input contains invalid Unicode text",
            key_id=envelope_key_id,
        )

    # Select the verification key by the kid inside the signed bytes. If the
    # signature then verifies, that kid is authenticated retroactively, so a
    # relabelled envelope cannot steer key selection at all.
    key_id = payload.get("kid")
    if not isinstance(key_id, str) or not key_id:
        return _fail(
            VerificationStatus.MALFORMED,
            "signed payload has no kid",
            key_id=envelope_key_id,
        )

    algorithm = payload.get("alg", "Ed25519")
    if algorithm != "Ed25519":
        # Signer-declared, and therefore a genuine statement about what this
        # verifier can do.
        return _fail(
            VerificationStatus.UNSUPPORTED,
            f"unsupported signature algorithm: {algorithm!r}",
            key_id=key_id,
        )

    signed_canonicalization = payload.get("canonicalization")
    if isinstance(signed_canonicalization, str) and signed_canonicalization != CANONICALIZATION:
        # Verifying under rules the payload was not signed with would produce a
        # confident answer to the wrong question. Only trusted because it is
        # signed; receipts predating this field carry no counterpart and are
        # handled by the envelope cross-check after verification.
        return _fail(
            VerificationStatus.UNSUPPORTED,
            f"unsupported canonicalization: {signed_canonicalization!r} (this "
            f"verifier implements {CANONICALIZATION!r})",
            key_id=key_id,
        )

    signature = _b64decode(signature_b64)
    if signature is None:
        return _fail(
            VerificationStatus.MALFORMED,
            "signature is not valid base64",
            key_id=key_id,
        )
    if len(signature) != _ED25519_SIGNATURE_LEN:
        return _fail(
            VerificationStatus.MALFORMED,
            "signature is not a 64-byte Ed25519 signature",
            key_id=key_id,
        )

    raw_public_key = key_set.get(key_id)
    if raw_public_key is None:
        # The payload names a key this verifier does not hold. Normally that is
        # "cannot judge" -- but first check whether the envelope points at a
        # key we *do* hold that actually signs these bytes. If it does, the
        # bundle is provably self-contradicting (signed under one key while
        # claiming another), and that is a verdict, not a gap. Skipping this
        # would let a forger name a nonexistent kid to downgrade a detectable
        # inconsistency into "refresh your key set and try again".
        envelope_key = key_set.get(envelope_key_id) if envelope_key_id != key_id else None
        if envelope_key is not None and len(envelope_key) == _ED25519_RAW_LEN:
            try:
                probe_bytes = signing_input.encode("utf-8")
            except UnicodeEncodeError:
                probe_bytes = None
            if probe_bytes is not None and _verify_ed25519_signature(
                envelope_key, signature, probe_bytes
            ):
                return _fail(
                    VerificationStatus.MISMATCH,
                    f"signed under kid {envelope_key_id!r} but the signed "
                    f"payload names kid {key_id!r}",
                    key_id=key_id,
                )
        # Not a verdict on the receipt: this verifier simply does not hold the
        # key. Refresh the key set from the issuer before drawing conclusions.
        return _fail(
            VerificationStatus.UNKNOWN_KEY,
            f"no published key for kid {key_id!r}",
            key_id=key_id,
        )
    if len(raw_public_key) != _ED25519_RAW_LEN:
        return _fail(
            VerificationStatus.MALFORMED,
            f"public key for kid {key_id!r} is not a 32-byte Ed25519 key",
            key_id=key_id,
        )

    try:
        signing_bytes = signing_input.encode("utf-8")
    except UnicodeEncodeError:
        return _fail(
            VerificationStatus.MALFORMED,
            "signing_input is not valid UTF-8 text",
            key_id=key_id,
        )

    # The signature covers signing_input verbatim. Re-serializing the parsed
    # object here would silently "fix" a payload whose bytes were altered in a
    # way that survives a JSON round trip, so the original string is used.
    if not _verify_ed25519_signature(raw_public_key, signature, signing_bytes):
        return _fail(
            VerificationStatus.INVALID,
            "signature does not verify over signing_input",
            key_id=key_id,
        )

    # The signature holds, so the signed payload is now authenticated ground
    # truth. Every envelope value that claims to describe it is checked against
    # it here. Disagreement is a finding about the bundle -- MISMATCH -- never
    # UNSUPPORTED, because at this point the verifier demonstrably *could*
    # judge the evidence.
    if envelope_key_id != key_id:
        return _fail(
            VerificationStatus.MISMATCH,
            "bundle names a different kid than the signed payload",
            key_id=key_id,
        )

    envelope_algorithm = bundle.get("alg", "Ed25519")
    if envelope_algorithm != algorithm:
        return _fail(
            VerificationStatus.MISMATCH,
            f"bundle claims algorithm {envelope_algorithm!r} but the signed "
            f"payload declares {algorithm!r}",
            key_id=key_id,
        )

    envelope_canonicalization = bundle.get("canonicalization", CANONICALIZATION)
    expected_canonicalization = (
        signed_canonicalization if isinstance(signed_canonicalization, str) else CANONICALIZATION
    )
    if envelope_canonicalization != expected_canonicalization:
        return _fail(
            VerificationStatus.MISMATCH,
            f"bundle claims canonicalization {envelope_canonicalization!r} but "
            f"the signed payload verifies under {expected_canonicalization!r}",
            key_id=key_id,
        )

    stated_hash = payload.get("payload_hash")
    if isinstance(stated_hash, str):
        inner = {k: v for k, v in payload.items() if k != "payload_hash"}
        try:
            canonical = canonical_json(inner)
        except (RecursionError, ValueError):
            return _fail(
                VerificationStatus.MALFORMED,
                "signed payload exceeds the supported JSON nesting depth",
                key_id=key_id,
            )
        computed = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if computed != stated_hash:
            return _fail(
                VerificationStatus.MISMATCH,
                "payload_hash does not match the signed payload",
                key_id=key_id,
            )

    receipt_id = payload.get("receipt_id")
    if not isinstance(receipt_id, str) or not receipt_id:
        return _fail(
            VerificationStatus.MALFORMED,
            "signed payload has no receipt_id",
            key_id=key_id,
        )

    # Envelope fields are unauthenticated. If the bundle advertises a receipt
    # id other than the signed one, whoever presented it is describing the
    # evidence incorrectly — refuse rather than quietly trusting the signed
    # value and letting the mismatch pass unnoticed.
    envelope_receipt_id = bundle.get("receipt_id")
    if envelope_receipt_id is not None and envelope_receipt_id != receipt_id:
        return _fail(
            VerificationStatus.MISMATCH,
            "bundle receipt_id does not match the signed receipt_id",
            receipt_id=receipt_id,
            key_id=key_id,
        )

    return VerificationResult(
        status=VerificationStatus.VERIFIED,
        receipt_id=receipt_id,
        key_id=key_id,
        claims=payload,
    )
