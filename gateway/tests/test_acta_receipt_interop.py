"""Tests for the ACTA receipt-envelope interop experiment.

The experiment (`examples/acta_receipt_interop.py`) answers
`docs/market-research-2026-08.md` §7 question 2 with running code. These
tests pin the two properties that make the answer trustworthy: the
transcoder refuses anything it cannot verify, and both signatures — the
embedded original and the outer envelope — actually bind their bytes.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "acta_receipt_interop", ROOT.parent / "examples" / "acta_receipt_interop.py"
)
assert _spec is not None and _spec.loader is not None
interop = importlib.util.module_from_spec(_spec)
# Dataclass creation resolves annotations through sys.modules, so the module
# must be registered before exec, not just held locally.
sys.modules[_spec.name] = interop
_spec.loader.exec_module(interop)


def _make_bundle(private_key: Ed25519PrivateKey, kid: str, **overrides):
    """Build a synthetic signed portable bundle, with override hooks."""
    claims = {
        "alg": "Ed25519",
        "created_at": "2026-08-15T00:00:00+00:00",
        "credits_charged": "1",
        "idempotency_record_id": "idm-test",
        "kid": kid,
        "ledger_entry_id": "ledger-test",
        "outcome": "success",
        "permit_id": "permit-test",
        "receipt_id": "rcpt-test",
        "request_hash": "aa" * 32,
        "response_hash": "bb" * 32,
        "tool": "partner.echo",
    }
    claims.update(overrides.pop("claims", {}))
    signing_input = json.dumps(
        claims, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    bundle = {
        "issuer": "https://example.invalid",
        "kid": kid,
        "signature": base64.b64encode(
            private_key.sign(signing_input.encode("utf-8"))
        ).decode("ascii"),
        "signing_input": signing_input,
    }
    bundle.update(overrides)
    return bundle


def _public_b64(private_key: Ed25519PrivateKey) -> str:
    """Return the raw public key of *private_key*, base64-encoded."""
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
    )

    return base64.b64encode(
        private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode("ascii")


def test_unverified_inner_signature_is_refused() -> None:
    """A bundle signed by a different key must not transcode."""
    key = Ed25519PrivateKey.generate()
    bundle = _make_bundle(key, "kid-a")
    wrong_key = Ed25519PrivateKey.generate()
    with pytest.raises(interop.TranscodeError, match="does not verify"):
        interop.verify_portable_bundle(bundle, _public_b64(wrong_key))


def test_tampered_signing_input_is_refused() -> None:
    """Altering signing_input after signing must break verification."""
    key = Ed25519PrivateKey.generate()
    bundle = _make_bundle(key, "kid-a")
    bundle["signing_input"] = bundle["signing_input"].replace('"1"', '"2"', 1)
    with pytest.raises(interop.TranscodeError, match="does not verify"):
        interop.verify_portable_bundle(bundle, _public_b64(key))


def test_kid_mismatch_between_bundle_and_signed_claim_is_refused() -> None:
    """The wrapper kid must match the kid inside the signed claims."""
    key = Ed25519PrivateKey.generate()
    bundle = _make_bundle(key, "kid-a")
    bundle["kid"] = "kid-b"
    with pytest.raises(interop.TranscodeError, match="kid"):
        interop.verify_portable_bundle(bundle, _public_b64(key))


def test_missing_economic_linkage_is_refused() -> None:
    """An economic receipt without its ledger link must not transcode."""
    key = Ed25519PrivateKey.generate()
    bundle = _make_bundle(key, "kid-a", claims={"ledger_entry_id": ""})
    with pytest.raises(interop.TranscodeError, match="ledger_entry_id"):
        interop.verify_portable_bundle(bundle, _public_b64(key))


def test_issuer_mutated_after_signing_is_refused() -> None:
    """The wrapper issuer is unsigned; the key-record binding must catch it."""
    key = Ed25519PrivateKey.generate()
    bundle = _make_bundle(key, "kid-a")
    bundle["issuer"] = "https://attacker.invalid"
    with pytest.raises(interop.TranscodeError, match="issuer"):
        interop.verify_portable_bundle(
            bundle, _public_b64(key), expected_issuer="https://example.invalid"
        )


def test_signature_alg_tamper_is_refused() -> None:
    """Changing only signature.alg must fail even though the sig bytes hold."""
    key = Ed25519PrivateKey.generate()
    bundle = _make_bundle(key, "kid-a")
    verified = interop.verify_portable_bundle(bundle, _public_b64(key))
    transcoder = Ed25519PrivateKey.generate()
    signed = interop.sign_acta_receipt(
        interop.to_acta_receipt(verified, issuer_id="test-kid"),
        transcoder,
        kid="test-kid",
    )
    signed["signature"] = {**signed["signature"], "alg": "ES256"}
    with pytest.raises(interop.TranscodeError, match="alg"):
        interop.verify_acta_receipt(signed, transcoder.public_key())


def test_outer_envelope_tamper_is_detected() -> None:
    """Changing any envelope field after signing must fail verification."""
    key = Ed25519PrivateKey.generate()
    bundle = _make_bundle(key, "kid-a")
    verified = interop.verify_portable_bundle(bundle, _public_b64(key))
    transcoder = Ed25519PrivateKey.generate()
    signed = interop.sign_acta_receipt(
        interop.to_acta_receipt(verified, issuer_id="test-kid"),
        transcoder,
        kid="test-kid",
    )
    signed["credits_charged"] = "999"
    with pytest.raises(interop.TranscodeError, match="does not verify"):
        interop.verify_acta_receipt(signed, transcoder.public_key())


def test_issuer_id_must_match_signing_kid() -> None:
    """The draft binds issuer_id to the signature kid; enforce both ways."""
    key = Ed25519PrivateKey.generate()
    bundle = _make_bundle(key, "kid-a")
    verified = interop.verify_portable_bundle(bundle, _public_b64(key))
    transcoder = Ed25519PrivateKey.generate()
    with pytest.raises(interop.TranscodeError, match="issuer_id"):
        interop.sign_acta_receipt(
            interop.to_acta_receipt(verified, issuer_id="someone-else"),
            transcoder,
            kid="test-kid",
        )


def test_inner_bundle_declared_alg_mismatch_is_refused() -> None:
    """A bundle declaring a non-Ed25519 alg must not verify as Ed25519."""
    key = Ed25519PrivateKey.generate()
    bundle = _make_bundle(key, "kid-a")
    bundle["alg"] = "ES256"
    with pytest.raises(interop.TranscodeError, match="alg"):
        interop.verify_portable_bundle(bundle, _public_b64(key))


@pytest.mark.parametrize(
    ("issuer_id", "kid", "match"),
    [
        (None, None, "issuer_id"),  # both absent: None == None must not pass
        (None, "test-kid", "issuer_id"),
        ("", "test-kid", "issuer_id"),
        ("test-kid", None, "kid"),
        ("test-kid", "", "kid"),
    ],
)
def test_missing_signer_identity_is_refused(
    issuer_id: str | None, kid: str | None, match: str
) -> None:
    """Each signer-identity field must independently refuse absent/empty."""
    key = Ed25519PrivateKey.generate()
    body = {"type": interop.ACTA_TYPE, "receipt_id": "rcpt-test"}
    if issuer_id is not None:
        body["issuer_id"] = issuer_id
    signature = key.sign(interop.jcs_canonicalize(body))
    signature_block = {"alg": "Ed25519", "sig": signature.hex()}
    if kid is not None:
        signature_block["kid"] = kid
    signed = {**body, "signature": signature_block}
    with pytest.raises(interop.TranscodeError, match=match):
        interop.verify_acta_receipt(signed, key.public_key())


def test_floats_fail_closed_in_canonicalization() -> None:
    """Floats are outside the JCS subset and must be refused outright."""
    with pytest.raises(interop.TranscodeError, match="float"):
        interop.jcs_canonicalize({"amount": 1.5})
