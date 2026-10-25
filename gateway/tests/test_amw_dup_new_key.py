"""Data-free tests for gateway/scripts/amw_dup_new_key.py.

Every database here is a hand-built temp SQLite file holding fixed
synthetic values only (wal_test_*, k-*, mcp-auto-*, example.invalid).
No real data, no network, no shared database.
"""

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
