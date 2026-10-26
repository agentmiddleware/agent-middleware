"""TENANT_RLS_MODE setting and 048 under forced RLS on PostgreSQL (opt-in)."""

import asyncio
import importlib.util
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from app.core.config import Settings, get_settings
from tests.support import db_gate

MIGRATION = Path(__file__).parents[1] / "migrations/versions/048_tenant_rls.py"


def _load_048():
    spec = importlib.util.spec_from_file_location("m048", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tenant_rls_mode_setting_matches_archived_api():
    assert Settings().TENANT_RLS_MODE == "off"
    assert Settings(TENANT_RLS_MODE=" Enforce ").TENANT_RLS_MODE == "enforce"
    with pytest.raises(ValueError, match="TENANT_RLS_MODE must be"):
        Settings(TENANT_RLS_MODE="enforced")


def test_048_covers_wallet_scoped_resolution_tables():
    module = _load_048()
    assert module.LATE_RLS_TABLES == {
        "receipt_resolutions": ("wallet_id",),
        "resolution_chain_heads": ("wallet_id",),
    }
    assert not set(module.LATE_RLS_TABLES) & set(module.RLS_TABLES)


@db_gate.collection_mark(
    url_env="TENANT_RLS_MIGRATION_DATABASE_URL",
    reason="requires a fresh disposable PostgreSQL DB owned by a "
    "NOSUPERUSER NOBYPASSRLS role",
)
def test_postgres_enforce_forces_rls_and_050_backfills(monkeypatch):
    import asyncpg

    url = os.environ["TENANT_RLS_MIGRATION_DATABASE_URL"]
    raw = url.replace("postgresql+asyncpg", "postgresql")

    def sql(statement, *, bypass=False):
        async def run():
            connection = await asyncpg.connect(raw)
            try:
                if bypass:
                    await connection.execute("SET app.rls_bypass = 'on'")
                if ";" in statement:
                    await connection.execute(statement)
                    return []
                return [tuple(r) for r in await connection.fetch(statement)]
            finally:
                await connection.close()

        return asyncio.run(run())

    assert sql(
        "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
    ) == [(False,)]
    assert sql("SELECT count(*) FROM pg_tables WHERE schemaname='public'") == [(0,)], (
        "refuse reused migration DB"
    )
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr(get_settings(), "TENANT_RLS_MODE", "enforce")

    def upgrade(target):
        command.upgrade(Config("alembic.ini"), target)
        asyncio.set_event_loop(asyncio.new_event_loop())

    upgrade("049_demo_key_attribution")
    # Minimal seed (no migration creates api_keys rows), under the RLS bypass.
    sql(
        "INSERT INTO wallets (wallet_id,wallet_type,balance,lifetime_credits,"
        "lifetime_debits,daily_spent,auto_refill,status) VALUES "
        "('w1','agent',0,0,0,0,false,'active');"
        "INSERT INTO api_keys (key_id,wallet_id,key_hash,key_prefix,last_used_at) "
        "VALUES ('k1','w1','h1','p1',now()),('k2','w1','h2','p2',NULL);",
        bypass=True,
    )
    upgrade("050_demo_key_first_used")
    assert sql("SELECT count(*) FROM api_keys") == [(0,)]  # owner is bound
    assert sql(
        "SELECT count(*) FROM api_keys WHERE first_used_at IS NOT NULL", bypass=True
    ) == [(1,)]
    module = _load_048()
    tables = sorted([*module.RLS_TABLES, *module.LATE_RLS_TABLES])
    rows = sql(
        "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, "
        "EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid "
        "AND p.polname = 'amw_tenant_isolation') FROM pg_class c "
        "WHERE c.relkind = 'r' AND c.relname = ANY(ARRAY["
        + ",".join(f"'{t}'" for t in tables)
        + "]) ORDER BY 1"
    )
    assert rows == [(t, True, True, True) for t in tables]
