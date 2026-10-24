#!/usr/bin/env python3
"""Standalone offline verifier for one Agent Middleware API receipt bundle.

What it checks: that the Ed25519 signature in the bundle verifies over the
bundle's `signing_input` bytes with the public key named by the signed `kid`,
that the envelope around those bytes agrees with them, that an optional
expected issuer label matches, and that a `payload_hash` statement matches
the signed payload. Exit 0 means verified, 1 means the bundle is well-formed
but does not hold together (bad signature or contradicting envelope - treat
as tampered), 2 means verification could not run (unknown key, malformed
input, bad usage).

A signature tagged `Ed25519-sha256-envelope:<base64>` (used for signing
inputs over 4096 bytes) is plain Ed25519 over the canonical JSON of
{"alg": "Ed25519-sha256-envelope", "kid": <kid>, "payload_sha256": <hex
SHA-256 of signing_input>}; it is accepted only above that threshold, so a
small receipt has one valid signature form. Any other tag is
malformed.

What it does NOT prove: exit 0 proves the receipt matches the key snapshot
you supplied. It does not authenticate who supplied that snapshot - fetch the
keys once over a channel you trust, or pin them out of band, before relying
on issuer identity. It signs hashes, not content: the signature covers
SHA-256 hashes of the request and the tool's response, never the response
body itself, and it says nothing about whether the tool's output was
correct.

This script is deliberately standalone: Python 3.9+ standard library plus
the `cryptography` package only. It imports nothing from the gateway or its
SDK, and it never makes a network call - the key set must already be on
disk (pass --keys). Usage:

    python3 -m pip install cryptography
    python3 verify_receipt.py --bundle receipt.json --keys trust-keys.json \
        --expect-issuer https://api.thisisatest.tech
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import sys

CANONICALIZATION = "awi-canonical-json/1"
ED25519_RAW_LEN = 32
ED25519_SIGNATURE_LEN = 64

# Bounded signing envelope, mirrored from b2a_sdk.receipt_verifier: an
# untagged signature covers signing_input; an envelope-tagged one covers the
# canonical {alg, kid, payload_sha256} record, and only above the threshold.
SIGNATURE_ALG = "Ed25519"
SIGNATURE_ENVELOPE_ALG = "Ed25519-sha256-envelope"
SIGNATURE_ENVELOPE_THRESHOLD_BYTES = 4096

EXIT_VERIFIED = 0
EXIT_INVALID = 1
EXIT_UNDETERMINED = 2

# Statuses that mean "do not accept this bundle".
REJECTED = ("invalid", "mismatch")


def canonical_json(payload):
    """Sorted-key compact JSON, the byte layout the gateway signs."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _b64decode(value, urlsafe=False):
    if not isinstance(value, str):
        return None
    try:
        if urlsafe:
            padded = value + "=" * (-len(value) % 4)
            return base64.urlsafe_b64decode(padded)
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None


def split_signature(value):
    """Return (scheme, base64). Standard base64 has no ':', so an untagged
    signature is unambiguous."""
    scheme, separator, encoded = value.partition(":")
    if not separator:
        return SIGNATURE_ALG, value
    return scheme, encoded


def signature_message(scheme, kid, signing_bytes):
    """The bytes a signature of `scheme` covers, or None if it cannot apply:
    an unknown scheme, or an envelope over a payload small enough to have
    been signed directly (no downgrade confusion)."""
    if scheme == SIGNATURE_ALG:
        return signing_bytes
    if (
        scheme == SIGNATURE_ENVELOPE_ALG
        and len(signing_bytes) > SIGNATURE_ENVELOPE_THRESHOLD_BYTES
    ):
        envelope = {
            "alg": SIGNATURE_ENVELOPE_ALG,
            "kid": kid,
            "payload_sha256": hashlib.sha256(signing_bytes).hexdigest(),
        }
        return canonical_json(envelope).encode("utf-8")
    return None


def _has_lone_surrogate(value):
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


def key_set_from_document(document):
    """Map kid to raw 32-byte keys. Drops `disabled` keys: the gateway
    refuses to verify against them, so a cached copy must not either."""
    if not isinstance(document, dict):
        raise ValueError("key set document must be a JSON object")
    entries = document.get("keys")
    if not isinstance(entries, list):
        raise ValueError("key set document has no 'keys' list")
    keys = {}
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
        raw = _b64decode(entry.get("public_key_b64", ""))
        if raw is None or len(raw) != ED25519_RAW_LEN:
            jwk = entry.get("jwk")
            if isinstance(jwk, dict) and jwk.get("crv") == "Ed25519":
                raw = _b64decode(jwk.get("x", ""), urlsafe=True)
        if raw is None or len(raw) != ED25519_RAW_LEN:
            continue
        keys[kid] = raw
    return keys


