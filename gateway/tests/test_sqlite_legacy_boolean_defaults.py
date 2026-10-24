"""Legacy SQLite defaults remain safe after upgrading an existing database."""

import asyncio
import sqlite3

from alembic import command
from alembic.config import Config


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

    _upgrade(monkeypatch, db_path, "head")
    with sqlite3.connect(db_path) as connection:
        assert set(_defaults(connection).values()) == {"0"}
        for table, _ in _COLUMNS:
            assert {
                row[1:] for row in connection.execute(f"PRAGMA index_list({table})")
            } == indexes[table]
            assert {
                row[2:]
                for row in connection.execute(f"PRAGMA foreign_key_list({table})")
            } == foreign_keys[table]
