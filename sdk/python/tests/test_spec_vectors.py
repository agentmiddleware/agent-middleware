"""The shared spec vectors pass through the offline verifiers.

Receipts go through :func:`verify_bundle`, permits through
:func:`verify_permit_bundle`. The rotation, disabled-key, and
future-algorithm cases below pin the coverage the go-to-market review asked
for: every verifier status exercised in both suites.
"""

from __future__ import annotations

import json
from pathlib import Path

from b2a_sdk.receipt_verifier import (
    VerificationStatus,
    key_set_from_document,
    verify_bundle,
    verify_permit_bundle,
)

VECTORS = Path(__file__).resolve().parents[3] / "spec" / "test-vectors"

SUITES = (
    ("receipts.json", verify_bundle),
    ("permits.json", verify_permit_bundle),
)

ALL_STATUSES = {"verified", "invalid", "mismatch", "unknown_key", "malformed", "unsupported"}


def _load(filename):
    return json.loads((VECTORS / filename).read_text())


def _by_name(cases, name):
    return next(case for case in cases if case["name"] == name)


def test_every_vector_matches_its_expected_status():
    default_keys = key_set_from_document(_load("keys.json"))
    for filename, verify in SUITES:
        for case in _load(filename)["cases"]:
            keys = key_set_from_document(case["keys"]) if "keys" in case else default_keys
            assert verify(case["bundle"], keys).status.value == case["expect"], (
                filename,
                case["name"],
            )


def test_every_status_is_exercised_in_both_suites():
    for filename, _ in SUITES:
        seen = {case["expect"] for case in _load(filename)["cases"]}
        assert ALL_STATUSES <= seen, (filename, ALL_STATUSES - seen)


def test_rotation_successor_key_verifies_under_published_keys():
    case = _by_name(_load("rotation.json")["cases"], "rotation.successor_key_valid")
    keys = key_set_from_document(case["keys"])
    result = verify_bundle(case["bundle"], keys)
    assert result.status is VerificationStatus.VERIFIED
    assert result.key_id == "sk_golden_0002"


def test_disabled_key_resolves_to_unknown_key_not_verified():
    case = _by_name(_load("receipts.json")["cases"], "rotation.disabled_key_unknown")
    document = case["keys"]
    assert any(entry.get("status") == "disabled" for entry in document["keys"])
    keys = key_set_from_document(document)
    assert "sk_golden_0000" not in keys
    assert verify_bundle(case["bundle"], keys).status is VerificationStatus.UNKNOWN_KEY


def test_future_algorithm_is_unsupported_not_invalid():
    keys = key_set_from_document(_load("keys.json"))
    for filename, verify in SUITES:
        case = _by_name(_load(filename)["cases"], "unsupported.future_alg")
        assert verify(case["bundle"], keys).status is VerificationStatus.UNSUPPORTED, filename


def test_verified_permit_reports_permit_id():
    keys = key_set_from_document(_load("keys.json"))
    case = _by_name(_load("permits.json")["cases"], "active_minimal.valid")
    result = verify_permit_bundle(case["bundle"], keys)
    assert result.ok
    assert result.permit_id == "pmt_golden_0001"
    assert result.receipt_id is None
