"""Focused unit tests for app.core.durable_state backend resolution and CRUD.

Covers the module's main public behavior without real external services:
memory fallback, explicit-backend guards, SQLite roundtrip, JSON encoding,
init-failure fallback, and the get/reset helpers. Redis/Postgres paths are
exercised through mocks only, never a live server.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

import pytest

from app.core.config import get_settings
from app.core.durable_state import (
    DurableStateConfigError,
    DurableStateStore,
    _json_default,
    get_durable_state,
    reset_durable_state_for_tests,
)
from app.core.runtime_degradation import (
    get_runtime_degradation,
    reset_runtime_degradation,
)


class _Color(Enum):
    RED = "red"


@dataclass
class _Point:
    x: int
    y: int


class _Model:
    def model_dump(self, mode="json"):
        return {"dumped": True}


@pytest.fixture
def clean_env(monkeypatch):
    """Point settings at throwaway values for each test."""
    monkeypatch.setenv("STATE_BACKEND", "memory")
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("SQLITE_URL", "")
    monkeypatch.setenv("ENVIRONMENT", "local")
    get_settings.cache_clear()
    reset_runtime_degradation()
    yield monkeypatch
    reset_runtime_degradation()
    get_settings.cache_clear()


def _make_store():
    return DurableStateStore()


def test_json_default_serializes_common_types():
    assert _json_default(datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02T03:04:05"
    assert _json_default(date(2026, 1, 2)) == "2026-01-02"
    assert _json_default(Decimal("1.5")) == 1.5
    assert _json_default(_Color.RED) == "red"
    assert sorted(_json_default({1, 2})) == [1, 2]
    assert _json_default(_Point(1, 2)) == {"x": 1, "y": 2}
    assert _json_default(_Model()) == {"dumped": True}
    with pytest.raises(TypeError):
        _json_default(object())


@pytest.mark.asyncio
async def test_memory_backend_crud_is_noop(clean_env):
    store = _make_store()
    await store._ensure_ready()
    assert store.backend == "memory"
    assert store.enabled is False
    assert await store.save_json("k", {"a": 1}) is False
    assert await store.load_json("k") is None
    assert await store.delete("k") is False
    assert await store.list_keys("k") == []
    report = await store.health_report()
    assert report["backend"] == "memory"
    assert report["enabled"] is False
    assert report["ok"] is True


@pytest.mark.asyncio
async def test_explicit_redis_without_url_falls_back_in_nonprod(clean_env):
    clean_env.setenv("STATE_BACKEND", "redis")
    get_settings.cache_clear()
    store = _make_store()
    await store._ensure_ready()
    assert store.backend == "memory"
    assert store.enabled is False
    snap = get_runtime_degradation()
    assert snap["durable_state"]["fell_back_to_memory"] is True
    assert snap["durable_state"]["intended_backend"] == "redis"


@pytest.mark.asyncio
async def test_production_rejects_memory_and_unconfigured_auto(clean_env):
    clean_env.setenv("ENVIRONMENT", "production")
    clean_env.setenv("STATE_BACKEND", "memory")
    get_settings.cache_clear()
    with pytest.raises(DurableStateConfigError):
        await _make_store()._ensure_ready()

    clean_env.setenv("STATE_BACKEND", "auto")
    get_settings.cache_clear()
    with pytest.raises(DurableStateConfigError):
        await _make_store()._ensure_ready()


@pytest.mark.asyncio
async def test_production_rejects_sqlite_even_when_configured(clean_env, tmp_path):
    clean_env.setenv("ENVIRONMENT", "production")
    clean_env.setenv("STATE_BACKEND", "sqlite")
    clean_env.setenv("SQLITE_URL", str(tmp_path / "state.db"))
    get_settings.cache_clear()
    with pytest.raises(DurableStateConfigError):
        await _make_store()._ensure_ready()


@pytest.mark.asyncio
async def test_sqlite_roundtrip_and_prefix_listing(clean_env, tmp_path):
    clean_env.setenv("STATE_BACKEND", "sqlite")
    clean_env.setenv("SQLITE_URL", str(tmp_path / "state.db"))
    get_settings.cache_clear()
    store = _make_store()
    try:
        await store._ensure_ready()
        assert store.backend == "sqlite"
        assert store.enabled is True

        assert await store.save_json("row.a.1", {"n": 1}) is True
        assert await store.save_json("row.a.2", {"n": 2}) is True
        assert await store.save_json("row.b.3", {"n": 3}) is True
        assert await store.load_json("row.a.1") == {"n": 1}
        assert await store.load_json("missing") is None
        assert await store.list_keys("row.a.") == ["row.a.1", "row.a.2"]

        await store.save_json("row.a.1", {"n": 11})
        assert await store.load_json("row.a.1") == {"n": 11}

        assert await store.delete("row.a.2") is True
        assert await store.load_json("row.a.2") is None

        report = await store.health_report()
        assert report == {"ok": True, "backend": "sqlite", "enabled": True}
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sqlite_save_rejects_unserializable_value(clean_env, tmp_path):
    clean_env.setenv("STATE_BACKEND", "sqlite")
    clean_env.setenv("SQLITE_URL", str(tmp_path / "state.db"))
    get_settings.cache_clear()
    store = _make_store()
    try:
        await store._ensure_ready()
        with pytest.raises(TypeError):
            await store.save_json("bad", object())
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_redis_init_failure_falls_back_in_nonprod(clean_env, monkeypatch):
    import app.core.durable_state as durable_state

    clean_env.setenv("STATE_BACKEND", "redis")
    clean_env.setenv("REDIS_URL", "redis://localhost:6379/0")
    get_settings.cache_clear()

    def _boom(*args, **kwargs):
        raise ConnectionError("redis down")

    monkeypatch.setattr(durable_state.redis, "from_url", _boom)
    store = _make_store()
    await store._ensure_ready()
    assert store.backend == "memory"
    assert store.enabled is False
    await store.close()


@pytest.mark.asyncio
async def test_close_resets_store_to_memory(clean_env, tmp_path):
    clean_env.setenv("STATE_BACKEND", "sqlite")
    clean_env.setenv("SQLITE_URL", str(tmp_path / "state.db"))
    get_settings.cache_clear()
    store = _make_store()
    await store._ensure_ready()
    assert store.backend == "sqlite"
    await store.close()
    assert store.backend == "memory"
    assert store.enabled is False


def test_get_and_reset_durable_state_helpers(clean_env):
    first = get_durable_state()
    assert get_durable_state() is first
    reset_durable_state_for_tests()
    assert get_durable_state() is not first
    reset_durable_state_for_tests()
