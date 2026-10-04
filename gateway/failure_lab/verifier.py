"""An independent receipt verifier that imports neither the app nor its SDK.

The PRD is explicit that signature validity must not be displayed as proof
that a business action occurred, and that the gateway's own verification
library must not be the only thing checking the gateway's own signatures.
This module therefore:

* depends on ``cryptography`` and the standard library only -- not on
  ``app`` and not on ``b2a_sdk``;
* is a from-scratch reading of the receipt envelope, so a bug shared between
  the signer and the shipped verifier does not cancel out here;
* returns **three separate claims**, never one boolean.

The three claims
----------------

``SIGNATURE_VALID``
    The Ed25519 signature verifies over the exact ``signing_input`` bytes,
    under the key named *inside* those signed bytes. This says the holder of
    that private key signed this statement. Nothing more.

``ISSUER_TRUST_ESTABLISHED``
    Whether the public key can be trusted to belong to the claimed issuer.
    Fetching it from the same origin being audited does **not** establish
    that: an origin that can serve receipts can serve keys that validate
    forged receipts. Only an out-of-band pin does. The caller says which it
    supplied, and lying to this function is the caller's problem, not a gap
    the function hides.

``DOWNSTREAM_EXECUTION_ESTABLISHED``
    Whether the business action actually happened downstream. A signature can
    never establish this. It is established only by an independent
    observation of the downstream system -- in this lab, the effect ledger --
    supplied by the caller and checked for consistency with the receipt.

Signed bytes versus envelope
----------------------------

The portable bundle carries fields outside the signature (``issuer``,
``kid``, ``canonicalization``, ``keys_url``, ``schema_version``,
``receipt_id``). Editing those does not break the signature, and pretending
otherwise would be the exact overclaim this module exists to prevent.
:func:`field_coverage` reports, field by field, which side of the signature
each one is on -- which is what the tampering scenario measures.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

ED25519_RAW_LENGTH = 32
ED25519_SIGNATURE_LENGTH = 64


class ClaimStatus(str, Enum):
    ESTABLISHED = "ESTABLISHED"
    NOT_ESTABLISHED = "NOT_ESTABLISHED"
    FAILED = "FAILED"


class KeySource(str, Enum):
    #: Fetched over TLS from the origin that issued the receipt. Sufficient to
    #: check a signature, insufficient to establish issuer trust.
    ISSUER_ORIGIN = "issuer_origin"
    #: Pinned out of band (a key distributed through a channel the issuing
    #: origin does not control).
    OUT_OF_BAND_PIN = "out_of_band_pin"


@dataclass(frozen=True)
class Claim:
    name: str
    status: ClaimStatus
    reason: str

    @property
    def established(self) -> bool:
        return self.status is ClaimStatus.ESTABLISHED

    def as_dict(self) -> dict[str, Any]:
        return {"claim": self.name, "status": self.status.value, "reason": self.reason}


@dataclass(frozen=True)
class VerificationReport:
    signature: Claim
    issuer_trust: Claim
    downstream_execution: Claim
    key_id: str | None = None
    envelope_key_id: str | None = None
    signed_payload: dict[str, Any] | None = None
    #: Envelope keys whose value disagrees with the signed payload's.
    envelope_disagreements: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def signature_valid(self) -> bool:
        return self.signature.established

    def as_dict(self) -> dict[str, Any]:
        return {
            "claims": [
                self.signature.as_dict(),
                self.issuer_trust.as_dict(),
                self.downstream_execution.as_dict(),
            ],
            "key_id": self.key_id,
            "envelope_key_id": self.envelope_key_id,
            "envelope_disagreements": self.envelope_disagreements,
            "notes": self.notes,
        }

    def summary_lines(self) -> list[str]:
        return [
            f"SIGNATURE VALID:                 {self.signature.status.value}",
            f"ISSUER TRUST ESTABLISHED:        {self.issuer_trust.status.value}",
            f"DOWNSTREAM EXECUTION ESTABLISHED:{self.downstream_execution.status.value}",
        ]


def _b64(value: Any, *, urlsafe: bool = False) -> bytes | None:
    if not isinstance(value, str):
        return None
    padded = value + "=" * (-len(value) % 4)
    try:
        if urlsafe:
            return base64.urlsafe_b64decode(padded)
        return base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        return None


def parse_key_document(document: Any) -> dict[str, bytes]:
    """Read ``/.well-known/trust-keys.json`` into ``{kid: raw_public_key}``.

    Keys the issuing plane marks disabled are dropped: a revoked key must not
    keep validating receipts because a cached document still lists it.
    """
    if not isinstance(document, dict):
        raise ValueError("key document must be a JSON object")
    entries = document.get("keys")
    if not isinstance(entries, list):
        raise ValueError("key document has no 'keys' list")
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
        raw = _b64(entry.get("public_key_b64", ""))
        if raw is None or len(raw) != ED25519_RAW_LENGTH:
            jwk = entry.get("jwk")
            if isinstance(jwk, dict) and jwk.get("crv") == "Ed25519":
                raw = _b64(jwk.get("x", ""), urlsafe=True)
        if raw is None or len(raw) != ED25519_RAW_LENGTH:
            continue
        keys[kid] = raw
    return keys


#: Envelope fields that are *not* covered by the signature. Editing any of
#: these leaves a valid signature, which is a property of the format and must
#: be reported rather than hidden.
ENVELOPE_ONLY_FIELDS = (
    "schema_version",
    "issuer",
    "alg",
    "kid",
    "canonicalization",
    "signature",
    "keys_url",
    "receipt_id",
)


def field_coverage(bundle: dict[str, Any]) -> dict[str, str]:
    """Report which bundle fields the signature covers.

    ``signed`` means the field appears inside ``signing_input``.
    ``envelope_only`` means editing it cannot invalidate the signature.
    ``envelope_mirrors_signed`` means it appears on both sides, so an edit to
    the envelope copy is detectable as a disagreement even though the
    signature itself still verifies.
    """
    coverage: dict[str, str] = {}
    try:
        payload = json.loads(bundle.get("signing_input", ""))
    except (ValueError, TypeError):
        payload = {}
    signed_keys = set(payload) if isinstance(payload, dict) else set()
    for key in bundle:
        if key == "signing_input":
            coverage[key] = "signed"
        elif key in signed_keys:
            coverage[key] = "envelope_mirrors_signed"
        else:
            coverage[key] = "envelope_only"
    for key in signed_keys:
        coverage.setdefault(f"signing_input.{key}", "signed")
    return coverage


def verify(
    bundle: Any,
    keys: dict[str, bytes],
    *,
    key_source: KeySource = KeySource.ISSUER_ORIGIN,
    expected_issuer: str | None = None,
    downstream_observation: dict[str, Any] | None = None,
) -> VerificationReport:
    """Verify one portable receipt bundle and report the three claims.

    ``downstream_observation`` is an independent record of what the downstream
    system actually did, of the shape
    ``{"executions": int, "operation_id": str, "source": str}``. Without it,
    downstream execution is reported NOT_ESTABLISHED -- which is the correct
    answer for a signature alone, not a missing feature.
    """
    notes: list[str] = []

    def fail(reason: str, *, key_id: str | None = None) -> VerificationReport:
        return VerificationReport(
            signature=Claim("SIGNATURE_VALID", ClaimStatus.FAILED, reason),
            issuer_trust=Claim(
                "ISSUER_TRUST_ESTABLISHED",
                ClaimStatus.NOT_ESTABLISHED,
                "not evaluated: the signature did not verify",
            ),
            downstream_execution=Claim(
                "DOWNSTREAM_EXECUTION_ESTABLISHED",
                ClaimStatus.NOT_ESTABLISHED,
                "not evaluated: the signature did not verify",
            ),
            key_id=key_id,
            envelope_key_id=(bundle.get("kid") if isinstance(bundle, dict) else None),
            notes=notes,
        )

    if isinstance(bundle, (str, bytes)):
        try:
            bundle = json.loads(bundle)
        except (ValueError, UnicodeDecodeError):
            return fail("bundle is not valid JSON")
    if not isinstance(bundle, dict):
        return fail("bundle is not a JSON object")

    signing_input = bundle.get("signing_input")
    signature_b64 = bundle.get("signature")
    envelope_kid = bundle.get("kid")
    if not isinstance(signing_input, str) or not signing_input:
        return fail("bundle has no signing_input")
    if not isinstance(signature_b64, str) or not signature_b64:
        return fail("bundle has no signature")

    try:
        payload = json.loads(signing_input)
    except ValueError:
        return fail("signing_input is not valid JSON")
    if not isinstance(payload, dict):
        return fail("signing_input is not a JSON object")

    # The key is chosen by the kid inside the SIGNED bytes, never by the
    # envelope. An envelope anyone can edit must not steer key selection.
    key_id = payload.get("kid")
    if not isinstance(key_id, str) or not key_id:
        return fail("signed payload names no key", key_id=None)

    algorithm = payload.get("alg", "Ed25519")
    if algorithm != "Ed25519":
        return fail(f"unsupported signature algorithm {algorithm!r}", key_id=key_id)

    signature = _b64(signature_b64)
    if signature is None or len(signature) != ED25519_SIGNATURE_LENGTH:
        return fail("signature is not a 64-byte Ed25519 signature", key_id=key_id)

    raw_key = keys.get(key_id)
    if raw_key is None:
        return fail(f"no published key for kid {key_id!r}", key_id=key_id)
    if len(raw_key) != ED25519_RAW_LENGTH:
        return fail(f"key {key_id!r} is not a 32-byte Ed25519 key", key_id=key_id)

    try:
        Ed25519PublicKey.from_public_bytes(raw_key).verify(
            signature, signing_input.encode("utf-8")
        )
    except InvalidSignature:
        return fail("signature does not verify over signing_input", key_id=key_id)
    except (ValueError, UnicodeEncodeError) as exc:
        return fail(f"signature could not be checked: {exc}", key_id=key_id)

    signature_claim = Claim(
        "SIGNATURE_VALID",
        ClaimStatus.ESTABLISHED,
        f"Ed25519 signature verifies over signing_input under kid {key_id!r}",
    )

    # Envelope/payload disagreement: the signature still verifies, so this is
    # a finding about the bundle, not a failed signature.
    disagreements: list[str] = []
    for name in ENVELOPE_ONLY_FIELDS:
        if name in bundle and name in payload and bundle[name] != payload[name]:
            disagreements.append(name)
    if isinstance(envelope_kid, str) and envelope_kid != key_id:
        disagreements.append("kid")
        notes.append(
            f"envelope names kid {envelope_kid!r} but the signed payload names {key_id!r}; "
            "the signed payload wins"
        )
    if disagreements:
        notes.append(
            "envelope fields disagree with the signed payload: "
            + ", ".join(sorted(set(disagreements)))
        )

    if expected_issuer is not None and bundle.get("issuer") != expected_issuer:
        notes.append(
            f"envelope issuer {bundle.get('issuer')!r} is not the expected "
            f"{expected_issuer!r}; note that issuer is outside the signature"
        )

    if key_source is KeySource.OUT_OF_BAND_PIN:
        issuer_claim = Claim(
            "ISSUER_TRUST_ESTABLISHED",
            ClaimStatus.ESTABLISHED,
            "the verifying key was pinned out of band, so it does not depend on the audited origin",
        )
    else:
        issuer_claim = Claim(
            "ISSUER_TRUST_ESTABLISHED",
            ClaimStatus.NOT_ESTABLISHED,
            (
                "the verifying key was fetched from the same origin that issued the "
                "receipt; an origin that can serve receipts can serve keys that "
                "validate forged ones. Out-of-band key distribution is not implemented."
            ),
        )

    downstream_claim = _downstream_claim(payload, downstream_observation)

    return VerificationReport(
        signature=signature_claim,
        issuer_trust=issuer_claim,
        downstream_execution=downstream_claim,
        key_id=key_id,
        envelope_key_id=envelope_kid if isinstance(envelope_kid, str) else None,
        signed_payload=payload,
        envelope_disagreements=sorted(set(disagreements)),
        notes=notes,
    )


def _downstream_claim(
    payload: dict[str, Any], observation: dict[str, Any] | None
) -> Claim:
    outcome = payload.get("outcome")
    if observation is None:
        return Claim(
            "DOWNSTREAM_EXECUTION_ESTABLISHED",
            ClaimStatus.NOT_ESTABLISHED,
            (
                "a signature states what the gateway recorded, never what the "
                "downstream system did. No independent observation was supplied."
            ),
        )
    executions = observation.get("executions")
    source = observation.get("source", "unspecified independent observer")
    if not isinstance(executions, int):
        return Claim(
            "DOWNSTREAM_EXECUTION_ESTABLISHED",
            ClaimStatus.NOT_ESTABLISHED,
            "the supplied observation did not report an execution count",
        )
    if outcome == "delivery_uncertain":
        return Claim(
            "DOWNSTREAM_EXECUTION_ESTABLISHED",
            ClaimStatus.ESTABLISHED if executions > 0 else ClaimStatus.NOT_ESTABLISHED,
            (
                f"{source} observed {executions} execution(s). The receipt itself "
                "says delivery_uncertain and claims nothing either way, which is "
                "consistent with this observation."
            ),
        )
    if executions > 0:
        return Claim(
            "DOWNSTREAM_EXECUTION_ESTABLISHED",
            ClaimStatus.ESTABLISHED,
            f"{source} observed {executions} execution(s) of this operation",
        )
    if outcome == "success":
        return Claim(
            "DOWNSTREAM_EXECUTION_ESTABLISHED",
            ClaimStatus.FAILED,
            (
                f"the receipt records outcome 'success' but {source} observed no "
                "downstream execution -- the receipt and the world disagree"
            ),
        )
    return Claim(
        "DOWNSTREAM_EXECUTION_ESTABLISHED",
        ClaimStatus.NOT_ESTABLISHED,
        f"{source} observed no execution, consistent with outcome {outcome!r}",
    )


def tamper(bundle: dict[str, Any], field_path: str, value: Any) -> dict[str, Any]:
    """Return a copy of ``bundle`` with one field edited.

    ``field_path`` is either a top-level envelope key (``issuer``) or
    ``signing_input.<key>`` to edit a field inside the signed bytes. Editing a
    signed field re-serializes ``signing_input``, which is what makes the
    signature fail -- exactly what an attacker would have to do.
    """
    edited = json.loads(json.dumps(bundle))
    if field_path.startswith("signing_input."):
        name = field_path.split(".", 1)[1]
        payload = json.loads(edited["signing_input"])
        payload[name] = value
        edited["signing_input"] = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return edited
    edited[field_path] = value
    return edited


def report_as_dict(report: VerificationReport) -> dict[str, Any]:
    return asdict(report)


__all__ = [
    "Claim",
    "ClaimStatus",
    "ENVELOPE_ONLY_FIELDS",
    "KeySource",
    "VerificationReport",
    "field_coverage",
    "parse_key_document",
    "tamper",
    "verify",
]
