"""
Shared test configuration.
Sets up pytest-asyncio, database, and common fixtures.
"""

import os
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import DateTime, event, inspect as sa_inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import Session

# --- Proof-surface test classification -------------------------------------
# The product is the trust plane (app/trust + the core trust routers). Everything
# else is a "proof surface" — example workloads that exercise the spine but are
# NOT the product (see README "Core platform vs. example workloads"). Tests for
# these are auto-marked `proof` so the inner dev loop can skip them:
#     make test       -> fast, core only  (pytest -m "not proof")
#     make test-all   -> full suite       (what CI runs)
# Conservative by design: when a test's surface is ambiguous, leave it OUT of
# this set so it stays in the fast (core) loop and is never silently skipped.
PROOF_SURFACE_TEST_MODULES = frozenset(
    {
        "test_awi",
        "test_awi_adoption",
        "test_awi_dom_bridge_integration",
        "test_awi_governed_mcp",
        "test_awi_http_governance",
        "test_awi_phase9",
        "test_behavioral_sandbox",
        "test_broadcast",
        "test_comms",
        "test_agent_comms_durable",
        "test_content_factory_generation_durable",
        "test_content_store",
        "test_discover_phase9",
        "test_discovery",
        "test_discovery_consistency",
        "test_discovery_drift",
        "test_discovery_honesty",
        "test_factory",
        "test_iot",
        "test_iot_registry",
        "test_media",
        "test_oracle",
        "test_oracle_durable_crawl",
        "test_oracle_store",
        "test_protocol",
        "test_red_team",
        "test_rtaas",
        "test_sandbox",
        "test_sandbox_integration",
        "test_telemetry",
        "test_telemetry_scope",
        "test_telemetry_store",
    }
)


# Tests that exercise dormant trust surfaces — routers and billing expansion
# routes that production (ENABLE_PROOF_SURFACES=false) does not mount: JWT
# auth, KYC, planner, Stripe webhooks, child/swarm wallets, transfers,
# top-ups, marketplace, velocity status, and the dry-run sandbox. These are
# real trust features (not proof scaffolding), so they stay in the fast core
# loop; the autouse fixture below mounts the dormant routes for them without
# flipping ENABLE_PROOF_SURFACES.
DORMANT_SURFACE_TEST_MODULES = frozenset(
    {
        "test_acp_bridge",
        "test_api_keys",
        "test_audit_routes",
        "test_billing",
        "test_discovery_drift",
        "test_golden_path",
        "test_kyc",
        "test_mcp_trust_mode",
        "test_mcp_upstream_governed",
        "test_owner_enforcement",
        "test_planner_constraints",
        "test_pods",
        "test_policy_bundles",
        "test_qa_money_auth_matrix",
        "test_revocation_containment",
        "test_secret_persistence",
        "test_stripe_integration",
        "test_swarm_wallets",
        "test_tenant_isolation_hardening",
        "test_transfers",
        "test_trust_package",
        "test_velocity_monitor",
        "test_wallet_ledger_integrity",
        "test_wallet_status_enforcement",
        "test_x402",
    }
)


def pytest_collection_modifyitems(config, items):
    """Auto-apply the `proof` / `dormant` markers by test module."""
    proof = pytest.mark.proof
    dormant = pytest.mark.dormant
    for item in items:
        stem = Path(item.nodeid.split("::", 1)[0]).stem
        if stem in PROOF_SURFACE_TEST_MODULES:
            item.add_marker(proof)
        if stem in DORMANT_SURFACE_TEST_MODULES:
            item.add_marker(dormant)


def iter_routes(routes):
    """Yield every leaf route from an app/router route list.

    Newer FastAPI versions wrap `app.include_router()` results in an
    `_IncludedRouter` that has no `.path` of its own. Walk into its
    `original_router.routes` so callers see the real `APIRoute` instances.
    """
    for route in routes:
        if hasattr(route, "path"):
            yield route
            continue
        original = getattr(route, "original_router", None)
        if original is not None and hasattr(original, "routes"):
            yield from iter_routes(original.routes)


# Set up SQLite for testing before any imports
# One SQLite file per pytest-xdist worker so parallel runs do not share a
# lock. Workers inherit the controller's environment, so the default the
# controller set is replaced here rather than kept by setdefault.
_DEFAULT_TEST_DB_URL = "sqlite+aiosqlite:///./test.db"
_XDIST_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "")
TEST_DB_FILE = f"test_{_XDIST_WORKER}.db" if _XDIST_WORKER else "test.db"
os.environ.setdefault("DATABASE_URL", _DEFAULT_TEST_DB_URL)
if _XDIST_WORKER and os.environ["DATABASE_URL"] == _DEFAULT_TEST_DB_URL:
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///./{TEST_DB_FILE}"


