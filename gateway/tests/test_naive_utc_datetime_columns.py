"""Every datetime column stores and returns timezone-free UTC.

The schema stores datetimes as ``TIMESTAMP WITHOUT TIME ZONE`` and the app
writes naive UTC (``app.core.time.utc_now``). SQLModel 0.0.45 changed the
mapping of an un-annotated ``datetime`` field to
``sqlmodel.sql.sqltypes.UTCDateTime``: it emits ``TIMESTAMP WITH TIME ZONE``,
raises ``ValueError`` when a naive value is bound, and returns aware values on
read. Fields therefore declare ``sa_type=NaiveUTCDateTime``.

The column sweep holds that for every table. The round trips cover the two
columns that still fell through to ``UTCDateTime`` after #476:
``daily_balance_snapshots.date`` and ``content_schedules.recommended_time``.
Both fail under SQLModel >= 0.0.45 without the annotation.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import DateTime, TypeDecorator, delete, select
from sqlalchemy.dialects import postgresql
from sqlmodel import SQLModel

from app.db.database import get_session_factory
from app.db.models import (
    ContentPieceModel,
    ContentPipelineModel,
    ContentScheduleModel,
    DailyBalanceSnapshot,
    WalletModel,
)

NAIVE_UTC = datetime(2026, 9, 27, 17, 30, 15, 123456)
PG_DIALECT = postgresql.dialect()


def _datetime_columns():
    for table in SQLModel.metadata.sorted_tables:
        for column in table.columns:
            type_ = column.type
            if isinstance(type_, TypeDecorator):
                type_ = type_.impl_instance
            if isinstance(type_, DateTime):
                yield f"{table.name}.{column.name}", column


DATETIME_COLUMNS = dict(_datetime_columns())


def test_sweep_covers_the_columns_it_guards():
    assert "daily_balance_snapshots.date" in DATETIME_COLUMNS
    assert "content_schedules.recommended_time" in DATETIME_COLUMNS
    assert "wallets.created_at" in DATETIME_COLUMNS


@pytest.mark.parametrize("name", sorted(DATETIME_COLUMNS))
def test_datetime_column_is_naive_utc(name):
    column = DATETIME_COLUMNS[name]
    # Same DDL as migrations/versions, so the models need no migration.
    assert column.type.compile(dialect=PG_DIALECT) == "TIMESTAMP WITHOUT TIME ZONE"

    bind = column.type.bind_processor(PG_DIALECT)
    bound = bind(NAIVE_UTC) if bind else NAIVE_UTC
    assert bound == NAIVE_UTC
    assert bound.tzinfo is None

    result = column.type.result_processor(PG_DIALECT, None)
    loaded = result(NAIVE_UTC) if result else NAIVE_UTC
    assert loaded == NAIVE_UTC
    assert loaded.tzinfo is None


@pytest_asyncio.fixture
async def rows():
    suffix = uuid.uuid4().hex[:12]
    ids = {
        "wallet": f"w-naive-{suffix}",
        "snapshot": f"snap-naive-{suffix}",
        "pipeline": f"p-naive-{suffix}",
        "content": f"c-naive-{suffix}",
        "schedule": f"s-naive-{suffix}",
    }
    factory = get_session_factory()
    async with factory() as session:
        session.add(WalletModel(wallet_id=ids["wallet"], wallet_type="sponsor"))
        session.add(
            ContentPipelineModel(pipeline_id=ids["pipeline"], title="naive utc")
        )
        await session.flush()
        session.add(
            ContentPieceModel(
                content_id=ids["content"],
                pipeline_id=ids["pipeline"],
                format="short_video",
                title="naive utc",
                download_url="file:///tmp/naive.mp4",
                status="ready",
            )
        )
        await session.commit()

    yield ids

    async with factory() as session:
        await session.execute(
            delete(ContentScheduleModel).where(
                ContentScheduleModel.schedule_id == ids["schedule"]
            )
        )
        await session.execute(
            delete(DailyBalanceSnapshot).where(
                DailyBalanceSnapshot.snapshot_id == ids["snapshot"]
            )
        )
        await session.execute(
            delete(ContentPieceModel).where(
                ContentPieceModel.content_id == ids["content"]
            )
        )
        await session.execute(
            delete(ContentPipelineModel).where(
                ContentPipelineModel.pipeline_id == ids["pipeline"]
            )
        )
        await session.execute(
            delete(WalletModel).where(WalletModel.wallet_id == ids["wallet"])
        )
        await session.commit()


async def test_daily_balance_snapshot_date_round_trips_naive_utc(rows):
    factory = get_session_factory()
    async with factory() as session:
        session.add(
            DailyBalanceSnapshot(
                snapshot_id=rows["snapshot"],
                wallet_id=rows["wallet"],
                date=NAIVE_UTC,
                opening_balance=Decimal("10"),
                closing_balance=Decimal("7.5"),
            )
        )
        await session.commit()

    async with factory() as session:
        loaded = (
            await session.execute(
                select(DailyBalanceSnapshot).where(
                    DailyBalanceSnapshot.wallet_id == rows["wallet"],
                    DailyBalanceSnapshot.date == NAIVE_UTC,
                )
            )
        ).scalar_one()

    assert loaded.snapshot_id == rows["snapshot"]
    assert loaded.date == NAIVE_UTC
    assert loaded.date.tzinfo is None


async def test_content_schedule_recommended_time_round_trips_naive_utc(rows):
    factory = get_session_factory()
    async with factory() as session:
        session.add(
            ContentScheduleModel(
                schedule_id=rows["schedule"],
                content_id=rows["content"],
                platform="tiktok",
                recommended_time=NAIVE_UTC,
                confidence=0.9,
            )
        )
        await session.commit()

    async with factory() as session:
        loaded = (
            await session.execute(
                select(ContentScheduleModel).where(
                    ContentScheduleModel.content_id == rows["content"],
                    ContentScheduleModel.recommended_time == NAIVE_UTC,
                )
            )
        ).scalar_one()

    assert loaded.schedule_id == rows["schedule"]
    assert loaded.recommended_time == NAIVE_UTC
    assert loaded.recommended_time.tzinfo is None
