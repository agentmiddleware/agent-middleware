"""Unit tests for the golden-file regeneration review gate.

Golden snapshots pin the bytes that permits and receipts sign. Rewriting
the expected output to make a refactor PR green must stay a conscious,
reviewed act, never a bare env var. These tests pin the gate helper itself;
the snapshot suites call it before touching any golden file.
"""

from __future__ import annotations

import pytest

from tests.support.golden_regen import (
    REGEN_ACK_ENV_VAR,
    require_golden_regen_ack,
)


def test_no_update_flag_means_no_regeneration(monkeypatch):
    monkeypatch.delenv("UPDATE_PERMIT_GOLDEN", raising=False)
    monkeypatch.delenv(REGEN_ACK_ENV_VAR, raising=False)
    assert require_golden_regen_ack("UPDATE_PERMIT_GOLDEN") is False


def test_update_flag_without_ack_fails(monkeypatch):
    """UPDATE=1 alone must fail, before any file is rewritten."""
    monkeypatch.setenv("UPDATE_PERMIT_GOLDEN", "1")
    monkeypatch.delenv(REGEN_ACK_ENV_VAR, raising=False)
    with pytest.raises(pytest.fail.Exception, match="GOLDEN_REGEN_ACK"):
        require_golden_regen_ack("UPDATE_PERMIT_GOLDEN")


def test_ack_must_be_exact(monkeypatch):
    """Near-miss ack values do not count as review."""
    monkeypatch.setenv("UPDATE_RECEIPT_GOLDEN", "1")
    for value in ("yes", "true", "0", ""):
        monkeypatch.setenv(REGEN_ACK_ENV_VAR, value)
        with pytest.raises(pytest.fail.Exception, match="GOLDEN_REGEN_ACK"):
            require_golden_regen_ack("UPDATE_RECEIPT_GOLDEN")


def test_update_flag_with_ack_allows_regeneration(monkeypatch):
    monkeypatch.setenv("UPDATE_PERMIT_GOLDEN", "1")
    monkeypatch.setenv(REGEN_ACK_ENV_VAR, "1")
    assert require_golden_regen_ack("UPDATE_PERMIT_GOLDEN") is True
