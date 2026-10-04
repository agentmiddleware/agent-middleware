"""
Database engine and session management for Agent Middleware API.
Provides async PostgreSQL connection via SQLModel/SQLAlchemy.

Supports SQLite fallback for testing when DATABASE_URL is not configured.

Schema boot policy
------------------
Production-like environments never call ``SQLModel.metadata.create_all``.
Schema must come from Alembic (entrypoint ``RUN_MIGRATIONS_ON_START=true``
or an out-of-band ``alembic upgrade head``). Boot verifies required trust
tables and fails closed if they are missing.

``create_all`` is reserved for ephemeral non-production SQLite (local/dev/test).
"""

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncGenerator

from sqlalchemy import event, inspect as sa_inspect
from sqlalchemy.engine import Result
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    create_async_engine,
    async_sessionmaker,
)
from sqlalchemy.sql import Executable
from sqlmodel import SQLModel

from ..core.config import get_settings
from ..core.db_urls import as_sqlalchemy_url
from ..core.time import to_naive_utc
from ..core.trust_mode import is_production_like_environment

logger = logging.getLogger(__name__)

# Trust-plane tables that must exist after Alembic (or a stamped legacy DB).
# alembic_version is intentionally omitted: a DB previously bootstrapped via
# create_all may have trust tables without a stamp; requiring the stamp would
# refuse a healthy service. Before enabling RUN_MIGRATIONS_ON_START, operators
# must compare physical schema and data-migration history and review recovery
# under docs/schema-042-rollout.md; table presence does not prove parity.
REQUIRED_TRUST_TABLES = frozenset(
    {
        "permits",
        "receipts",
        "idempotency_records",
        "mcp_dispatch_attempts",
    }
)
REQUIRED_TRUST_COLUMNS = {
    "mcp_dispatch_attempts": frozenset({"dispatch_claim_hash"}),
    "permits": frozenset(
        {
            "repeat_window_seconds",
            "action_contract_version",
            "action_payload_hash",
            "action_schema_id",
            "action_schema_version",
            "action_public_tool_id",
            "action_upstream_binding_hash",
        }
    ),
    "receipts": frozenset(
        {
            "action_contract_version",
            "action_payload_hash",
            "action_schema_id",
            "action_schema_version",
            "action_public_tool_id",
            "action_upstream_binding_hash",
        }
    ),
}

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


class SchemaInitError(RuntimeError):
    """Raised when production schema is missing or unsafe to auto-create."""


