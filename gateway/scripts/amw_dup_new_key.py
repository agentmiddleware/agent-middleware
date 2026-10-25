#!/usr/bin/env python3
"""Read-only measurement of same-call repeats under a new idempotency key.

Counts how often the same governed MCP call arrives under a different
idempotency key within a time window. Read-only and aggregate-only: every
measurement query is a single SELECT/WITH...SELECT checked by
``assert_select_only``, and the outputs hold counts only, never raw
wallet ids, tool names, keys, or arguments.

Write calls are rows of ``mcp_dispatch_attempts`` joined to
``idempotency_records`` on ``idempotency_record_id``. A fingerprint is
``(wallet_id, public_tool_id, request_hash)`` from those stored columns
only; the script never reads raw arguments and never hashes them.

Usage:
    AMW_DS_READONLY_DATABASE_URL=<read-only-dsn> \\
        python gateway/scripts/amw_dup_new_key.py --out-dir /tmp/amw-dup

Limits (by design):

- Needs explicit owner approval and a dedicated read-only credential in
  ``AMW_DS_READONLY_DATABASE_URL``. Do not use credentials found on disk
  and do not point this at a writable or primary DSN.
- ``request_hash`` covers ``{tool_name, arguments, wallet_id, permit_id}``
  and excludes the idempotency key, so the same call under a different
  permit is a different fingerprint and is NOT counted. That undercount
  is accepted: no argument re-hashing, no fallback key matching, and no
  dedup logic in the gateway.
- A pair is two consecutive calls of one fingerprint, ordered by
  ``created_at``, whose idempotency keys differ with gap <= window.
"""

import argparse
import asyncio
import contextlib
import csv
import io
import math
import os
import re
import sqlite3
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy import Connection, CursorResult, Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

ENV_VAR = "AMW_DS_READONLY_DATABASE_URL"
AUTO_PREFIX = "mcp-auto-"
_AUTO_PREFIX_LEN = len(AUTO_PREFIX)
BUCKETS: tuple[str, ...] = ("0-1", "1-5", "5-10", "10-30", "30-60", "60+")
CSV_FILENAME = "dup_pairs.csv"
SUMMARY_FILENAME = "summary.md"
CSV_HEADER: tuple[str, ...] = ("metric", "key_kind", "bucket", "count")
STATEMENT_TIMEOUT_SECONDS_DEFAULT = 300.0
POSTGRES_SET_TRANSACTION = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
POSTGRES_SESSION_CHARACTERISTICS = (
    "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"
)
POSTGRES_SHOW_READ_ONLY = "SHOW transaction_read_only"
POSTGRES_SNAPSHOT = (
    "one REPEATABLE READ READ ONLY transaction on a single connection"
    " (all aggregate queries share one snapshot)"
)
SQLITE_SNAPSHOT = "single read-only SQLite connection"

_FORBIDDEN_KEYWORDS: tuple[str, ...] = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "CREATE",
    "TRUNCATE",
    "GRANT",
    "COPY",
    "MERGE",
    "CALL",
    "DO",
    "SET",
)
_FORBIDDEN_RES = tuple(
    re.compile(rf"\b{keyword}\b", re.IGNORECASE) for keyword in _FORBIDDEN_KEYWORDS
)
_SELECT_RE = re.compile(r"\bSELECT\b", re.IGNORECASE)
_FIRST_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_GAP_EXPR = {
    "postgresql": "EXTRACT(EPOCH FROM (created_at - prev_created_at))",
    "sqlite": "((julianday(created_at) - julianday(prev_created_at)) * 86400.0)",
}

_LIMITS_BLOCK = """\
## Limits

- This run needed explicit owner approval and a dedicated read-only
  credential in AMW_DS_READONLY_DATABASE_URL. Never use credentials found
  on disk and never point this script at a writable or primary DSN.
- request_hash covers {tool_name, arguments, wallet_id, permit_id} and
  excludes the idempotency key, so the same call under a different permit
  is a different fingerprint and is NOT counted. That undercount is by
  design: no argument re-hashing, no fallback key matching, and no dedup
  logic in the gateway.
- A pair is two consecutive calls of one fingerprint, ordered by
  created_at, whose idempotency keys differ with gap <= window.
"""