def require_disposable_test_database() -> None:
    """Refuse inherited databases before test setup or destructive cleanup."""
    url = os.environ.get("DATABASE_URL", "")
    if url == f"sqlite+aiosqlite:///./{TEST_DB_FILE}":
        return
    if (
        os.environ.get("AMW_TEST_DB_DISPOSABLE") == "1"
        and os.environ.get("ENVIRONMENT") == "test"
    ):
        try:
            marked_url = make_url(url)
        except (ArgumentError, ValueError):
            pass
        else:
            if (
                marked_url.drivername == "postgresql+asyncpg"
                and marked_url.host
                and marked_url.database
            ):
                return
    if (
        os.environ.get("RUN_POSTGRES_CONCURRENCY_TESTS") == "1"
        or os.environ.get("RUN_MCP_MULTIPROCESS_TESTS") == "1"
    ):
        from tests.support.action_database_guard import (
            require_action_database_url,
            require_action_test_environment,
        )

        require_action_test_environment(os.environ.get("ENVIRONMENT"))
        require_action_database_url(url)
        return
    # An inherited URL may contain credentials; never include it in the error.
    raise ValueError("unsafe_test_database_url")


require_disposable_test_database()
# Durable state stays in memory for the suite. State this explicitly: the tests
# always ran on memory state, but only as an accident of auto-resolution
# choosing postgres and asyncpg then failing on the SQLite DSN above. Once a
# SQLite DATABASE_URL resolves to the SQLite backend, "auto" opens a real
# aiosqlite connection whose worker thread is not a daemon, and an unclosed one
# blocks interpreter shutdown. See the teardown in setup_database.
os.environ.setdefault("STATE_BACKEND", "memory")
# Ephemeral SQLite create_all even when a job sets ENVIRONMENT=production
# (production_trust CI). Never applies to Postgres — see allows_metadata_create_all.
os.environ.setdefault("ALLOW_METADATA_CREATE_ALL", "true")
# Match the application default. Proof-marked tests opt in through the fixture
# below, so importing the app in an ordinary test never silently mounts demos.
os.environ.setdefault("ENABLE_PROOF_SURFACES", "false")
# Configure auth so tests don't hit the fail-safe 503 in verify_api_key.
# Tests authenticate with X-API-Key: test-key (see RateLimitMiddleware, which
# also special-cases this value to bypass rate limiting).
os.environ.setdefault("VALID_API_KEYS", "test-key")
# Production now defaults to strict trust mode (TRUST_MODE_ENABLED=true,
# ALLOW_LEGACY_UNPERMITTED_MCP=false). Many tests predate that flip and
# exercise MCP paths without supplying permits; opt the test suite back into
# legacy/permissive behavior unless an individual test explicitly monkeypatches
# strict mode (see test_mcp_trust_mode.py and test_trust_negative_security.py
# for examples that flip back to strict at the test boundary).
os.environ.setdefault("TRUST_MODE_ENABLED", "false")
os.environ.setdefault("ALLOW_LEGACY_UNPERMITTED_MCP", "true")
# Stop the suite from self-throttling. RateLimitMiddleware exempts only the
# literal "test-key"; unauthenticated requests all share the "anonymous"
# bucket and provisioned agent keys each get their own, so a fast full-suite
# run can cross the 120/min default and return 429 where a test expects
# 401/403 (observed as a flaky `assert 429 == 401` in
# test_tenant_isolation_hardening). Raising the limit for tests removes the
# cross-test coupling without changing production behavior; the one test that
# exercises limiting (test_runtime_degradation) builds its own middleware with
# an explicit per-instance limit and is unaffected.
os.environ.setdefault("RATE_LIMIT_PER_MINUTE", "1000000")


def interleaving_factory(real_factory, hook, state, *, fire_on: int = 1):
    """Wrap a session factory so `hook` runs once, mid-transaction.

    Fires after ``fire_on`` statements have been issued on the wrapped session,
    which places it after the value under test has been read and before it is
    written back.

    ``fire_on`` counts statements that reach ``Session.execute``. ``session.get``
    does **not**: it is delegated straight to the inner session, so a row loaded
    that way is invisible here. Choosing a seam before such a load lets the
    service re-read the row *after* the concurrent write lands, and the race
    never happens -- a test that does that passes against the unfixed service
    while ``state["fired"]`` reads true throughout. Always confirm a new seam by
    reverting the service and watching the test fail.
    """

    class _Session:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        async def execute(self, *args, **kwargs):
            state["executes"] = state.get("executes", 0) + 1
            n = state["executes"]
            # Fire *after* the statement returns, so the hook lands once the
            # value has been read but before anything is written back. Firing
            # beforehand would place the concurrent writer ahead of the read,
            # which is not the race. Counting completed reads also keeps the
            # seam identical across the fixed and unfixed code, which differ in
            # how many statements they issue.
            result = await self._inner.execute(*args, **kwargs)
            if n == fire_on and not state.get("fired"):
                state["fired"] = True
                await hook()
            return result

    class _CM:
        def __init__(self, cm):
            self._cm = cm

        async def __aenter__(self):
            return _Session(await self._cm.__aenter__())

        async def __aexit__(self, *exc):
            return await self._cm.__aexit__(*exc)

    return lambda: lambda: _CM(real_factory())


