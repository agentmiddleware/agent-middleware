"""Shared metering helpers for governed invoke paths (MCP + AWI HTTP).

Keeps permit budget, ledger debit, and receipt credits on the same math.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any


class ChargeCreditMismatchError(RuntimeError):
    """Ledger debit does not match the authorized/reserved credit amount."""


def ledger_debit_credits(ledger_amount: Any) -> Decimal:
    """Absolute credits debited from a ledger entry amount field."""
    return abs(Decimal(str(ledger_amount)))


def aligned_credits_charged(
    *,
    ledger_amount: Any,
    authorized_credits: Decimal,
    context: str,
) -> Decimal:
    """
    Return credits to record on the receipt.

    Success path: ledger debit must equal authorized (and reserved) credits.
    Zero debit returns Decimal("0") without mismatch (e.g. dry paths).
    """
    charged = ledger_debit_credits(ledger_amount)
    if charged == Decimal("0"):
        return Decimal("0")
    if charged != authorized_credits:
        raise ChargeCreditMismatchError(
            f"{context}: ledger charged {charged} != authorized {authorized_credits}"
        )
    return authorized_credits
