"""DATABASE_URL normalization and durable-state fail-closed behavior."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from app.core.db_urls import (
    as_asyncpg_url,
    as_sqlalchemy_url,
    is_postgres_url,
    sqlite_path_from_url,
)
from app.core.durable_state import (
    _TRACKED_SQLITE_CONNECTIONS,
    _release_tracked_sqlite_connections,
    DurableStateConfigError,
    DurableStateStore,
    reset_durable_state_for_tests,
)
from app.core.runtime_degradation import (
    get_runtime_degradation,
    reset_runtime_degradation,
)


def test_as_sqlalchemy_url_postgres_variants():
    assert as_sqlalchemy_url("postgresql://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"
    assert as_sqlalchemy_url("postgres://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"
    assert (
        as_sqlalchemy_url("postgresql+asyncpg://u:p@h/db")
        == "postgresql+asyncpg://u:p@h/db"
    )
    assert (
        as_sqlalchemy_url("sqlite+aiosqlite:///./x.db") == "sqlite+aiosqlite:///./x.db"
    )
    assert as_sqlalchemy_url("") == ""


def test_as_asyncpg_url_strips_driver():
    assert as_asyncpg_url("postgresql+asyncpg://u:p@h/db") == "postgresql://u:p@h/db"
    assert as_asyncpg_url("postgresql://u:p@h/db") == "postgresql://u:p@h/db"
    assert as_asyncpg_url("postgres://u:p@h/db") == "postgresql://u:p@h/db"


def test_uppercase_schemes_normalize_without_touching_credentials():
    """is_postgres_url accepts any case, so the converters must too.

    Only the scheme is lowercased — user, password, host, and path are
    case-sensitive and must survive verbatim.
    """
    upper = "POSTGRESQL://Us3R:PaSsW0rd@Host/DB"
    assert is_postgres_url(upper)
    assert as_asyncpg_url(upper) == "postgresql://Us3R:PaSsW0rd@Host/DB"
    assert as_sqlalchemy_url(upper) == "postgresql+asyncpg://Us3R:PaSsW0rd@Host/DB"
    assert as_asyncpg_url("POSTGRES://u:p@h/db") == "postgresql://u:p@h/db"


def test_url_helpers_are_idempotent():
    raw = "postgresql://u:p@h/db"
    sa = as_sqlalchemy_url(raw)
    assert as_sqlalchemy_url(sa) == sa
    assert as_asyncpg_url(as_asyncpg_url(sa)) == as_asyncpg_url(sa)
    assert as_asyncpg_url(sa) == raw


def test_suite_runs_on_memory_durable_state():
    """The suite's own durable-state posture, asserted rather than assumed.

    conftest sets STATE_BACKEND=memory. Without it, the SQLite DATABASE_URL the
    suite also sets would resolve to the SQLite backend and open an aiosqlite
    connection whose worker thread is not a daemon — an unclosed one blocks
    interpreter shutdown, so the suite passes and pytest never exits. This guard
    fails loudly if that line is ever dropped.
    """
    assert DurableStateStore()._resolve_backend() == "memory"


def test_is_postgres_url_rejects_non_postgres_backends():
    assert is_postgres_url("postgresql://u:p@h/db")
    assert is_postgres_url("postgres://u:p@h/db")
    assert is_postgres_url("postgresql+asyncpg://u:p@h/db")
    assert is_postgres_url("POSTGRESQL://u:p@h/db")
    assert not is_postgres_url("sqlite+aiosqlite:///./x.db")
    assert not is_postgres_url("")


def test_sqlite_path_from_url_extracts_aiosqlite_path():
    assert sqlite_path_from_url("sqlite+aiosqlite:///./x.db") == "./x.db"
    assert sqlite_path_from_url("sqlite+aiosqlite:////abs/x.db") == "/abs/x.db"
    assert sqlite_path_from_url("sqlite:///./x.db") == "./x.db"
    assert sqlite_path_from_url("postgresql://u:p@h/db") == ""
    assert sqlite_path_from_url("") == ""


def test_in_memory_sqlite_is_not_a_durable_path():
    """In-memory SQLite must never be offered as durable state.

    It is valid SQLite but keeps nothing across a restart, so accepting it
    would reintroduce the silent non-durability this helper prevents.
    """
    assert sqlite_path_from_url("sqlite:///:memory:") == ""
    assert sqlite_path_from_url("sqlite+aiosqlite:///:memory:") == ""
    assert sqlite_path_from_url("sqlite+aiosqlite://") == ""
    assert sqlite_path_from_url("sqlite+aiosqlite:///file::memory:?cache=shared") == ""
    assert sqlite_path_from_url("sqlite+aiosqlite:///file:x?mode=memory") == ""


def test_durable_filenames_containing_memory_text_are_kept():
    """``mode=memory`` counts only as a file: URI query parameter.

    A substring test would classify these durable paths as ephemeral and route
    their state to memory, losing it on restart.
    """
    assert sqlite_path_from_url("sqlite:///mode=memory.db") == "mode=memory.db"
    assert (
        sqlite_path_from_url("sqlite+aiosqlite:////var/data/mode=memory/app.db")
        == "/var/data/mode=memory/app.db"
    )


def test_auto_backend_does_not_select_in_memory_sqlite(monkeypatch):
    """An in-memory DATABASE_URL falls through rather than posing as durable."""
    reset_runtime_degradation()
    reset_durable_state_for_tests()
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("STATE_BACKEND", "auto")
    monkeypatch.setenv("SQLITE_URL", "")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "")
    from app.core.config import get_settings

    get_settings.cache_clear()
    store = DurableStateStore()
    assert store._resolve_backend() == "memory"
    get_settings.cache_clear()
    reset_durable_state_for_tests()
    reset_runtime_degradation()


def test_auto_backend_uses_sqlite_for_a_sqlite_database_url(monkeypatch):
    """A SQLite DATABASE_URL must not route auto-resolution to asyncpg.

    Regression: auto-resolution treated any non-empty DATABASE_URL as postgres,
    so the standard local setting sent a sqlite DSN to asyncpg, logged a
    connection traceback, and silently degraded durable state to in-memory.
    """
    reset_runtime_degradation()
    reset_durable_state_for_tests()
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("STATE_BACKEND", "auto")
    monkeypatch.setenv("SQLITE_URL", "")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./local.db")
    monkeypatch.setenv("REDIS_URL", "")
    from app.core.config import get_settings

    get_settings.cache_clear()
    store = DurableStateStore()
    assert store._resolve_backend() == "sqlite"
    assert store._sqlite_url == "./local.db"
    report = get_runtime_degradation()
    assert report["durable_state"]["fell_back_to_memory"] is False
    get_settings.cache_clear()
    reset_durable_state_for_tests()
    reset_runtime_degradation()


def test_auto_backend_still_uses_postgres_for_a_postgres_database_url(monkeypatch):
    reset_runtime_degradation()
    reset_durable_state_for_tests()
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("STATE_BACKEND", "auto")
    monkeypatch.setenv("SQLITE_URL", "")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("REDIS_URL", "")
    from app.core.config import get_settings

    get_settings.cache_clear()
    store = DurableStateStore()
    assert store._resolve_backend() == "postgres"
    get_settings.cache_clear()
    reset_durable_state_for_tests()
    reset_runtime_degradation()


def test_sqlite_without_url_falls_back_outside_production(monkeypatch):
    reset_runtime_degradation()
    reset_durable_state_for_tests()
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("STATE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_URL", "")
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("REDIS_URL", "")
    from app.core.config import get_settings

    get_settings.cache_clear()
    store = DurableStateStore()
    assert store._resolve_backend() == "memory"
    report = get_runtime_degradation()
    assert report["durable_state"]["fell_back_to_memory"] is True
    assert report["durable_state"]["intended_backend"] == "sqlite"
    get_settings.cache_clear()
    reset_durable_state_for_tests()
    reset_runtime_degradation()


def test_sqlite_without_url_refuses_production_like(monkeypatch):
    """Production still refuses this config; the reason is now stricter.

    This previously failed only because SQLITE_URL was unset, which implied a
    configured SQLITE_URL would have been accepted. SQLite is now refused in
    production-like environments outright, so the refusal no longer depends on
    the URL being missing -- see
    ``test_sqlite_with_url_refuses_production_like`` for the configured case.
    """
    reset_runtime_degradation()
    reset_durable_state_for_tests()
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("STATE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_URL", "")
    monkeypatch.setenv("DATABASE_URL", "")
    from app.core.config import get_settings

    get_settings.cache_clear()
    store = DurableStateStore()
    with pytest.raises(DurableStateConfigError, match="not allowed in production"):
        store._resolve_backend()
    get_settings.cache_clear()
    reset_durable_state_for_tests()
    reset_runtime_degradation()


@pytest.mark.anyio
async def test_postgres_init_fail_closed_in_production(monkeypatch):
    reset_runtime_degradation()
    reset_durable_state_for_tests()
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("STATE_BACKEND", "postgres")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://nobody:wrong@127.0.0.1:1/none"
    )
    from app.core.config import get_settings

    get_settings.cache_clear()
    store = DurableStateStore()
    with pytest.raises(DurableStateConfigError, match="failed to initialize"):
        await store._ensure_ready()
    report = get_runtime_degradation()
    assert report["durable_state"]["fell_back_to_memory"] is True
    assert report["durable_state"]["intended_backend"] == "postgres"
    get_settings.cache_clear()
    reset_durable_state_for_tests()
    reset_runtime_degradation()


def test_memory_backend_forbidden_in_production(monkeypatch):
    reset_runtime_degradation()
    reset_durable_state_for_tests()
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("STATE_BACKEND", "memory")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    from app.core.config import get_settings

    get_settings.cache_clear()
    store = DurableStateStore()
    with pytest.raises(DurableStateConfigError, match="memory is not allowed"):
        store._resolve_backend()
    get_settings.cache_clear()
    reset_durable_state_for_tests()
    reset_runtime_degradation()


def test_postgres_missing_url_refuses_production_like(monkeypatch):
    reset_runtime_degradation()
    reset_durable_state_for_tests()
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("STATE_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("SQLITE_URL", "")
    from app.core.config import get_settings

    get_settings.cache_clear()
    store = DurableStateStore()
    with pytest.raises(DurableStateConfigError, match="DATABASE_URL"):
        store._resolve_backend()
    get_settings.cache_clear()
    reset_durable_state_for_tests()
    reset_runtime_degradation()


def test_auto_without_urls_refuses_production_like(monkeypatch):
    reset_runtime_degradation()
    reset_durable_state_for_tests()
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("STATE_BACKEND", "auto")
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("SQLITE_URL", "")
    from app.core.config import get_settings

    get_settings.cache_clear()
    store = DurableStateStore()
    with pytest.raises(DurableStateConfigError, match="require DATABASE_URL"):
        store._resolve_backend()
    get_settings.cache_clear()
    reset_durable_state_for_tests()
    reset_runtime_degradation()


# --- shutdown backstop for the aiosqlite worker thread ---------------------
# aiosqlite.Connection composes a worker Thread with no daemon=True, and
# interpreter shutdown joins non-daemon threads. A connection nobody closes
# therefore hangs the process at exit with no traceback. close() is the real
# cleanup path; these cover the backstop for consumers that never reach it.

# Opens the SQLite backend and exits WITHOUT calling close_durable_state().
# Without the backstop this process never terminates.
_LEAK_SCRIPT = """
import asyncio, logging, os, sys
logging.basicConfig(level=logging.WARNING)
sys.path.insert(0, {repo!r})
os.environ["STATE_BACKEND"] = "sqlite"
os.environ["SQLITE_URL"] = {db!r}
os.environ["ENVIRONMENT"] = "development"

