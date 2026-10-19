"""The shared test fixtures must reject inherited database targets before I/O."""

import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.conftest import (
    clean_database,
    require_disposable_test_database,
    setup_database,
)

ACTION_URL = "postgresql+asyncpg://sellers@127.0.0.1:55439/amw_action_guard_1"
PROOF_URL = "postgresql+asyncpg://sellers@db.example.invalid/amw_pg_concurrency"
CI_PROOF_URL = "postgresql+asyncpg://sellers@127.0.0.1:55439/amw_pg_concurrency"


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://user:synthetic-password@db.example.invalid/production",
        "sqlite+aiosqlite:///./other.db",
        "sqlite+aiosqlite:///./test.db?mode=ro",
        "sqlite+aiosqlite:///:memory:",
        "",
    ],
)
def test_ordinary_runs_reject_inherited_database_url(monkeypatch, url):
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("AMW_TEST_DB_DISPOSABLE", raising=False)
    monkeypatch.delenv("RUN_POSTGRES_CONCURRENCY_TESTS", raising=False)
    monkeypatch.delenv("RUN_MCP_MULTIPROCESS_TESTS", raising=False)

    with pytest.raises(ValueError, match="^unsafe_test_database_url$"):
        require_disposable_test_database()


@pytest.mark.parametrize("flag", [None, "0", "true"])
def test_non_local_postgres_requires_explicit_disposable_flag(monkeypatch, flag):
    monkeypatch.setenv("DATABASE_URL", PROOF_URL)
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.delenv("RUN_POSTGRES_CONCURRENCY_TESTS", raising=False)
    monkeypatch.delenv("RUN_MCP_MULTIPROCESS_TESTS", raising=False)
    if flag is None:
        monkeypatch.delenv("AMW_TEST_DB_DISPOSABLE", raising=False)
    else:
        monkeypatch.setenv("AMW_TEST_DB_DISPOSABLE", flag)

    with pytest.raises(ValueError, match="^unsafe_test_database_url$"):
        require_disposable_test_database()


@pytest.mark.parametrize(
    "url, postgres_flag",
    [(PROOF_URL, False), (CI_PROOF_URL, True)],
)
def test_explicit_disposable_flag_allows_postgres_proofs(
    monkeypatch, url, postgres_flag
):
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("AMW_TEST_DB_DISPOSABLE", "1")
    if postgres_flag:
        monkeypatch.setenv("RUN_POSTGRES_CONCURRENCY_TESTS", "1")

    require_disposable_test_database()


@pytest.mark.parametrize(
    "url", ["sqlite+aiosqlite:///./other.db", "postgresql+asyncpg://"]
)
def test_disposable_flag_rejects_other_or_incomplete_urls(monkeypatch, url):
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("AMW_TEST_DB_DISPOSABLE", "1")

    with pytest.raises(ValueError, match="^unsafe_test_database_url$"):
        require_disposable_test_database()


def test_disposable_flag_requires_test_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", PROOF_URL)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("AMW_TEST_DB_DISPOSABLE", "1")

    with pytest.raises(ValueError, match="^unsafe_test_database_url$"):
        require_disposable_test_database()


@pytest.mark.parametrize(
    "flag", ["RUN_POSTGRES_CONCURRENCY_TESTS", "RUN_MCP_MULTIPROCESS_TESTS"]
)
def test_explicit_local_postgres_profile_is_allowed(monkeypatch, flag):
    monkeypatch.setenv("DATABASE_URL", ACTION_URL)
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.delenv("AMW_TEST_DB_DISPOSABLE", raising=False)
    monkeypatch.setenv(flag, "1")

    require_disposable_test_database()


@pytest.mark.parametrize(
    "url, environment",
    [
        (ACTION_URL, "production"),
        (ACTION_URL + "?host=db.example.invalid", "test"),
        (ACTION_URL.replace("amw_action_guard_1", "production"), "test"),
        (CI_PROOF_URL, "test"),
    ],
)
def test_opt_in_still_rejects_unsafe_postgres_profile(monkeypatch, url, environment):
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.delenv("AMW_TEST_DB_DISPOSABLE", raising=False)
    monkeypatch.setenv("RUN_POSTGRES_CONCURRENCY_TESTS", "1")

    with pytest.raises(ValueError, match="^unsafe_action_test_"):
        require_disposable_test_database()


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", [setup_database, clean_database])
async def test_fixture_rechecks_url_before_setup_or_cleanup(monkeypatch, fixture):
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./other.db")

    with pytest.raises(ValueError, match="^unsafe_test_database_url$"):
        await anext(fixture.__wrapped__())


def test_unsafe_inherited_url_stops_pytest_before_database_setup(tmp_path):
    test_file = Path(__file__).with_name("test_clean_database_coverage.py")
    sentinel = tmp_path / "test.db"
    sentinel.write_bytes(b"existing database sentinel")
    url = "postgresql+asyncpg://user:synthetic-password@db.example.invalid/production"
    script = """
import pathlib, socket, sys
network_marker = pathlib.Path(sys.argv[1])
def no_network(*args, **kwargs):
    network_marker.touch()
    raise AssertionError("network reached")
socket.socket.connect = no_network
socket.socket.connect_ex = no_network
socket.create_connection = no_network
socket.getaddrinfo = no_network
import pytest
raise SystemExit(pytest.main([sys.argv[2], "-q"]))
"""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(test_file.parents[1]),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTEST_PLUGINS": "pytest_asyncio.plugin",
        "DATABASE_URL": url,
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path / "network-reached"),
            str(test_file),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "unsafe_test_database_url" in output
    assert url not in output
    assert "synthetic-password" not in output
    assert sentinel.read_bytes() == b"existing database sentinel"
    assert not (tmp_path / "network-reached").exists()
