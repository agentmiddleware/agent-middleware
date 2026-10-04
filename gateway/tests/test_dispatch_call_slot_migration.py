"""Concurrent index interruptions must not strand an unstamped column addition."""

import asyncio
import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import create_async_engine

from tests.support.action_database_guard import (
    require_action_database_url,
    require_action_test_environment,
)

TARGET = os.environ.get("DISPATCH_MIGRATION_DATABASE_URL")
if TARGET:
    require_action_database_url(TARGET)
    require_action_test_environment(os.environ.get("ENVIRONMENT"))

PREVIOUS = "037_mcp_dispatch_claim_hash"
REVISION = "038_dispatch_call_slot_dup_idx"
INDEX = "ix_mcp_dispatch_attempts_duplicate_detection"


@pytest.mark.skipif(not TARGET, reason="requires fresh disposable migration PostgreSQL")
def test_dispatch_call_slot_upgrade_recovers_after_concurrent_index_failure(
    monkeypatch,
):
    assert TARGET is not None
    monkeypatch.setenv("DATABASE_URL", TARGET)
    config = Config("alembic.ini")

    async def query(statement):
        engine = create_async_engine(TARGET)
        try:
            async with engine.begin() as connection:
                result = await connection.execute(text(statement))
                return result.all() if result.returns_rows else []
        finally:
            await engine.dispose()

    def sql(statement):
        return asyncio.run(query(statement))

    assert sql("SELECT tablename FROM pg_tables WHERE schemaname='public'") == [], (
        "refuse reused migration DB"
    )
    command.upgrade(config, PREVIOUS)

    def interrupt_index(
        connection, cursor, statement, parameters, context, executemany
    ):
        if statement.startswith(f"CREATE INDEX CONCURRENTLY {INDEX}"):
            raise RuntimeError("synthetic concurrent-index interruption")

    event.listen(Engine, "before_cursor_execute", interrupt_index)
    try:
        with pytest.raises(
            RuntimeError, match="synthetic concurrent-index interruption"
        ):
            command.upgrade(config, REVISION)
    finally:
        event.remove(Engine, "before_cursor_execute", interrupt_index)

    assert sql("SELECT version_num FROM alembic_version") == [(PREVIOUS,)]
    assert sql(
        "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='mcp_dispatch_attempts' "
        "AND column_name='call_slot_reserved'"
    ) == [("boolean", "NO", "false")]
    assert sql(f"SELECT to_regclass('{INDEX}')") == [(None,)]

    command.upgrade(config, REVISION)
    assert sql("SELECT version_num FROM alembic_version") == [(REVISION,)]
    assert sql(
        f"SELECT indisvalid FROM pg_index WHERE indexrelid='{INDEX}'::regclass"
    ) == [(True,)]

    # Simulate interruption after a successful index build but before stamping.
    command.stamp(config, PREVIOUS)
    command.upgrade(config, REVISION)
    assert sql("SELECT version_num FROM alembic_version") == [(REVISION,)]

    # Existing incompatible columns must never be silently accepted as recovery.
    for definition in (
        "INTEGER NOT NULL DEFAULT 0",
        "BOOLEAN DEFAULT false",
        "BOOLEAN NOT NULL DEFAULT true",
    ):
        command.downgrade(config, PREVIOUS)
        sql(
            f"ALTER TABLE mcp_dispatch_attempts ADD COLUMN call_slot_reserved {definition}"
        )
        with pytest.raises(
            RuntimeError, match="migration038_incompatible_call_slot_reserved"
        ):
            command.upgrade(config, REVISION)
        assert sql("SELECT version_num FROM alembic_version") == [(PREVIOUS,)]
        sql("ALTER TABLE mcp_dispatch_attempts DROP COLUMN call_slot_reserved")
        command.upgrade(config, REVISION)

    command.upgrade(config, "head")
    assert sql("SELECT version_num FROM alembic_version") == [
        ("042_permit_action_binding",)
    ]