def _strip_leading_comments(body: str) -> str:
    core = body.lstrip()
    while True:
        if core.startswith("--"):
            end = core.find("\n")
            core = "" if end == -1 else core[end + 1 :]
        elif core.startswith("/*"):
            end = core.find("*/")
            if end == -1:
                return ""
            core = core[end + 2 :]
        else:
            return core
        core = core.lstrip()


def assert_select_only(sql: str) -> None:
    """Reject any statement that is not a single SELECT/WITH...SELECT.

    Raises:
        ValueError: If the statement is empty, holds more than one
            statement, does not start with SELECT (or WITH...SELECT), or
            contains a data-modifying keyword anywhere (which also rules
            out WITH queries holding data-modifying CTEs).
    """
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("empty SQL statement is not allowed")
    body = sql.strip()
    text_body = body[:-1] if body.endswith(";") else body
    if ";" in text_body:
        raise ValueError("multiple statements are not allowed")
    first = _FIRST_WORD_RE.match(_strip_leading_comments(text_body))
    if first is None:
        raise ValueError("statement must start with SELECT or WITH")
    keyword = first.group(0).upper()
    if keyword == "SELECT":
        pass
    elif keyword == "WITH":
        if _SELECT_RE.search(text_body) is None:
            raise ValueError("WITH query without SELECT is not allowed")
    else:
        raise ValueError(f"only SELECT/WITH queries are allowed, got {keyword}")
    for word, pattern in zip(_FORBIDDEN_KEYWORDS, _FORBIDDEN_RES, strict=True):
        if pattern.search(text_body) is not None:
            raise ValueError(f"forbidden keyword is not allowed: {word}")


def execute_select(
    conn: Connection, sql: str, params: Mapping[str, Any] | None = None
) -> CursorResult[Any]:
    """Run one guarded read-only query. All measurement queries use this."""
    assert_select_only(sql)
    return conn.execute(text(sql), dict(params or {}))


async def execute_select_async(
    conn: AsyncConnection,
    sql: str,
    params: Mapping[str, Any] | None = None,
) -> CursorResult[Any]:
    """Async variant of ``execute_select`` for the PostgreSQL path."""
    assert_select_only(sql)
    return await conn.execute(text(sql), dict(params or {}))


