"""Disposable PostgreSQL migration and concurrent operator proofs."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.support import db_gate
from tests.support.action_database_guard import (
    require_action_database_url,
    require_action_test_environment,
)


@db_gate.collection_mark(
    url_env="RESOLUTION_MIGRATION_DATABASE_URL",
    reason="requires fresh disposable migration PostgreSQL",
)
def test_resolution_evidence_migration_roundtrip(monkeypatch):
    target = os.environ["RESOLUTION_MIGRATION_DATABASE_URL"]
    require_action_database_url(target)
    require_action_test_environment(os.environ.get("ENVIRONMENT"))
    monkeypatch.setenv("DATABASE_URL", target)
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))

    async def query(statement):
        engine = create_async_engine(target)
        try:
            async with engine.begin() as connection:
                result = await connection.execute(text(statement))
                return result.all()
        finally:
            await engine.dispose()

    def sql(statement):
        return asyncio.run(query(statement))

    assert sql("SELECT tablename FROM pg_tables WHERE schemaname='public'") == [], (
        "refuse reused database"
    )
    command.upgrade(config, "057_receipt_permit_seq_unique")
    for _ in range(2):
        command.upgrade(config, "058_resolution_evidence")
        assert sql(
            "SELECT column_name, is_nullable FROM information_schema.columns WHERE table_name='receipt_resolutions' AND column_name IN ('evidence','evidence_hash') ORDER BY column_name"
        ) == [("evidence", "YES"), ("evidence_hash", "YES")]
        command.downgrade(config, "057_receipt_permit_seq_unique")
        assert (
            sql(
                "SELECT column_name FROM information_schema.columns WHERE table_name='receipt_resolutions' AND column_name IN ('evidence','evidence_hash')"
            )
            == []
        )
    command.upgrade(config, "head")
