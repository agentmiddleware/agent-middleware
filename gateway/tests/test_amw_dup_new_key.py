"""Data-free tests for gateway/scripts/amw_dup_new_key.py.

Every database here is a hand-built temp SQLite file holding fixed
synthetic values only (wal_test_*, k-*, mcp-auto-*, example.invalid).
No real data, no network, no shared database.
"""

import asyncio
import csv
import importlib.util
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "amw_dup_new_key.py"


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("amw_dup_new_key", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


amw = _load_script()

WALLET_1 = "wal_test_1"
WALLET_2 = "wal_test_2"
TOOL_A = "tool_alpha"
TOOL_B = "tool_beta"
HASH_A = "reqhash_aaa_test_0001"
HASH_B = "reqhash_bbb_test_0002"
AUTO_1 = "mcp-auto-00000000000000000000000000000001"
AUTO_2 = "mcp-auto-00000000000000000000000000000002"
BASE = datetime(2026, 1, 1, 0, 0, 0)

SCHEMA = """\
CREATE TABLE mcp_dispatch_attempts (
    attempt_id TEXT PRIMARY KEY,
    idempotency_record_id TEXT NOT NULL,
    wallet_id TEXT NOT NULL,
    public_tool_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE idempotency_records (
    record_id TEXT PRIMARY KEY,
    wallet_id TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S.%f")


def _insert_call(
    conn: sqlite3.Connection,
    *,
    attempt: str,
    record: str,
    wallet: str,
    tool: str,
    req_hash: str,
    key: str,
    created: datetime,
) -> None:
    stamped = _stamp(created)
    conn.execute(
        "INSERT INTO idempotency_records"
        " (record_id, wallet_id, endpoint, idempotency_key, request_hash, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (record, wallet, "/mcp/invoke", key, req_hash, stamped),
    )
    conn.execute(
        "INSERT INTO mcp_dispatch_attempts"
        " (attempt_id, idempotency_record_id, wallet_id, public_tool_id,"
        " request_hash, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (attempt, record, wallet, tool, req_hash, stamped),
    )


def _build_standard_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        base = BASE
        _insert_call(
            conn,
            attempt="att-a1",
            record="rec-a1",
            wallet=WALLET_1,
            tool=TOOL_A,
            req_hash=HASH_A,
            key="k-1",
            created=base,
        )
        _insert_call(
            conn,
            attempt="att-a2",
            record="rec-a2",
            wallet=WALLET_1,
            tool=TOOL_A,
            req_hash=HASH_A,
            key="k-2",
            created=base + timedelta(seconds=2),
        )
        _insert_call(
            conn,
            attempt="att-a3",
            record="rec-a3",
            wallet=WALLET_1,
            tool=TOOL_A,
            req_hash=HASH_A,
            key=AUTO_1,
            created=base + timedelta(seconds=9),
        )
        _insert_call(
            conn,
            attempt="att-a4",
            record="rec-a4",
            wallet=WALLET_1,
            tool=TOOL_A,
            req_hash=HASH_A,
            key=AUTO_1,
            created=base + timedelta(seconds=10),
        )
        _insert_call(
            conn,
            attempt="att-a5",
            record="rec-a5",
            wallet=WALLET_1,
            tool=TOOL_A,
            req_hash=HASH_A,
            key=AUTO_2,
            created=base + timedelta(seconds=41),
        )
        _insert_call(
            conn,
            attempt="att-b1",
            record="rec-b1",
            wallet=WALLET_1,
            tool=TOOL_B,
            req_hash=HASH_B,
            key="k-9",
            created=base,
        )
        _insert_call(
            conn,
            attempt="att-b2",
            record="rec-b2",
            wallet=WALLET_1,
            tool=TOOL_B,
            req_hash=HASH_B,
            key="k-10",
            created=base + timedelta(seconds=20),
        )
        _insert_call(
            conn,
            attempt="att-b3",
            record="rec-b3",
            wallet=WALLET_1,
            tool=TOOL_B,
            req_hash=HASH_B,
            key="k-11",
            created=base + timedelta(seconds=120),
        )
        _insert_call(
            conn,
            attempt="att-c1",
            record="rec-c1",
            wallet=WALLET_2,
            tool=TOOL_A,
            req_hash=HASH_A,
            key="k-1",
            created=base,
        )
        _insert_call(
            conn,
            attempt="att-c2",
            record="rec-c2",
            wallet=WALLET_2,
            tool=TOOL_A,
            req_hash=HASH_A,
            key="k-1",
            created=base + timedelta(seconds=1),
        )
        _insert_call(
            conn,
            attempt="att-c3",
            record="rec-c3",
            wallet=WALLET_2,
            tool=TOOL_A,
            req_hash=HASH_A,
            key="k-2",
            created=base + timedelta(seconds=1, milliseconds=500),
        )
        conn.commit()
    finally:
        conn.close()


def _run_script(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    out_dir: Path,
    *extra_args: str,
) -> int:
    monkeypatch.setenv(amw.ENV_VAR, f"sqlite:///{db_path}")
    return int(amw.main(["--out-dir", str(out_dir), *list(extra_args)]))


def _read_csv(out_dir: Path) -> dict[tuple[str, str, str], int]:
    counts: dict[tuple[str, str, str], int] = {}
    with (out_dir / "dup_pairs.csv").open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == ["metric", "key_kind", "bucket", "count"]
        for row in reader:
            counts[(row["metric"], row["key_kind"], row["bucket"])] = int(row["count"])
    return counts


def test_pairs_histogram_and_key_splits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "dup.db"
    out_dir = tmp_path / "out"
    _build_standard_db(db_path)
    assert _run_script(monkeypatch, db_path, out_dir) == 0
    counts = _read_csv(out_dir)
    assert counts[("total_calls", "", "")] == 11
    assert counts[("pairs_total", "", "")] == 5
    assert counts[("pairs_hist", "", "0-1")] == 1
    assert counts[("pairs_hist", "", "1-5")] == 1
    assert counts[("pairs_hist", "", "5-10")] == 1
    assert counts[("pairs_hist", "", "10-30")] == 1
    assert counts[("pairs_hist", "", "30-60")] == 1
    assert counts[("pairs_hist", "", "60+")] == 0
    assert counts[("pairs_later_kind", "auto", "")] == 2
    assert counts[("pairs_later_kind", "caller", "")] == 3
    assert counts[("pairs_combo", "both-auto", "")] == 1
    assert counts[("pairs_combo", "mixed", "")] == 1
    assert counts[("pairs_combo", "both-caller", "")] == 3


def test_window_is_respected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "dup.db"
    _build_standard_db(db_path)
    narrow = tmp_path / "narrow"
    assert _run_script(monkeypatch, db_path, narrow, "--window-seconds", "5") == 0
    narrow_counts = _read_csv(narrow)
    assert narrow_counts[("pairs_total", "", "")] == 2
    assert narrow_counts[("pairs_hist", "", "0-1")] == 1
    assert narrow_counts[("pairs_hist", "", "1-5")] == 1
    wide = tmp_path / "wide"
    assert _run_script(monkeypatch, db_path, wide, "--window-seconds", "130") == 0
    wide_counts = _read_csv(wide)
    assert wide_counts[("pairs_total", "", "")] == 6
    assert wide_counts[("pairs_hist", "", "60+")] == 1


def test_same_key_retries_are_not_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "retry.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        _insert_call(
            conn,
            attempt="att-1",
            record="rec-1",
            wallet=WALLET_1,
            tool=TOOL_A,
            req_hash=HASH_A,
            key="k-1",
            created=BASE,
        )
        _insert_call(
            conn,
            attempt="att-2",
            record="rec-2",
            wallet=WALLET_1,
            tool=TOOL_A,
            req_hash=HASH_A,
            key="k-1",
            created=BASE + timedelta(seconds=1),
        )
        _insert_call(
            conn,
            attempt="att-3",
            record="rec-3",
            wallet=WALLET_1,
            tool=TOOL_A,
            req_hash=HASH_A,
            key="k-2",
            created=BASE + timedelta(seconds=3),
        )
        conn.commit()
    finally:
        conn.close()
    out_dir = tmp_path / "out"
    assert _run_script(monkeypatch, db_path, out_dir) == 0
    counts = _read_csv(out_dir)
    assert counts[("total_calls", "", "")] == 3
    assert counts[("pairs_total", "", "")] == 1
    assert counts[("pairs_hist", "", "1-5")] == 1


def test_since_and_until_filter_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "dup.db"
    _build_standard_db(db_path)
    after = tmp_path / "after"
    assert (
        _run_script(monkeypatch, db_path, after, "--since", "2026-01-01T00:01:00") == 0
    )
    after_counts = _read_csv(after)
    assert after_counts[("total_calls", "", "")] == 1
    assert after_counts[("pairs_total", "", "")] == 0
    before = tmp_path / "before"
    assert (
        _run_script(monkeypatch, db_path, before, "--until", "2026-01-01T00:00:05") == 0
    )
    before_counts = _read_csv(before)
    assert before_counts[("total_calls", "", "")] == 6
    assert before_counts[("pairs_total", "", "")] == 2


@pytest.mark.parametrize(
    "statement",
    [
        "",
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a = 1",
        "DELETE FROM t",
        "DROP TABLE t",
        "ALTER TABLE t ADD COLUMN a INT",
        "CREATE TABLE t (a INT)",
        "TRUNCATE TABLE t",
        "GRANT SELECT ON t TO r",
        "COPY t FROM STDIN",
        "MERGE INTO t USING s ON t.a = s.a WHEN MATCHED THEN UPDATE SET a = 1",
        "CALL do_something()",
        "DO $$ BEGIN END $$",
        "SET TRANSACTION READ ONLY",
        "SELECT 1; SELECT 2",
        "SELECT 1; DROP TABLE t",
        "WITH gone AS (DELETE FROM t RETURNING *) SELECT * FROM gone",
        "WITH added AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM added",
        "WITH changed AS (UPDATE t SET a = 1 RETURNING *) SELECT * FROM changed",
    ],
)
def test_guard_rejects_non_select_statements(statement: str) -> None:
    with pytest.raises(ValueError, match="allowed|forbidden|without SELECT"):
        amw.assert_select_only(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT 1",
        "select a from t",
        "SELECT 1;",
        "WITH one AS (SELECT 1) SELECT * FROM one",
        "-- leading comment\nSELECT 1",
    ],
)
def test_guard_allows_plain_selects(statement: str) -> None:
    amw.assert_select_only(statement)


def test_execute_helper_calls_the_guard() -> None:
    with pytest.raises(ValueError, match="allowed|forbidden"):
        amw.execute_select(None, "DELETE FROM t")


def test_not_measured_when_env_is_unset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(amw.ENV_VAR, raising=False)
    out_dir = tmp_path / "out"
    assert amw.main(["--out-dir", str(out_dir)]) == 0
    captured = capsys.readouterr()
    assert "not measured" in captured.out
    assert "://" not in captured.out
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "not measured" in summary
    assert "://" not in summary


def test_bogus_url_password_never_leaks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(
        amw.ENV_VAR, "bogus-scheme://user:hunter2-test@example.invalid/db"
    )
    out_dir = tmp_path / "out"
    assert amw.main(["--out-dir", str(out_dir)]) == 0
    captured = capsys.readouterr()
    assert "not measured" in captured.out
    assert "hunter2-test" not in captured.out
    assert "example.invalid" not in captured.out
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    csv_text = (out_dir / "dup_pairs.csv").read_text(encoding="utf-8")
    assert "not measured" in summary
    assert "hunter2-test" not in summary
    assert "hunter2-test" not in csv_text
    assert "example.invalid" not in summary


def test_outputs_hold_no_raw_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "dup.db"
    out_dir = tmp_path / "out"
    _build_standard_db(db_path)
    assert _run_script(monkeypatch, db_path, out_dir) == 0
    csv_text = (out_dir / "dup_pairs.csv").read_text(encoding="utf-8")
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    for raw in (
        WALLET_1,
        WALLET_2,
        TOOL_A,
        TOOL_B,
        HASH_A,
        HASH_B,
        "k-1",
        "k-2",
        "k-9",
        "k-10",
        "k-11",
        AUTO_1,
        AUTO_2,
    ):
        assert raw not in csv_text
        assert raw not in summary


def test_summary_lists_sql_range_and_row_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "dup.db"
    out_dir = tmp_path / "out"
    _build_standard_db(db_path)
    assert _run_script(monkeypatch, db_path, out_dir) == 0
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "LAG(" in summary
    assert "COUNT(*)" in summary
    assert "2026-01-01" in summary
    assert "rows_read: 11" in summary
    assert "window_seconds: 60" in summary


def test_sqlite_connection_refuses_writes(tmp_path: Path) -> None:
    db_path = tmp_path / "dup.db"
    _build_standard_db(db_path)
    engine = amw.create_readonly_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            found = conn.execute(
                text("SELECT COUNT(*) FROM mcp_dispatch_attempts")
            ).scalar()
            assert found == 11
            with pytest.raises(DBAPIError, match="readonly"):
                conn.execute(
                    text(
                        "INSERT INTO mcp_dispatch_attempts"
                        " (attempt_id, idempotency_record_id, wallet_id,"
                        " public_tool_id, request_hash, created_at)"
                        " VALUES ('att-x', 'rec-x', 'w', 't', 'h',"
                        " '2026-01-01 00:00:00.000000')"
                    )
                )
    finally:
        engine.dispose()


FAKE_TOTAL = 7
FAKE_PAIRS = 3
FAKE_LO = "2026-01-01 00:00:00.000000"
FAKE_HI = "2026-01-01 00:01:00.000000"
PG_SYNTH_PASSWORD = "pg-synth-pass-0001"
PG_SYNTH_HOST = "example.invalid"


class _FakeResult:
    """Fixed synthetic result for one recorded statement (no DB)."""

    def __init__(
        self,
        *,
        scalar: Any = None,
        one: Any = None,
        all_rows: Any = None,
    ) -> None:
        self._scalar = scalar
        self._one = one
        self._all_rows = [] if all_rows is None else all_rows

    def scalar(self) -> Any:
        return self._scalar

    def mappings(self) -> "_FakeResult":
        return self

    def one(self) -> Any:
        return self._one

    def all(self) -> Any:
        return self._all_rows


def _fake_aggregate_result(sql: str) -> _FakeResult:
    upper = sql.strip().upper()
    if "MIN(CREATED_AT)" in upper and "MAX(CREATED_AT)" in upper:
        return _FakeResult(one={"lo": FAKE_LO, "hi": FAKE_HI, "n": FAKE_TOTAL})
    if "AS BUCKET" in upper:
        return _FakeResult(
            all_rows=[{"bucket": "0-1", "n": 1}, {"bucket": "1-5", "n": 2}]
        )
    if "AS KIND" in upper:
        return _FakeResult(
            all_rows=[{"kind": "auto", "n": 1}, {"kind": "caller", "n": 2}]
        )
    if "AS COMBO" in upper:
        return _FakeResult(all_rows=[{"combo": "mixed", "n": FAKE_PAIRS}])
    if "FROM PAIRS" in upper:
        return _FakeResult(scalar=FAKE_PAIRS)
    if "FROM CALLS" in upper:
        return _FakeResult(scalar=FAKE_TOTAL)
    return _FakeResult(scalar=0, one={"lo": None, "hi": None, "n": 0}, all_rows=[])


def _statement_text(statement: Any) -> str:
    maybe_text = getattr(statement, "text", None)
    if isinstance(maybe_text, str):
        return maybe_text
    return str(statement)


class _FakeConnection:
    """Sync recorder: every statement goes through this one object."""

    def __init__(self, *, show_value: str = "on") -> None:
        self.statements: list[str] = []
        self.show_value = show_value

    def execute(self, statement: Any, parameters: Any = None) -> _FakeResult:
        sql = _statement_text(statement)
        self.statements.append(sql)
        upper = sql.strip().upper()
        if upper.startswith("SHOW"):
            return _FakeResult(scalar=self.show_value)
        if upper.startswith("SET"):
            return _FakeResult(scalar=None)
        return _fake_aggregate_result(sql)


class _FakeAsyncConnection:
    """Async recorder mirroring the production asyncpg transaction shape."""

    def __init__(self, *, show_value: str = "on") -> None:
        self.statements: list[str] = []
        self.show_value = show_value

    async def execute(self, statement: Any, parameters: Any = None) -> _FakeResult:
        sql = _statement_text(statement)
        self.statements.append(sql)
        upper = sql.strip().upper()
        if upper.startswith("SHOW"):
            return _FakeResult(scalar=self.show_value)
        if upper.startswith("SET"):
            return _FakeResult(scalar=None)
        return _fake_aggregate_result(sql)


class _FakeAsyncEngine:
    """Counts transaction blocks; yields one shared fake connection."""

    def __init__(self, conn: _FakeAsyncConnection) -> None:
        self._conn = conn
        self.begin_count = 0
        self.disposed = False

    def begin(self) -> Any:
        engine = self

        class _Ctx:
            async def __aenter__(self) -> _FakeAsyncConnection:
                engine.begin_count += 1
                return engine._conn

            async def __aexit__(self, *exc: Any) -> bool:
                return False

        return _Ctx()

    async def dispose(self) -> None:
        self.disposed = True


def _first_select_index(statements: list[str]) -> int:
    for index, sql in enumerate(statements):
        lead = sql.strip().upper()
        if lead.startswith("SELECT") or lead.startswith("WITH"):
            return index
    return len(statements)


def test_postgres_fence_is_first_before_any_select() -> None:
    fake = _FakeConnection(show_value="on")
    queries = amw.build_queries("postgresql", since=None, until=None)
    measurement = amw.run_postgres_measurement_sync(
        fake,
        queries,
        since=None,
        until=None,
        window=60.0,
        statement_timeout_seconds=300.0,
    )
    assert measurement.total_calls == FAKE_TOTAL
    assert measurement.pairs_total == FAKE_PAIRS
    assert len(fake.statements) == 10
    assert fake.statements[0] == amw.POSTGRES_SET_TRANSACTION
    assert "REPEATABLE READ" in fake.statements[0]
    assert "READ ONLY" in fake.statements[0]
    assert fake.statements[1] == amw.POSTGRES_SESSION_CHARACTERISTICS
    assert fake.statements[2].startswith("SET LOCAL statement_timeout")
    assert "300000" in fake.statements[2]
    assert fake.statements[3] == amw.POSTGRES_SHOW_READ_ONLY
    assert _first_select_index(fake.statements) == 4


def test_postgres_show_off_raises_verification_failed() -> None:
    fake = _FakeConnection(show_value="off")
    with pytest.raises(
        amw._NotMeasured, match="read-only verification failed"
    ) as excinfo:
        amw._enforce_postgres_readonly(fake, statement_timeout_seconds=300.0)
    assert excinfo.value.reason == "read-only verification failed"
    assert fake.statements[0] == amw.POSTGRES_SET_TRANSACTION
    assert _first_select_index(fake.statements) == len(fake.statements)


def test_postgres_show_off_via_main_is_not_measured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def _raise_not_on(*args: Any, **kwargs: Any) -> Any:
        raise amw._NotMeasured("read-only verification failed")

    monkeypatch.setattr(amw, "_postgres_single_transaction", _raise_not_on)
    monkeypatch.setenv(
        amw.ENV_VAR,
        f"postgresql://analyst:{PG_SYNTH_PASSWORD}@{PG_SYNTH_HOST}/db",
    )
    out_dir = tmp_path / "out"
    assert amw.main(["--out-dir", str(out_dir)]) == 0
    captured = capsys.readouterr()
    assert "not measured" in captured.out
    assert PG_SYNTH_PASSWORD not in captured.out
    assert PG_SYNTH_HOST not in captured.out
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    csv_text = (out_dir / "dup_pairs.csv").read_text(encoding="utf-8")
    assert "not measured" in summary
    assert PG_SYNTH_PASSWORD not in summary
    assert PG_SYNTH_PASSWORD not in csv_text
    assert PG_SYNTH_HOST not in summary


def test_postgres_url_normalisation_uses_asyncpg() -> None:
    userinfo = f"analyst:{PG_SYNTH_PASSWORD}@{PG_SYNTH_HOST}:5432/db"
    assert (
        amw.normalize_postgres_url_to_asyncpg(f"postgres://{userinfo}")
        == f"postgresql+asyncpg://{userinfo}"
    )
    assert (
        amw.normalize_postgres_url_to_asyncpg(f"postgresql://{userinfo}")
        == f"postgresql+asyncpg://{userinfo}"
    )
    assert (
        amw.normalize_postgres_url_to_asyncpg(f"postgresql+psycopg://{userinfo}")
        == f"postgresql+asyncpg://{userinfo}"
    )
    assert (
        amw.normalize_postgres_url_to_asyncpg(
            f"postgresql+psycopg2://{userinfo}?sslmode=require"
        )
        == f"postgresql+asyncpg://{userinfo}?sslmode=require"
    )
    already = f"postgresql+asyncpg://{userinfo}"
    assert amw.normalize_postgres_url_to_asyncpg(already) == already
    engine = amw.create_postgres_async_engine(f"postgresql://{userinfo}")
    assert engine.url.drivername == "postgresql+asyncpg"
    asyncio.run(engine.dispose())


def test_postgres_main_normalises_and_never_leaks_password(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, str] = {}
    queries = amw.build_queries("postgresql", since=None, until=None)
    canned = amw.Measurement(
        total_calls=FAKE_TOTAL,
        date_lo=FAKE_LO,
        date_hi=FAKE_HI,
        rows_read=FAKE_TOTAL,
        pairs_total=FAKE_PAIRS,
        hist={
            "0-1": 1,
            "1-5": 2,
            "5-10": 0,
            "10-30": 0,
            "30-60": 0,
            "60+": 0,
        },
        later_kind={"auto": 1, "caller": 2},
        combo={"both-auto": 0, "mixed": FAKE_PAIRS, "both-caller": 0},
        queries=queries,
    )

    async def _capture(normalized_url: str, *args: Any, **kwargs: Any) -> Any:
        captured["url"] = normalized_url
        return canned

    monkeypatch.setattr(amw, "_postgres_single_transaction", _capture)
    monkeypatch.setenv(
        amw.ENV_VAR,
        f"postgres://analyst:{PG_SYNTH_PASSWORD}@{PG_SYNTH_HOST}/db",
    )
    out_dir = tmp_path / "out"
    assert amw.main(["--out-dir", str(out_dir)]) == 0
    assert captured["url"].startswith("postgresql+asyncpg://")
    assert PG_SYNTH_HOST in captured["url"]
    captured_out = capsys.readouterr()
    assert PG_SYNTH_PASSWORD not in captured_out.out
    assert PG_SYNTH_HOST not in captured_out.out
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    csv_text = (out_dir / "dup_pairs.csv").read_text(encoding="utf-8")
    assert PG_SYNTH_PASSWORD not in summary
    assert PG_SYNTH_PASSWORD not in csv_text
    assert PG_SYNTH_HOST not in summary
    assert "REPEATABLE READ" in summary
    assert "snapshot:" in summary


def test_postgres_aggregates_share_single_connection() -> None:
    fake = _FakeConnection(show_value="on")
    queries = amw.build_queries("postgresql", since=None, until=None)
    amw.run_postgres_measurement_sync(
        fake,
        queries,
        since=None,
        until=None,
        window=60.0,
        statement_timeout_seconds=300.0,
    )
    assert len(fake.statements) == 10
    aggregates = fake.statements[4:]
    assert queries.total in aggregates
    assert queries.date_range in aggregates
    assert queries.pairs in aggregates
    assert queries.hist in aggregates
    assert queries.later_kind in aggregates
    assert queries.combo in aggregates


def test_postgres_async_transaction_uses_one_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_conn = _FakeAsyncConnection(show_value="on")
    fake_engine = _FakeAsyncEngine(fake_conn)
    captured: dict[str, str] = {}

    def _fake_create(url: str) -> _FakeAsyncEngine:
        captured["url"] = url
        return fake_engine

    monkeypatch.setattr(amw, "create_async_engine", _fake_create)
    queries = amw.build_queries("postgresql", since=None, until=None)
    measurement = asyncio.run(
        amw._postgres_single_transaction(
            f"postgresql+asyncpg://analyst@{PG_SYNTH_HOST}/db",
            queries,
            since=None,
            until=None,
            window=60.0,
            statement_timeout_seconds=300.0,
        )
    )
    assert captured["url"].startswith("postgresql+asyncpg://")
    assert fake_engine.begin_count == 1
    assert fake_engine.disposed is True
    assert len(fake_conn.statements) == 10
    assert fake_conn.statements[0] == amw.POSTGRES_SET_TRANSACTION
    assert _first_select_index(fake_conn.statements) == 4
    assert measurement.total_calls == FAKE_TOTAL


def test_statement_timeout_is_configurable() -> None:
    assert "300000" in amw.postgres_statement_timeout_sql(300.0)
    assert (
        amw.build_postgres_fence_statements(statement_timeout_seconds=5.0)[2]
        == "SET LOCAL statement_timeout = 5000"
    )
    args = amw.parse_args(["--out-dir", "/tmp/out-test"])
    assert args.statement_timeout_seconds == 300.0
    args = amw.parse_args(
        ["--out-dir", "/tmp/out-test", "--statement-timeout-seconds", "12.5"]
    )
    assert args.statement_timeout_seconds == 12.5


def test_summary_states_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "REPEATABLE READ" in amw.POSTGRES_SNAPSHOT
    db_path = tmp_path / "dup.db"
    out_dir = tmp_path / "out"
    _build_standard_db(db_path)
    assert _run_script(monkeypatch, db_path, out_dir) == 0
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "- snapshot:" in summary
    assert amw.SQLITE_SNAPSHOT in summary
