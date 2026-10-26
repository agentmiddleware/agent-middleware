"""SQLite boolean columns created as false must stay false.

refresh_tokens.revoked, permits.requires_human_approval, and
human_approvals.simulated are read through SQLAlchemy. On SQLite the text
value 'false' is read as true. These tests cover a fresh upgrade, where an
omitted column must store an integer false, and an upgrade through 024,
where text 'false' becomes false and text 'true' stays true.
"""

from __future__ import annotations

import asyncio
import sqlite3

from alembic import command
from alembic.config import Config
from sqlalchemy import Boolean, Column, MetaData, String, Table, create_engine, select


def _upgrade(monkeypatch, db_path, revision: str) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    command.upgrade(Config("alembic.ini"), revision)
    asyncio.set_event_loop(asyncio.new_event_loop())


def _seed_wallet_and_key(db_path) -> None:
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        INSERT INTO wallets (
            wallet_id, wallet_type, owner_key, balance, lifetime_credits,
            lifetime_debits, daily_spent, auto_refill, status
        ) VALUES ('w-bool', 'agent', '', 0, 0, 0, 0, 0, 'active')
        """
    )
    connection.execute(
        """
        INSERT INTO signing_keys (key_id, alg, public_key_b64, status)
        VALUES ('k-bool', 'Ed25519', 'public', 'active')
        """
    )
    connection.commit()
    connection.close()


def _insert_permit(db_path, permit_id: str, *, approval_sql: str | None) -> None:
    approval_column = ""
    approval_value = ""
    if approval_sql is not None:
        approval_column = ", requires_human_approval"
        approval_value = f", {approval_sql}"
    connection = sqlite3.connect(db_path)
    connection.execute(
        f"""
        INSERT INTO permits (
            permit_id, issuer_wallet_id, subject_wallet_id, scopes_json,
            allowed_tools_json, max_credits, expires_at, nonce, status,
            signature, key_id, issued_at{approval_column}
        ) VALUES (
            ?, 'w-bool', 'w-bool', '[]', '[]', 10, '2030-01-01 00:00:00',
            ?, 'active', 'sig', 'k-bool', '2026-01-01 00:00:00'{approval_value}
        )
        """,
        (permit_id, f"nonce-{permit_id}"),
    )
    connection.commit()
    connection.close()


def _read_bool(db_path, table: str, column: str, key_column: str, key: str):
    engine = create_engine(f"sqlite:///{db_path}")
    reflected = Table(
        table,
        MetaData(),
        Column(key_column, String, primary_key=True),
        Column(column, Boolean),
    )
    with engine.connect() as connection:
        value = connection.execute(
            select(reflected.c[column]).where(reflected.c[key_column] == key)
        ).scalar_one()
    engine.dispose()
    return value


def _typeof(db_path, table: str, column: str, key_column: str, key: str) -> str:
    connection = sqlite3.connect(db_path)
    row = connection.execute(
        f"SELECT typeof({column}) FROM {table} WHERE {key_column} = ?",
        (key,),
    ).fetchone()
    connection.close()
    return row[0]


def test_024_repairs_every_text_false_boolean(tmp_path, monkeypatch):
    """Text 'false' on the three string-default booleans must become false."""
    db_path = tmp_path / "boolean-repair.db"
    _upgrade(monkeypatch, db_path, "023_human_approval_gate")
    _seed_wallet_and_key(db_path)
    _insert_permit(db_path, "p-text", approval_sql="'false'")
    _insert_permit(db_path, "p-true", approval_sql="'true'")
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        INSERT INTO refresh_tokens (jti, wallet_id, revoked, created_at, expires_at)
        VALUES ('j-text', 'w-bool', 'false', '2026-01-01', '2026-02-01')
        """
    )
    connection.execute(
        """
        INSERT INTO refresh_tokens (jti, wallet_id, revoked, created_at, expires_at)
        VALUES ('j-true', 'w-bool', 'true', '2026-01-01', '2026-02-01')
        """
    )
    connection.execute(
        """
        INSERT INTO human_approvals (
            approval_id, wallet_id, permit_id, tool, idempotency_key,
            simulated, requested_at, expires_at
        ) VALUES (
            'a-text', 'w-bool', 'p-text', 'tool', 'idem-text',
            'false', '2026-01-01', '2026-02-01'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO human_approvals (
            approval_id, wallet_id, permit_id, tool, idempotency_key,
            simulated, requested_at, expires_at
        ) VALUES (
            'a-true', 'w-bool', 'p-true', 'tool', 'idem-true',
            'true', '2026-01-01', '2026-02-01'
        )
        """
    )
    connection.commit()
    connection.close()

    assert (
        _read_bool(db_path, "permits", "requires_human_approval", "permit_id", "p-text")
        is True
    )
    assert _read_bool(db_path, "refresh_tokens", "revoked", "jti", "j-text") is True
    assert (
        _read_bool(db_path, "human_approvals", "simulated", "approval_id", "a-text")
        is True
    )

    _upgrade(monkeypatch, db_path, "024_human_approval_hardening")

    assert (
        _read_bool(db_path, "permits", "requires_human_approval", "permit_id", "p-text")
        is False
    )
    assert _read_bool(db_path, "refresh_tokens", "revoked", "jti", "j-text") is False
    assert (
        _read_bool(db_path, "human_approvals", "simulated", "approval_id", "a-text")
        is False
    )
    assert (
        _read_bool(db_path, "permits", "requires_human_approval", "permit_id", "p-true")
        is True
    )
    assert _read_bool(db_path, "refresh_tokens", "revoked", "jti", "j-true") is True
    assert (
        _read_bool(db_path, "human_approvals", "simulated", "approval_id", "a-true")
        is True
    )
    assert (
        _typeof(db_path, "permits", "requires_human_approval", "permit_id", "p-text")
        == "integer"
    )
    assert _typeof(db_path, "refresh_tokens", "revoked", "jti", "j-text") == "integer"
    assert (
        _typeof(db_path, "human_approvals", "simulated", "approval_id", "a-text")
        == "integer"
    )


