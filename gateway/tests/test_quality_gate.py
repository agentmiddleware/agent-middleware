"""Gate for Pattern 9 (tests that cannot fail).

The shared helpers in ``tests.support.quality`` pin exact expected values.
These tests prove the helpers reject the vacuous shapes they replace
(``total >= 0``, status tuples, key-only asserts), and that the repo gates
(``xfail_strict``, ruff PT+B on tests) are actually switched on. If a helper
ever stops rejecting a vacuous input, this file fails.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.support.quality import (
    assert_counts_agree,
    assert_exact_status,
    assert_exact_total,
)

GATEWAY_ROOT = Path(__file__).resolve().parents[1]


def _response(status: int, text: str = "") -> SimpleNamespace:
    return SimpleNamespace(status_code=status, text=text)


# --- assert_exact_status ---


def test_exact_status_accepts_the_one_expected_status():
    assert_exact_status(_response(200), 200)


def test_exact_status_rejects_any_other_status_with_body():
    with pytest.raises(AssertionError, match="404"):
        assert_exact_status(_response(404, "wallet_not_found"), 200)


def test_exact_status_rejects_status_tuples():
    """A (200, 404) tuple must not pass as the expected value."""
    with pytest.raises(AssertionError):
        assert_exact_status(_response(200), (200, 404))  # type: ignore[arg-type]


# --- assert_exact_total ---


def test_exact_total_accepts_an_exact_zero():
    assert assert_exact_total({"total": 0, "anomalies": []}, 0) == 0


def test_exact_total_rejects_a_placeholder_count():
    with pytest.raises(AssertionError, match="total"):
        assert_exact_total({"total": 7}, 0)


def test_exact_total_rejects_a_missing_count():
    with pytest.raises(AssertionError, match="total"):
        assert_exact_total({"anomalies": []}, 0)


def test_exact_total_rejects_a_string_count():
    """A string "0" must fail, not compare equal."""
    with pytest.raises(AssertionError, match="int"):
        assert_exact_total({"total": "0"}, 0)


def test_exact_total_rejects_a_bool_count():
    with pytest.raises(AssertionError, match="int"):
        assert_exact_total({"total": False}, 0)


# --- assert_counts_agree ---


def _consistent() -> dict:
    return {
        "total": 2,
        "vulnerabilities": [{"severity": "critical"}, {"severity": "high"}],
        "critical_count": 1,
        "high_count": 1,
    }


def test_counts_agree_accepts_a_consistent_report():
    assert (
        assert_counts_agree(_consistent(), count_keys=("critical_count", "high_count"))
        == 2
    )


def test_counts_agree_rejects_an_empty_list_with_nonzero_total():
    payload = _consistent()
    payload["vulnerabilities"] = []
    with pytest.raises(AssertionError, match="disagrees"):
        assert_counts_agree(payload, count_keys=("critical_count", "high_count"))


def test_counts_agree_rejects_counts_that_exceed_the_total():
    payload = _consistent()
    payload["critical_count"] = 5
    with pytest.raises(AssertionError, match="sum"):
        assert_counts_agree(payload, count_keys=("critical_count", "high_count"))


def test_counts_agree_rejects_a_missing_count_key():
    payload = _consistent()
    del payload["high_count"]
    with pytest.raises(AssertionError, match="high_count"):
        assert_counts_agree(payload, count_keys=("critical_count", "high_count"))


def test_counts_agree_rejects_a_negative_count():
    payload = _consistent()
    payload["high_count"] = -1
    with pytest.raises(AssertionError, match="non-negative"):
        assert_counts_agree(payload, count_keys=("critical_count", "high_count"))


# --- repo gates are switched on ---


def test_pytest_config_is_xfail_strict():
    with open(GATEWAY_ROOT / "pyproject.toml", "rb") as fh:
        config = tomllib.load(fh)
    assert config["tool"]["pytest"]["ini_options"]["xfail_strict"] is True


def test_ruff_enforces_pytest_style_and_bugbear():
    with open(GATEWAY_ROOT / "ruff.toml", "rb") as fh:
        config = tomllib.load(fh)
    select = config["lint"]["select"]
    assert "PT" in select, "flake8-pytest-style must be on"
    assert "B" in select, "bugbear must be on"
    tests_ignores = config["lint"]["per-file-ignores"]["tests/**"]
    assert "PT011" not in tests_ignores, "bare pytest.raises must stay banned in tests"
