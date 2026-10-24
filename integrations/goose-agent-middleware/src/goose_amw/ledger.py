"""SQLite idempotency ledger with a bounded replay window.

States: ``in_flight`` -> ``completed`` | ``failed`` | ``failed_unknown``.

* ``completed``: a retry inside the window replays the stored result; the tool
  does not run again.
* ``failed``: the upstream answered with a definite error (no effect); a retry
  may run again.
* ``failed_unknown``: the outcome is unclear (transport lost after dispatch).
  It is never re-run automatically, inside or outside the window, until an
  operator resolves it.
* A ``completed`` entry older than the window no longer dedupes, so an
  identical write made on purpose much later runs normally.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_WINDOW_SECONDS = 600


@dataclass(frozen=True)
class Claim:
    """Result of trying to claim a key before running a tool."""

    action: str  # "run" | "replay" | "refuse"
    result: str | None = None
    status: str | None = None


class Ledger:
    def __init__(self, path: str | Path, window_seconds: float = DEFAULT_WINDOW_SECONDS):
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS calls ("
            " key TEXT PRIMARY KEY, tool TEXT NOT NULL, args_hash TEXT NOT NULL,"
            " status TEXT NOT NULL, result TEXT, first_request_id TEXT,"
            " created_at REAL NOT NULL, updated_at REAL NOT NULL, executions INTEGER NOT NULL)"
        )

    def claim(
        self,
        key: str,
        *,
        tool: str,
        args_hash: str,
        request_id: str | None,
        now: float | None = None,
    ) -> Claim:
        now = time.time() if now is None else now
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT status, result, created_at FROM calls WHERE key = ?", (key,)
                ).fetchone()
                if row is not None:
                    status, result, created_at = row
                    if status == "failed_unknown":
                        self._db.execute("COMMIT")
                        return Claim("refuse", status=status)
                    if status == "in_flight":
                        self._db.execute("COMMIT")
                        return Claim("refuse", status=status)
                    if status == "completed" and now - created_at < self.window_seconds:
                        self._db.execute("COMMIT")
                        return Claim("replay", result=result, status=status)
                    # failed (no effect) or completed outside the window: run again.
                    self._db.execute(
                        "UPDATE calls SET status='in_flight', result=NULL, created_at=?,"
                        " updated_at=?, executions=executions+1 WHERE key=?",
                        (now, now, key),
                    )
                else:
                    self._db.execute(
                        "INSERT INTO calls VALUES (?, ?, ?, 'in_flight', NULL, ?, ?, ?, 1)",
                        (key, tool, args_hash, request_id, now, now),
                    )
                self._db.execute("COMMIT")
                return Claim("run")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def finish(self, key: str, status: str, result: str | None = None) -> None:
        if status not in ("completed", "failed", "failed_unknown"):
            raise ValueError(status)
        with self._lock:
            self._db.execute(
                "UPDATE calls SET status=?, result=?, updated_at=? WHERE key=?",
                (status, result, time.time(), key),
            )

    def mark_stale_in_flight_unknown(self) -> int:
        """At startup, an ``in_flight`` row means a crash mid-call: outcome unknown."""
        with self._lock:
            cur = self._db.execute(
                "UPDATE calls SET status='failed_unknown', updated_at=? WHERE status='in_flight'",
                (time.time(),),
            )
            return cur.rowcount

    def get(self, key: str) -> dict | None:
        cur = self._db.execute(
            "SELECT key, tool, status, result, executions, first_request_id FROM calls WHERE key=?",
            (key,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        names = ["key", "tool", "status", "result", "executions", "first_request_id"]
        return dict(zip(names, row, strict=True))