class _NotMeasured(Exception):
    """Fixed-reason measurement abort. The reason never holds secrets."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def normalize_postgres_url_to_asyncpg(url: str) -> str:
    """Map a PostgreSQL DSN to its ``postgresql+asyncpg://`` spelling.

    Accepts ``postgres://``, ``postgresql://``, and any
    ``postgresql+<driver>://`` spelling (including ``+psycopg``,
    ``+psycopg2``, and the already-correct ``+asyncpg``). Only the scheme
    is changed; user, password, host, port, database, and query survive
    untouched. The gateway only ships asyncpg, so the PostgreSQL path
    must always connect through this spelling.
    """
    trimmed = url.strip()
    marker = "://"
    index = trimmed.find(marker)
    if index == -1:
        raise ValueError("unsupported database")
    scheme = trimmed[:index].lower()
    rest = trimmed[index + len(marker) :]
    if scheme.split("+")[0] not in ("postgres", "postgresql"):
        raise ValueError("unsupported database")
    return "postgresql+asyncpg://" + rest


def postgres_statement_timeout_sql(statement_timeout_seconds: float) -> str:
    """Render the fenced ``SET LOCAL statement_timeout`` statement."""
    if (
        not isinstance(statement_timeout_seconds, (int, float))
        or not math.isfinite(float(statement_timeout_seconds))
        or float(statement_timeout_seconds) < 0
    ):
        raise ValueError("invalid statement timeout")
    millis = int(round(float(statement_timeout_seconds) * 1000))
    return f"SET LOCAL statement_timeout = {millis}"


def build_postgres_fence_statements(
    *, statement_timeout_seconds: float = STATEMENT_TIMEOUT_SECONDS_DEFAULT
) -> list[str]:
    """Ordered fence statements for the single measurement transaction.

    PostgreSQL forbids ``SET TRANSACTION`` after any query in the
    transaction, so these run first, in this order, before any ``SELECT``:
    ``SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY``, the
    session characteristics fence, ``SET LOCAL statement_timeout``, then
    ``SHOW transaction_read_only`` (verified to be ``on``).
    """
    return [
        POSTGRES_SET_TRANSACTION,
        POSTGRES_SESSION_CHARACTERISTICS,
        postgres_statement_timeout_sql(statement_timeout_seconds),
        POSTGRES_SHOW_READ_ONLY,
    ]


def create_sqlite_readonly_engine(url: str) -> Engine:
    """Create a read-only SQLite engine (tests only, file opens ``mode=ro``)."""
    parsed = make_url(url)
    database = parsed.database or ""
    if database == ":memory:":
        return create_engine("sqlite:///:memory:")
    encoded = quote(database, safe="/:")

    def _connect() -> sqlite3.Connection:
        return sqlite3.connect(f"file:{encoded}?mode=ro", uri=True)

    return create_engine("sqlite://", creator=_connect)


def create_postgres_async_engine(url: str) -> AsyncEngine:
    """Create the PostgreSQL async engine (always via asyncpg)."""
    return create_async_engine(normalize_postgres_url_to_asyncpg(url))


def create_readonly_engine(url: str) -> Engine | AsyncEngine:
    """Create an engine for a read-only DSN.

    SQLite (tests only) opens the file with ``mode=ro`` so writes fail.
    PostgreSQL is normalised to ``postgresql+asyncpg://`` and uses an
    async engine; its connections are fenced with read-only transaction
    state as the first statements of the measurement transaction.
    """
    parsed = make_url(url)
    base = parsed.drivername.split("+")[0]
    if base in ("postgres", "postgresql"):
        return create_postgres_async_engine(url)
    if base != "sqlite":
        return create_engine(url)
    return create_sqlite_readonly_engine(url)


def _enforce_postgres_readonly(
    conn: Connection,
    *,
    statement_timeout_seconds: float = STATEMENT_TIMEOUT_SECONDS_DEFAULT,
) -> None:
    # Fixed literal statements that fence the transaction read-only. They
    # run before any measurement query and intentionally bypass
    # assert_select_only, which forbids SET for measurement queries.
    statements = build_postgres_fence_statements(
        statement_timeout_seconds=statement_timeout_seconds
    )
    try:
        conn.execute(text(statements[0]))
        conn.execute(text(statements[1]))
        conn.execute(text(statements[2]))
        mode = conn.execute(text(statements[3])).scalar()
    except Exception as exc:
        raise _NotMeasured("read-only setup failed") from exc
    if str(mode).strip().lower() != "on":
        raise _NotMeasured("read-only verification failed")


async def _enforce_postgres_readonly_async(
    conn: AsyncConnection,
    *,
    statement_timeout_seconds: float = STATEMENT_TIMEOUT_SECONDS_DEFAULT,
) -> None:
    """Async fence; same ordered statements as ``_enforce_postgres_readonly``."""
    statements = build_postgres_fence_statements(
        statement_timeout_seconds=statement_timeout_seconds
    )
    try:
        await conn.execute(text(statements[0]))
        await conn.execute(text(statements[1]))
        await conn.execute(text(statements[2]))
        result = await conn.execute(text(statements[3]))
        mode = result.scalar()
    except Exception as exc:
        raise _NotMeasured("read-only setup failed") from exc
    if str(mode).strip().lower() != "on":
        raise _NotMeasured("read-only verification failed")


@dataclass(frozen=True)
class QuerySet:
    """Exact SQL run for each reported count."""

    total: str
    date_range: str
    pairs: str
    hist: str
    later_kind: str
    combo: str


def build_queries(
    dialect: str, *, since: datetime | None, until: datetime | None
) -> QuerySet:
    """Build the dialect-specific measurement queries.

    Args:
        dialect: ``"postgresql"`` or ``"sqlite"`` (gap arithmetic differs).
        since: Optional inclusive lower bound on dispatch ``created_at``.
        until: Optional inclusive upper bound on dispatch ``created_at``.
    """
    if dialect not in _GAP_EXPR:
        raise ValueError(f"unsupported dialect: {dialect}")
    filters = []
    if since is not None:
        filters.append("a.created_at >= :since")
    if until is not None:
        filters.append("a.created_at <= :until")
    where_sql = ""
    if filters:
        where_sql = "\n            WHERE " + " AND ".join(filters)
    calls = (
        "calls AS (\n"
        "        SELECT\n"
        "            a.attempt_id AS attempt_id,\n"
        "            a.wallet_id AS wallet_id,\n"
        "            a.public_tool_id AS tool,\n"
        "            a.request_hash AS request_hash,\n"
        "            r.idempotency_key AS idempotency_key,\n"
        "            a.created_at AS created_at\n"
        "        FROM mcp_dispatch_attempts AS a\n"
        "        JOIN idempotency_records AS r\n"
        f"            ON r.record_id = a.idempotency_record_id{where_sql}\n"
        "    )"
    )
    lagged = (
        "lagged AS (\n"
        "        SELECT\n"
        "            wallet_id,\n"
        "            tool,\n"
        "            request_hash,\n"
        "            idempotency_key,\n"
        "            created_at,\n"
        "            LAG(idempotency_key) OVER (\n"
        "                PARTITION BY wallet_id, tool, request_hash\n"
        "                ORDER BY created_at, attempt_id\n"
        "            ) AS prev_key,\n"
        "            LAG(created_at) OVER (\n"
        "                PARTITION BY wallet_id, tool, request_hash\n"
        "                ORDER BY created_at, attempt_id\n"
        "            ) AS prev_created_at\n"
        "        FROM calls\n"
        "    )"
    )
    gap = _GAP_EXPR[dialect]
    keyed = (
        "keyed AS (\n"
        "        SELECT\n"
        "            idempotency_key,\n"
        "            prev_key,\n"
        f"            {gap} AS gap_seconds\n"
        "        FROM lagged\n"
        "    )"
    )
    pairs_cte = (
        "pairs AS (\n"
        "        SELECT idempotency_key, prev_key, gap_seconds\n"
        "        FROM keyed\n"
        "        WHERE prev_key IS NOT NULL\n"
        "          AND idempotency_key <> prev_key\n"
        "          AND gap_seconds <= :window\n"
        "    )"
    )
    full = f"WITH {calls},\n    {lagged},\n    {keyed},\n    {pairs_cte}"
    total = f"WITH {calls}\nSELECT COUNT(*) AS n FROM calls"
    date_range = (
        f"WITH {calls}\n"
        "SELECT MIN(created_at) AS lo, MAX(created_at) AS hi,"
        " COUNT(*) AS n FROM calls"
    )
    pairs = f"{full}\nSELECT COUNT(*) AS n FROM pairs"
    hist = (
        f"{full}\n"
        "SELECT CASE WHEN gap_seconds <= 1 THEN '0-1'"
        " WHEN gap_seconds <= 5 THEN '1-5'"
        " WHEN gap_seconds <= 10 THEN '5-10'"
        " WHEN gap_seconds <= 30 THEN '10-30'"
        " WHEN gap_seconds <= 60 THEN '30-60'"
        " ELSE '60+' END AS bucket, COUNT(*) AS n"
        " FROM pairs GROUP BY 1 ORDER BY 1"
    )
    auto_later = f"substr(idempotency_key, 1, {_AUTO_PREFIX_LEN}) = '{AUTO_PREFIX}'"
    auto_prev = f"substr(prev_key, 1, {_AUTO_PREFIX_LEN}) = '{AUTO_PREFIX}'"
    caller_later = f"substr(idempotency_key, 1, {_AUTO_PREFIX_LEN}) <> '{AUTO_PREFIX}'"
    caller_prev = f"substr(prev_key, 1, {_AUTO_PREFIX_LEN}) <> '{AUTO_PREFIX}'"
    later_kind = (
        f"{full}\n"
        f"SELECT CASE WHEN {auto_later} THEN 'auto' ELSE 'caller' END AS kind,"
        " COUNT(*) AS n FROM pairs GROUP BY 1 ORDER BY 1"
    )
    combo = (
        f"{full}\n"
        f"SELECT CASE WHEN {auto_later} AND {auto_prev} THEN 'both-auto'"
        f" WHEN {caller_later} AND {caller_prev} THEN 'both-caller'"
        " ELSE 'mixed' END AS combo, COUNT(*) AS n FROM pairs GROUP BY 1 ORDER BY 1"
    )
    return QuerySet(
        total=total,
        date_range=date_range,
        pairs=pairs,
        hist=hist,
        later_kind=later_kind,
        combo=combo,
    )


@dataclass
class Measurement:
    """Aggregate counts plus the exact SQL that produced them."""

    total_calls: int
    date_lo: str | None
    date_hi: str | None
    rows_read: int
    pairs_total: int
    hist: dict[str, int]
    later_kind: dict[str, int]
    combo: dict[str, int]
    queries: QuerySet


def run_measurement(
    conn: Connection,
    queries: QuerySet,
    *,
    since: datetime | None,
    until: datetime | None,
    window: float,
) -> Measurement:
    """Run each query in ``queries`` and collect aggregate counts."""
    base: dict[str, Any] = {}
    if since is not None:
        base["since"] = since
    if until is not None:
        base["until"] = until
    full = dict(base)
    full["window"] = window
    try:
        total = int(execute_select(conn, queries.total, base).scalar() or 0)
        span = execute_select(conn, queries.date_range, base).mappings().one()
        pairs_total = int(execute_select(conn, queries.pairs, full).scalar() or 0)
        hist_rows = execute_select(conn, queries.hist, full).mappings().all()
        later_rows = execute_select(conn, queries.later_kind, full).mappings().all()
        combo_rows = execute_select(conn, queries.combo, full).mappings().all()
    except ValueError:
        raise
    except Exception as exc:
        raise _NotMeasured("query failed") from exc
    return _collect_measurement(
        total=total,
        span=span,
        pairs_total=pairs_total,
        hist_rows=hist_rows,
        later_rows=later_rows,
        combo_rows=combo_rows,
        queries=queries,
    )


def _collect_measurement(
    *,
    total: int,
    span: Any,
    pairs_total: int,
    hist_rows: Any,
    later_rows: Any,
    combo_rows: Any,
    queries: QuerySet,
) -> Measurement:
    hist = dict.fromkeys(BUCKETS, 0)
    for row in hist_rows:
        if row["bucket"] in hist:
            hist[row["bucket"]] = int(row["n"])
    later_kind = {"auto": 0, "caller": 0}
    for row in later_rows:
        if row["kind"] in later_kind:
            later_kind[row["kind"]] = int(row["n"])
    combo = {"both-auto": 0, "mixed": 0, "both-caller": 0}
    for row in combo_rows:
        if row["combo"] in combo:
            combo[row["combo"]] = int(row["n"])
    date_lo = span["lo"]
    date_hi = span["hi"]
    return Measurement(
        total_calls=total,
        date_lo=None if date_lo is None else str(date_lo),
        date_hi=None if date_hi is None else str(date_hi),
        rows_read=int(span["n"] or 0),
        pairs_total=pairs_total,
        hist=hist,
        later_kind=later_kind,
        combo=combo,
        queries=queries,
    )


async def run_measurement_async(
    conn: AsyncConnection,
    queries: QuerySet,
    *,
    since: datetime | None,
    until: datetime | None,
    window: float,
) -> Measurement:
    """Async variant of ``run_measurement`` for the PostgreSQL path."""
    base: dict[str, Any] = {}
    if since is not None:
        base["since"] = since
    if until is not None:
        base["until"] = until
    full = dict(base)
    full["window"] = window
    try:
        total_result = await execute_select_async(conn, queries.total, base)
        total = int(total_result.scalar() or 0)
        span_result = await execute_select_async(conn, queries.date_range, base)
        span = span_result.mappings().one()
        pairs_result = await execute_select_async(conn, queries.pairs, full)
        pairs_total = int(pairs_result.scalar() or 0)
        hist_result = await execute_select_async(conn, queries.hist, full)
        hist_rows = hist_result.mappings().all()
        later_result = await execute_select_async(conn, queries.later_kind, full)
        later_rows = later_result.mappings().all()
        combo_result = await execute_select_async(conn, queries.combo, full)
        combo_rows = combo_result.mappings().all()
    except ValueError:
        raise
    except Exception as exc:
        raise _NotMeasured("query failed") from exc
    return _collect_measurement(
        total=total,
        span=span,
        pairs_total=pairs_total,
        hist_rows=hist_rows,
        later_rows=later_rows,
        combo_rows=combo_rows,
        queries=queries,
    )


def run_postgres_measurement_sync(
    conn: Connection,
    queries: QuerySet,
    *,
    since: datetime | None,
    until: datetime | None,
    window: float,
    statement_timeout_seconds: float = STATEMENT_TIMEOUT_SECONDS_DEFAULT,
) -> Measurement:
    """Fence then measure on one sync connection (single-transaction shape)."""
    _enforce_postgres_readonly(
        conn, statement_timeout_seconds=statement_timeout_seconds
    )
    return run_measurement(conn, queries, since=since, until=until, window=window)


async def run_postgres_measurement_async(
    conn: AsyncConnection,
    queries: QuerySet,
    *,
    since: datetime | None,
    until: datetime | None,
    window: float,
    statement_timeout_seconds: float = STATEMENT_TIMEOUT_SECONDS_DEFAULT,
) -> Measurement:
    """Fence then measure on one async connection (single-transaction shape)."""
    await _enforce_postgres_readonly_async(
        conn, statement_timeout_seconds=statement_timeout_seconds
    )
    return await run_measurement_async(
        conn, queries, since=since, until=until, window=window
    )


async def _postgres_single_transaction(
    normalized_url: str,
    queries: QuerySet,
    *,
    since: datetime | None,
    until: datetime | None,
    window: float,
    statement_timeout_seconds: float,
) -> Measurement:
    """Run the fence plus all aggregates in one REPEATABLE READ transaction."""
    try:
        engine = create_async_engine(normalized_url)
    except Exception as exc:
        raise _NotMeasured("connection failed") from exc
    try:
        try:
            async with engine.begin() as conn:
                return await run_postgres_measurement_async(
                    conn,
                    queries,
                    since=since,
                    until=until,
                    window=window,
                    statement_timeout_seconds=statement_timeout_seconds,
                )
        except _NotMeasured:
            raise
        except ValueError:
            raise
        except Exception as exc:
            raise _NotMeasured("connection failed") from exc
    finally:
        with contextlib.suppress(Exception):
            await engine.dispose()


def render_csv(measurement: Measurement) -> str:
    """Aggregate-only CSV with columns metric,key_kind,bucket,count."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(list(CSV_HEADER))
    writer.writerow(["total_calls", "", "", measurement.total_calls])
    writer.writerow(["pairs_total", "", "", measurement.pairs_total])
    for bucket in BUCKETS:
        writer.writerow(["pairs_hist", "", bucket, measurement.hist[bucket]])
    for kind in ("auto", "caller"):
        writer.writerow(["pairs_later_kind", kind, "", measurement.later_kind[kind]])
    for combo in ("both-auto", "mixed", "both-caller"):
        writer.writerow(["pairs_combo", combo, "", measurement.combo[combo]])
    return buf.getvalue()