def allows_metadata_create_all(*, dialect_name: str, environment: str) -> bool:
    """Return True only for ephemeral SQLite (tests/local).

    Postgres (any env) and production-like environments must use Alembic,
    unless ``ALLOW_METADATA_CREATE_ALL=true`` is set for the pytest harness
    (ephemeral SQLite only — never for Postgres).
    """
    if not dialect_name.startswith("sqlite"):
        return False
    if os.environ.get("ALLOW_METADATA_CREATE_ALL", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return True
    return not is_production_like_environment(environment)


def _get_database_url() -> str | None:
    """Get database URL from settings or environment (SQLAlchemy async form)."""
    settings = get_settings()
    raw = settings.DATABASE_URL or None
    if not raw:
        raw = os.environ.get("DATABASE_URL")
    if not raw:
        return None
    return as_sqlalchemy_url(raw)


def get_engine() -> AsyncEngine | None:
    """Get or create the async database engine.

    Returns None if DATABASE_URL is not configured.
    """
    global _engine

    if _engine is None:
        db_url = _get_database_url()

        if not db_url:
            logger.warning("DATABASE_URL not configured. Database features disabled.")
            return None

        is_sqlite = db_url.startswith("sqlite")

        engine_kwargs: dict = {
            "echo": get_settings().DEBUG,
            "pool_pre_ping": True,
        }
        if is_sqlite:
            # SQLite doesn't benefit from a large pool (single-writer) and
            # choking on concurrent writes manifests as OperationalError
            # "database is locked". connect_args + WAL mode below solve it
            # for dev/test; production runs on PostgreSQL.
            engine_kwargs["connect_args"] = {"timeout": 30}
        else:
            engine_kwargs["pool_size"] = get_settings().DB_POOL_SIZE
            engine_kwargs["max_overflow"] = get_settings().DB_MAX_OVERFLOW

        _engine = create_async_engine(db_url, **engine_kwargs)

        if is_sqlite:
            # WAL mode permits concurrent reads during a write and smooths
            # over the background-task-writes-while-next-test-runs case.
            # busy_timeout gives writers a second chance instead of
            # raising immediately.
            @event.listens_for(_engine.sync_engine, "connect")
            def _set_sqlite_pragmas(dbapi_conn, _connection_record):  # noqa: ANN001
                cursor = dbapi_conn.cursor()
                try:
                    # Match Postgres: enforce FKs so insert-order bugs surface
                    # in tests (e.g. ledger_entries before wallets).
                    cursor.execute("PRAGMA foreign_keys=ON")
                    cursor.execute("PRAGMA journal_mode=WAL")
                    # Aligned with connect_args timeout above: overloaded CI
                    # runners have been observed exceeding a 10s wait with a
                    # handful of concurrent writers.
                    cursor.execute("PRAGMA busy_timeout=30000")
                    cursor.execute("PRAGMA synchronous=NORMAL")
                finally:
                    cursor.close()

        _install_naive_utc_bind_guard(_engine)

        logger.info(
            "Database engine created: dialect=%s",
            "sqlite" if is_sqlite else "postgres",
        )

    return _engine


def _normalize_bind_parameters(parameters: Any) -> Any:
    """Coerce tz-aware datetimes in a bind-parameter set to naive UTC.

    Returns ``parameters`` unchanged (same object) when there is nothing to
    rewrite, so the overwhelmingly common case costs one scan and no copy.
    """
    if isinstance(parameters, dict):
        rewritten = {
            key: to_naive_utc(value) if isinstance(value, datetime) else value
            for key, value in parameters.items()
        }
        # Identity, not equality: bind values are arbitrary user types whose
        # __eq__ may be expensive or may not return a bool.
        if all(rewritten[key] is value for key, value in parameters.items()):
            return parameters
        return rewritten
    if isinstance(parameters, (tuple, list)):
        rewritten_seq = [
            to_naive_utc(value) if isinstance(value, datetime) else value
            for value in parameters
        ]
        if all(a is b for a, b in zip(rewritten_seq, parameters)):
            return parameters
        return tuple(rewritten_seq) if isinstance(parameters, tuple) else rewritten_seq
    return parameters


def _install_naive_utc_bind_guard(engine: AsyncEngine) -> None:
    """Safety-net: normalize tz-aware datetime binds for PostgreSQL/asyncpg.

    Every datetime column in this schema is ``TIMESTAMP WITHOUT TIME ZONE``
    and ``app.core.time.utc_now`` is the storage convention. The merge-safe
    fix in ``PermitService.create_permit`` (and any future call sites) now
    normalizes *before* signing and persisting, so this guard should never
    fire on the happy path. It remains as a production safety net for any
    overlooked call site that still passes a tz-aware datetime to a bind.

    Scoped to PostgreSQL only: SQLite accepts tz-aware values silently, and
    the explicit normalization at call sites already keeps SQLite and Postgres
    behavior identical. Should a column ever be migrated to
    ``TIMESTAMP WITH TIME ZONE``, this guard must be revisited.
    """
    if engine.dialect.name != "postgresql":
        return

    @event.listens_for(engine.sync_engine, "before_cursor_execute", retval=True)
    def _coerce_naive_utc(  # noqa: ANN001, ANN202
        _conn,
        _cursor,
        statement,
        parameters,
        _context,
        executemany,
    ):
        if executemany and isinstance(parameters, (list, tuple)):
            normalized = [_normalize_bind_parameters(p) for p in parameters]
            if all(a is b for a, b in zip(normalized, parameters)):
                return statement, parameters
            return statement, normalized
        return statement, _normalize_bind_parameters(parameters)


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Get or create the async session factory.

    Raises RuntimeError if database is not configured.
    """
    global _session_factory

    if _session_factory is None:
        engine = get_engine()
        if engine is None:
            raise RuntimeError(
                "DATABASE_URL not configured. "
                "Set STATE_BACKEND=postgres and provide DATABASE_URL."
            )

        _session_factory = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autocommit=False,
            autoflush=False,
        )

    return _session_factory


def is_database_configured() -> bool:
    """Check if database is configured."""
    return get_engine() is not None


def _missing_required_tables(sync_conn) -> set[str]:  # noqa: ANN001
    existing = set(sa_inspect(sync_conn).get_table_names())
    return set(REQUIRED_TRUST_TABLES) - existing


def _missing_required_columns(sync_conn) -> set[str]:  # noqa: ANN001
    inspector = sa_inspect(sync_conn)
    existing_tables = set(inspector.get_table_names())
    missing: set[str] = set()
    for table_name, required_columns in REQUIRED_TRUST_COLUMNS.items():
        if table_name not in existing_tables:
            continue
        existing_columns = {
            str(column["name"]) for column in inspector.get_columns(table_name)
        }
        missing.update(
            f"{table_name}.{column_name}"
            for column_name in required_columns - existing_columns
        )
    return missing


def _stale_alembic_message(sync_conn) -> str | None:  # noqa: ANN001
    """If alembic_version is stamped but behind packaged head, return an error.

    Legacy create_all DBs without a stamp use the separate table/column checks.
    Those boot checks do not establish historical data-migration equivalence.
    """
    inspector = sa_inspect(sync_conn)
    if "alembic_version" not in inspector.get_table_names():
        return None

    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    context = MigrationContext.configure(sync_conn)
    current = set(context.get_current_heads())
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = set(script.get_heads())
    if not current:
        return (
            "alembic_version exists but has no revision. "
            "Stop for manual review of physical schema and data-migration history; "
            "stamp only a proven matching historical revision, then apply required "
            "migrations under docs/schema-042-rollout.md. Table presence is not parity."
        )
    if current != heads:
        return (
            "Database Alembic revision is behind packaged head: "
            f"current={sorted(current)} heads={sorted(heads)}. "
            "Run `alembic upgrade head` (or set RUN_MIGRATIONS_ON_START=true)."
        )
    return None


async def verify_required_schema(engine: AsyncEngine) -> None:
    """Fail closed when Alembic/trust tables are missing or stamp is stale."""
    async with engine.connect() as conn:
        missing = await conn.run_sync(_missing_required_tables)
        missing_columns = await conn.run_sync(_missing_required_columns)
        stale = await conn.run_sync(_stale_alembic_message)
    # A stamped database can be both behind head and missing tables introduced
    # by later revisions. Report the revision mismatch first: it is the
    # actionable root cause and avoids implying that create_all is appropriate.
    if stale:
        raise SchemaInitError(stale)
    if missing:
        raise SchemaInitError(
            "Required database tables missing: "
            + ", ".join(sorted(missing))
            + ". Run `alembic upgrade head` (or set RUN_MIGRATIONS_ON_START=true "
            "on the Docker entrypoint) before starting the API. "
            "Production-like boots do not call create_all."
        )
    if missing_columns:
        raise SchemaInitError(
            "Required database columns missing: "
            + ", ".join(sorted(missing_columns))
            + ". Bring the schema to the packaged Alembic head before starting "
            "the API (normally `alembic upgrade head`; review an unstamped "
            "legacy schema before stamping its exact existing revision)."
        )


async def init_db() -> None:
    """
    Initialize database schema for the current environment.

    - Non-production SQLite: ``create_all`` for ephemeral test/dev DBs.
    - Otherwise (Postgres, or any production-like env): verify Alembic schema;
      never paper over missing migrations with ``create_all``.
    """
    engine = get_engine()
    if engine is None:
        logger.warning("Database not configured, skipping initialization")
        return

    settings = get_settings()
    dialect = engine.dialect.name
    environment = settings.ENVIRONMENT

    if allows_metadata_create_all(dialect_name=dialect, environment=environment):
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
            missing_columns = await conn.run_sync(_missing_required_columns)
        if missing_columns:
            raise SchemaInitError(
                "Required database columns missing after create_all: "
                + ", ".join(sorted(missing_columns))
                + ". create_all cannot alter existing tables; run the packaged "
                "Alembic migration before starting the API."
            )
        logger.info(
            "Database tables initialized via create_all",
            extra={"dialect": dialect, "environment": environment},
        )
        return

    await verify_required_schema(engine)
    logger.info(
        "Database schema verified (Alembic path; create_all skipped)",
        extra={"dialect": dialect, "environment": environment},
    )


async def close_db() -> None:
    """Close the database engine and cleanup connections."""
    global _engine, _session_factory

    if _engine:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("Database engine closed")


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Context manager for database sessions.

    Usage:
        async with get_session() as session:
            result = await session.execute(select(Model))

    Raises RuntimeError if database is not configured.
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


class DatabaseManager:
    """
    Database manager for dependency injection.

    Usage in FastAPI:
        @app.get("/example")
        async def example(db: DatabaseManager):
            async with db.session() as session:
                ...
    """

    def __init__(self):
        self._factory = get_session_factory()

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        """Get a new database session."""
        async with self._factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def execute(self, query: Executable) -> Result[Any]:
        """Execute a query and return results."""
        async with self.session() as session:
            result = await session.execute(query)
            return result