def test_head_omitted_boolean_defaults_read_as_false(tmp_path, monkeypatch):
    """An omitted boolean must stay false after later migrations."""
    db_path = tmp_path / "boolean-default.db"
    _upgrade(monkeypatch, db_path, "head")
    _seed_wallet_and_key(db_path)
    _insert_permit(db_path, "p-default", approval_sql=None)
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        INSERT INTO refresh_tokens (jti, wallet_id, created_at, expires_at)
        VALUES ('j-default', 'w-bool', '2026-01-01', '2026-02-01')
        """
    )
    connection.execute(
        """
        INSERT INTO human_approvals (
            approval_id, wallet_id, permit_id, tool, idempotency_key,
            requested_at, expires_at
        ) VALUES (
            'a-default', 'w-bool', 'p-default', 'tool', 'idem-default',
            '2026-01-01', '2026-02-01'
        )
        """
    )
    connection.commit()
    connection.close()

    reads = {
        "requires_human_approval": _read_bool(
            db_path, "permits", "requires_human_approval", "permit_id", "p-default"
        ),
        "allow_identical_repeats": _read_bool(
            db_path, "permits", "allow_identical_repeats", "permit_id", "p-default"
        ),
        "revoked": _read_bool(db_path, "refresh_tokens", "revoked", "jti", "j-default"),
        "simulated": _read_bool(
            db_path, "human_approvals", "simulated", "approval_id", "a-default"
        ),
    }
    assert reads == {
        "requires_human_approval": False,
        "allow_identical_repeats": False,
        "revoked": False,
        "simulated": False,
    }
    assert (
        _typeof(db_path, "permits", "requires_human_approval", "permit_id", "p-default")
        == "integer"
    )
    assert (
        _typeof(db_path, "refresh_tokens", "revoked", "jti", "j-default") == "integer"
    )
    assert (
        _typeof(db_path, "human_approvals", "simulated", "approval_id", "a-default")
        == "integer"
    )
