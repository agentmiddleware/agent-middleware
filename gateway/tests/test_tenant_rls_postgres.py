"""Opt-in PostgreSQL proof for 048_tenant_rls and the 050 backfill.

Set TENANT_RLS_PG_ADMIN_URL to a superuser URL on a disposable local server
(e.g. postgresql://postgres@127.0.0.1:5432/postgres). The test creates its own
database and two non-superuser roles, runs every migration as the database
owner, and drops everything afterwards.

* enforce mode: 048 FORCEs RLS, so the owner itself is wallet-scoped. The 050
  backfill must still update rows (migrations/env.py sets app.rls_bypass), and
  every table in RLS_TABLES (including receipt_resolutions and
  resolution_chain_heads) must show only the current wallet's rows.
* default mode (off): policies exist but RLS stays disabled; a no-op for
  every role.
* enable mode: RLS is enabled but not forced; the owner keeps full access and
  a separate non-owner role sees only its wallet.
* the boot schema check passes against the migrated database.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

from tests.support import db_gate

GATEWAY_ROOT = Path(__file__).resolve().parents[1]
ADMIN_ENV = "TENANT_RLS_PG_ADMIN_URL"
W1, W2 = "wallet-rls-one", "wallet-rls-two"

pytestmark = db_gate.collection_mark(
    url_env=ADMIN_ENV, reason="requires a disposable local PostgreSQL superuser URL"
)


def _rls_tables() -> dict[str, tuple[str, ...]]:
    path = GATEWAY_ROOT / "migrations" / "versions" / "048_tenant_rls.py"
    spec = importlib.util.spec_from_file_location("m048", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {**module.RLS_TABLES, **module.LATE_RLS_TABLES}


def _with(url: str, *, user: str | None = None, db: str | None = None) -> str:
    parts = urlsplit(url.replace("postgresql+asyncpg", "postgresql"))
    netloc = parts.netloc.rsplit("@", 1)[-1]
    if user:
        netloc = f"{user}@{netloc}"
    elif "@" in parts.netloc:
        netloc = parts.netloc
    path = f"/{db}" if db else parts.path
    return urlunsplit((parts.scheme, netloc, path, parts.query, ""))


async def _run(url: str, *statements: str, fetch: str | None = None):
    import asyncpg

    conn = await asyncpg.connect(url)
    try:
        async with conn.transaction():
            for statement in statements:
                await conn.execute(statement)
            if fetch:
                return [tuple(r) for r in await conn.fetch(fetch)]
    finally:
        await conn.close()


def sql(url, *statements, fetch=None):
    return asyncio.run(_run(url, *statements, fetch=fetch))


def migrate(url: str, revision: str, mode: str | None):
    env = {**os.environ, "DATABASE_URL": url.replace("postgresql://", "postgresql+asyncpg://", 1)}
    env.pop("TENANT_RLS_MODE", None)
    if mode:
        env["TENANT_RLS_MODE"] = mode
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        cwd=GATEWAY_ROOT, env=env, capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stderr[-4000:]


_FILL = {
    "integer": "1", "bigint": "1", "smallint": "1", "numeric": "1",
    "double precision": "1", "real": "1", "boolean": "false",
    "timestamp without time zone": "now()", "timestamp with time zone": "now()",
    "date": "current_date", "json": "'{}'", "jsonb": "'{}'", "uuid": "gen_random_uuid()",
}


def _seed_statements(admin_db: str, tables) -> list[str]:
    """One row per wallet per table, FK triggers off (superuser, replica role)."""
    tables = {"wallets": ("wallet_id",), **tables}
    names = ",".join(f"'{t}'" for t in tables)
    cols = sql(admin_db, fetch=(
        "SELECT table_name, column_name, data_type, character_maximum_length FROM information_schema.columns "
        f"WHERE table_schema='public' AND table_name IN ({names}) "
        "AND is_nullable='NO' AND column_default IS NULL ORDER BY table_name, ordinal_position"
    ))
    required: dict[str, list[tuple[str, str, int | None]]] = {t: [] for t in tables}
    for table, column, dtype, max_len in cols:
        required[table].append((column, dtype, max_len))
    # Enumerated CHECKs (col IN ('a', ...)): use the first allowed literal.
    allowed: dict[tuple[str, str], str] = {}
    for table, definition in sql(admin_db, fetch=(
        "SELECT conrelid::regclass::text, pg_get_constraintdef(oid) FROM pg_constraint "
        f"WHERE contype='c' AND conrelid::regclass::text IN ({names})"
    )):
        match = re.search(r"\(+(\w+)\)?(?:::text)? = ANY \(\(?ARRAY\['([^']*)'", definition)
        if match:
            allowed[(table, match.group(1))] = f"'{match.group(2)}'"
    out = ["SET LOCAL session_replication_role = replica"]
    for table, wallet_cols in tables.items():
        for wallet in (W1, W2):
            values = {c: f"'{wallet}'" for c in wallet_cols}
            for column, dtype, max_len in required[table]:
                if column in values:
                    continue
                text = uuid.uuid4().hex[: max_len or 32]
                values[column] = allowed.get(
                    (table, column), _FILL.get(dtype, f"'{text}'")
                )
            out.append(
                f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join(values.values())})"
            )
    return out


@pytest.fixture
def cluster():
    admin = os.environ[ADMIN_ENV]
    host = urlsplit(admin).hostname or ""
    if host not in ("", "localhost", "127.0.0.1", "::1") and not host.startswith("/"):
        pytest.fail("refusing non-local admin URL")
    suffix = uuid.uuid4().hex[:8]
    owner, reader, db = f"rls_owner_{suffix}", f"rls_reader_{suffix}", f"rls_proof_{suffix}"
    admin_url = _with(admin)
    sql(admin_url, f"CREATE ROLE {owner} LOGIN NOSUPERUSER NOBYPASSRLS")
    sql(admin_url, f"CREATE ROLE {reader} LOGIN NOSUPERUSER NOBYPASSRLS")

    async def create_db():
        import asyncpg
        conn = await asyncpg.connect(admin_url)
        try:
            await conn.execute(f"CREATE DATABASE {db} OWNER {owner}")
        finally:
            await conn.close()

    asyncio.run(create_db())
    try:
        yield {
            "admin": _with(admin, db=db),
            "owner": _with(admin, user=owner, db=db),
            "reader": _with(admin, user=reader, db=db),
            "reader_role": reader,
        }
    finally:
        async def drop():
            import asyncpg
            conn = await asyncpg.connect(admin_url)
            try:
                await conn.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
                await conn.execute(f"DROP ROLE IF EXISTS {owner}")
                await conn.execute(f"DROP ROLE IF EXISTS {reader}")
            finally:
                await conn.close()

        asyncio.run(drop())


def _visible(url: str, table: str, cols, wallet: str | None) -> set[str]:
    scope = [f"SELECT set_config('app.current_wallet_id', '{wallet}', true)"] if wallet else []
    rows = sql(url, *scope, fetch=f"SELECT DISTINCT {cols[0]} FROM {table}")
    return {r[0] for r in rows}


def test_enforce_mode_force_rls_isolates_every_table_and_backfill_runs(cluster):
    tables = _rls_tables()
    assert {"receipt_resolutions", "resolution_chain_heads"} <= set(tables)
    migrate(cluster["owner"], "049_demo_key_attribution", "enforce")
    flags = dict(
        (r[0], (r[1], r[2]))
        for r in sql(cluster["admin"], fetch=(
            "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relnamespace='public'::regnamespace AND relkind='r'"
        ))
    )
    for table in tables:
        assert flags[table] == (True, True), table
    # Seed rows before 050 so its backfill has work to do under FORCE.
    sql(cluster["admin"], *_seed_statements(cluster["admin"], tables))
    sql(cluster["admin"], "UPDATE api_keys SET last_used_at = now(), first_used_at = NULL")
    migrate(cluster["owner"], "head", "enforce")
    backfilled = sql(cluster["admin"], fetch=(
        "SELECT count(*) FROM api_keys WHERE first_used_at IS NOT NULL"
    ))[0][0]
    assert backfilled == 2

    for table, cols in tables.items():
        assert _visible(cluster["owner"], table, cols, None) == set(), table
        assert _visible(cluster["owner"], table, cols, W1) == {W1}, table
        assert _visible(cluster["owner"], table, cols, W2) == {W2}, table
        # WITH CHECK: the owner cannot move a visible row into another wallet
        # (every wallet column, since any one matching admits the row).
        with pytest.raises(Exception, match="row-level security"):
            sql(
                cluster["owner"],
                f"SELECT set_config('app.current_wallet_id', '{W1}', true)",
                f"UPDATE {table} SET "
                + ", ".join(f"{c} = '{W2}'" for c in cols)
                + f" WHERE {cols[0]} = '{W1}'",
            )

    from sqlalchemy.ext.asyncio import create_async_engine
    from app.db.database import verify_required_schema

    async def boot_check():
        engine = create_async_engine(
            cluster["owner"].replace("postgresql://", "postgresql+asyncpg://", 1)
        )
        try:
            await verify_required_schema(engine)
        finally:
            await engine.dispose()

    asyncio.run(boot_check())


@pytest.mark.parametrize("mode", [None, "enable"], ids=["default-off", "enable"])
def test_off_is_noop_and_enable_isolates_non_owner_roles(cluster, mode):
    tables = _rls_tables()
    migrate(cluster["owner"], "head", mode)
    flags = dict(
        (r[0], (r[1], r[2]))
        for r in sql(cluster["admin"], fetch=(
            "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relnamespace='public'::regnamespace AND relkind='r'"
        ))
    )
    policies = {
        r[0]
        for r in sql(cluster["admin"], fetch=(
            "SELECT tablename FROM pg_policies WHERE policyname='amw_tenant_isolation'"
        ))
    }
    sql(cluster["admin"], *_seed_statements(cluster["admin"], tables))
    sql(cluster["owner"], f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {cluster['reader_role']}")
    enabled = mode == "enable"
    for table, cols in tables.items():
        assert table in policies, table
        assert flags[table] == (enabled, False), table
        assert _visible(cluster["owner"], table, cols, None) == {W1, W2}, table
        expected_unscoped = set() if enabled else {W1, W2}
        assert _visible(cluster["reader"], table, cols, None) == expected_unscoped, table
        expected_w1 = {W1} if enabled else {W1, W2}
        assert _visible(cluster["reader"], table, cols, W1) == expected_w1, table
