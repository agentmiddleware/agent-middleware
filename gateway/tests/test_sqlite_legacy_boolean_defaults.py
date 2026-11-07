"""Legacy SQLite defaults remain safe after upgrading an existing database."""

import asyncio
import sqlite3

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest


_COLUMNS = (
    ("refresh_tokens", "revoked"),
    ("human_approvals", "simulated"),
    ("permits", "requires_human_approval"),
)


def _upgrade(monkeypatch, db_path, revision):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    command.upgrade(Config("alembic.ini"), revision)
    asyncio.set_event_loop(asyncio.new_event_loop())


def _defaults(connection):
    return {
        (table, column): next(
            row[4]
            for row in connection.execute(f"PRAGMA table_info({table})")
            if row[1] == column
        )
        for table, column in _COLUMNS
    }


def test_upgrade_repairs_legacy_sqlite_defaults(tmp_path, monkeypatch):
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert len(script.get_heads()) == 1
    assert script.get_revision("051_sqlite_boolean_defaults").down_revision == (
        "050_demo_key_first_used"
    )

    db_path = tmp_path / "legacy-defaults.db"
    _upgrade(monkeypatch, db_path, "023_human_approval_gate")

    # Reproduce the schema defaults emitted by the original 022 and 023.
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA writable_schema = ON")
        for table, column in _COLUMNS:
            old = f"{column} BOOLEAN DEFAULT 0 NOT NULL"
            new = f"{column} BOOLEAN DEFAULT 'false' NOT NULL"
            connection.execute(
                "UPDATE sqlite_master SET sql = replace(sql, ?, ?) "
                "WHERE type = 'table' AND name = ?",
                (old, new, table),
            )
            assert connection.execute("SELECT changes()").fetchone()[0] == 1
        connection.execute("PRAGMA writable_schema = OFF")
        schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
        connection.execute(f"PRAGMA schema_version = {schema_version + 1}")

    _upgrade(monkeypatch, db_path, "044_receipt_resolutions")
    with sqlite3.connect(db_path) as connection:
        assert set(_defaults(connection).values()) == {"'false'"}
        connection.execute(
            "INSERT INTO wallets (wallet_id, wallet_type, owner_key, balance, "
            "lifetime_credits, lifetime_debits, daily_spent, auto_refill, status) "
            "VALUES ('w-legacy', 'agent', '', 0, 0, 0, 0, 0, 'active')"
        )
        connection.execute(
            "INSERT INTO signing_keys (key_id, alg, public_key_b64, status) "
            "VALUES ('k-legacy', 'Ed25519', 'public', 'active')"
        )
        connection.executemany(
            "INSERT INTO permits (permit_id, issuer_wallet_id, subject_wallet_id, "
            "scopes_json, allowed_tools_json, max_credits, expires_at, nonce, "
            "status, signature, key_id) VALUES (?, 'w-legacy', 'w-legacy', "
            "'[]', '[]', 1, '2030-01-01', ?, 'active', 'sig', 'k-legacy')",
            (("p-default", "n-default"), ("p-true", "n-true")),
        )
        connection.executemany(
            "INSERT INTO refresh_tokens (jti, wallet_id, created_at, expires_at) "
            "VALUES (?, 'w-legacy', '2026-01-01', '2026-02-01')",
            (("j-default",), ("j-true",)),
        )
        connection.executemany(
            "INSERT INTO human_approvals (approval_id, wallet_id, permit_id, "
            "tool, idempotency_key, requested_at, expires_at) "
            "VALUES (?, 'w-legacy', ?, 'tool', ?, '2026-01-01', '2026-02-01')",
            (
                ("a-default", "p-default", "idem-default"),
                ("a-true", "p-true", "idem-true"),
            ),
        )
        for table, column, key in (
            ("refresh_tokens", "revoked", "jti"),
            ("human_approvals", "simulated", "approval_id"),
            ("permits", "requires_human_approval", "permit_id"),
        ):
            connection.execute(
                f"UPDATE {table} SET {column} = 'true' WHERE {key} LIKE '%-true'"
            )
            assert connection.execute(
                f"SELECT {column}, typeof({column}) FROM {table} ORDER BY {key}"
            ).fetchall() == [("false", "text"), ("true", "text")]
        indexes = {
            table: {
                row[1:] for row in connection.execute(f"PRAGMA index_list({table})")
            }
            for table, _ in _COLUMNS
        }
        foreign_keys = {
            table: {
                row[2:]
                for row in connection.execute(f"PRAGMA foreign_key_list({table})")
            }
            for table, _ in _COLUMNS
        }

    _upgrade(monkeypatch, db_path, "051_sqlite_boolean_defaults")
    with sqlite3.connect(db_path) as connection:
        assert set(_defaults(connection).values()) == {"0"}
        for table, column, key in (
            ("refresh_tokens", "revoked", "jti"),
            ("human_approvals", "simulated", "approval_id"),
            ("permits", "requires_human_approval", "permit_id"),
        ):
            assert connection.execute(
                f"SELECT {column}, typeof({column}) FROM {table} ORDER BY {key}"
            ).fetchall() == [(0, "integer"), (1, "integer")]
            assert {
                row[1:] for row in connection.execute(f"PRAGMA index_list({table})")
            } == indexes[table]
            assert {
                row[2:]
                for row in connection.execute(f"PRAGMA foreign_key_list({table})")
            } == foreign_keys[table]

    with pytest.raises(
        RuntimeError, match="sqlite_boolean_defaults_downgrade_unsupported"
    ):
        command.downgrade(Config("alembic.ini"), "050_demo_key_first_used")
    asyncio.set_event_loop(asyncio.new_event_loop())
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("051_sqlite_boolean_defaults",)
        assert set(_defaults(connection).values()) == {"0"}
