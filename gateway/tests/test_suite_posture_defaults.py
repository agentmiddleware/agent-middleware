"""Pin the gap between the default test posture and production posture.

The default suite runs permissive on purpose (legacy unpermitted MCP
allowed, no rate limits) so old tests exercise MCP paths without permits.
That means "all tests pass" overstates production readiness unless three
things stay true: production defaults stay strict, the strict-mode CI run
keeps content, and the Postgres concurrency CI job keeps existing. This
test fails loudly if any of those drift.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def test_suite_trust_defaults_are_permissive_while_production_is_strict():
    """The suite opts out of strict mode; production must not follow it."""
    from app.core.config import Settings

    assert os.environ.get("TRUST_MODE_ENABLED") == "false"
    assert os.environ.get("ALLOW_LEGACY_UNPERMITTED_MCP") == "true"
    assert Settings.model_fields["TRUST_MODE_ENABLED"].default is True
    assert Settings.model_fields["ALLOW_LEGACY_UNPERMITTED_MCP"].default is False


def test_suite_rate_limit_is_disabled_while_production_limits():
    """The suite default removes throttling; production keeps 120/min.

    Real limit behavior is covered only by tests that build their own
    middleware (test_runtime_degradation, test_rate_limit_threshold), so
    this pin keeps the exemption from leaking into production config.
    """
    from app.core.config import Settings

    assert os.environ.get("RATE_LIMIT_PER_MINUTE") == "1000000"
    assert Settings.model_fields["RATE_LIMIT_PER_MINUTE"].default == 120


def test_strict_mode_ci_run_has_content():
    """The production_trust CI run must keep proving the strict posture."""
    posture = (
        Path(__file__).resolve().parent / "test_production_trust_posture.py"
    ).read_text()
    strict_tests = posture.count("pytest.mark.production_trust")
    assert strict_tests >= 5, (
        f"only {strict_tests} production_trust marks left; the strict-mode "
        f"CI run is losing coverage"
    )
    workflow = CI_WORKFLOW.read_text()
    assert "TRUST_MODE_ENABLED=true" in workflow
    assert "ALLOW_LEGACY_UNPERMITTED_MCP=false" in workflow


def test_postgres_proof_ci_jobs_exist():
    """Real database and crash coverage must execute in required CI."""
    workflow = CI_WORKFLOW.read_text()
    assert "postgres-concurrency" in workflow
    assert "postgres-proofs" in workflow
    assert 'CI_STRICT: "1"' in workflow
    assert "RUN_POSTGRES_CONCURRENCY_TESTS" in workflow
    assert "test_action_postgres.py" in workflow
    assert "test_mcp_postgres_multiprocess.py" in workflow
    assert "test_action_multiprocess.py" in workflow
    assert "Prove gated tests actually ran" in workflow


def test_trust_release_gate_is_a_required_ci_check():
    """Coverage, OpenAPI parity, and the simulation inventory must fail CI.

    ``scripts/trust_release_gate.sh`` is the documented required check. It
    only affects branch protection when the job exists and ``CI result``
    waits on it. The script's first step is ``make check-railway-iac``;
    an undefined target aborts the gate before those checks run.
    """
    workflow = CI_WORKFLOW.read_text()
    assert "\n  trust_release_gate:\n" in workflow
    assert "name: trust_release_gate" in workflow
    assert "scripts/trust_release_gate.sh" in workflow
    assert 'UV_PYTHON: "3.12"' in workflow
    result_block = workflow.split("name: CI result", 1)[1].split("steps:", 1)[0]
    assert "trust_release_gate" in result_block
    assert "gateway-image-smoke" in result_block

    makefile = (ROOT / "gateway" / "Makefile").read_text()
    assert "\ncheck-railway-iac:" in makefile
