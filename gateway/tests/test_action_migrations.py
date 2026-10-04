"""Additive authority migration must never downgrade accepted bindings away."""

import os
import sqlite3
import subprocess
import sys

import pytest

from tests.support.action_database_guard import (
    require_action_database_url,
    require_action_test_environment,
)

# Validate the separately selected migration target before pytest DB setup too.
if os.environ.get("ACTION_MIGRATION_DATABASE_URL"):
    require_action_database_url(os.environ["ACTION_MIGRATION_DATABASE_URL"])
    require_action_test_environment(os.environ.get("ENVIRONMENT"))

FIELDS = (
    "action_contract_version",
    "action_payload_hash",
    "action_schema_id",
    "action_schema_version",
    "action_public_tool_id",
    "action_upstream_binding_hash",
)
ACTION_REVISION = "042_permit_action_binding"
SCRUB_REVISION = "041_scrub_content_owner_keys"
CONTENT_TABLES = ("content_pipelines", "content_campaigns")
AUTHORITIES = [
    (table, field) for table in ("permits", "receipts") for field in FIELDS
] + [("idempotency_records", "owner")]


def content_inserts():
    for table, id_column, required in (
        ("content_pipelines", "pipeline_id", "title"),
        ("content_campaigns", "campaign_id", "campaign_title,source_url"),
    ):
        values = "'fixture'" if table == "content_pipelines" else "'fixture','local'"
        for identity, owner in (
            ("legacy", "synthetic-owner"),
            ("wallet", "w"),
            ("empty", ""),
        ):
            yield (
                f"INSERT INTO {table} ({id_column},{required},owner_key) "
                f"VALUES ('{identity}',{values},'{owner}')"
            )


def retained_authority_sql(authority):
    table, column = authority
    if table == "idempotency_records":
        return (
            "INSERT INTO idempotency_records (record_id,wallet_id,endpoint,idempotency_key,request_hash,created_at) "
            "VALUES ('o','w','/mcp/action/v1','act1-key','hash','2026-01-01')"
        )
    # Unknown versions and each partial binding independently retain authority.
    value = "99" if column == "action_contract_version" else "'digest'"
    return f"UPDATE {table} SET {column} = {value}"


def migrate(url, revision, direction="upgrade"):
    return subprocess.run(
        [sys.executable, "-m", "alembic", direction, revision],
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("start_revision", ["040_permit_repeat_window", SCRUB_REVISION])
@pytest.mark.parametrize("authority", [None, *AUTHORITIES])
def test_action_migration_retains_legacy_and_blocks_authority_loss(
    tmp_path, start_revision, authority
):
    path = tmp_path / "migration.sqlite"
    url = f"sqlite+aiosqlite:///{path}"
    result = migrate(url, "040_permit_repeat_window")
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO wallets (wallet_id, wallet_type, balance, lifetime_credits, lifetime_debits, daily_spent, auto_refill, status) VALUES ('w','agent',100,100,0,0,0,'active')"
        )
        db.execute(
            "INSERT INTO signing_keys (key_id,alg,public_key_b64,status) VALUES ('k','Ed25519','public','active')"
        )
        db.execute(
            "INSERT INTO permits (permit_id,issuer_wallet_id,subject_wallet_id,scopes_json,allowed_tools_json,max_credits,spent_credits,expires_at,nonce,status,signature,key_id,issued_at) VALUES ('p','w','w','[]','[]',10,0,'2030-01-01','n','active','literal-old-signature','k','2026-01-01')"
        )
        db.execute(
            "INSERT INTO receipts (receipt_id,permit_id,wallet_id,tool,request_hash,credits_authorized,credits_charged,outcome,created_at,signature,signature_key_id) VALUES ('legacy','p','w','tool','hash',0,0,'success','2026-01-01','literal-old-receipt','k')"
        )
        receipt_before = db.execute("SELECT * FROM receipts").fetchone()
        before = db.execute("SELECT * FROM permits").fetchone()
        for statement in content_inserts():
            db.execute(statement)
    if start_revision == SCRUB_REVISION:
        result = migrate(url, SCRUB_REVISION)
        assert result.returncode == 0, result.stderr
        with sqlite3.connect(path) as db:
            for table in CONTENT_TABLES:
                assert sorted(db.execute(f"SELECT owner_key FROM {table}")) == [
                    ("",),
                    ("",),
                    ("w",),
                ]
    result = migrate(url, "head")
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (
            ACTION_REVISION,
        )
        assert db.execute("SELECT * FROM permits").fetchone() == (
            *before,
            *([None] * 6),
        )
        assert db.execute(
            "SELECT * FROM receipts WHERE receipt_id='legacy'"
        ).fetchone() == (*receipt_before, *([None] * 6))
        for table in ("permits", "receipts"):
            cols = {r[1]: r for r in db.execute(f"PRAGMA table_info({table})")}
            assert all(name in cols and cols[name][3] == 0 for name in FIELDS)
        for table in CONTENT_TABLES:
            assert sorted(db.execute(f"SELECT owner_key FROM {table}")) == [
                ("",),
                ("",),
                ("w",),
            ]
        if authority is not None:
            db.execute(retained_authority_sql(authority))
        retained = {
            table: db.execute(f"SELECT * FROM {table}").fetchall()
            for table in ("permits", "receipts", "idempotency_records")
        }
    result = migrate(url, SCRUB_REVISION, "downgrade")
    if authority is None:
        assert result.returncode == 0, result.stderr
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT * FROM permits").fetchone() == before
            assert db.execute("SELECT * FROM receipts").fetchone() == receipt_before
            assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (
                SCRUB_REVISION,
            )
    else:
        assert result.returncode != 0, "downgrade discarded durable action authority"
        assert "action_authority_retained" in result.stderr
        with sqlite3.connect(path) as db:
            assert (
                db.execute("SELECT version_num FROM alembic_version").fetchone()[0]
                == ACTION_REVISION
            )
            for table, rows in retained.items():
                assert db.execute(f"SELECT * FROM {table}").fetchall() == rows
    with sqlite3.connect(path) as db:
        for table in CONTENT_TABLES:
            assert sorted(db.execute(f"SELECT owner_key FROM {table}")) == [
                ("",),
                ("",),
                ("w",),
            ]


