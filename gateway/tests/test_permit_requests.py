"""Unit tests for the pure helpers in app.services.permit_requests.

The request -> notify -> approve -> mint -> poll flow itself is covered by
tests/test_permit_request_flow.py. These tests pin the security properties
that do not need a database or Sentinel: the reviewed terms hash (order
insensitive, sensitive to every field) and the deterministic Sentinel
idempotency key, plus the error shapes the routers map to status codes.
"""

from datetime import datetime, timezone
from decimal import Decimal

from app.services.permit_requests import (
    REQUEST_STATUS_APPROVED,
    REQUEST_STATUS_EXPIRED,
    REQUEST_STATUS_FAILED,
    REQUEST_STATUS_REJECTED,
    TERMINAL_STATUSES,
    PermitRequestConflictError,
    PermitRequestError,
    permit_request_hash,
    sentinel_request_key,
)


def _terms(**overrides):
    terms = {
        "issuer_wallet_id": "wallet-issuer",
        "subject_wallet_id": "wallet-subject",
        "allowed_tools": ["summarize", "fetch-url"],
        "scopes": ["tool:fetch-url:invoke"],
        "max_credits": Decimal("20"),
        "permit_expires_at": datetime(2035, 1, 1, tzinfo=timezone.utc),
        "requires_human_approval": False,
        "justification": "Draft the weekly digest.",
    }
    terms.update(overrides)
    return terms


def test_request_hash_is_deterministic():
    assert permit_request_hash(**_terms()) == permit_request_hash(**_terms())


def test_request_hash_ignores_tool_and_scope_ordering():
    base = permit_request_hash(
        **_terms(
            allowed_tools=["fetch-url", "summarize"],
            scopes=["tool:fetch-url:invoke", "tool:summarize:invoke"],
        )
    )
    reordered = permit_request_hash(
        **_terms(
            allowed_tools=["summarize", "fetch-url"],
            scopes=["tool:summarize:invoke", "tool:fetch-url:invoke"],
        )
    )
    assert base == reordered


def test_request_hash_ignores_credit_number_formatting():
    base = permit_request_hash(**_terms())
    reformatted = permit_request_hash(**_terms(max_credits=Decimal("20.0")))
    assert base == reformatted


def test_request_hash_treats_naive_and_aware_expiry_identically():
    base = permit_request_hash(**_terms())
    naive = permit_request_hash(**_terms(permit_expires_at=datetime(2035, 1, 1)))
    assert base == naive


def test_request_hash_binds_every_reviewed_field():
    base = permit_request_hash(**_terms())
    variants = [
        _terms(issuer_wallet_id="wallet-other"),
        _terms(subject_wallet_id="wallet-other"),
        _terms(allowed_tools=["summarize", "wire-transfer"]),
        _terms(scopes=["tool:wire-transfer:invoke"]),
        _terms(max_credits=Decimal("21")),
        _terms(permit_expires_at=datetime(2036, 1, 1, tzinfo=timezone.utc)),
        _terms(requires_human_approval=True),
        _terms(justification="Something else entirely."),
    ]
    for variant in variants:
        assert permit_request_hash(**variant) != base


def test_sentinel_request_key_shape_is_stable():
    key = sentinel_request_key("abc123", subject_wallet_id="w", idempotency_key="k")
    assert key.startswith("mw-preq-")
    assert len(key) == len("mw-preq-") + 44
    assert key == sentinel_request_key(
        "abc123", subject_wallet_id="w", idempotency_key="k"
    )


def test_sentinel_request_key_binds_identity_and_terms():
    base = sentinel_request_key("hash-a", subject_wallet_id="w", idempotency_key="k")
    assert (
        sentinel_request_key("hash-b", subject_wallet_id="w", idempotency_key="k")
        != base
    )
    assert (
        sentinel_request_key("hash-a", subject_wallet_id="w2", idempotency_key="k")
        != base
    )
    assert (
        sentinel_request_key("hash-a", subject_wallet_id="w", idempotency_key="k2")
        != base
    )


def test_error_reasons_are_client_facing_codes():
    err = PermitRequestError("human_approval_not_configured")
    assert err.reason == "human_approval_not_configured"
    assert str(err) == "human_approval_not_configured"

    conflict = PermitRequestConflictError()
    assert conflict.reason == "permit_request_terms_conflict"


def test_terminal_statuses_cover_every_non_pollable_state():
    assert TERMINAL_STATUSES == frozenset(
        {
            REQUEST_STATUS_APPROVED,
            REQUEST_STATUS_REJECTED,
            REQUEST_STATUS_EXPIRED,
            REQUEST_STATUS_FAILED,
        }
    )
