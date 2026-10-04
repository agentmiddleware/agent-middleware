"""Permit / ledger / receipt credit alignment helpers."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.governed_metering import (
    ChargeCreditMismatchError,
    aligned_credits_charged,
    ledger_debit_credits,
)


def test_ledger_debit_credits_abs():
    assert ledger_debit_credits(Decimal("-3")) == Decimal("3")
    assert ledger_debit_credits("-1.5") == Decimal("1.5")


def test_aligned_credits_charged_match():
    assert aligned_credits_charged(
        ledger_amount=Decimal("-3"),
        authorized_credits=Decimal("3"),
        context="test",
    ) == Decimal("3")


def test_aligned_credits_charged_zero_debit():
    assert aligned_credits_charged(
        ledger_amount=Decimal("0"),
        authorized_credits=Decimal("3"),
        context="test",
    ) == Decimal("0")


def test_aligned_credits_charged_mismatch():
    with pytest.raises(ChargeCreditMismatchError, match="ledger charged"):
        aligned_credits_charged(
            ledger_amount=Decimal("-2"),
            authorized_credits=Decimal("3"),
            context="awi_http:awi_rag_query",
        )