def render_csv_empty() -> str:
    """Header-only CSV for the not-measured case (no counts to report)."""
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerow(list(CSV_HEADER))
    return buf.getvalue()


def _params_block(
    *, window: float, since: datetime | None, until: datetime | None
) -> str:
    since_text = since.isoformat() if since is not None else "-"
    until_text = until.isoformat() if until is not None else "-"
    return (
        f"- window_seconds: {window:g}\n"
        f"- since: {since_text}\n"
        f"- until: {until_text}\n"
        "- source: mcp_dispatch_attempts JOIN idempotency_records"
        " ON r.record_id = a.idempotency_record_id\n"
    )


def render_summary(
    measurement: Measurement,
    *,
    window: float,
    since: datetime | None,
    until: datetime | None,
    snapshot: str = POSTGRES_SNAPSHOT,
) -> str:
    """Markdown with each count next to the exact SQL that produced it."""
    generated = datetime.now(timezone.utc).isoformat()
    queries = measurement.queries
    date_lo = measurement.date_lo or "-"
    date_hi = measurement.date_hi or "-"
    hist_lines = ["| bucket | count |", "|---|---|"]
    hist_lines.extend(
        f"| {bucket} | {measurement.hist[bucket]} |" for bucket in BUCKETS
    )
    later_lines = ["| later key kind | count |", "|---|---|"]
    later_lines.extend(
        f"| {kind} | {measurement.later_kind[kind]} |" for kind in ("auto", "caller")
    )
    combo_lines = ["| key pair | count |", "|---|---|"]
    combo_lines.extend(
        f"| {combo} | {measurement.combo[combo]} |"
        for combo in ("both-auto", "mixed", "both-caller")
    )
    parts = [
        "# AMW duplicate-key measurement",
        "",
        f"- generated_at: {generated}",
        _params_block(window=window, since=since, until=until).rstrip("\n"),
        f"- snapshot: {snapshot}",
        f"- rows_read: {measurement.rows_read}",
        f"- date_range: {date_lo} to {date_hi} (dispatch created_at)",
        "",
        f"## total_calls: {measurement.total_calls}",
        "",
        "```sql",
        queries.total,
        "```",
        "",
        f"## pairs_total: {measurement.pairs_total}",
        "",
        "```sql",
        queries.pairs,
        "```",
        "",
        "## pairs_hist",
        "",
        "\n".join(hist_lines),
        "",
        "```sql",
        queries.hist,
        "```",
        "",
        "## pairs_later_kind",
        "",
        "\n".join(later_lines),
        "",
        "```sql",
        queries.later_kind,
        "```",
        "",
        "## pairs_combo",
        "",
        "\n".join(combo_lines),
        "",
        "```sql",
        queries.combo,
        "```",
        "",
        "## date_range detail",
        "",
        f"- min_created_at: {date_lo}",
        f"- max_created_at: {date_hi}",
        f"- rows_read: {measurement.rows_read}",
        "",
        "```sql",
        queries.date_range,
        "```",
        "",
        _LIMITS_BLOCK.rstrip("\n"),
        "",
    ]
    return "\n".join(parts)


