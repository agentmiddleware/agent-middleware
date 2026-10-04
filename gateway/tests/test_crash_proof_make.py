"""Exercise Make's forwarding into the real guard without connecting to a DB."""

from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def proof_environment(tmp_path):
    (tmp_path / "uv").write_text(
        f"#!{sys.executable}\n"
        "from tests.test_mcp_postgres_multiprocess import _require_explicit_isolation\n"
        "_require_explicit_isolation()\n"
        "print('isolation validated; no database connection attempted')\n"
    )
    (tmp_path / "uv").chmod(0o700)
    (tmp_path / "alembic").write_text('#!/bin/sh\ntouch "$MIGRATION_MARKER"\nexit 0\n')
    (tmp_path / "alembic").chmod(0o700)
    return {
        "PATH": f"{tmp_path}:/usr/bin:/bin",
        "PYTHONPATH": str(ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "MIGRATION_MARKER": str(tmp_path / "migration-invoked"),
        "DATABASE_URL": "postgresql+asyncpg://synthetic.invalid/disposable",
        "STATE_BACKEND": "postgres",
        "ENVIRONMENT": "test",
        "MCP_STRESS_DB_ISOLATED": "1",
    }


@pytest.mark.parametrize(
    "target", ["prove-crash-recovery", "prove-trust-plane-postgres"]
)
@pytest.mark.parametrize(
    "overrides, accepted",
    [
        ({}, True),
        ({"ENVIRONMENT": "production"}, False),
        ({"MCP_STRESS_DB_ISOLATED": ""}, False),
        ({"DATABASE_URL": "sqlite:///unused.db"}, False),
        ({"STATE_BACKEND": "sqlite"}, False),
        ({"RAILWAY_PROJECT_ID": "synthetic-project"}, False),
        (
            {
                "RAILWAY_PROJECT_ID": "synthetic-project",
                "MCP_STRESS_EXPECTED_RAILWAY_PROJECT_ID": "different-project",
            },
            False,
        ),
    ],
)
def test_proof_make_preserves_isolation_before_any_mutation(
    proof_environment, target, overrides, accepted, tmp_path
):
    proof_environment.update(overrides)
    result = subprocess.run(
        ["/usr/bin/make", "-f", str(ROOT / "Makefile"), target],
        cwd=tmp_path,
        env=proof_environment,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert not Path(proof_environment["MIGRATION_MARKER"]).exists()
    assert (result.returncode == 0) is accepted, result.stdout + result.stderr
    assert ("isolation validated" in result.stdout) is accepted