def _ed25519_verify(raw_public_key, signature, signing_bytes):
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
    except ImportError as exc:
        raise ValueError(
            "Ed25519 verification needs the 'cryptography' package: "
            "python3 -m pip install cryptography"
        ) from exc
    try:
        Ed25519PublicKey.from_public_bytes(raw_public_key).verify(
            signature, signing_bytes
        )
    except InvalidSignature:
        return False
    except Exception as exc:
        # A verifier failure is not a cryptographic negative: report it as
        # "could not determine", never as forgery.
        raise ValueError("Ed25519 verifier failed unexpectedly") from exc
    return True


def verify_bundle(bundle, key_set, expected_issuer=None):
    """Return (status, reason, receipt_id, key_id, claims).

    Key selection reads the kid inside the *signed* payload, never the
    unauthenticated envelope, so a relabelled envelope cannot steer it.
    """
    if isinstance(bundle, (str, bytes)):
        try:
            bundle = json.loads(bundle)
        except (ValueError, RecursionError):
            return ("malformed", "bundle is not valid JSON", None, None, {})
    if not isinstance(bundle, dict):
        return ("malformed", "bundle must be a JSON object", None, None, {})

    signing_input = bundle.get("signing_input")
    signature_b64 = bundle.get("signature")
    envelope_key_id = bundle.get("kid")
    if not isinstance(signing_input, str) or not signing_input:
        return ("malformed", "bundle has no signing_input", None, None, {})
    if not isinstance(signature_b64, str) or not signature_b64:
        return ("malformed", "bundle has no signature", None, None, {})
    if not isinstance(envelope_key_id, str) or not envelope_key_id:
        return ("malformed", "bundle has no kid", None, None, {})

    if expected_issuer is not None and bundle.get("issuer") != expected_issuer:
        return (
            "mismatch",
            "issuer mismatch: bundle claims %r" % (bundle.get("issuer"),),
            None,
            envelope_key_id,
            {},
        )

    try:
        payload = json.loads(signing_input)
    except (ValueError, RecursionError):
        return (
            "malformed",
            "signing_input is not valid JSON",
            None,
            envelope_key_id,
            {},
        )
    if not isinstance(payload, dict):
        return (
            "malformed",
            "signing_input is not a JSON object",
            None,
            envelope_key_id,
            {},
        )
    if _has_lone_surrogate(payload):
        return (
            "malformed",
            "signing_input contains invalid Unicode text",
            None,
            envelope_key_id,
            {},
        )

    key_id = payload.get("kid")
    if not isinstance(key_id, str) or not key_id:
        return ("malformed", "signed payload has no kid", None, envelope_key_id, {})

    if payload.get("alg", "Ed25519") != "Ed25519":
        return (
            "unsupported",
            "unsupported signature algorithm: %r" % (payload.get("alg"),),
            None,
            key_id,
            {},
        )
    signed_canon = payload.get("canonicalization")
    if isinstance(signed_canon, str) and signed_canon != CANONICALIZATION:
        return (
            "unsupported",
            "unsupported canonicalization: %r (this verifier "
            "implements %r)" % (signed_canon, CANONICALIZATION),
            None,
            key_id,
            {},
        )

    signature_scheme, encoded_signature = split_signature(signature_b64)
    if signature_scheme not in (SIGNATURE_ALG, SIGNATURE_ENVELOPE_ALG):
        # The tag sits outside the signed bytes, so an unknown one is a
        # damaged signature field, not a statement about this verifier.
        return (
            "malformed",
            "unknown signature scheme: %r" % (signature_scheme,),
            None,
            key_id,
            {},
        )
    signature = _b64decode(encoded_signature)
    if signature is None:
        return ("malformed", "signature is not valid base64", None, key_id, {})
    if len(signature) != ED25519_SIGNATURE_LEN:
        return (
            "malformed",
            "signature is not a 64-byte Ed25519 signature",
            None,
            key_id,
            {},
        )

    raw_public_key = key_set.get(key_id)
    if raw_public_key is None:
        # The payload names a key we do not hold - normally "cannot judge".
        # But first check whether the envelope names a key we *do* hold that
        # signs these bytes: then the bundle contradicts itself, which is a
        # verdict (mismatch), not a gap.
        envelope_key = (
            key_set.get(envelope_key_id) if envelope_key_id != key_id else None
        )
        if envelope_key is not None and len(envelope_key) == ED25519_RAW_LEN:
            try:
                probe = signing_input.encode("utf-8")
            except UnicodeEncodeError:
                probe = None
            if probe is not None:
                probe = signature_message(signature_scheme, envelope_key_id, probe)
            if probe is not None and _ed25519_verify(envelope_key, signature, probe):
                return (
                    "mismatch",
                    "signed under kid %r but the signed payload names "
                    "kid %r" % (envelope_key_id, key_id),
                    None,
                    key_id,
                    {},
                )
        return (
            "unknown_key",
            "no published key for kid %r" % (key_id,),
            None,
            key_id,
            {},
        )
    if len(raw_public_key) != ED25519_RAW_LEN:
        return (
            "malformed",
            "public key for kid %r is not a 32-byte Ed25519 key" % (key_id,),
            None,
            key_id,
            {},
        )

    try:
        signing_bytes = signing_input.encode("utf-8")
    except UnicodeEncodeError:
        return ("malformed", "signing_input is not valid UTF-8 text", None, key_id, {})

    # The signature covers signing_input verbatim (or, for the envelope, its
    # SHA-256): the original string is used, never a re-serialization that
    # could silently "fix" altered bytes.
    signed_message = signature_message(signature_scheme, key_id, signing_bytes)
    if signed_message is None:
        return (
            "mismatch",
            "envelope signature on a signing_input small enough to sign directly",
            None,
            key_id,
            {},
        )
    if not _ed25519_verify(raw_public_key, signature, signed_message):
        return (
            "invalid",
            "signature does not verify over signing_input",
            None,
            key_id,
            {},
        )

    # The signature holds, so the payload is authenticated ground truth.
    # Every envelope value describing it is cross-checked here; disagreement
    # is mismatch (a finding about the bundle), never "unsupported".
    if envelope_key_id != key_id:
        return (
            "mismatch",
            "bundle names a different kid than the signed payload",
            None,
            key_id,
            {},
        )
    if bundle.get("alg", "Ed25519") != "Ed25519":
        return (
            "mismatch",
            "bundle claims algorithm %r but the signed payload declares "
            "'Ed25519'" % (bundle.get("alg"),),
            None,
            key_id,
            {},
        )
    expected_canon = signed_canon if isinstance(signed_canon, str) else CANONICALIZATION
    if bundle.get("canonicalization", CANONICALIZATION) != expected_canon:
        return (
            "mismatch",
            "bundle claims canonicalization %r but the signed payload "
            "verifies under %r" % (bundle.get("canonicalization"), expected_canon),
            None,
            key_id,
            {},
        )

    stated_hash = payload.get("payload_hash")
    if isinstance(stated_hash, str):
        inner = {k: v for k, v in payload.items() if k != "payload_hash"}
        try:
            canonical = canonical_json(inner)
        except (RecursionError, ValueError):
            return (
                "malformed",
                "signed payload exceeds the supported JSON nesting depth",
                None,
                key_id,
                {},
            )
        computed = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if computed != stated_hash:
            return (
                "mismatch",
                "payload_hash does not match the signed payload",
                None,
                key_id,
                {},
            )

    receipt_id = payload.get("receipt_id")
    if not isinstance(receipt_id, str) or not receipt_id:
        return ("malformed", "signed payload has no receipt_id", None, key_id, {})
    envelope_receipt_id = bundle.get("receipt_id")
    if envelope_receipt_id is not None and envelope_receipt_id != receipt_id:
        return (
            "mismatch",
            "bundle receipt_id does not match the signed receipt_id",
            receipt_id,
            key_id,
            {},
        )

    return ("verified", None, receipt_id, key_id, payload)


