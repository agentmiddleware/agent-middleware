"""Verifier behaviour that needs no key material.

These cases all resolve before any signature check, so they run against a bare
SDK install without the ``verify`` extra. Signature-level tests live in the
main repository suite, where cryptography is always present.
"""

from __future__ import annotations

import base64
import json

import pytest

from b2a_sdk import receipt_verifier, verify_cli
from b2a_sdk.receipt_verifier import (
    CANONICALIZATION,
    VerificationError,
    VerificationResult,
    VerificationStatus,
    canonical_json,
    key_set_from_document,
    verify_bundle,
)


def _bundle(**overrides):
    bundle = {
        "receipt_id": "rcpt-1",
        "kid": "key-1",
        "alg": "Ed25519",
        "canonicalization": CANONICALIZATION,
        "signing_input": json.dumps({"receipt_id": "rcpt-1", "kid": "key-1"}),
        "signature": base64.b64encode(b"\x00" * 64).decode(),
        "issuer": "https://api.example.com",
    }
    bundle.update(overrides)
    return bundle


def test_canonical_json_sorts_every_level_without_whitespace():
    encoded = canonical_json({"b": 1, "a": {"d": 2, "c": 3}})

    assert encoded == '{"a":{"c":3,"d":2},"b":1}'


def test_canonical_json_escapes_non_ascii():
    """ensure_ascii is part of the contract; a UTF-8 verifier would diverge."""
    assert canonical_json({"k": "café"}) == '{"k":"caf\\u00e9"}'


def test_key_set_reads_raw_and_jwk_forms():
    document = {
        "keys": [
            {"kid": "raw", "alg": "Ed25519", "public_key_b64": "A" * 43 + "="},
            {
                "kid": "jwk-only",
                "alg": "Ed25519",
                "jwk": {"crv": "Ed25519", "x": "B" * 43},
            },
        ]
    }

    keys = key_set_from_document(document)

    assert set(keys) == {"raw", "jwk-only"}
    assert all(len(value) == 32 for value in keys.values())


def test_key_set_drops_disabled_keys():
    """A revoked key must not keep validating from a cached document."""
    document = {
        "keys": [
            {
                "kid": "revoked",
                "alg": "Ed25519",
                "status": "disabled",
                "public_key_b64": "A" * 43 + "=",
            },
            {
                "kid": "live",
                "alg": "Ed25519",
                "status": "active",
                "public_key_b64": "B" * 43 + "=",
            },
        ]
    }

    assert set(key_set_from_document(document)) == {"live"}


def test_key_revoked_status_is_reserved_for_sdk_compatibility():
    assert VerificationStatus.KEY_REVOKED.value == "key_revoked"


def test_key_set_skips_unusable_entries_without_failing_the_whole_document():
    document = {
        "keys": [
            {"kid": "short", "alg": "Ed25519", "public_key_b64": "AAAA"},
            {"kid": "not-base64", "alg": "Ed25519", "public_key_b64": "!!!!"},
            {"alg": "Ed25519", "public_key_b64": "A" * 43 + "="},
            {"kid": "good", "alg": "Ed25519", "public_key_b64": "C" * 43 + "="},
        ]
    }

    assert set(key_set_from_document(document)) == {"good"}


def test_key_set_rejects_a_document_with_no_keys_list():
    with pytest.raises(VerificationError):
        key_set_from_document({"nope": []})


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"signing_input": ""}, VerificationStatus.MALFORMED),
        ({"signature": ""}, VerificationStatus.MALFORMED),
        ({"kid": ""}, VerificationStatus.MALFORMED),
        ({"signing_input": "not json"}, VerificationStatus.MALFORMED),
        ({"signing_input": "[1,2]"}, VerificationStatus.MALFORMED),
        # UNSUPPORTED must be driven by what the SIGNER declared, inside the
        # signed bytes -- never by the envelope. These still resolve before any
        # signature check, so they need no key material.
        (
            {"signing_input": json.dumps({"receipt_id": "rcpt-1", "kid": "key-1", "alg": "RS256"})},
            VerificationStatus.UNSUPPORTED,
        ),
        (
            {
                "signing_input": json.dumps(
                    {
                        "receipt_id": "rcpt-1",
                        "kid": "key-1",
                        "canonicalization": "something-else/9",
                    }
                )
            },
            VerificationStatus.UNSUPPORTED,
        ),
    ],
)
def test_unusable_bundles_report_why(overrides, expected):
    result = verify_bundle(_bundle(**overrides), {"key-1": b"\x00" * 32})

    assert result.status is expected
    assert not result.ok
    # None of these are evidence of tampering.
    assert not result.is_tampered