from app.core.durable_state import get_durable_state

async def main():
    store = get_durable_state()
    await store.save_json("k", {{"v": 1}})
    assert store.backend == "sqlite", store.backend

asyncio.run(main())
print("WORK_DONE", flush=True)
# Deliberately no close_durable_state() -- that is the whole point.
"""


def _run_leak_script(tmp_path):
    """Run the leaking script in a subprocess under a hard timeout.

    Returns (returncode, stdout, stderr). A timeout is reported as 124 to
    match the shell convention used elsewhere in this repo's verification.
    """
    import subprocess
    import sys as _sys

    script = _LEAK_SCRIPT.format(
        repo=str(Path(__file__).resolve().parent.parent),
        db=str(tmp_path / "atexit_probe.db"),
    )
    env = {**os.environ}
    env.pop("DATABASE_URL", None)
    try:
        proc = subprocess.run(
            [_sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        # ``text=True`` does NOT apply on this path: CPython attaches the raw
        # buffers to TimeoutExpired without decoding them, so these are bytes
        # even though a completed run yields str. Decoding here keeps the
        # timeout branch -- the one that reports a hang -- from dying with a
        # TypeError instead of the assertion the caller is waiting to read.
        return 124, _as_text(exc.stdout), _as_text(exc.stderr)
    return proc.returncode, proc.stdout, proc.stderr


def _as_text(stream) -> str:
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        return stream.decode(errors="replace")
    return stream


def test_process_exits_when_sqlite_state_is_never_closed(tmp_path):
    """The acceptance gate: a leaked connection must not wedge interpreter exit.

    Asserting on the exit code rather than on output is deliberate. A passing
    log line reads identically whether the process exited or hung -- that
    ambiguity is exactly what let the original six-hour CI hang through.
    """
    returncode, stdout, stderr = _run_leak_script(tmp_path)

    assert "WORK_DONE" in stdout, f"script did not complete its work: {stdout!r}"
    assert returncode != 124, (
        "process hung at exit: the aiosqlite worker thread was never released"
    )
    assert returncode == 0, f"unexpected exit code {returncode}"
    # Exit code alone proves termination, not *why*. A refactor that stopped
    # opening the connection -- or daemonized the worker -- would keep the
    # assertions above green while deleting the mechanism under test.
    assert "was not closed" in stderr, (
        "the process exited without the backstop running, so this test no "
        f"longer proves the backstop is what released the thread: {stderr!r}"
    )


def test_clean_close_untracks_and_releases_the_worker_thread(tmp_path):
    """The normal path stays authoritative: close() releases and deregisters."""
    import threading

    reset_durable_state_for_tests()
    store = DurableStateStore()
    store._state_backend = "sqlite"
    store._sqlite_url = str(tmp_path / "clean_close.db")

    async def exercise():
        await store.save_json("k", {"v": 1})
        assert store.backend == "sqlite"
        conn = store._sqlite_conn
        assert conn is not None
        assert id(conn) in _TRACKED_SQLITE_CONNECTIONS, (
            "backstop did not track the connection"
        )
        await store.close()
        return conn

    conn = asyncio.run(exercise())

    assert id(conn) not in _TRACKED_SQLITE_CONNECTIONS, (
        "connection still tracked after a clean close"
    )
    assert store._sqlite_owner_pid is None
    conn._thread.join(timeout=10)
    assert not conn._thread.is_alive(), "worker thread outlived close()"
    assert conn._thread not in threading.enumerate()
    reset_durable_state_for_tests()


def test_shutdown_backstop_is_inert_in_a_forked_child(tmp_path, monkeypatch):
    """A forked child must never close a connection its parent owns."""
    reset_durable_state_for_tests()
    store = DurableStateStore()
    store._state_backend = "sqlite"
    store._sqlite_url = str(tmp_path / "fork_guard.db")

    async def setup():
        await store.save_json("k", {"v": 1})

    asyncio.run(setup())
    conn = store._sqlite_conn
    assert conn is not None
    assert id(conn) in _TRACKED_SQLITE_CONNECTIONS

    # Pretend we are a child that inherited the registry across fork().
    monkeypatch.setattr(os, "getpid", lambda: store._sqlite_owner_pid + 1)
    _release_tracked_sqlite_connections()
    assert conn._thread.is_alive(), "child closed a connection it does not own"
    # A skipped entry must survive: the child had no right to drop it, and the
    # owner still needs it to release the thread at its own shutdown.
    assert id(conn) in _TRACKED_SQLITE_CONNECTIONS, (
        "the child discarded a registry entry it was not entitled to handle"
    )

    # The real owner still releases it -- no re-insertion needed.
    monkeypatch.undo()
    _release_tracked_sqlite_connections()
    conn._thread.join(timeout=10)
    assert not conn._thread.is_alive()
    assert id(conn) not in _TRACKED_SQLITE_CONNECTIONS

    store._unregister_sqlite_shutdown_backstop()
    store._sqlite_conn = None
    reset_durable_state_for_tests()


# Probes the three aiosqlite properties the backstop relies on. Runs in a
# child process on purpose: if the contract regresses, the probe leaves a live
# non-daemon worker behind, and in-process that would wedge pytest itself at
# exit -- the very failure this suite exists to catch. Isolating it means a
# broken contract is a reported failure, not a hung run.
_CONTRACT_SCRIPT = """
import asyncio, inspect, sys, threading
import aiosqlite

