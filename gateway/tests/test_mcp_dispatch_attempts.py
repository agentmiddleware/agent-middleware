"""Focused unit tests for app.services.mcp_dispatch_attempts.

Covers the import-safe surface of the durable dispatch state machine:
reconciliation lifetime, operator metrics, error hierarchy, state sets,
duplicate guard metadata, and the request/result validators. All tests are
synchronous and hermetic (no database, no network).
"""

from __future__ import annotations

import math

import pytest

from app.services.mcp_dispatch_attempts import (
    DISPATCH_ACTIVE_STATES,
    DISPATCH_CLAIMED,
    DISPATCH_LEGACY_DISPATCHED,
    DISPATCH_PREPARED,
    DISPATCH_SENT_STATES,
    DISPATCH_TERMINAL_STATES,
    DUPLICATE_DENIAL_REASON_CODE,
    DUPLICATE_GUARD_METRIC_SCOPES,
    MAX_UPSTREAM_CALL_TIMEOUT_SECONDS,
    MAX_UPSTREAM_CONNECT_TIMEOUT_SECONDS,
    DispatchAttemptConflictError,
    DispatchAttemptError,
    DispatchClaimUnavailableError,
    DispatchPrepareCommitUncertainError,
    DispatchPrepareRolledBackError,
    DispatchResultRejectedError,
    DispatchResultTooLargeError,
    DispatchAttemptMetrics,
    _assert_origin,
    _assert_sha256,
    _bounded_result,
    _loads_dict,
    _validated_terminal_result,
    dispatch_reconciliation_idle_seconds,
)


def test_reconciliation_idle_returns_fixed_maximum_lifetime():
    """Any valid timeout pair yields the same rolling-deploy-safe lifetime."""
    expected = (
        MAX_UPSTREAM_CONNECT_TIMEOUT_SECONDS
        + (3 * MAX_UPSTREAM_CALL_TIMEOUT_SECONDS)
        + 30
    )
    assert (
        dispatch_reconciliation_idle_seconds(
            connect_timeout_seconds=5.0, call_timeout_seconds=60.0
        )
        == expected
    )
    assert (
        dispatch_reconciliation_idle_seconds(
            connect_timeout_seconds=MAX_UPSTREAM_CONNECT_TIMEOUT_SECONDS,
            call_timeout_seconds=MAX_UPSTREAM_CALL_TIMEOUT_SECONDS,
        )
        == expected
    )
    assert expected >= 300


@pytest.mark.parametrize(
    "connect,call",
    [
        (0.0, 60.0),
        (-1.0, 60.0),
        (5.0, 0.0),
        (5.0, -30.0),
        (math.inf, 60.0),
        (5.0, math.inf),
        (math.nan, 60.0),
        (5.0, math.nan),
    ],
)
def test_reconciliation_idle_rejects_non_positive_or_non_finite(connect, call):
    """Non-positive or non-finite timeouts cannot define a live call."""
    with pytest.raises(ValueError, match="dispatch_timeout_invalid"):
        dispatch_reconciliation_idle_seconds(
            connect_timeout_seconds=connect, call_timeout_seconds=call
        )


@pytest.mark.parametrize(
    "connect,call",
    [
        (MAX_UPSTREAM_CONNECT_TIMEOUT_SECONDS + 1, 60.0),
        (5.0, MAX_UPSTREAM_CALL_TIMEOUT_SECONDS + 1),
    ],
)
def test_reconciliation_idle_rejects_above_supported_maximum(connect, call):
    """Timeouts beyond the supported maxima are rejected, not clamped."""
    with pytest.raises(ValueError, match="dispatch_timeout_exceeds_supported_maximum"):
        dispatch_reconciliation_idle_seconds(
            connect_timeout_seconds=connect, call_timeout_seconds=call
        )


def test_reconciliation_backlog_sums_categories():
    """The backlog counts every unreconciled category exactly once."""
    metrics = DispatchAttemptMetrics(
        state_counts={"prepared": 2},
        stale_active=1,
        unfinalized_terminal=2,
        terminal_idempotency_incomplete=3,
    )
    assert metrics.reconciliation_backlog == 6
    empty = DispatchAttemptMetrics(
        state_counts={},
        stale_active=0,
        unfinalized_terminal=0,
        terminal_idempotency_incomplete=0,
    )
    assert empty.reconciliation_backlog == 0


def test_error_hierarchy_preserves_catch_order():
    """Conflict and result errors stay catchable as their base errors."""
    assert issubclass(DispatchAttemptConflictError, DispatchAttemptError)
    assert issubclass(DispatchPrepareRolledBackError, DispatchAttemptError)
    assert issubclass(DispatchPrepareCommitUncertainError, DispatchAttemptConflictError)
    assert issubclass(DispatchClaimUnavailableError, DispatchAttemptConflictError)
    assert issubclass(DispatchResultRejectedError, DispatchAttemptError)
    assert issubclass(DispatchResultTooLargeError, DispatchResultRejectedError)


def test_state_sets_are_consistent():
    """Active states split into prepared plus sent; terminal never overlaps."""
    assert DISPATCH_TERMINAL_STATES == frozenset(
        {
            "succeeded",
            "returned_error",
            "delivery_uncertain",
            "response_rejected",
        }
    )
    assert DISPATCH_SENT_STATES == frozenset(
        {DISPATCH_LEGACY_DISPATCHED, DISPATCH_CLAIMED}
    )
    assert DISPATCH_ACTIVE_STATES == frozenset(
        {DISPATCH_PREPARED, *DISPATCH_SENT_STATES}
    )
    assert not (DISPATCH_ACTIVE_STATES & DISPATCH_TERMINAL_STATES)


