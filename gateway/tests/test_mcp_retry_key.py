"""Data-free unit tests for deterministic unkeyed MCP retry keys (SEL-484)."""

from app.services.mcp_retry_key import DERIVED_KEY_PREFIX, derive_unkeyed_retry_key

BASE = dict(wallet_id="w1", key_id="k1", tool_name="t", window_seconds=300, now=1000.0)


def _key(**overrides):
    return derive_unkeyed_retry_key(
        **{**BASE, "arguments": {"a": 1, "b": 2}, **overrides}
    )


def test_deterministic_and_prefixed():
    assert _key() == _key()
    assert _key().startswith(DERIVED_KEY_PREFIX)


def test_argument_order_does_not_matter():
    assert _key(arguments={"b": 2, "a": 1}) == _key()


def test_none_and_empty_arguments_match():
    assert _key(arguments=None) == _key(arguments={})


def test_each_binding_changes_key():
    base = _key()
    assert _key(arguments={"a": 1, "b": 3}) != base
    assert _key(tool_name="other") != base
    assert _key(wallet_id="w2") != base
    assert _key(key_id="k2") != base
    assert _key(now=1000.0 + 300) != base


def test_same_window_matches():
    assert _key(now=900.0) == _key(now=1199.9)


def test_disabled_returns_none():
    assert _key(window_seconds=0) is None
    assert _key(wallet_id=None) is None
