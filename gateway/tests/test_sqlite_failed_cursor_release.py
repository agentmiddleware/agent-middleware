"""The SQLite connection factory keeps failed statements off the event loop.

Through CPython 3.11, a sqlite3 cursor whose execute() raised still owns its
prepared statement, and destroying that cursor later calls sqlite3_reset(),
which takes the connection mutex. aiosqlite runs statements on a worker thread
that holds that mutex for the whole busy-handler wait, so when the cyclic GC
destroys such a cursor on the event-loop thread while a sibling statement is
waiting for the WAL write lock, the loop freezes for the full busy timeout and
the lock holder, which can only commit from the loop, never does. Under
concurrent governed calls this surfaced as a 30 s stall plus "database is
locked" on the Python 3.11 CI leg only.

This test is the mechanism in miniature, with a 10 s busy timeout: connection
B holds the write lock, connection A has a cursor from a failed INSERT and
then starts a write that waits for B, the main thread destroys the failed
cursor, and B is scheduled to commit 0.2 s later. With the factory, the main
thread is never blocked on the mutex, B commits, and A's write succeeds.
Without it, on 3.11, the main thread blocks for about 10 s and A's write
fails with "database is locked"; on 3.12+ the interpreter already detaches
the statement, so the factory is a no-op there and the test passes either
way.

The timing bound is deliberately loose. Destroying the cursor runs a full
cyclic collection, which late in a long test session is slow on its own
(about 2 s was observed in a loaded xdist worker); what the fix removes is a
wait as long as the busy timeout, so the assertion is against half of it,
and a weak reference proves the cursor really was destroyed on this thread.
"""

from __future__ import annotations

import asyncio
import gc
import sqlite3
import time
import weakref
from pathlib import Path

import aiosqlite
import pytest

from app.db.database import _StatementReleasingConnection

BUSY_TIMEOUT_SECONDS = 10.0


async def _stall_seconds(
    db_path: Path, factory: type[sqlite3.Connection] | None
) -> tuple[float, str]:
    """Return (seconds the main thread was blocked, outcome of A's write)."""
    kwargs: dict[str, object] = {"timeout": BUSY_TIMEOUT_SECONDS}
    if factory is not None:
        kwargs["factory"] = factory
    conn_a = await aiosqlite.connect(db_path, **kwargs)
    conn_b = await aiosqlite.connect(db_path, **kwargs)
    try:
        await conn_a.execute("PRAGMA journal_mode=WAL")
        await conn_a.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        await conn_a.commit()
        await conn_b.execute("PRAGMA journal_mode=WAL")

        # A failed statement leaves a cursor that, on 3.11, still owns it.
        cursor = await conn_a.cursor()
        await cursor.execute("INSERT INTO t (id, v) VALUES (?, ?)", (1, "x"))
        await conn_a.commit()
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            await cursor.execute("INSERT INTO t (id, v) VALUES (?, ?)", (1, "dup"))
        await conn_a.rollback()
        failed_cursor = cursor._cursor  # the underlying sqlite3.Cursor
        cursor_ref = weakref.ref(failed_cursor)
        del cursor

        # B takes the WAL write lock and keeps it while the loop runs.
        await conn_b.execute("BEGIN IMMEDIATE")
        await conn_b.execute("INSERT INTO t (id, v) VALUES (2, 'b')")

        # A's worker thread enters sqlite3_step() and waits in the busy handler.
        pending_insert = asyncio.ensure_future(
            conn_a.execute("INSERT INTO t (id, v) VALUES (3, 'a')")
        )
        await asyncio.sleep(0.2)

        async def release_lock() -> None:
            await asyncio.sleep(0.2)
            await conn_b.commit()

        releaser = asyncio.ensure_future(release_lock())
        await asyncio.sleep(0)

        # Destroying the failed cursor on this thread is what blocked on 3.11.
        started = time.monotonic()
        del failed_cursor
        gc.collect()
        blocked = time.monotonic() - started

        await releaser
        try:
            await pending_insert
            outcome = "ok"
        except sqlite3.OperationalError as exc:
            outcome = str(exc)
        if cursor_ref() is not None:
            outcome = "failed cursor survived collection; destruction was not exercised"
        return blocked, outcome
    finally:
        await conn_a.close()
        await conn_b.close()


def test_failed_statement_release_keeps_event_loop_running(tmp_path: Path) -> None:
    blocked, outcome = asyncio.run(
        _stall_seconds(tmp_path / "stall.db", _StatementReleasingConnection)
    )
    assert outcome == "ok"
    assert blocked < BUSY_TIMEOUT_SECONDS / 2, (
        f"main thread blocked {blocked:.2f}s in cursor destruction"
    )