def _read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="verify_receipt.py",
        description="Verify one portable receipt bundle offline against a "
        "local key snapshot. No account, no network.",
    )
    parser.add_argument(
        "--bundle", required=True, help="Path to the portable receipt bundle JSON."
    )
    parser.add_argument(
        "--keys",
        required=True,
        help="Path to a locally held trust-keys.json snapshot (fully offline).",
    )
    parser.add_argument(
        "--expect-issuer",
        help="Require the bundle's unsigned issuer label to "
        "equal this origin. A consistency guard, not "
        "issuer authentication.",
    )
    args = parser.parse_args(argv)

    try:
        bundle = _read_json(args.bundle)
    except (OSError, ValueError) as exc:
        print("could not read bundle: %s" % (exc,), file=sys.stderr)
        return EXIT_UNDETERMINED
    try:
        key_set = key_set_from_document(_read_json(args.keys))
    except (OSError, ValueError) as exc:
        print("could not load key set: %s" % (exc,), file=sys.stderr)
        return EXIT_UNDETERMINED

    try:
        status, reason, receipt_id, key_id, claims = verify_bundle(
            bundle, key_set, expected_issuer=args.expect_issuer
        )
    except ValueError as exc:
        print("verification could not run: %s" % (exc,), file=sys.stderr)
        return EXIT_UNDETERMINED

    if status == "verified":
        print("VERIFIED  %s" % (receipt_id,))
        print("  signed by   %s" % (key_id,))
        print("  permit      %s" % (claims.get("permit_id"),))
        print("  tool        %s" % (claims.get("tool"),))
        print("  outcome     %s" % (claims.get("outcome"),))
        if claims.get("reason_code"):
            print("  reason      %s" % (claims.get("reason_code"),))
        print(
            "  credits     %s charged of %s authorized"
            % (claims.get("credits_charged"), claims.get("credits_authorized"))
        )
        print("  at          %s" % (claims.get("created_at"),))
        return EXIT_VERIFIED
    print("%s  %s" % (status.upper(), reason), file=sys.stderr)
    if status in REJECTED:
        return EXIT_INVALID
    return EXIT_UNDETERMINED


if __name__ == "__main__":
    raise SystemExit(main())