def render_not_measured_summary(
    reason: str,
    *,
    window: float,
    since: datetime | None,
    until: datetime | None,
) -> str:
    """Summary for the not-measured case. ``reason`` is a fixed string."""
    generated = datetime.now(timezone.utc).isoformat()
    parts = [
        "# AMW duplicate-key measurement",
        "",
        f"Result: not measured: {reason}",
        "",
        f"- generated_at: {generated}",
        _params_block(window=window, since=since, until=until).rstrip("\n"),
        "",
        "No queries were run and no counts are reported.",
        "",
        _LIMITS_BLOCK.rstrip("\n"),
        "",
    ]
    return "\n".join(parts)


def _atomic_write_text(path: Path, content: str) -> None:
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _parse_time(value: str) -> datetime:
    cleaned = value.strip()
    if cleaned.endswith(("Z", "z")):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid datetime: {value}") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI args. Only ``--out-dir`` is required."""
    parser = argparse.ArgumentParser(
        prog="amw_dup_new_key.py",
        description="Read-only count of same-call repeats under a new key.",
    )
    parser.add_argument(
        "--out-dir",
        required=True,
        help="Directory for dup_pairs.csv and summary.md.",
    )
    parser.add_argument(
        "--window-seconds",
        type=float,
        default=60.0,
        help="Max gap in seconds between consecutive same-fingerprint calls.",
    )
    parser.add_argument(
        "--since",
        type=_parse_time,
        default=None,
        help="Only calls at or after this ISO 8601 time.",
    )
    parser.add_argument(
        "--until",
        type=_parse_time,
        default=None,
        help="Only calls at or before this ISO 8601 time.",
    )
    parser.add_argument(
        "--statement-timeout-seconds",
        type=float,
        default=STATEMENT_TIMEOUT_SECONDS_DEFAULT,
        help="PostgreSQL SET LOCAL statement_timeout for the measurement.",
    )
    args = parser.parse_args(argv)
    if args.window_seconds < 0:
        parser.error("--window-seconds must be >= 0")
    timeout = float(args.statement_timeout_seconds)
    if not math.isfinite(timeout) or timeout < 0:
        parser.error("--statement-timeout-seconds must be >= 0")
    return args


