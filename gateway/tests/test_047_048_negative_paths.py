"""Negative paths for ported 047 (reconcile lease) and 048 (tenant RLS)."""

import asyncio
import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from tests.test_api_chain_port_migrations import RECONCILE_COLUMNS, RECONCILE_INDEXES


def _config(tmp_path, monkeypatch):
    db_path = tmp_path / "neg.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    return Config("alembic.ini"), f"sqlite:///{db_path}"


def _run(fn, config, target):
    fn(config, target)
    asyncio.set_event_loop(asyncio.new_event_loop())


def _insert_attempt(conn, **overrides):
    """Insert one mcp_dispatch_attempts row, filling NOT NULL columns by type."""
    values = {}
    for col in inspect(conn).get_columns("mcp_dispatch_attempts"):
        name = col["name"]
        if name in overrides:
            values[name] = overrides[name]
        elif (
            not col["nullable"]
            and col.get("default") is None
            and not col.get("autoincrement")
        ):
            kind = str(col["type"]).upper()
            if "INT" in kind or "NUMERIC" in kind or "FLOAT" in kind:
                values[name] = 0
            elif "BOOL" in kind:
                values[name] = False
            elif "DATE" in kind or "TIME" in kind:
                values[name] = "2026-01-01 00:00:00"
            else:
                values[name] = f"{name}-1"
    names = ", ".join(values)
    params = ", ".join(f":{n}" for n in values)
    conn.execute(
        text(f"INSERT INTO mcp_dispatch_attempts ({names}) VALUES ({params})"), values
    )


def test_047_downgrade_refuses_when_reconcile_status_is_set(tmp_path, monkeypatch):
    config, sync_url = _config(tmp_path, monkeypatch)
    _run(command.upgrade, config, "047_dispatch_reconcile_lease")
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        _insert_attempt(conn, reconcile_status="refunded")
    engine.dispose()
    with pytest.raises(RuntimeError, match="047 downgrade refused: 1 dispatch"):
        _run(command.downgrade, config, "046_api_key_usage_events")
    engine = create_engine(sync_url)
    cols = {c["name"] for c in inspect(engine).get_columns("mcp_dispatch_attempts")}
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    engine.dispose()
    assert RECONCILE_COLUMNS <= cols
    assert version == "047_dispatch_reconcile_lease"


def test_047_upgrade_fills_in_partially_existing_columns_and_indexes(
    tmp_path, monkeypatch
):
    config, sync_url = _config(tmp_path, monkeypatch)
    _run(command.upgrade, config, "046_api_key_usage_events")
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE mcp_dispatch_attempts ADD COLUMN reconcile_status VARCHAR(32)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX ix_mcp_dispatch_attempts_reconcile_status "
                "ON mcp_dispatch_attempts (reconcile_status)"
            )
        )
    engine.dispose()
    _run(command.upgrade, config, "047_dispatch_reconcile_lease")
    engine = create_engine(sync_url)
    insp = inspect(engine)
    cols = [c["name"] for c in insp.get_columns("mcp_dispatch_attempts")]
    idx = {i["name"] for i in insp.get_indexes("mcp_dispatch_attempts")}
    engine.dispose()
    assert RECONCILE_COLUMNS <= set(cols)
    assert cols.count("reconcile_status") == 1
    assert RECONCILE_INDEXES <= idx


@pytest.mark.skipif(
    not os.environ.get("TENANT_RLS_OFF_MIGRATION_DATABASE_URL"),
    reason="set TENANT_RLS_OFF_MIGRATION_DATABASE_URL to an empty PostgreSQL DB",
)
def test_postgres_default_off_creates_policies_but_leaves_rls_disabled(monkeypatch):
    import asyncpg

    from app.core.config import get_settings

    url = os.environ["TENANT_RLS_OFF_MIGRATION_DATABASE_URL"]
    raw = url.replace("postgresql+asyncpg", "postgresql")

    def fetch(statement):
        async def run():
            connection = await asyncpg.connect(raw)
            try:
                return [tuple(r) for r in await connection.fetch(statement)]
            finally:
                await connection.close()

        return asyncio.run(run())

    assert fetch("SELECT count(*) FROM pg_tables WHERE schemaname='public'") == [
        (0,)
    ], "refuse reused migration DB"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr(get_settings(), "TENANT_RLS_MODE", "off")
    _run(command.upgrade, Config("alembic.ini"), "head")
    policies = {r[0] for r in fetch("SELECT DISTINCT polname FROM pg_policy")}
    assert {"amw_tenant_isolation", "amw_delegated_issuer_read"} <= policies
    assert fetch(
        "SELECT count(*) FROM pg_class WHERE relnamespace = 'public'::regnamespace "
        "AND relkind = 'r' AND (relrowsecurity OR relforcerowsecurity)"
    ) == [(0,)]
