"""Offline checks for signed receipt resolutions and complete wallet chains."""

from __future__ import annotations

import base64
import hashlib

import pytest

from b2a_sdk import verify_resolution, verify_resolution_chain
from b2a_sdk.receipt_verifier import (
    CANONICALIZATION,
    VerificationStatus,
    canonical_json,
    key_set_from_document,
)


@pytest.fixture
def signer():
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    kid = "resolution-test-key"
    key_document = {
        "keys": [
            {
                "kid": kid,
                "alg": "Ed25519",
                "status": "active",
                "public_key_b64": base64.b64encode(public_key).decode(),
            }
        ]
    }

    def sign(signing_input: str) -> str:
        return base64.b64encode(private_key.sign(signing_input.encode("utf-8"))).decode()

    return kid, key_set_from_document(key_document), sign


def _hash(signing_input: str) -> str:
    return hashlib.sha256(signing_input.encode("utf-8")).hexdigest()


def _receipt(signer, *, receipt_id="rcpt-1", wallet_id="wallet-1", outcome="delivery_uncertain"):
    kid, _, sign = signer
    signing_input = canonical_json(
        {
            "alg": "Ed25519",
            "kid": kid,
            "receipt_id": receipt_id,
            "wallet_id": wallet_id,
            "outcome": outcome,
        }
    )
    return {
        "receipt_id": receipt_id,
        "issuer": "https://api.example.com",
        "alg": "Ed25519",
        "kid": kid,
        "canonicalization": CANONICALIZATION,
        "signing_input": signing_input,
        "signature": sign(signing_input),
    }


def _resolution(
    signer,
    receipt,
    *,
    resolution_id="res-1",
    sequence=1,
    prev_hash=None,
    outcome="confirmed_success",
    receipt_hash=None,
):
    kid, _, sign = signer
    payload = {
        "version": "resolution-v0.1",
        "resolution_id": resolution_id,
        "receipt_id": receipt["receipt_id"],
        "wallet_id": "wallet-1",
        "sequence": sequence,
        "prev_hash": prev_hash,
        "receipt_hash": receipt_hash or _hash(receipt["signing_input"]),
        "outcome": outcome,
        "created_at": "2026-10-22T10:00:00+00:00",
        "alg": "Ed25519",
        "kid": kid,
    }
    payload["payload_hash"] = _hash(canonical_json(payload))
    signing_input = canonical_json(payload)
    return {
        **payload,
        "canonicalization": CANONICALIZATION,
        "signing_input": signing_input,
        "signature": sign(signing_input),
        "resolution_hash": _hash(signing_input),
    }


def test_resolution_verifies_exact_receipt_signing_bytes(signer):
    receipt = _receipt(signer)
    record = _resolution(signer, receipt)

    result = verify_resolution(record, receipt, signer[1])

    assert result.status is VerificationStatus.VERIFIED
    assert result.ok
    assert result.claims["receipt_hash"] == _hash(receipt["signing_input"])
    assert result.resolution_hash == record["resolution_hash"]


def test_tampered_signed_record_is_rejected(signer):
    receipt = _receipt(signer)
    record = _resolution(signer, receipt)
    tampered = dict(
        record,
        signing_input=record["signing_input"].replace(
            "confirmed_success", "confirmed_not_delivered"
        ),
    )

    result = verify_resolution(tampered, receipt, signer[1])

    assert result.status is VerificationStatus.INVALID
    assert result.is_rejected


def test_tampered_envelope_mirror_is_rejected(signer):
    receipt = _receipt(signer)
    record = _resolution(signer, receipt)

    result = verify_resolution(dict(record, outcome="confirmed_not_delivered"), receipt, signer[1])

    assert result.status is VerificationStatus.MISMATCH
    assert result.is_rejected


def test_signed_wrong_receipt_hash_is_rejected(signer):
    receipt = _receipt(signer)
    record = _resolution(signer, receipt, receipt_hash="f" * 64)

    result = verify_resolution(record, receipt, signer[1])

    assert result.status is VerificationStatus.MISMATCH
    assert result.is_rejected


def test_signed_incorrect_payload_hash_is_rejected(signer):
    receipt = _receipt(signer)
    record = _resolution(signer, receipt)
    wrong_hash = "f" * 64
    assert wrong_hash != record["payload_hash"]
    signing_input = record["signing_input"].replace(record["payload_hash"], wrong_hash)
    assert signing_input != record["signing_input"]
    resigned = dict(
        record,
        payload_hash=wrong_hash,
        signing_input=signing_input,
        signature=signer[2](signing_input),
        resolution_hash=_hash(signing_input),
    )

    result = verify_resolution(resigned, receipt, signer[1])

    assert result.status is VerificationStatus.MISMATCH
    assert result.is_rejected


