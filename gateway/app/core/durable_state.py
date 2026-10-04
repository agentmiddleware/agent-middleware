"""
Durable state backend abstraction for runtime service stores.

Priority order (STATE_BACKEND=auto):
1) PostgreSQL (DATABASE_URL)
2) Redis (REDIS_URL)
3) SQLite file (SQLITE_URL)
4) In-memory (dev/test only)

Production-like environments refuse silent memory fallback when an explicit
backend is configured or when the resolved backend fails to initialize.
"""

from __future__ import annotations

import aiosqlite
import asyncio
import atexit
import json
import logging
import os
import threading
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from functools import lru_cache
from typing import Any

import asyncpg
import redis.asyncio as redis

from .config import get_settings
from .db_urls import as_asyncpg_url, is_postgres_url, sqlite_path_from_url
from .runtime_degradation import mark_durable_state_fell_back
from .trust_mode import is_production_like_environment

logger = logging.getLogger(__name__)


class DurableStateConfigError(RuntimeError):
    """Raised when durable state cannot be configured safely."""


def _json_default(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)  # type: ignore[arg-type]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, set):
        return list(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


# --- SQLite worker-thread shutdown backstop --------------------------------
# ``aiosqlite.Connection`` composes its worker ``Thread`` without
# ``daemon=True``, and CPython joins non-daemon threads during interpreter
# shutdown. A connection nobody closes therefore wedges the process at exit
# with no traceback and no output — the failure mode that cancelled CI at
# GitHub's six-hour limit before ``close_durable_state`` was wired into the
# test teardown. ``close()`` is still the real cleanup path; this only catches
# consumers that never reach it (a script, a REPL, a worker entrypoint).
#
# Registration goes through ``threading._register_atexit``, NOT ``atexit``.
# Plain ``atexit`` handlers run *after* the non-daemon join, so they can never
# break this particular hang — verified by observation, and the reason the
# obvious implementation does not work. ``threading._register_atexit`` runs
# inside ``threading._shutdown`` before the join; the stdlib's own
# ``concurrent.futures.thread`` relies on it for the same purpose.
_TRACKED_SQLITE_CONNECTIONS: dict[int, tuple[aiosqlite.Connection, int]] = {}
_SQLITE_SHUTDOWN_HOOK_REGISTERED = False


def _release_tracked_sqlite_connections() -> None:
    """Release worker threads for connections nobody closed."""
    current_pid = os.getpid()
    for key, (conn, owner_pid) in list(_TRACKED_SQLITE_CONNECTIONS.items()):
        # Shutdown hooks are inherited across fork(). A child must never close
        # a connection owned by its parent, and must leave the entry in place
        # rather than dropping something it was not entitled to handle.
        if owner_pid != current_pid:
            continue
        logger.warning(
            "Durable-state SQLite connection was not closed; releasing its "
            "worker thread at shutdown. Call close_durable_state() on exit."
        )
        try:
            # ``stop`` rather than ``close``: it is synchronous and tolerates a
            # missing event loop by design, enqueueing its close-and-stop work
            # with a ``None`` future. No loop has to survive until shutdown.
            conn.stop()
        except Exception:
            # Warning, not debug: reaching here means the worker thread is still
            # alive and interpreter exit is about to block on it.
            logger.warning(
                "Failed to stop SQLite worker at shutdown; interpreter exit "
                "may block on it.",
                exc_info=True,
            )
        _TRACKED_SQLITE_CONNECTIONS.pop(key, None)


def _ensure_sqlite_shutdown_hook() -> None:
    """Register the shutdown hook once per process."""
    global _SQLITE_SHUTDOWN_HOOK_REGISTERED
    if _SQLITE_SHUTDOWN_HOOK_REGISTERED:
        return
    register_early = getattr(threading, "_register_atexit", None)
    if register_early is not None:
        register_early(_release_tracked_sqlite_connections)
    else:
        # No known CPython lacks it (3.9+), but degrade rather than crash on
        # import if a future runtime drops the private hook. Say so loudly:
        # plain ``atexit`` runs *after* the non-daemon join, so this fallback
        # cannot actually prevent the hang — it only makes the leak visible.
        logger.warning(
            "threading._register_atexit is unavailable; falling back to atexit, "
            "which runs too late to release a leaked SQLite worker thread. "
            "Call close_durable_state() on shutdown to avoid a hang at exit."
        )
        atexit.register(_release_tracked_sqlite_connections)
    _SQLITE_SHUTDOWN_HOOK_REGISTERED = True


def _track_sqlite_connection(conn: aiosqlite.Connection, owner_pid: int) -> None:
    _ensure_sqlite_shutdown_hook()
    _TRACKED_SQLITE_CONNECTIONS[id(conn)] = (conn, owner_pid)


def _untrack_sqlite_connection(conn: aiosqlite.Connection) -> None:
    _TRACKED_SQLITE_CONNECTIONS.pop(id(conn), None)


class DurableStateStore:
    """Simple key/value JSON state store backed by PostgreSQL or Redis."""

    def __init__(self):
        settings = get_settings()
        self.namespace = settings.STATE_NAMESPACE
        self._state_backend = settings.STATE_BACKEND.strip().lower()
        self._redis_url = settings.REDIS_URL.strip()
        self._database_url = settings.DATABASE_URL.strip()
        self._sqlite_url = settings.SQLITE_URL.strip()
        # A SQLAlchemy SQLite ``DATABASE_URL`` is the standard local-development
        # setting. Without this, auto-resolution below would see a non-empty
        # DATABASE_URL, choose the postgres backend, hand a sqlite DSN to
        # asyncpg, and silently degrade to in-memory state after the failure.
        if not self._sqlite_url:
            self._sqlite_url = sqlite_path_from_url(self._database_url)
        self._production_like = is_production_like_environment(settings.ENVIRONMENT)

        self._init_lock = asyncio.Lock()
        self._initialized = False
        self._backend: str = "memory"
        self._redis: redis.Redis | None = None
        self._pg_pool: asyncpg.Pool | None = None
        self._sqlite_conn: aiosqlite.Connection | None = None
        self._sqlite_owner_pid: int | None = None

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def enabled(self) -> bool:
        return self._backend != "memory"

    def _resolve_backend(self) -> str:
        if self._state_backend in ("postgres", "postgresql"):
            if self._database_url:
                return "postgres"
            return self._missing_explicit_backend("postgres", "DATABASE_URL")
        if self._state_backend == "redis":
            if self._redis_url:
                return "redis"
            return self._missing_explicit_backend("redis", "REDIS_URL")
        if self._state_backend == "sqlite":
            if self._production_like:
                raise DurableStateConfigError(
                    "STATE_BACKEND=sqlite is not allowed in production-like "
                    "environments: the trust plane's money and permit paths "
                    "serialize concurrent writers with SELECT ... FOR UPDATE, "
                    "which SQLAlchemy silently drops on SQLite. Use "
                    "STATE_BACKEND=postgres with DATABASE_URL."
                )
            if self._sqlite_url:
                return "sqlite"
            return self._missing_explicit_backend("sqlite", "SQLITE_URL")
        if self._state_backend == "memory":
            if self._production_like:
                raise DurableStateConfigError(
                    "STATE_BACKEND=memory is not allowed in production-like "
                    "environments"
                )
            return "memory"

        # auto/default
        if self._database_url and is_postgres_url(self._database_url):
            return "postgres"
        if self._redis_url:
            return "redis"
        if self._sqlite_url and not self._production_like:
            return "sqlite"
        if self._production_like:
            # SQLITE_URL is deliberately not a production fallback here: see the
            # explicit STATE_BACKEND=sqlite branch above for why row locks make
            # SQLite unsafe for the money and permit paths.
            raise DurableStateConfigError(
                "Production-like environments require DATABASE_URL or REDIS_URL "
                "for durable state (SQLite is not a supported production "
                "backend)"
            )
        return "memory"

    def _missing_explicit_backend(self, intended: str, missing_var: str) -> str:
        message = f"STATE_BACKEND={intended} requires {missing_var} to be set"
        if self._production_like:
            raise DurableStateConfigError(message)
        logger.warning("%s; falling back to in-memory for non-production.", message)
        mark_durable_state_fell_back(intended)
        return "memory"

    def _register_sqlite_shutdown_backstop(self) -> None:
        """Track this connection so interpreter shutdown cannot hang on it."""
        conn = self._sqlite_conn
        if conn is None:
            return
        self._sqlite_owner_pid = os.getpid()
        _track_sqlite_connection(conn, self._sqlite_owner_pid)

    def _unregister_sqlite_shutdown_backstop(self) -> None:
        """Stop tracking once the connection is closed through the normal path."""
        if self._sqlite_conn is not None:
            _untrack_sqlite_connection(self._sqlite_conn)
        self._sqlite_owner_pid = None

    async def _ensure_ready(self) -> None:
        if self._initialized:
            return

        async with self._init_lock:
            if self._initialized:
                return

            self._backend = self._resolve_backend()

            try:
                if self._backend == "postgres":
                    pg_url = as_asyncpg_url(self._database_url)
                    self._pg_pool = await asyncpg.create_pool(
                        pg_url,
                        min_size=1,
                        max_size=5,
                        timeout=10,
                    )
                    async with self._pg_pool.acquire() as conn:
                        await conn.execute(
                            """
                            CREATE TABLE IF NOT EXISTS app_state_kv (
                                namespace TEXT NOT NULL,
                                state_key TEXT NOT NULL,
                                payload JSONB NOT NULL,
                                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                                PRIMARY KEY(namespace, state_key)
                            )
                            """
                        )
                elif self._backend == "redis":
                    self._redis = redis.from_url(
                        self._redis_url,
                        encoding="utf-8",
                        decode_responses=True,
                    )
                    await self._redis.ping()
                elif self._backend == "sqlite":
                    self._sqlite_conn = await aiosqlite.connect(self._sqlite_url)
                    self._register_sqlite_shutdown_backstop()
                    self._sqlite_conn.row_factory = aiosqlite.Row
                    await self._sqlite_conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS app_state_kv (
                            namespace TEXT NOT NULL,
                            state_key TEXT NOT NULL,
                            payload TEXT NOT NULL,
                            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                            PRIMARY KEY(namespace, state_key)
                        )
                        """
                    )
                    await self._sqlite_conn.commit()
            except DurableStateConfigError:
                raise
            except Exception as exc:
                intended = self._backend
                logger.exception(
                    "Failed to initialize durable state backend '%s'",
                    intended,
                )
                mark_durable_state_fell_back(intended)
                await self.close()
                if self._production_like:
                    raise DurableStateConfigError(
                        f"Durable state backend '{intended}' failed to initialize "
                        f"in production-like environment: {exc}"
                    ) from exc
                logger.warning(
                    "Falling back to in-memory durable state after '%s' failure "
                    "(non-production).",
                    intended,
                )
                self._backend = "memory"

            self._initialized = True

    def _redis_key(self, key: str) -> str:
        return f"{self.namespace}:{key}"

    async def load_json(self, key: str) -> Any | None:
        await self._ensure_ready()
        if self._backend == "memory":
            return None

        if self._backend == "postgres":
            assert self._pg_pool is not None
            async with self._pg_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT payload FROM app_state_kv WHERE namespace = $1 "
                    "AND state_key = $2",
                    self.namespace,
                    key,
                )
            if not row:
                return None
            payload = row["payload"]
            if isinstance(payload, str):
                return json.loads(payload)
            return payload

        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            async with self._sqlite_conn.execute(
                "SELECT payload FROM app_state_kv WHERE namespace = ? AND state_key = ?",
                (self.namespace, key),
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                return None
            return json.loads(row["payload"])

        assert self._redis is not None
        raw = await self._redis.get(self._redis_key(key))
        if raw is None:
            return None
        return json.loads(raw)

    async def save_json(self, key: str, value: Any) -> bool:
        await self._ensure_ready()
        if self._backend == "memory":
            return False

        encoded = json.dumps(value, default=_json_default)

        if self._backend == "postgres":
            assert self._pg_pool is not None
            async with self._pg_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO app_state_kv (namespace, state_key, payload, updated_at)
                    VALUES ($1, $2, $3::jsonb, NOW())
                    ON CONFLICT (namespace, state_key)
                    DO UPDATE SET payload = EXCLUDED.payload, updated_at = NOW()
                    """,
                    self.namespace,
                    key,
                    encoded,
                )
            return True

        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            await self._sqlite_conn.execute(
                """
                INSERT INTO app_state_kv (namespace, state_key, payload, updated_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(namespace, state_key)
                DO UPDATE SET payload = excluded.payload, updated_at = datetime('now')
                """,
                (self.namespace, key, encoded),
            )
            await self._sqlite_conn.commit()
            return True

        assert self._redis is not None
        await self._redis.set(self._redis_key(key), encoded)
        return True

    async def delete(self, key: str) -> bool:
        await self._ensure_ready()
        if self._backend == "memory":
            return False

        if self._backend == "postgres":
            assert self._pg_pool is not None
            async with self._pg_pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM app_state_kv WHERE namespace = $1 AND state_key = $2",
                    self.namespace,
                    key,
                )
            return True

        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            await self._sqlite_conn.execute(
                "DELETE FROM app_state_kv WHERE namespace = ? AND state_key = ?",
                (self.namespace, key),
            )
            await self._sqlite_conn.commit()
            return True

        assert self._redis is not None
        await self._redis.delete(self._redis_key(key))
        return True

    async def list_keys(self, prefix: str = "") -> list[str]:
        """List durable state keys by prefix for row-keyed service state."""
        await self._ensure_ready()
        if self._backend == "memory":
            return []

        if self._backend == "postgres":
            assert self._pg_pool is not None
            async with self._pg_pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT state_key
                    FROM app_state_kv
                    WHERE namespace = $1 AND state_key LIKE $2
                    ORDER BY state_key
                    """,
                    self.namespace,
                    f"{prefix}%",
                )
            return [row["state_key"] for row in rows]

        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            async with self._sqlite_conn.execute(
                """
                SELECT state_key
                FROM app_state_kv
                WHERE namespace = ? AND state_key LIKE ?
                ORDER BY state_key
                """,
                (self.namespace, f"{prefix}%"),
            ) as cursor:
                rows = await cursor.fetchall()
            return [row["state_key"] for row in rows]

        assert self._redis is not None
        keys: list[str] = []
        redis_prefix = self._redis_key(prefix)
        async for key in self._redis.scan_iter(match=f"{redis_prefix}*"):
            if key.startswith(f"{self.namespace}:"):
                keys.append(key.split(":", 1)[1])
        return sorted(keys)

    async def health_report(self) -> dict[str, Any]:
        await self._ensure_ready()

        if self._backend == "memory":
            return {
                "ok": not self._production_like,
                "backend": "memory",
                "enabled": False,
                "reason": "No DATABASE_URL/REDIS_URL/SQLITE_URL configured "
                "or durable backend unavailable",
            }

        if self._backend == "postgres":
            try:
                assert self._pg_pool is not None
                async with self._pg_pool.acquire() as conn:
                    await conn.fetchval("SELECT 1")
                return {"ok": True, "backend": "postgres", "enabled": True}
            except Exception as exc:
                return {
                    "ok": False,
                    "backend": "postgres",
                    "enabled": True,
                    "error": str(exc),
                }

        if self._backend == "sqlite":
            try:
                assert self._sqlite_conn is not None
                await self._sqlite_conn.execute("SELECT 1")
                return {"ok": True, "backend": "sqlite", "enabled": True}
            except Exception as exc:
                return {
                    "ok": False,
                    "backend": "sqlite",
                    "enabled": True,
                    "error": str(exc),
                }

        try:
            assert self._redis is not None
            await self._redis.ping()
            return {"ok": True, "backend": "redis", "enabled": True}
        except Exception as exc:
            return {
                "ok": False,
                "backend": "redis",
                "enabled": True,
                "error": str(exc),
            }

    async def close(self) -> None:
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                logger.debug("Failed to close Redis client cleanly", exc_info=True)
            self._redis = None

        if self._pg_pool is not None:
            try:
                await self._pg_pool.close()
            except Exception:
                logger.debug("Failed to close Postgres pool cleanly", exc_info=True)
            self._pg_pool = None

        if self._sqlite_conn is not None and self._sqlite_owner_pid not in (
            None,
            os.getpid(),
        ):
            # Inherited across fork(). The connection object came with us but its
            # worker thread did not -- fork duplicates only the calling thread --
            # so ``await conn.close()`` would block forever on a future nothing
            # can resolve. The parent still owns the real connection, so the
            # child must not touch it: drop the local references and stop.
            # The registry entry is left alone; the shutdown hook already skips
            # connections owned by another process.
            logger.debug(
                "Discarding a SQLite connection inherited from pid %s; "
                "the owning process is responsible for closing it.",
                self._sqlite_owner_pid,
            )
            self._sqlite_conn = None
            self._sqlite_owner_pid = None
        elif self._sqlite_conn is not None:
            conn = self._sqlite_conn
            released = True
            try:
                await conn.close()
            except Exception:
                logger.debug("Failed to close SQLite connection cleanly", exc_info=True)
                # ``Connection.close`` stops the worker from a finally-block, so
                # today it is released even on this path. Do not depend on that:
                # it is an aiosqlite internal, and a leaked non-daemon worker
                # blocks interpreter exit with no traceback. ``stop`` is
                # idempotent enough to call after a close that already ran it.
                try:
                    conn.stop()
                except Exception:
                    logger.warning(
                        "SQLite worker thread could not be stopped after a "
                        "failed close; leaving it tracked so the shutdown hook "
                        "can try again before interpreter exit.",
                        exc_info=True,
                    )
                    released = False
            if released:
                # Untrack only once the worker is actually released. Keeping a
                # dead entry would make the shutdown hook warn about a leak that
                # did not happen; dropping a live one would remove the backstop
                # from the single case that still needs it.
                self._unregister_sqlite_shutdown_backstop()
            self._sqlite_conn = None

        self._initialized = False
        self._backend = "memory"


@lru_cache()
def get_durable_state() -> DurableStateStore:
    return DurableStateStore()


def reset_durable_state_for_tests() -> None:
    """Drop the cached store so the next get_durable_state() rebuilds."""
    get_durable_state.cache_clear()


async def close_durable_state() -> None:
    await get_durable_state().close()
    reset_durable_state_for_tests()
