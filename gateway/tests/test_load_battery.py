"""Synthetic lifecycle and verdict checks for the legacy load script."""

from decimal import Decimal
import subprocess

import pytest


@pytest.fixture
def battery(monkeypatch):
    # Its legacy CLI defaults must not leak into other tests on import.
    monkeypatch.setenv("VALID_API_KEYS", "synthetic-unused")
    monkeypatch.setenv("TRUST_MODE_ENABLED", "false")
    monkeypatch.setenv("ALLOW_LEGACY_UNPERMITTED_MCP", "true")
    from scripts import load_battery

    monkeypatch.setattr(load_battery, "DB_URL", None)
    monkeypatch.setattr(load_battery, "_OWNED_CONTAINER_ID", None)
    monkeypatch.setattr(load_battery.time, "sleep", lambda _: None)
    # _ensure_postgres writes these; register restoration before it runs.
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("STATE_BACKEND", "sqlite")
    return load_battery


def test_caller_database_never_creates_or_stops_a_container(battery, monkeypatch):
    url = "postgresql+asyncpg://synthetic.invalid/disposable"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr(battery, "DB_URL", url)

    def unexpected_command(*args, **kwargs):
        pytest.fail("caller-owned database reached a Docker command")

    monkeypatch.setattr(battery.subprocess, "run", unexpected_command)
    assert battery._ensure_postgres() == url
    battery._stop_postgres()


@pytest.mark.parametrize("mode", ["reuse", "created", "create-failed", "not-ready"])
def test_cleanup_only_stops_this_process_successfully_created_id(
    battery, monkeypatch, mode
):
    calls = []

    def command(argv, **kwargs):
        calls.append(argv)
        operation = argv[1]
        if operation == "ps":
            return subprocess.CompletedProcess(
                argv, 0, "borrowed-id" if mode == "reuse" else ""
            )
        if operation == "run":
            if mode == "create-failed":
                raise subprocess.CalledProcessError(1, argv)
            return subprocess.CompletedProcess(argv, 0, "created-id\n")
        if operation == "exec":
            assert argv[2] == "created-id"
            return subprocess.CompletedProcess(argv, 1 if mode == "not-ready" else 0)
        assert operation == "stop"
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(battery.subprocess, "run", command)
    try:
        if mode == "create-failed":
            with pytest.raises(subprocess.CalledProcessError):
                battery._ensure_postgres()
        elif mode == "not-ready":
            with pytest.raises(RuntimeError, match="failed to start"):
                battery._ensure_postgres()
        else:
            battery._ensure_postgres()
    finally:
        battery._stop_postgres()
        battery._stop_postgres()
    stops = [argv for argv in calls if argv[1] == "stop"]
    assert stops == (
        [["docker", "stop", "-t", "5", "created-id"]]
        if mode in {"created", "not-ready"}
        else []
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "success",
        "all-errors",
        "all-denied",
        "partial-errors",
        "missing",
        "idem",
        "budget",
    ],
)
async def test_summary_report_and_exit_require_complete_success(
    battery, monkeypatch, tmp_path, capsys, mode
):
    from app.db import database

    async def nothing():
        pass

    async def run_battery(concurrency, total):
        result = battery.LoadResult(
            concurrency, total, total, 0, 0, Decimal(total), [100.0] * total
        )
        if mode == "all-errors":
            result.success_count, result.error_count = 0, total
        elif mode == "all-denied":
            result.success_count, result.denied_count = 0, total
        elif mode == "partial-errors":
            result.success_count, result.error_count = total - 1, 1
        elif mode == "missing":
            result.success_count -= 1
        elif mode == "idem":
            result.idempotency_violations.append("synthetic duplicate receipt")
        elif mode == "budget":
            result.budget_anomalies.append("synthetic budget mismatch")
        return result

    monkeypatch.setattr(battery, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(battery, "_ensure_postgres", lambda: "unused")
    monkeypatch.setattr(battery, "_run_migrations", lambda: None)
    monkeypatch.setattr(battery, "_run_battery", run_battery)
    monkeypatch.setattr(database, "init_db", nothing)
    monkeypatch.setattr(database, "close_db", nothing)
    result = await battery.main()
    output = capsys.readouterr().out
    report = (tmp_path / "reports/load_battery_report.md").read_text()
    passed = mode == "success"
    assert result == (0 if passed else 1)
    assert output.count("✅ PASS") == (3 if passed else 0)
    assert output.count("⚠️ FAIL") == (0 if passed else 3)
    assert report.count("| PASS |") == (3 if passed else 0)
    assert report.count("| FAIL |") == (0 if passed else 3)


def test_empty_workload_cannot_pass(battery):
    assert not battery.LoadResult(10, 0, 0, 0, 0, Decimal(0)).passed
