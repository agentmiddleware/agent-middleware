"""The independent effect ledger: ground truth for what the downstream did.

This is a plain SQLite file owned by the simulated business tool. It is
deliberately **not** the gateway database, not a table in it, and not reachable
through any gateway API. When a scenario reports "downstream executions: 2",
that number is a ``COUNT(*)`` over this file.

Two properties are load-bearing:

* ``effects`` is append-only and **duplicate-tolerant**. A second execution of
  the same business operation is a second row. Nothing here hides a duplicate
  behind a uniqueness constraint; a constraint would turn the thing being
  measured into a silent no-op.
* Every write is committed with ``synchronous=FULL`` before the tool answers,
  so a downstream crash immediately after execution still leaves the row.

``native_results`` is separate: it is the *tool's* own idempotency store,
used only by configurations that model a correctly built downstream. It is
kept in the same file so a downstream restart preserves it, which is what a
real system of record would do.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from collections.abc import Callable


@dataclass(frozen=True)
class EffectRecord:
    effect_id: str
    operation_id: str
    request_id: str
    idempotency_key: str
    amount: int
    currency: str
    customer_id: str
    payment_id: str
    executed_at: str
    worker_pid: int
    configuration: str
    #: Ordinal of this execution for its operation_id (1 = first execution).
    execution_ordinal: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class NativeConflictError(Exception):
    """The same business operation was presented with a different payload."""

    code = "operation_id_reused_with_different_payload"

    def __init__(self, operation_id: str) -> None:
        super().__init__(f"{self.code}: {operation_id}")
        self.operation_id = operation_id


@dataclass(frozen=True)
class ExecutionOutcome:
    """What one call to :meth:`EffectLedger.execute` did."""

    #: The committed effect row, or ``None`` when a prior result was replayed.
    record: EffectRecord | None
    replayed: bool
    result: dict[str, Any]


class EffectLedger:
    """Durable, duplicate-visible record of downstream executions."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    # -- connection -------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS effects (
                    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    effect_id TEXT NOT NULL UNIQUE,
                    operation_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    currency TEXT NOT NULL,
                    customer_id TEXT NOT NULL,
                    payment_id TEXT NOT NULL,
                    executed_at TEXT NOT NULL,
                    worker_pid INTEGER NOT NULL,
                    configuration TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS ix_effects_operation "
                "ON effects (operation_id)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS native_results (
                    operation_id TEXT PRIMARY KEY,
                    request_fingerprint TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    # -- effects ----------------------------------------------------------

    def execute(
        self,
        *,
        operation_id: str,
        request_id: str,
        idempotency_key: str,
        amount: int,
        currency: str,
        customer_id: str,
        payment_id: str,
        configuration: str,
        native_fingerprint: str | None = None,
        make_result: Callable[[EffectRecord], dict[str, Any]] | None = None,
    ) -> ExecutionOutcome:
        """Execute one business operation and commit the effect row.

        With ``native_fingerprint`` set, the tool behaves like a correctly
        built downstream: the lookup of a prior result, the effect insert, and
        the result store happen in **one** ``BEGIN IMMEDIATE`` transaction, so
        two concurrent requests for the same operation cannot both execute.
        A prior result with a different fingerprint is a conflict, never a
        silent second execution. Without it the tool is naive: every request
        is a new effect.
        """
        effect_id = f"effect_{uuid.uuid4().hex[:12]}"
        executed_at = _utc_now_iso()
        pid = os.getpid()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if native_fingerprint is not None:
                    prior = connection.execute(
                        "SELECT request_fingerprint, result_json FROM native_results "
                        "WHERE operation_id = ?",
                        (operation_id,),
                    ).fetchone()
                    if prior is not None:
                        if prior["request_fingerprint"] != native_fingerprint:
                            raise NativeConflictError(operation_id)
                        connection.execute("COMMIT")
                        return ExecutionOutcome(
                            record=None,
                            replayed=True,
                            result=json.loads(prior["result_json"]),
                        )
                connection.execute(
                    """
                    INSERT INTO effects (
                        effect_id, operation_id, request_id, idempotency_key,
                        amount, currency, customer_id, payment_id, executed_at,
                        worker_pid, configuration
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        effect_id,
                        operation_id,
                        request_id,
                        idempotency_key,
                        amount,
                        currency,
                        customer_id,
                        payment_id,
                        executed_at,
                        pid,
                        configuration,
                    ),
                )
                ordinal = connection.execute(
                    "SELECT COUNT(*) FROM effects WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()[0]
                record = EffectRecord(
                    effect_id=effect_id,
                    operation_id=operation_id,
                    request_id=request_id,
                    idempotency_key=idempotency_key,
                    amount=amount,
                    currency=currency,
                    customer_id=customer_id,
                    payment_id=payment_id,
                    executed_at=executed_at,
                    worker_pid=pid,
                    configuration=configuration,
                    execution_ordinal=int(ordinal),
                )
                result = make_result(record) if make_result else record.as_dict()
                if native_fingerprint is not None:
                    connection.execute(
                        "INSERT INTO native_results "
                        "(operation_id, request_fingerprint, result_json, created_at) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            operation_id,
                            native_fingerprint,
                            json.dumps(result, sort_keys=True),
                            executed_at,
                        ),
                    )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return ExecutionOutcome(record=record, replayed=False, result=result)

    def effects(self, operation_id: str | None = None) -> list[EffectRecord]:
        sql = (
            "SELECT effect_id, operation_id, request_id, idempotency_key, amount, "
            "currency, customer_id, payment_id, executed_at, worker_pid, "
            "configuration FROM effects"
        )
        params: tuple[Any, ...] = ()
        if operation_id is not None:
            sql += " WHERE operation_id = ?"
            params = (operation_id,)
        sql += " ORDER BY row_id"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        ordinals: dict[str, int] = {}
        records: list[EffectRecord] = []
        for row in rows:
            ordinals[row["operation_id"]] = ordinals.get(row["operation_id"], 0) + 1
            records.append(
                EffectRecord(
                    effect_id=row["effect_id"],
                    operation_id=row["operation_id"],
                    request_id=row["request_id"],
                    idempotency_key=row["idempotency_key"],
                    amount=int(row["amount"]),
                    currency=row["currency"],
                    customer_id=row["customer_id"],
                    payment_id=row["payment_id"],
                    executed_at=row["executed_at"],
                    worker_pid=int(row["worker_pid"]),
                    configuration=row["configuration"],
                    execution_ordinal=ordinals[row["operation_id"]],
                )
            )
        return records

    def execution_count(self, operation_id: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM effects"
        params: tuple[Any, ...] = ()
        if operation_id is not None:
            sql += " WHERE operation_id = ?"
            params = (operation_id,)
        with self._connect() as connection:
            return int(connection.execute(sql, params).fetchone()[0])

    def snapshot(self) -> list[dict[str, Any]]:
        """The PRD's ledger shape: one entry per effect with its execution count."""
        counts: dict[str, int] = {}
        for record in self.effects():
            counts[record.operation_id] = counts.get(record.operation_id, 0) + 1
        return [
            {
                "effect_id": record.effect_id,
                "operation_id": record.operation_id,
                "amount": record.amount,
                "currency": record.currency,
                "executed_at": record.executed_at,
                "execution_count": counts[record.operation_id],
                "execution_ordinal": record.execution_ordinal,
                "request_id": record.request_id,
                "idempotency_key": record.idempotency_key,
                "worker_pid": record.worker_pid,
                "configuration": record.configuration,
            }
            for record in self.effects()
        ]

    def reset(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM effects")
            connection.execute("DELETE FROM native_results")


__all__ = ["EffectLedger", "EffectRecord", "ExecutionOutcome", "NativeConflictError"]
