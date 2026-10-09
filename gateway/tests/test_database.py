"""Session, engine and bind-parameter behavior of app.db.database.

Schema boot (init_db, verify_required_schema, allows_metadata_create_all) is
covered by tests/test_schema_boot.py. This file covers the rest of the
module's public surface: engine/session lifecycle, the commit/rollback
contract of get_session and DatabaseManager, and bind-parameter
normalization.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.database as database
from app.db.database import (
    DatabaseManager,
    _install_naive_utc_bind_guard,
    _normalize_bind_parameters,
    close_db,
    get_engine,
    get_session,
    get_session_factory,
    is_database_configured,
)


def _aware(year: int = 2026) -> datetime:
    return datetime(year, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def test_normalize_dict_rewrites_aware_datetimes_to_naive_utc():
    params = {"when": _aware(), "name": "x"}
    out = _normalize_bind_parameters(params)
    assert out is not params
    assert out["when"] == datetime(2026, 1, 2, 3, 4, 5)
    assert out["when"].tzinfo is None
    assert out["name"] == "x"


def test_normalize_dict_returns_same_object_when_nothing_to_rewrite():
    params = {"a": 1, "b": "x", "when": datetime(2026, 1, 2, 3, 4, 5)}
    assert _normalize_bind_parameters(params) is params


def test_normalize_sequence_keeps_type_and_identity_when_clean():
    params = [_aware(), "x"]
    out = _normalize_bind_parameters(params)
    assert isinstance(out, list)
    assert out[0] == datetime(2026, 1, 2, 3, 4, 5)

    clean_list = [1, "x"]
    assert _normalize_bind_parameters(clean_list) is clean_list
    clean_tuple = (1, "x")
    assert _normalize_bind_parameters(clean_tuple) is clean_tuple


def test_normalize_tuple_with_aware_datetime_becomes_tuple():
    out = _normalize_bind_parameters((_aware(), 1))
    assert isinstance(out, tuple)
    assert out[0] == datetime(2026, 1, 2, 3, 4, 5)


def test_normalize_non_mapping_passthrough():
    assert _normalize_bind_parameters(None) is None
    assert _normalize_bind_parameters("select 1") == "select 1"


async def test_install_bind_guard_is_noop_for_sqlite():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        _install_naive_utc_bind_guard(engine)
    finally:
        await engine.dispose()


async def _make_probe_factory(tmp_path, monkeypatch):
    db_path = tmp_path / "probe.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE probe (id INTEGER PRIMARY KEY, v TEXT)"))
    monkeypatch.setattr(database, "_engine", engine)
    monkeypatch.setattr(
        database,
        "_session_factory",
        async_sessionmaker(
            engine,
            class_=database.AsyncSession,
            expire_on_commit=False,
            autocommit=False,
            autoflush=False,
        ),
    )
    return engine


async def test_get_session_commits_on_success(tmp_path, monkeypatch):
    await _make_probe_factory(tmp_path, monkeypatch)
    async with get_session() as session:
        await session.execute(text("INSERT INTO probe (v) VALUES ('a')"))

    async with get_session() as session:
        rows = (await session.execute(text("SELECT v FROM probe"))).all()
    assert [row[0] for row in rows] == ["a"]


async def test_get_session_rolls_back_on_error(tmp_path, monkeypatch):
    await _make_probe_factory(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="boom"):
        async with get_session() as session:
            await session.execute(text("INSERT INTO probe (v) VALUES ('b')"))
            raise RuntimeError("boom")

    async with get_session() as session:
        rows = (await session.execute(text("SELECT v FROM probe"))).all()
    assert rows == []


async def test_database_manager_session_rolls_back_on_error(tmp_path, monkeypatch):
    await _make_probe_factory(tmp_path, monkeypatch)
    manager = DatabaseManager()
    with pytest.raises(RuntimeError, match="boom"):
        async with manager.session() as session:
            await session.execute(text("INSERT INTO probe (v) VALUES ('c')"))
            raise RuntimeError("boom")

    async with manager.session() as session:
        rows = (await session.execute(text("SELECT v FROM probe"))).all()
    assert rows == []


async def test_database_manager_execute_returns_result(tmp_path, monkeypatch):
    await _make_probe_factory(tmp_path, monkeypatch)
    manager = DatabaseManager()
    result = await manager.execute(text("SELECT 1"))
    assert result.scalar() == 1


async def test_unconfigured_database_returns_none_and_factory_raises(monkeypatch):
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_session_factory", None)
    monkeypatch.setattr(
        database, "get_settings", lambda: SimpleNamespace(DATABASE_URL=None)
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert get_engine() is None
    assert is_database_configured() is False
    with pytest.raises(RuntimeError, match="DATABASE_URL not configured"):
        get_session_factory()


async def test_is_database_configured_true_when_engine_present(tmp_path, monkeypatch):
    await _make_probe_factory(tmp_path, monkeypatch)
    assert is_database_configured() is True


async def test_close_db_clears_globals(tmp_path, monkeypatch):
    engine = await _make_probe_factory(tmp_path, monkeypatch)
    assert database._engine is not None
    await close_db()
    assert database._engine is None
    assert database._session_factory is None
    await engine.dispose()
