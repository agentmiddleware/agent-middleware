"""Exact credit storage shared by request validation and signed services."""

from decimal import Decimal


def credit_amount_fits_storage(amount: Decimal) -> bool:
    """Whether a non-negative Numeric(20, 8) credit amount survives storage.

    SQLite converts the bound Decimal through a float and reconstructs eight
    fractional places, so even some in-scale large values lose precision.
    Keep the same conservative contract on both supported backends; never
    silently round a signed amount.
    """
    return (
        amount.is_finite()
        and Decimal("0") <= amount < Decimal("1000000000000")
        and Decimal(f"{float(amount):.8f}") == amount
    )