if inspect.iscoroutinefunction(aiosqlite.Connection.stop):
    print("FAIL stop-is-async", flush=True)
    raise SystemExit(1)

async def _open():
    return await aiosqlite.connect(":memory:")

loop = asyncio.new_event_loop()
try:
    conn = loop.run_until_complete(_open())
finally:
    loop.close()
asyncio.set_event_loop(None)

if not conn._thread.is_alive():
    print("FAIL worker-not-started", flush=True)
    raise SystemExit(1)
if conn._thread.daemon:
    print("FAIL worker-is-daemon", flush=True)
    raise SystemExit(1)

# No running or current event loop here -- exactly the shutdown condition.
conn.stop()
conn._thread.join(timeout=10)
if conn._thread.is_alive() or conn._thread in threading.enumerate():
    print("FAIL stop-did-not-release-worker", flush=True)
    raise SystemExit(1)

print("CONTRACT_OK", flush=True)
"""


def test_aiosqlite_stop_contract_still_holds():
    """Pin the aiosqlite behaviour the backstop depends on, by behaviour.

    The shutdown hook calls ``Connection.stop`` from a context with no running
    event loop. That works because ``stop`` is synchronous, the worker thread is
    non-daemon, and ``stop`` releases it without a loop. All three are internal
    to aiosqlite and could change in a future release.

    Asserting them rather than pinning a version keeps dependency upgrades
    unblocked while making a silent behavioural regression impossible: a broken
    contract becomes a named failure instead of a six-hour CI hang.
    """
    import subprocess
    import sys as _sys

    try:
        proc = subprocess.run(
            [_sys.executable, "-c", _CONTRACT_SCRIPT],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        # The child is killed by subprocess.run before this propagates, so a
        # regressed contract cannot leak a worker thread into this process.
        raise AssertionError(
            "the aiosqlite contract probe hung; Connection.stop no longer "
            f"releases the worker: {_as_text(exc.stdout)!r}"
        ) from exc

    assert proc.returncode == 0, (
        "the aiosqlite behaviour this backstop depends on has changed "
        f"({proc.stdout.strip() or 'no marker'}); stderr={proc.stderr!r}"
    )
    assert "CONTRACT_OK" in proc.stdout, (
        f"contract probe did not confirm the contract: {proc.stdout!r}"
    )


def test_connection_stays_tracked_when_both_cleanup_paths_fail(tmp_path):
    """A cleanup that fails outright must not remove the backstop.

    close() falls back to stop() when the close raises. If that fallback also
    raises, the worker thread may still be alive -- and that is precisely the
    case where the shutdown hook must still be able to reach it. Untracking
    there would strip the backstop from the one path that still needs it.
    """
    reset_durable_state_for_tests()
    store = DurableStateStore()
    store._state_backend = "sqlite"
    store._sqlite_url = str(tmp_path / "cleanup_failure.db")

    async def setup():
        await store.save_json("k", {"v": 1})

    asyncio.run(setup())
    conn = store._sqlite_conn
    assert conn is not None
    assert id(conn) in _TRACKED_SQLITE_CONNECTIONS

    real_stop = conn.stop

    async def _boom_close(*_a, **_k):
        raise RuntimeError("simulated close failure")

    def _boom_stop(*_a, **_k):
        raise RuntimeError("simulated stop failure")

    conn.close = _boom_close
    conn.stop = _boom_stop

    try:
        asyncio.run(store.close())

        assert id(conn) in _TRACKED_SQLITE_CONNECTIONS, (
            "both cleanup paths failed, so the connection must stay tracked for "
            "the shutdown hook to retry"
        )
    finally:
        # Unconditional: if the assertion above fails, the worker is still alive
        # and untracked, so skipping this would hang the suite at exit instead
        # of reporting the failure -- the same trap the contract probe had.
        conn.stop = real_stop
        _TRACKED_SQLITE_CONNECTIONS.pop(id(conn), None)
        real_stop()
        conn._thread.join(timeout=10)
        reset_durable_state_for_tests()

    assert not conn._thread.is_alive()


# Forks after the SQLite backend is live, then closes the store in the child.
# fork() duplicates only the calling thread, so the child inherits the
# Connection object but not its worker. Awaiting close() there blocks forever
# on a future nothing can resolve. Runs in a subprocess so a regression is a
# reported failure rather than a wedged suite.
_FORK_CLOSE_SCRIPT = """
import asyncio, os, sys
sys.path.insert(0, {repo!r})
os.environ["STATE_BACKEND"] = "sqlite"
os.environ["SQLITE_URL"] = {db!r}
os.environ["ENVIRONMENT"] = "development"