def _write_not_measured(
    out_dir: Path,
    *,
    window: float,
    since: datetime | None,
    until: datetime | None,
    reason: str,
) -> int:
    print(f"not measured: {reason}")
    _atomic_write_text(
        out_dir / SUMMARY_FILENAME,
        render_not_measured_summary(reason, window=window, since=since, until=until),
    )
    _atomic_write_text(out_dir / CSV_FILENAME, render_csv_empty())
    return 0


@contextlib.contextmanager
def _ro_session(engine: Engine, dialect: str) -> Iterator[Connection]:
    if dialect == "postgresql":
        with engine.begin() as conn:
            yield conn
    else:
        with engine.connect() as conn:
            yield conn


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns 0 for both measured and not-measured runs."""
    args = parse_args(argv)
    out_dir = Path(args.out_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        print("error: cannot create output directory", file=sys.stderr)
        return 1
    window = float(args.window_seconds)
    since = args.since
    until = args.until
    statement_timeout_seconds = float(args.statement_timeout_seconds)
    # The DSN comes only from this variable. Nothing else in the
    # environment or on disk is consulted, and the value is never printed.
    url = os.environ.get(ENV_VAR)
    if url is None or not url.strip():
        return _write_not_measured(
            out_dir,
            window=window,
            since=since,
            until=until,
            reason=f"{ENV_VAR} is not set",
        )
    cleaned = url.strip()
    try:
        drivername = make_url(cleaned).drivername
    except Exception:
        return _write_not_measured(
            out_dir,
            window=window,
            since=since,
            until=until,
            reason="connection failed",
        )
    base = drivername.split("+")[0]
    if base == "sqlite":
        dialect = "sqlite"
    elif base in ("postgresql", "postgres"):
        dialect = "postgresql"
    else:
        return _write_not_measured(
            out_dir,
            window=window,
            since=since,
            until=until,
            reason="unsupported database",
        )
    queries = build_queries(dialect, since=since, until=until)
    if dialect == "postgresql":
        try:
            normalized = normalize_postgres_url_to_asyncpg(cleaned)
        except Exception:
            return _write_not_measured(
                out_dir,
                window=window,
                since=since,
                until=until,
                reason="connection failed",
            )
        try:
            measurement = asyncio.run(
                _postgres_single_transaction(
                    normalized,
                    queries,
                    since=since,
                    until=until,
                    window=window,
                    statement_timeout_seconds=statement_timeout_seconds,
                )
            )
        except _NotMeasured as exc:
            return _write_not_measured(
                out_dir, window=window, since=since, until=until, reason=exc.reason
            )
        except ValueError:
            raise
        except Exception:
            return _write_not_measured(
                out_dir, window=window, since=since, until=until, reason="query failed"
            )
        snapshot = POSTGRES_SNAPSHOT
    else:
        try:
            engine = create_sqlite_readonly_engine(cleaned)
        except Exception:
            return _write_not_measured(
                out_dir,
                window=window,
                since=since,
                until=until,
                reason="connection failed",
            )
        try:
            with _ro_session(engine, dialect) as conn:
                measurement = run_measurement(
                    conn, queries, since=since, until=until, window=window
                )
        except _NotMeasured as exc:
            return _write_not_measured(
                out_dir, window=window, since=since, until=until, reason=exc.reason
            )
        except ValueError:
            raise
        except Exception:
            return _write_not_measured(
                out_dir, window=window, since=since, until=until, reason="query failed"
            )
        finally:
            with contextlib.suppress(Exception):
                engine.dispose()
        snapshot = SQLITE_SNAPSHOT
    _atomic_write_text(out_dir / CSV_FILENAME, render_csv(measurement))
    _atomic_write_text(
        out_dir / SUMMARY_FILENAME,
        render_summary(
            measurement, window=window, since=since, until=until, snapshot=snapshot
        ),
    )
    print(f"total_calls: {measurement.total_calls}")
    print(f"pairs_total: {measurement.pairs_total}")
    print(f"rows_read: {measurement.rows_read}")
    print(f"date_range: {measurement.date_lo or '-'} to {measurement.date_hi or '-'}")
    print(f"out_dir: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