@pytest.mark.skipif(
    not os.environ.get("ACTION_MIGRATION_DATABASE_URL"),
    reason="requires fresh disposable local action migration DB",
)
def test_postgres_legacy_roundtrip_and_fail_closed_downgrade():
    import asyncio
    import asyncpg

    url = os.environ["ACTION_MIGRATION_DATABASE_URL"]
    require_action_database_url(url)
    require_action_test_environment(os.environ.get("ENVIRONMENT"))

    async def query(sql):
        connection = await asyncpg.connect(
            url.replace("postgresql+asyncpg", "postgresql")
        )
        try:
            return [dict(r) for r in await connection.fetch(sql)]
        finally:
            await connection.close()

    def sql(statement):
        return asyncio.run(query(statement))

    assert sql("SELECT tablename FROM pg_tables WHERE schemaname='public'") == [], (
        "refuse reused migration DB"
    )
    result = migrate(url, "040_permit_repeat_window")
    assert result.returncode == 0, result.stderr
    sql(
        "INSERT INTO wallets (wallet_id,wallet_type,balance,lifetime_credits,lifetime_debits,daily_spent,auto_refill,status) VALUES ('w','agent',100,100,0,0,false,'active') RETURNING wallet_id"
    )
    sql(
        "INSERT INTO signing_keys (key_id,alg,public_key_b64,status) VALUES ('k','Ed25519','public','active') RETURNING key_id"
    )
    sql(
        "INSERT INTO permits (permit_id,issuer_wallet_id,subject_wallet_id,scopes_json,allowed_tools_json,max_credits,spent_credits,expires_at,nonce,status,signature,key_id,issued_at) VALUES ('p','w','w','[]','[]',10,0,'2030-01-01','n','active','literal-old-signature','k','2026-01-01') RETURNING permit_id"
    )
    sql(
        "INSERT INTO receipts (receipt_id,permit_id,wallet_id,tool,request_hash,credits_authorized,credits_charged,outcome,created_at,signature,signature_key_id) VALUES ('r','p','w','tool','hash',0,0,'success','2026-01-01','literal-old-receipt','k') RETURNING receipt_id"
    )
    before = {t: sql(f"SELECT * FROM {t}")[0] for t in ("permits", "receipts")}
    for statement in content_inserts():
        sql(statement)
    result = migrate(url, "head")
    assert result.returncode == 0, result.stderr
    assert sql("SELECT version_num FROM alembic_version") == [
        {"version_num": ACTION_REVISION}
    ]
    for table, old in before.items():
        assert sql(f"SELECT * FROM {table}")[0] == {**old, **dict.fromkeys(FIELDS)}
    for table in CONTENT_TABLES:
        assert sql(f"SELECT owner_key FROM {table} ORDER BY owner_key") == [
            {"owner_key": ""},
            {"owner_key": ""},
            {"owner_key": "w"},
        ]
    result = migrate(url, SCRUB_REVISION, "downgrade")
    assert result.returncode == 0, result.stderr
    assert sql("SELECT version_num FROM alembic_version") == [
        {"version_num": SCRUB_REVISION}
    ]
    assert {t: sql(f"SELECT * FROM {t}")[0] for t in before} == before
    # The second upgrade exercises the 041 -> 042 entry path independently.
    result = migrate(url, "head")
    assert result.returncode == 0, result.stderr
    for table, old in before.items():
        assert sql(f"SELECT * FROM {table}")[0] == {**old, **dict.fromkeys(FIELDS)}
        assert sql(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_schema='public' AND table_name='{table}' "
            "AND column_name LIKE 'action_%' AND is_nullable='YES' ORDER BY column_name"
        ) == [{"column_name": name} for name in sorted(FIELDS)]
    for authority in AUTHORITIES:
        sql(retained_authority_sql(authority))
        retained = {
            table: sql(f"SELECT * FROM {table}")
            for table in ("permits", "receipts", "idempotency_records")
        }
        result = migrate(url, SCRUB_REVISION, "downgrade")
        assert result.returncode != 0 and "action_authority_retained" in result.stderr
        assert sql("SELECT version_num FROM alembic_version") == [
            {"version_num": ACTION_REVISION}
        ]
        for table, rows in retained.items():
            assert sql(f"SELECT * FROM {table}") == rows
        table, column = authority
        if table != "idempotency_records":
            # Reset only this synthetic partial field to isolate the next case.
            sql(f"UPDATE {table} SET {column}=NULL")
    for table in CONTENT_TABLES:
        assert sql(f"SELECT owner_key FROM {table} ORDER BY owner_key") == [
            {"owner_key": ""},
            {"owner_key": ""},
            {"owner_key": "w"},
        ]
    print(
        "PostgreSQL: 040 -> 041 -> 042 and 041 -> 042; content owners scrubbed; "
        "legacy permit/receipt bytes retained; every partial authority and owner downgrade blocked"
    )
