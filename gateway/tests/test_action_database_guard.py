"""No-network regressions for the destructive action-proof database preflight."""

import os
from pathlib import Path
import subprocess
import sys

import pytest

VALID_URL = "postgresql+asyncpg://sellers@127.0.0.1:55439/amw_action_guard_1"


@pytest.mark.parametrize(
    "module,flag",
    [
        ("test_action_multiprocess", "RUN_MCP_MULTIPROCESS_TESTS"),
        ("test_action_postgres", "RUN_POSTGRES_CONCURRENCY_TESTS"),
        ("test_action_migrations", "ACTION_MIGRATION_DATABASE_URL"),
    ],
)
@pytest.mark.parametrize(
    "query", ["?host=example.invalid", "?port=5432", "?database=unapproved", ""]
)
def test_collection_guard_precedes_session_database_setup(
    tmp_path, module, flag, query
):
    """Exercise actual pytest ordering; driver/setup sentinels prohibit real I/O."""
    root = Path(__file__).resolve().parents[1]
    parent_databases = [
        Path.cwd() / name for name in ("test.db", "test.db-shm", "test.db-wal")
    ]
    parent_contents = {
        path: path.read_bytes() if path.exists() else None for path in parent_databases
    }
    child_database = tmp_path / "test.db"
    child_database.write_bytes(b"nested pytest cleanup sentinel")
    setup_marker = tmp_path / "setup-reached"
    network_marker = tmp_path / "network-reached"
    script = """
import pathlib, socket, sys
setup_marker, network_marker, test_path = sys.argv[1:]
def no_network(*args, **kwargs):
    pathlib.Path(network_marker).touch()
    raise AssertionError("NETWORK_MUST_NOT_BE_REACHED")
socket.socket.connect = no_network
socket.socket.connect_ex = no_network
socket.create_connection = no_network
socket.getaddrinfo = no_network
import pytest
class Probe:
    def pytest_sessionstart(self, session):
        from app.db import database
        async def setup_sentinel():
            pathlib.Path(setup_marker).touch()
            raise AssertionError("DATABASE_SETUP_REACHED")
        database.init_db = setup_sentinel
raise SystemExit(pytest.main([test_path, "-x", "-q"], plugins=[Probe()]))
"""
    env = {
        **os.environ,
        "ENVIRONMENT": "test",
        "STATE_BACKEND": "postgres",
        "MCP_STRESS_DB_ISOLATED": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONPATH": os.pathsep.join(
            filter(None, [str(root), os.environ.get("PYTHONPATH")])
        ),
    }
    # Explicit pytest-asyncio loading preserves the actual session fixture.
    env["PYTEST_PLUGINS"] = "pytest_asyncio.plugin"
    # Under pytest-xdist the parent's worker id and per-worker DATABASE_URL
    # would leak into the child run; the child must use its own defaults.
    if os.environ.get("PYTEST_XDIST_WORKER"):
        for key in ("PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT", "PYTEST_XDIST_TESTRUNUID", "DATABASE_URL"):
            env.pop(key, None)
    for key in (
        "RUN_MCP_MULTIPROCESS_TESTS",
        "RUN_POSTGRES_CONCURRENCY_TESTS",
        "ACTION_MIGRATION_DATABASE_URL",
    ):
        env.pop(key, None)
    env["DATABASE_URL"] = VALID_URL + query
    if flag == "ACTION_MIGRATION_DATABASE_URL":
        env[flag] = VALID_URL + query
        env["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'runner.db'}"
    else:
        env[flag] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(setup_marker),
            str(network_marker),
            str(root / "tests" / f"{module}.py"),
        ],
        # The safe profile reaches conftest setup, which removes ./test.db.
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = result.stdout + result.stderr
    for path, before in parent_contents.items():
        after = path.read_bytes() if path.exists() else None
        assert after == before, f"nested pytest changed parent database {path}"
    assert not network_marker.exists(), output
    assert result.returncode != 0
    if query:
        assert "unsafe_action_test_database_url" in output, output
        assert not setup_marker.exists(), "unsafe URL reached session database setup"
        assert child_database.read_bytes() == b"nested pytest cleanup sentinel"
    else:
        assert setup_marker.exists(), output
        assert not child_database.exists(), (
            "safe setup did not clean its isolated database"
        )
        assert "DATABASE_SETUP_REACHED" in output, output


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-url",
        VALID_URL.replace("asyncpg", "psycopg"),
        VALID_URL.replace("sellers@", "other@"),
        VALID_URL.replace("sellers@", "sellers:password@"),
        VALID_URL.replace("127.0.0.1", "example.invalid"),
        VALID_URL.replace("127.0.0.1", "localhost"),
        VALID_URL.replace("55439", "5432"),
        VALID_URL.replace(":55439", ""),
        VALID_URL.replace("amw_action_guard_1", "production"),
        VALID_URL.replace("amw_action_guard_1", "amw_action_"),
        VALID_URL.replace("amw_action_guard_1", "amw_action_../production"),
        VALID_URL.replace("amw_action_guard_1", "amw_action_" + "a" * 64),
        *[
            VALID_URL + query
            for query in (
                "?host=example.invalid",
                "?host=127.0.0.1&host=example.invalid",
                "?port=5432",
                "?database=production",
                "?user=other",
                "?password=secret",
                "?ssl=false",
                "?host=127.0.0.1",
                "?host=",
                "?",
                "#fragment",
            )
        ],
    ],
)
def test_fixed_profile_rejects_unsafe_target(value):
    from tests.support.action_database_guard import require_action_database_url

    with pytest.raises(ValueError, match="unsafe_action_test_database_url"):
        require_action_database_url(value)


def test_valid_profile_resolves_to_exact_driver_target():
    from sqlalchemy.engine import make_url
    from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg
    from tests.support.action_database_guard import require_action_database_url

    url = require_action_database_url(VALID_URL)
    args, kwargs = PGDialect_asyncpg().create_connect_args(make_url(url))
    assert args == []
    assert kwargs == {
        "host": "127.0.0.1",
        "port": 55439,
        "user": "sellers",
        "database": "amw_action_guard_1",
    }