def running_on_sqlite() -> bool:
    """True when this run's ``DATABASE_URL`` names SQLite.

    ``DATABASE_URL`` above is a ``setdefault``, so exporting one before pytest
    points the whole suite at another engine. Tests that force a concurrent
    writer into the middle of an open transaction need to know: on SQLite the
    surrounding ``SELECT ... FOR UPDATE`` is a silent no-op and the second
    writer proceeds, which is exactly the condition those tests exist to
    exercise. On PostgreSQL the lock is real, so the second writer waits for a
    transaction that cannot commit until the second writer returns, and the
    test hangs rather than fails.
    """
    from app.core.db_urls import is_sqlite_url

    return is_sqlite_url(os.environ.get("DATABASE_URL", ""))


#: Guard for tests whose interleave depends on the row lock being a no-op.
#: They are skipped, not adapted: their subject is what happens when nothing
#: serializes the writers, which is not a state PostgreSQL can be put into.
requires_sqlite_row_lock_noop = pytest.mark.skipif(
    not running_on_sqlite(),
    reason=(
        "forces a concurrent writer inside an open transaction; needs the "
        "SQLite no-op row lock, and would deadlock against a real one"
    ),
)


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture
def action_permit_route():
    """Opt action fixtures into frozen issuance without changing app startup."""
    from app.main import app
    from app.routers.permits import action_router

    original_routes = list(app.router.routes)
    app.include_router(action_router)
    mounted_routes = app.router.routes[len(original_routes) :]
    app.openapi_schema = None
    try:
        yield
    finally:
        app.router.routes[:] = [
            route for route in app.router.routes if route not in mounted_routes
        ]
        app.openapi_schema = None


def _mount_dormant_test_routes() -> None:
    """Mount dormant trust surfaces (+ Stripe webhooks) on the shared app once.

    Mirrors what app.main does when ENABLE_PROOF_SURFACES is true, via the
    same mount_dormant_trust_surfaces helper so the sets cannot drift. The
    webhooks router is added here because production mounts it on Stripe
    configuration, which the suite deliberately never sets globally.
    """
    from app.main import app, mount_dormant_trust_surfaces
    from app.routers import webhooks

    if getattr(app.state, "dormant_test_routes_mounted", False):
        return
    mount_dormant_trust_surfaces(app)
    app.include_router(webhooks.router)
    app.state.dormant_test_routes_mounted = True


@pytest.fixture(autouse=True)
def enable_proof_surfaces_for_marked_tests(request):
    """Explicitly opt proof-marked tests into the frozen route collection."""
    if request.node.get_closest_marker("proof") is None:
        yield
        return

    from app.core.config import get_settings
    from app.main import PROOF_SURFACE_ROUTERS, app
    from app.services.mcp_phase9_tools import sync_proof_surface_mcp_registration

    cfg = get_settings()
    previous = cfg.ENABLE_PROOF_SURFACES
    cfg.ENABLE_PROOF_SURFACES = True
    if not getattr(app.state, "proof_test_routes_mounted", False):
        for router_module in PROOF_SURFACE_ROUTERS:
            app.include_router(router_module.router)
        app.state.proof_test_routes_mounted = True
    # Proof-surface flows may also lean on dormant trust surfaces (e.g. child
    # wallets for swarm demos); production mounts both groups under the same
    # flag, so the test posture mirrors that.
    _mount_dormant_test_routes()
    sync_proof_surface_mcp_registration()
    try:
        yield
    finally:
        cfg.ENABLE_PROOF_SURFACES = previous
        sync_proof_surface_mcp_registration()


@pytest.fixture(autouse=True)
def mount_dormant_surfaces_for_marked_tests(request):
    """Mount dormant trust routes for dormant-marked tests.

    Unlike the proof fixture this does NOT flip ENABLE_PROOF_SURFACES: the
    dormant routers have no request-time flag checks, and several dormant
    modules also assert flag-off behavior of other payloads (e.g. core-only
    billing categories).
    """
    if request.node.get_closest_marker("dormant") is None:
        yield
        return
    _mount_dormant_test_routes()
    yield


