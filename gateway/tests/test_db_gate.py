"""The database gate itself: closed gates skip quietly, never loudly in CI.

Every real-database proof in this suite gates on ``tests.support.db_gate``.
These tests pin that contract: an unset opt-in skips locally but fails when
``CI_STRICT=1``, so a CI job that forgets to provision its database goes red
instead of reporting a green suite that proved nothing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support import db_gate

PROBE_FLAG = "FLEET_DB_GATE_PROBE_FLAG_NEVER_SET"


def test_ci_strict_reads_environment(monkeypatch):
    monkeypatch.delenv(db_gate.CI_STRICT_ENV_VAR, raising=False)
    assert db_gate.ci_strict() is False
    monkeypatch.setenv(db_gate.CI_STRICT_ENV_VAR, "1")
    assert db_gate.ci_strict() is True


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, False),
        ({PROBE_FLAG: "0"}, False),
        ({PROBE_FLAG: "1"}, True),
    ],
)
def test_gate_open_flag(monkeypatch, env, expected):
    monkeypatch.delenv(PROBE_FLAG, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert db_gate.gate_open(flag=PROBE_FLAG) is expected


def test_gate_open_url_env(monkeypatch):
    monkeypatch.delenv("FLEET_DB_GATE_PROBE_URL", raising=False)
    assert db_gate.gate_open(url_env="FLEET_DB_GATE_PROBE_URL") is False
    monkeypatch.setenv("FLEET_DB_GATE_PROBE_URL", "postgresql://x")
    assert db_gate.gate_open(url_env="FLEET_DB_GATE_PROBE_URL") is True


def test_require_opt_in_passes_when_set(monkeypatch):
    monkeypatch.setenv(PROBE_FLAG, "1")
    db_gate.require_opt_in(flag=PROBE_FLAG, reason="probe")  # must not raise


def test_require_opt_in_skips_when_closed(monkeypatch):
    monkeypatch.delenv(PROBE_FLAG, raising=False)
    monkeypatch.delenv(db_gate.CI_STRICT_ENV_VAR, raising=False)
    with pytest.raises(pytest.skip.Exception):
        db_gate.require_opt_in(flag=PROBE_FLAG, reason="probe")


def test_require_opt_in_fails_under_ci_strict(monkeypatch):
    monkeypatch.delenv(PROBE_FLAG, raising=False)
    monkeypatch.setenv(db_gate.CI_STRICT_ENV_VAR, "1")
    with pytest.raises(pytest.fail.Exception, match="CI_STRICT"):
        db_gate.require_opt_in(flag=PROBE_FLAG, reason="probe")


class _FakeDialect:
    def __init__(self, name):
        self.name = name


class _FakeEngine:
    def __init__(self, name):
        self.dialect = _FakeDialect(name)


def test_require_live_postgres_passes_on_postgres(monkeypatch):
    monkeypatch.setenv(PROBE_FLAG, "1")
    monkeypatch.setattr("app.db.database.get_engine", lambda: _FakeEngine("postgresql"))
    db_gate.require_live_postgres(flag=PROBE_FLAG, reason="probe")


def test_require_live_postgres_skips_on_sqlite(monkeypatch):
    monkeypatch.setenv(PROBE_FLAG, "1")
    monkeypatch.delenv(db_gate.CI_STRICT_ENV_VAR, raising=False)
    monkeypatch.setattr("app.db.database.get_engine", lambda: _FakeEngine("sqlite"))
    with pytest.raises(pytest.skip.Exception):
        db_gate.require_live_postgres(flag=PROBE_FLAG, reason="probe")


def test_require_live_postgres_fails_on_sqlite_under_ci_strict(monkeypatch):
    monkeypatch.setenv(PROBE_FLAG, "1")
    monkeypatch.setenv(db_gate.CI_STRICT_ENV_VAR, "1")
    monkeypatch.setattr("app.db.database.get_engine", lambda: _FakeEngine("sqlite"))
    with pytest.raises(pytest.fail.Exception, match="CI_STRICT"):
        db_gate.require_live_postgres(flag=PROBE_FLAG, reason="probe")


def test_collection_mark_is_noop_when_open(monkeypatch):
    monkeypatch.setenv(PROBE_FLAG, "1")
    mark = db_gate.collection_mark(flag=PROBE_FLAG, reason="probe")
    assert mark.name == "skipif"
    assert mark.args[0] is False


def test_collection_mark_skips_when_closed(monkeypatch):
    monkeypatch.delenv(PROBE_FLAG, raising=False)
    monkeypatch.delenv(db_gate.CI_STRICT_ENV_VAR, raising=False)
    mark = db_gate.collection_mark(flag=PROBE_FLAG, reason="probe")
    assert mark.name == "skipif"
    assert mark.args[0] is True


def test_collection_mark_fails_under_ci_strict(monkeypatch):
    monkeypatch.delenv(PROBE_FLAG, raising=False)
    monkeypatch.setenv(db_gate.CI_STRICT_ENV_VAR, "1")
    with pytest.raises(pytest.fail.Exception, match="CI_STRICT"):
        db_gate.collection_mark(flag=PROBE_FLAG, reason="probe")


def _run_probe(tmp_path: Path, *, ci_strict: bool) -> subprocess.CompletedProcess:
    probe = tmp_path / "test_db_gate_probe.py"
    probe.write_text(
        "import pytest\n"
        "from tests.support import db_gate\n"
        "\n"
        "pytestmark = db_gate.collection_mark(\n"
        f'    flag="{PROBE_FLAG}",\n'
        '    reason="probe",\n'
        ")\n"
        "\n"
        "\n"
        "def test_probe():\n"
        "    pass\n"
    )
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            filter(
                None,
                [
                    str(Path(__file__).resolve().parents[1]),
                    os.environ.get("PYTHONPATH"),
                ],
            )
        ),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    env.pop(PROBE_FLAG, None)
    if ci_strict:
        env[db_gate.CI_STRICT_ENV_VAR] = "1"
    else:
        env.pop(db_gate.CI_STRICT_ENV_VAR, None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(probe), "-q", "-p", "no:cacheprovider"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_closed_gate_skips_outside_ci(tmp_path):
    result = _run_probe(tmp_path, ci_strict=False)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "skipped" in output.lower(), output


def test_closed_gate_errors_under_ci_strict(tmp_path):
    result = _run_probe(tmp_path, ci_strict=True)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "CI_STRICT" in output, output
    assert "1 skipped" not in output, output
