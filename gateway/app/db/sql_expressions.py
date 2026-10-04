"""Reusable SQL expressions for writes that must not lose a concurrent update.

The money paths in this application mutate counters that several requests can
touch at once. The unsafe form is always the same: read a column into Python,
compute a new total, write the total back. That is a read-modify-write, and it
is serialized only by whatever row lock happens to be held — which is nothing
at all on SQLite, where ``SELECT ... FOR UPDATE`` parses and does nothing.

Expressing the arithmetic against the *column* instead of against a value this
process read keeps the whole operation relative, so the database applies it to
whatever is stored at write time.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

from sqlalchemy import case
from sqlalchemy.sql.elements import ColumnElement


def clamped_decrement(column: Any, amount: Decimal) -> Any:
    """``max(0, column - amount)``, evaluated by the database.

    The Python spelling of this clamp (``max(Decimal("0"), col - amount)``)
    has to read the column first, which reintroduces the lost update the
    relative form exists to prevent.
    """
    return case(
        (cast(ColumnElement[bool], column - amount < Decimal("0")), Decimal("0")),
        else_=column - amount,
    )
