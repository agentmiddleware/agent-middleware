"""Data-free unit tests for deterministic unkeyed MCP retry keys (SEL-484)."""

import json

from app.services.mcp_retry_key import (
    DERIVED_KEY_PREFIX,
    bucket_for,
    derive_retry_identity,
    is_no_effect_failure,
    principal_binding,
    slot_key,
)

BASE = dict(wallet_id="w1", key_id="k1", principal="", tool_name="t")


def _identity(**overrides):
    return derive_retry_identity(**{**BASE, "arguments": {"a": 1, "b": 2}, **overrides})


def _slot(identity=None, bucket=3, attempt=0, window=300):
    return slot_key(
        identity or _identity(), window_seconds=window, bucket=bucket, attempt=attempt
    )


def test_deterministic_and_prefixed():
    assert _slot() == _slot()
    assert _slot().startswith(DERIVED_KEY_PREFIX)


def test_argument_order_and_empty_arguments():
    assert _identity(arguments={"b": 2, "a": 1}) == _identity()
    assert _identity(arguments=None) == _identity(arguments={})


def test_each_binding_changes_identity():
    base = _identity()
    assert _identity(arguments={"a": 1, "b": 3}) != base
    assert _identity(tool_name="other") != base
    assert _identity(wallet_id="w2") != base
    assert _identity(key_id="k2") != base
    assert _identity(principal="okta\x1eiss\x1esub") != base


def test_principal_binding_separates_humans():
    class P:
        def __init__(self, subject):
            self.provider, self.issuer, self.subject = "okta", "iss", subject

    assert principal_binding(None) == ""
    assert principal_binding(P("a")) == principal_binding(P("a"))
    assert principal_binding(P("a")) != principal_binding(P("b"))
    assert _identity(principal=principal_binding(P("a"))) != _identity(
        principal=principal_binding(P("b"))
    )


def test_slots_differ_by_bucket_attempt_and_window():
    base = _slot()
    assert _slot(bucket=4) != base
    assert _slot(attempt=1) != base
    assert _slot(window=60) != base


def test_bucket_for():
    assert bucket_for(299.999, 300) == 0
    assert bucket_for(300.001, 300) == 1


def test_no_effect_failure_detection():
    refunded = {
        "error": "upstream_unreachable",
        "upstream_result": {"error": "failed_refunded", "error_code": "x"},
        "receipt": {"outcome": "failed_refunded"},
    }
    assert is_no_effect_failure(refunded)
    assert is_no_effect_failure(json.dumps(refunded))
    assert not is_no_effect_failure({"result": {"ok": True}, "receipt": {}})
    assert not is_no_effect_failure({**refunded, "receipt": {"outcome": "succeeded"}})
    assert not is_no_effect_failure("not json")
    assert not is_no_effect_failure(None)
