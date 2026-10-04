"""The independent verifier must stay independent, and must keep its claims apart.

The value of a second verifier is that it does not share code with the signer.
These tests pin that property mechanically, and pin the separation of
"the signature checks out" from "the issuer is who it says" from "the business
action happened" -- the conflation the PRD exists to prevent.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from failure_lab.verifier import (
    ENVELOPE_ONLY_FIELDS,
    ClaimStatus,
    KeySource,
    field_coverage,
    parse_key_document,
    tamper,
    verify,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
KEY_ID = "lab-test-ed25519"


@pytest.fixture
def signed_bundle() -> tuple[dict, dict[str, bytes]]:
    """A genuine portable receipt, signed here rather than by the app."""
    private = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    raw_public = private.public_key().public_bytes_raw()
    payload = {
        "kid": KEY_ID,
        "alg": "Ed25519",
        "receipt_id": "rcpt-abc123",
        "permit_id": "perm-1",
        "wallet_id": "wal-1",
        "tool": "refund.create",
        "outcome": "delivery_uncertain",
        "credits_charged": "5",
        "credits_authorized": "5",
        "request_hash": "a" * 64,
        "response_hash": None,
        "created_at": "2026-09-19T00:00:00",
    }
    signing_input = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    signature = base64.b64encode(private.sign(signing_input.encode())).decode()
    bundle = {
        "schema_version": "1.0",
        "receipt_id": "rcpt-abc123",
        "issuer": "https://api.example.test",
        "alg": "Ed25519",
        "kid": KEY_ID,
        "canonicalization": "jcs",
        "signing_input": signing_input,
        "signature": signature,
        "keys_url": "/.well-known/trust-keys.json",
    }
    return bundle, {KEY_ID: raw_public}


def test_verifier_imports_neither_the_app_nor_the_product_sdk():
    """A verifier that shares code with the signer cannot catch a shared bug."""
    probe = (
        "import sys, failure_lab.verifier;"
        "leaked=[m for m in sys.modules "
        "if m == 'app' or m.startswith('app.') or m.startswith('b2a_sdk')];"
        "print(','.join(sorted(leaked)))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=True,
    )
    assert completed.stdout.strip() == ""


def test_genuine_receipt_separates_the_three_claims(signed_bundle):
    bundle, keys = signed_bundle
    report = verify(bundle, keys)

    assert report.signature.status is ClaimStatus.ESTABLISHED
    # Keys fetched from the audited origin cannot establish issuer trust.
    assert report.issuer_trust.status is ClaimStatus.NOT_ESTABLISHED
    # A signature never establishes that the business action happened.
    assert report.downstream_execution.status is ClaimStatus.NOT_ESTABLISHED


def test_out_of_band_pin_is_what_establishes_issuer_trust(signed_bundle):
    bundle, keys = signed_bundle
    report = verify(bundle, keys, key_source=KeySource.OUT_OF_BAND_PIN)
    assert report.issuer_trust.status is ClaimStatus.ESTABLISHED


def test_downstream_execution_needs_an_independent_observation(signed_bundle):
    bundle, keys = signed_bundle
    observed = verify(
        bundle,
        keys,
        downstream_observation={
            "executions": 1,
            "operation_id": "refund:pay_1",
            "source": "effect ledger",
        },
    )
    assert observed.downstream_execution.status is ClaimStatus.ESTABLISHED
    assert "effect ledger" in observed.downstream_execution.reason


def test_a_success_receipt_contradicted_by_the_world_fails_that_claim(signed_bundle):
    bundle, keys = signed_bundle
    private = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    payload = json.loads(bundle["signing_input"])
    payload["outcome"] = "success"
    signing_input = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    bundle = {
        **bundle,
        "signing_input": signing_input,
        "signature": base64.b64encode(private.sign(signing_input.encode())).decode(),
    }
    report = verify(
        bundle,
        keys,
        downstream_observation={"executions": 0, "source": "effect ledger"},
    )
    assert report.signature.status is ClaimStatus.ESTABLISHED
    assert report.downstream_execution.status is ClaimStatus.FAILED


@pytest.mark.parametrize(
    "path",
    [
        "signing_input.outcome",
        "signing_input.credits_charged",
        "signing_input.tool",
        "signing_input.permit_id",
        "signing_input.wallet_id",
        "signing_input.created_at",
        "signing_input.receipt_id",
        "signing_input.request_hash",
    ],
)
def test_every_signed_field_is_covered_by_the_signature(signed_bundle, path):
    bundle, keys = signed_bundle
    forged = tamper(bundle, path, "tampered")
    assert verify(forged, keys).signature.status is ClaimStatus.FAILED


def test_envelope_fields_survive_tampering_and_that_is_reported(signed_bundle):
    """Editing an unsigned envelope field leaves a valid signature. Say so."""
    bundle, keys = signed_bundle
    forged = tamper(bundle, "issuer", "https://attacker.test")
    report = verify(forged, keys, expected_issuer="https://api.example.test")

    assert report.signature.status is ClaimStatus.ESTABLISHED
    assert any("issuer" in note for note in report.notes)

    coverage = field_coverage(bundle)
    assert coverage["issuer"] == "envelope_only"
    assert coverage["signing_input"] == "signed"
    assert coverage["kid"] == "envelope_mirrors_signed"
    for name in ("schema_version", "canonicalization", "keys_url"):
        assert coverage[name] == "envelope_only", name


def test_relabelling_the_envelope_kid_cannot_steer_key_selection(signed_bundle):
    """Key choice comes from the signed bytes, so an edited envelope is inert."""
    bundle, keys = signed_bundle
    forged = tamper(bundle, "kid", "some-other-key")
    report = verify(forged, keys)
    assert report.signature.status is ClaimStatus.ESTABLISHED
    assert report.key_id == KEY_ID
    assert "kid" in report.envelope_disagreements


def test_an_unknown_signed_kid_is_not_a_silent_pass(signed_bundle):
    bundle, keys = signed_bundle
    forged = tamper(bundle, "signing_input.kid", "unpublished-key")
    report = verify(forged, keys)
    assert report.signature.status is ClaimStatus.FAILED


def test_disabled_keys_are_dropped_from_the_key_set():
    document = {
        "keys": [
            {
                "kid": "live",
                "alg": "Ed25519",
                "public_key_b64": base64.b64encode(bytes(32)).decode(),
            },
            {
                "kid": "revoked",
                "alg": "Ed25519",
                "status": "disabled",
                "public_key_b64": base64.b64encode(bytes(32)).decode(),
            },
        ]
    }
    keys = parse_key_document(document)
    assert "live" in keys
    assert "revoked" not in keys


def test_envelope_only_fields_are_enumerated_for_the_tampering_report():
    assert "issuer" in ENVELOPE_ONLY_FIELDS
    assert "signing_input" not in ENVELOPE_ONLY_FIELDS