def test_missing_key_reports_unknown_key_not_invalid():
    result = verify_bundle(_bundle(), {})

    assert result.status is VerificationStatus.UNKNOWN_KEY
    assert not result.is_tampered


def test_bundle_accepted_as_a_json_string():
    result = verify_bundle(json.dumps(_bundle()), {})

    assert result.status is VerificationStatus.UNKNOWN_KEY


def test_non_json_string_bundle_is_malformed():
    assert verify_bundle("{oops", {}).status is VerificationStatus.MALFORMED


def test_extreme_signing_input_nesting_is_malformed_not_a_crash():
    nested = "[" * 1_100 + "0" + "]" * 1_100

    result = verify_bundle(_bundle(signing_input=nested), {"key-1": b"\x00" * 32})

    assert result.status is VerificationStatus.MALFORMED
    assert not result.is_tampered


@pytest.mark.parametrize(
    "signing_input",
    [
        '{"value":' + "9" * 5_000 + "}",
        '{"value":"\\ud800"}',
    ],
    ids=["huge_integer", "lone_surrogate"],
)
def test_pathological_json_values_are_malformed_not_internal_errors(signing_input):
    result = verify_bundle(
        _bundle(signing_input=signing_input),
        {"key-1": b"\x00" * 32},
    )

    assert result.status is VerificationStatus.MALFORMED
    assert not result.is_tampered


def test_non_object_bundle_is_malformed():
    assert verify_bundle([1, 2, 3], {}).status is VerificationStatus.MALFORMED


@pytest.mark.parametrize(
    "signature",
    ["!!!!", "AA==", base64.b64encode(b"\x00" * 65).decode()],
)
def test_invalid_base64_or_signature_length_is_malformed(signature):
    result = verify_bundle(
        _bundle(signature=signature),
        {"key-1": b"\x00" * 32},
    )

    assert result.status is VerificationStatus.MALFORMED
    assert not result.is_tampered


def test_issuer_mismatch_is_checked_before_any_key_lookup():
    result = verify_bundle(_bundle(), {}, expected_issuer="https://other.example.com")

    assert result.status is VerificationStatus.MISMATCH
    assert "issuer" in (result.reason or "")


def test_malformed_public_key_is_not_reported_as_tampering():
    result = verify_bundle(_bundle(), {"key-1": b"too-short"})

    assert result.status is VerificationStatus.MALFORMED
    assert not result.is_tampered


def test_unexpected_crypto_backend_error_is_not_an_invalid_verdict(monkeypatch):
    # Reaches the signature stage, so it needs the InvalidSignature import to
    # succeed; on a bare install the install-hint VerificationError wins first.
    pytest.importorskip("cryptography")

    class BrokenVerifier:
        def verify(self, _signature, _signing_bytes):
            raise RuntimeError("crypto backend failed")

    monkeypatch.setattr(
        receipt_verifier,
        "_load_verifier",
        lambda _raw_public_key: BrokenVerifier(),
    )

    with pytest.raises(VerificationError, match="failed unexpectedly"):
        verify_bundle(_bundle(), {"key-1": b"\x00" * 32})


def _run_human_readable_cli(monkeypatch, capsys, claims):
    def fake_read_json(path):
        return {"keys": []} if path == "keys.json" else {}

    monkeypatch.setattr(verify_cli, "_read_json", fake_read_json)
    monkeypatch.setattr(
        verify_cli,
        "verify_bundle",
        lambda bundle, key_set, expected_issuer=None: VerificationResult(
            status=VerificationStatus.VERIFIED,
            receipt_id="rcpt-1",
            key_id="key-1",
            claims=claims,
        ),
    )

    exit_code = verify_cli.main(["--bundle", "bundle.json", "--keys", "keys.json"])

    assert exit_code == verify_cli.EXIT_VERIFIED
    return capsys.readouterr().out


def test_human_readable_cli_displays_signed_denial_reason(monkeypatch, capsys):
    output = _run_human_readable_cli(
        monkeypatch,
        capsys,
        {
            "permit_id": "permit-1",
            "tool": "partner.echo",
            "outcome": "denied",
            "reason_code": "permit_budget_exceeded",
            "credits_charged": "0",
            "credits_authorized": "2",
            "created_at": "2026-08-11T00:00:00Z",
        },
    )

    assert "  reason      permit_budget_exceeded" in output


def test_human_readable_cli_omits_reason_for_success_receipt(monkeypatch, capsys):
    output = _run_human_readable_cli(
        monkeypatch,
        capsys,
        {
            "permit_id": "permit-1",
            "tool": "partner.echo",
            "outcome": "success",
            "credits_charged": "1",
            "credits_authorized": "1",
            "created_at": "2026-08-11T00:00:00Z",
        },
    )

    assert "\n  reason      " not in output