@pytest_asyncio.fixture(scope="session", autouse=True)
async def setup_database():
    """Set up database tables before any tests run."""
    require_disposable_test_database()
    import app.db.approval_gate_models  # noqa: F401 -- registers table on SQLModel.metadata
    from app.core.durable_state import close_durable_state
    from app.db.database import init_db, close_db

    # Close any existing connection
    await close_db()

    # Start from a clean SQLite file so stale rows from prior runs
    # (e.g., unique constraints on seeded session IDs) don't leak
    # into the next run.
    for db_file in (TEST_DB_FILE, f"{TEST_DB_FILE}-shm", f"{TEST_DB_FILE}-wal"):
        if os.path.exists(db_file):
            os.remove(db_file)

    # Initialize tables
    await init_db()

    yield

    # Cleanup
    await close_db()
    # Release any durable-state backend a test opted into. aiosqlite runs each
    # connection on a non-daemon worker thread, so an unclosed one keeps the
    # interpreter alive after the run finishes — the suite passes and pytest
    # never exits. STATE_BACKEND=memory above means this is normally a no-op;
    # it exists so a test that deliberately selects a real backend cannot wedge
    # the process.
    await close_durable_state()


#: Tables ``clean_database`` empties, in delete order: every table is listed
#: before any table it holds a foreign key to, so a bulk DELETE succeeds with
#: PRAGMA foreign_keys=ON (Postgres-parity for SQLite tests). ``wallets`` comes
#: last, after its self-FK on parent_wallet_id is cleared. A table in
#: SQLModel.metadata that is neither listed here nor named in
#: tests/test_clean_database_coverage.py's exemption set fails that test, so a
#: new trust-plane table cannot silently escape per-test cleanup.
CLEAN_DATABASE_TABLES = (
    "approval_gate_approvals",
    "receipt_resolutions",
    "resolution_chain_heads",
    "receipts",
    "mcp_dispatch_attempts",
    "human_approvals",
    "permit_requests",
    "quotes",
    "idempotency_records",
    "permits",
    "agent_comms_messages",
    "content_factory_generations",
    "ledger_entries",
    "billing_alerts",
    "policy_bundles",
    "daily_balance_snapshots",
    "kyc_verifications",
    "refresh_tokens",
    "api_key_usage_events",
    "api_keys",
    "key_rotation_logs",
    "service_registry",
    "control_plane_audit_events",
    "audit_chain_heads",
    "signing_keys",
    "wallets",
)


@pytest_asyncio.fixture(scope="function")
async def clean_database():
    """Clean database tables before and after tests that use this fixture."""
    require_disposable_test_database()
    from app.db.database import get_session_factory

    async def clear_tables():
        factory = get_session_factory()
        async with factory() as session:
            for table in CLEAN_DATABASE_TABLES:
                if table == "wallets":
                    await session.execute(
                        text("UPDATE wallets SET parent_wallet_id = NULL")
                    )
                await session.execute(text(f"DELETE FROM {table}"))
            await session.commit()

    await clear_tables()
    yield
    await clear_tables()


@pytest.fixture
def enforce_naive_utc_datetime_columns():
    """Fail before SQLite hides an aware value bound to the naive-UTC schema.

    PostgreSQL's asyncpg driver rejects this mismatch, while SQLite silently
    serializes the value and lets the same test pass. Inspect both ORM flushes
    and Core/SQL query parameters. Keep this opt-in so focused production-parity
    tests can guard trust-critical paths without expanding unrelated scope.
    """

    def iter_parameter_values(value):
        if isinstance(value, Mapping):
            for nested in value.values():
                yield from iter_parameter_values(nested)
        elif isinstance(value, (list, tuple, set)):
            for nested in value:
                yield from iter_parameter_values(nested)
        else:
            yield value

    def assert_naive_datetime_values(session, _flush_context, _instances):
        for instance in [*session.new, *session.dirty]:
            mapper = sa_inspect(instance).mapper
            for attribute in mapper.column_attrs:
                if not any(
                    isinstance(column.type, DateTime) and not column.type.timezone
                    for column in attribute.columns
                ):
                    continue
                value = getattr(instance, attribute.key, None)
                if isinstance(value, datetime) and value.utcoffset() is not None:
                    raise AssertionError(
                        f"{type(instance).__name__}.{attribute.key} must be naive UTC"
                    )

    def assert_naive_statement_parameters(
        connection,
        clauseelement,
        multiparams,
        params,
        _execution_options,
    ):
        compiled_params = clauseelement.compile(dialect=connection.dialect).params
        for value in iter_parameter_values((multiparams, params, compiled_params)):
            if isinstance(value, datetime) and value.utcoffset() is not None:
                raise AssertionError("SQL datetime parameters must be naive UTC")

    event.listen(Session, "before_flush", assert_naive_datetime_values)
    event.listen(Engine, "before_execute", assert_naive_statement_parameters)
    try:
        yield
    finally:
        event.remove(Session, "before_flush", assert_naive_datetime_values)
        event.remove(Engine, "before_execute", assert_naive_statement_parameters)