def test_duplicate_denial_metadata_is_stable():
    """Denial reason code and metric scopes keep their documented shape."""
    assert DUPLICATE_DENIAL_REASON_CODE == "duplicate_request_new_key"
    assert set(DUPLICATE_GUARD_METRIC_SCOPES) == {
        "log_mode_blocks",
        "enforce_mode_blocks",
        "enforce_mode_denials_durable",
    }
    durable = DUPLICATE_GUARD_METRIC_SCOPES["enforce_mode_denials_durable"]
    assert durable["durable"] is True
    assert durable["scope"] == "durable"


def test_assert_sha256_accepts_hex_and_normalizes_case():
    """A valid digest passes through lowercased."""
    digest = "AB" + "cd12" * 15 + "34"
    assert len(digest) == 64
    assert _assert_sha256(digest) == digest.lower()


@pytest.mark.parametrize(
    "value",
    ["", "abc", "z" * 64, "12" * 31, "00" * 33, "not-a-hash-at-all"],
)
def test_assert_sha256_rejects_malformed_digests(value):
    """Short, long, or non-hex digests are rejected."""
    with pytest.raises(DispatchAttemptError, match="dispatch_request_hash_invalid"):
        _assert_sha256(value)


@pytest.mark.parametrize(
    "origin",
    ["https://upstream.example", "http://localhost:8080"],
)
def test_assert_origin_accepts_bare_origins(origin):
    """Bare http/https origins pass through unchanged."""
    assert _assert_origin(origin) == origin


@pytest.mark.parametrize(
    "origin",
    [
        "ftp://upstream.example",
        "https://upstream.example/tools",
        "https://upstream.example?x=1",
        "https://upstream.example#frag",
        "https://user@upstream.example",
        "https://user:pass@upstream.example",
        "not-a-url",
        "",
    ],
)
def test_assert_origin_rejects_non_origins(origin):
    """Paths, queries, credentials, and non-http schemes are rejected."""
    with pytest.raises(DispatchAttemptError, match="dispatch_upstream_origin_invalid"):
        _assert_origin(origin)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, {}),
        ("", {}),
        ("not json", {}),
        ("[1, 2]", {}),
        ('"str"', {}),
        ('{"a": 1}', {"a": 1}),
    ],
)
def test_loads_dict_returns_empty_on_missing_or_invalid(raw, expected):
    """Only a JSON object decodes; everything else yields an empty dict."""
    assert _loads_dict(raw) == expected


def test_bounded_result_serializes_small_payload():
    """A small payload returns its serialization, size, and hash."""
    serialized, size, digest = _bounded_result({"ok": True}, max_result_bytes=4096)
    assert isinstance(serialized, str) and serialized
    assert size == len(serialized.encode("utf-8"))
    assert digest is not None and len(digest) == 64


def test_bounded_result_none_payload_is_empty():
    """A missing payload stores nothing instead of failing."""
    assert _bounded_result(None, max_result_bytes=4096) == (None, None, None)


def test_bounded_result_rejects_oversize_payload():
    """Payloads beyond the storage bound are rejected before persisting."""
    with pytest.raises(DispatchResultTooLargeError):
        _bounded_result({"data": "x" * 4096}, max_result_bytes=16)


def test_bounded_result_rejects_invalid_limit():
    """A non-positive storage bound is a configuration error."""
    with pytest.raises(DispatchAttemptError, match="dispatch_result_limit_invalid"):
        _bounded_result({"ok": True}, max_result_bytes=0)


def test_validated_terminal_result_accepts_each_terminal_state():
    """Every terminal state with a payload validates, including errors."""
    for state in sorted(DISPATCH_TERMINAL_STATES):
        payload = {"ok": True} if state == "succeeded" else None
        serialized, size, digest = _validated_terminal_result(
            state=state,
            result_payload=payload,
            error_code=None if state == "succeeded" else "upstream_timeout",
            max_result_bytes=4096,
        )
        if payload is None:
            assert (serialized, size, digest) == (None, None, None)
        else:
            assert serialized and size and digest


def test_validated_terminal_result_rejects_unknown_state():
    """Non-terminal states can never finalize an attempt."""
    with pytest.raises(DispatchAttemptError, match="dispatch_terminal_state_invalid"):
        _validated_terminal_result(
            state="prepared",
            result_payload={"ok": True},
            error_code=None,
            max_result_bytes=4096,
        )


def test_validated_terminal_result_requires_success_payload():
    """A success with no result body cannot be stored."""
    with pytest.raises(DispatchAttemptError, match="dispatch_success_result_required"):
        _validated_terminal_result(
            state="succeeded",
            result_payload=None,
            error_code=None,
            max_result_bytes=4096,
        )


@pytest.mark.parametrize("error_code", ["", " padded ", "x" * 65])
def test_validated_terminal_result_rejects_bad_error_codes(error_code):
    """Blank, padded, or overlong error codes are rejected."""
    with pytest.raises(DispatchAttemptError, match="dispatch_error_code_invalid"):
        _validated_terminal_result(
            state="returned_error",
            result_payload=None,
            error_code=error_code,
            max_result_bytes=4096,
        )


def test_validated_terminal_result_rejects_non_serializable_payload():
    """A payload that cannot be canonicalized is rejected, not truncated."""
    with pytest.raises(DispatchResultRejectedError):
        _validated_terminal_result(
            state="succeeded",
            result_payload={"value": math.nan},
            error_code=None,
            max_result_bytes=4096,
        )