from app.core.durable_state import close_durable_state, get_durable_state

store = get_durable_state()
asyncio.run(store.save_json("k", {{"v": 1}}))
assert store.backend == "sqlite", store.backend

pid = os.fork()
if pid == 0:
    # The child must not block trying to close a connection it does not own.
    asyncio.run(close_durable_state())
    print("CHILD_CLOSED", flush=True)
    os._exit(0)

_, status = os.waitpid(pid, 0)
print("CHILD_STATUS", status, flush=True)
asyncio.run(close_durable_state())
print("PARENT_CLOSED", flush=True)
"""


def test_close_does_not_block_on_a_connection_inherited_across_fork(tmp_path):
    """A forked child must not deadlock closing its parent's connection."""
    import subprocess
    import sys as _sys

    script = _FORK_CLOSE_SCRIPT.format(
        repo=str(Path(__file__).resolve().parent.parent),
        db=str(tmp_path / "fork_close.db"),
    )
    env = {**os.environ}
    env.pop("DATABASE_URL", None)
    try:
        proc = subprocess.run(
            [_sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            "closing a fork-inherited SQLite connection deadlocked: "
            f"{_as_text(exc.stdout)!r}"
        ) from exc

    assert "CHILD_CLOSED" in proc.stdout, (
        f"child never finished close_durable_state(): {proc.stdout!r} "
        f"stderr={proc.stderr!r}"
    )
    assert "CHILD_STATUS 0" in proc.stdout, f"child exited badly: {proc.stdout!r}"
    assert "PARENT_CLOSED" in proc.stdout, (
        f"parent could not close its own connection afterwards: {proc.stdout!r}"
    )
    assert proc.returncode == 0, f"unexpected exit code {proc.returncode}"


def test_sqlite_with_url_refuses_production_like(monkeypatch):
    """SQLite is not a supported production backend, even fully configured.

    The trust plane's money and permit paths serialize concurrent writers with
    ``SELECT ... FOR UPDATE``. SQLAlchemy silently drops that on SQLite, so the
    once-only and read-modify-write guards those paths rely on stop holding.
    A configured SQLITE_URL previously satisfied the explicit backend with no
    production objection at all.
    """
    reset_runtime_degradation()
    reset_durable_state_for_tests()
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("STATE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_URL", "/var/lib/agent-middleware/state.db")
    monkeypatch.setenv("DATABASE_URL", "")
    from app.core.config import get_settings

    get_settings.cache_clear()
    store = DurableStateStore()
    with pytest.raises(DurableStateConfigError, match="not allowed in production"):
        store._resolve_backend()
    get_settings.cache_clear()
    reset_durable_state_for_tests()
    reset_runtime_degradation()


def test_auto_backend_refuses_sqlite_in_production_like(monkeypatch):
    """Auto-resolution must not quietly land on SQLite in production either.

    A SQLite ``DATABASE_URL`` is the ordinary local-development setting, and
    auto-resolution maps it to the sqlite backend. In a production-like
    environment that turned a misconfigured DSN into an unsafe backend without
    a word; it must fail closed instead.
    """
    reset_runtime_degradation()
    reset_durable_state_for_tests()
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("STATE_BACKEND", "auto")
    monkeypatch.setenv("SQLITE_URL", "")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./prod.db")
    from app.core.config import get_settings

    get_settings.cache_clear()
    store = DurableStateStore()
    with pytest.raises(DurableStateConfigError, match="SQLite is not a supported"):
        store._resolve_backend()
    get_settings.cache_clear()
    reset_durable_state_for_tests()
    reset_runtime_degradation()