def test_wrong_wallet_or_receipt_is_rejected(signer):
    receipt = _receipt(signer)
    record = _resolution(signer, receipt)

    assert (
        verify_resolution(record, _receipt(signer, wallet_id="wallet-2"), signer[1]).status
        is VerificationStatus.MISMATCH
    )
    assert (
        verify_resolution(record, _receipt(signer, receipt_id="rcpt-2"), signer[1]).status
        is VerificationStatus.MISMATCH
    )


@pytest.mark.parametrize("outcome", ["success", "denied"])
def test_resolution_requires_uncertain_receipt_outcome(signer, outcome):
    receipt = _receipt(signer, outcome=outcome)
    record = _resolution(signer, receipt)

    result = verify_resolution(record, receipt, signer[1])

    assert result.status is VerificationStatus.MISMATCH
    assert result.reason == "receipt outcome is not delivery_uncertain"


def test_valid_two_record_chain_and_target_receipt(signer):
    first_receipt = _receipt(signer)
    second_receipt = _receipt(signer, receipt_id="rcpt-2")
    first = _resolution(signer, first_receipt)
    second = _resolution(
        signer,
        second_receipt,
        resolution_id="res-2",
        sequence=2,
        prev_hash=first["resolution_hash"],
        outcome="confirmed_not_delivered",
    )

    result = verify_resolution_chain([first, second], second_receipt, signer[1])

    assert result.status is VerificationStatus.VERIFIED
    assert result.records_verified == 2
    assert result.claims["receipt_id"] == "rcpt-2"
    assert result.resolution_hash == second["resolution_hash"]


@pytest.mark.parametrize("outcome", ["success", "denied"])
def test_chain_requires_uncertain_target_receipt_outcome(signer, outcome):
    receipt = _receipt(signer, outcome=outcome)
    record = _resolution(signer, receipt)

    result = verify_resolution_chain([record], receipt, signer[1])

    assert result.status is VerificationStatus.MISMATCH
    assert result.reason == "receipt outcome is not delivery_uncertain"


def test_reordered_and_forked_chains_are_rejected(signer):
    first_receipt = _receipt(signer)
    second_receipt = _receipt(signer, receipt_id="rcpt-2")
    third_receipt = _receipt(signer, receipt_id="rcpt-3")
    first = _resolution(signer, first_receipt)
    second = _resolution(
        signer,
        second_receipt,
        resolution_id="res-2",
        sequence=2,
        prev_hash=first["resolution_hash"],
    )
    fork = _resolution(
        signer, third_receipt, resolution_id="res-3", sequence=2, prev_hash=first["resolution_hash"]
    )

    assert (
        verify_resolution_chain([second, first], first_receipt, signer[1]).status
        is VerificationStatus.MISMATCH
    )
    assert (
        verify_resolution_chain([first, second, fork], first_receipt, signer[1]).status
        is VerificationStatus.MISMATCH
    )


def test_chain_rejects_missing_predecessor_and_wrong_link(signer):
    first_receipt = _receipt(signer)
    second_receipt = _receipt(signer, receipt_id="rcpt-2")
    first = _resolution(signer, first_receipt)
    second = _resolution(
        signer, second_receipt, resolution_id="res-2", sequence=2, prev_hash="f" * 64
    )

    assert (
        verify_resolution_chain([first, second], first_receipt, signer[1]).status
        is VerificationStatus.MISMATCH
    )
    assert (
        verify_resolution_chain([second], second_receipt, signer[1]).status
        is VerificationStatus.MISMATCH
    )


def test_chain_requires_supplied_receipt_and_rejects_replay(signer):
    receipt = _receipt(signer)
    other_receipt = _receipt(signer, receipt_id="rcpt-2")
    first = _resolution(signer, receipt)
    repeated = _resolution(
        signer, receipt, resolution_id="res-2", sequence=2, prev_hash=first["resolution_hash"]
    )

    assert (
        verify_resolution_chain([first], other_receipt, signer[1]).status
        is VerificationStatus.MISMATCH
    )
    assert (
        verify_resolution_chain([first, repeated], receipt, signer[1]).status
        is VerificationStatus.MISMATCH
    )


def test_unknown_key_is_not_reported_as_tampering(signer):
    receipt = _receipt(signer)
    record = _resolution(signer, receipt)

    result = verify_resolution(record, receipt, {})

    assert result.status is VerificationStatus.UNKNOWN_KEY
    assert not result.is_rejected
