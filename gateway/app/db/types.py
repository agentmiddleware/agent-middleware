"""SQLAlchemy type decorators for safe datetime handling.

Centralizes timezone normalization before dialect serialization,
preventing the asyncpg timezone-naivety bug that shipped to production.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, TypeDecorator

from app.core.time import to_naive_utc


class NaiveUTCDateTime(TypeDecorator[datetime]):
    """Store timezone-aware datetimes as naive UTC.

    On bind (Python → DB): converts aware datetimes to naive UTC.
    On result (DB → Python): returns naive UTC datetime unchanged.

    This ensures the signed payload and persisted row use identical
    timestamps on every dialect (SQLite, PostgreSQL, asyncpg).

    Replaces the per-query cursor hook in app/db/database.py.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(
        self, value: datetime | None, dialect: Any
    ) -> datetime | None:
        if value is None:
            return None
        return to_naive_utc(value)

    def process_result_value(
        self, value: datetime | None, dialect: Any
    ) -> datetime | None:
        # DB returns naive UTC; keep it naive for consistency
        return value
