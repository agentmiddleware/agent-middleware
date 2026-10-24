"""Ratchet: tests that touch the shared database must clean up after itself.

``clean_database`` (tests/conftest.py) empties an explicit table list, and
tests/test_clean_database_coverage.py keeps that list in sync with the
schema. The remaining gap is files that never take the fixture: about half
the suite relies on other isolation, so a new test that writes wallets,
permits, or keys against the shared app can leak rows into the next test
without any gate noticing.

This test flags any test module that reaches the shared database (imports
the shared ``app`` from app.main, or a session factory from
app.db.database) without taking ``clean_database``. Modules that predate
the gate are frozen in ``LEGACY_MODULES_WITHOUT_CLEANUP`` below: a new
module may only join that set with a review-visible reason, and the
long-term direction is to shrink the set by adopting the fixture.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

# Session entry points to the shared test database. Importing the shared
# app counts because every request through it can write (permits, wallets,
# keys, ledger entries); read-only users of the app are grandfathered in
# the legacy set, not exempted, so the set stays an honest census.
DB_SESSION_IMPORTS = frozenset(
    {"get_session_factory", "get_engine", "init_db", "get_session"}
)

# Test modules that reach the shared database without taking
# clean_database, frozen at gate introduction. New modules must not be
# added without a comment explaining their isolation; prefer adopting
# clean_database so this set shrinks over time.
LEGACY_MODULES_WITHOUT_CLEANUP = frozenset(
    {
        "test_action_multiprocess",
        "test_action_preacceptance",
        "test_agent_first_contract",
        "test_api_keys",
        "test_awi_adoption",
        "test_backend_contract_regressions",
        "test_behavioral_sandbox",
        "test_build_provenance",
        "test_commit_sha_precedence",
        "test_content_store",
        "test_discover_kyc_honesty",
        "test_discovery",
        "test_discovery_consistency",
        "test_discovery_drift",
        "test_discovery_honesty",
        "test_discovery_tools_consistency",
        "test_duplicate_guard_config",
        "test_duplicate_guard_postgres_concurrency",
        "test_head_method",
        "test_health_public_projection",
        "test_health_redis_liveness",
        "test_health_sim_parity",
        "test_human_accessibility",
        "test_iot",
        "test_iot_registry",
        "test_key_distribution_references",
        "test_kyc",
        "test_mcp_postgres_multiprocess",
        "test_naive_utc_datetime_columns",
        "test_oracle_durable_crawl",
        "test_oracle_store",
        "test_permit_postgres_concurrency",
        "test_phase7_followups",
        "test_pods",
        "test_production_trust_posture",
        "test_public_metadata",
        "test_rate_limit_threshold",
        "test_request_body_limit",
        "test_route_auth_inventory",
        "test_runtime_degradation",
        "test_runtime_mode",
        "test_sandbox_integration",
        "test_scaffolding_harden",
        "test_schema_boot",
        "test_security_headers",
        "test_security_store",
        "test_stale_commit_sha_prevention",
        "test_stripe_integration",
        "test_swarm_wallets",
        "test_telemetry_scope",
        "test_telemetry_store",
        "test_transfers",
        "test_v1_production",
        "test_wedge_honesty",
    }
)


def _module_status(path: Path) -> tuple[bool, bool]:
    """Return (touches shared DB, takes clean_database) for one module."""
    tree = ast.parse(path.read_text())
    touches_db = False
    uses_cleanup = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "app.main" and any(
                alias.name == "app" for alias in node.names
            ):
                touches_db = True
            elif node.module == "app.db.database" and any(
                alias.name in DB_SESSION_IMPORTS for alias in node.names
            ):
                touches_db = True
            elif node.module == "tests.conftest" and any(
                alias.name == "clean_database" for alias in node.names
            ):
                uses_cleanup = True
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if "clean_database" in {arg.arg for arg in node.args.args}:
                uses_cleanup = True
    return touches_db, uses_cleanup


def _modules_without_cleanup() -> set[str]:
    found = set()
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        touches_db, uses_cleanup = _module_status(path)
        if touches_db and not uses_cleanup:
            found.add(path.stem)
    return found


def test_no_new_modules_skip_database_cleanup():
    new = _modules_without_cleanup() - LEGACY_MODULES_WITHOUT_CLEANUP
    assert not new, (
        f"test modules reach the shared database without clean_database: "
        f"{sorted(new)}. Take the clean_database fixture, or document the "
        f"module's isolation by adding it to "
        f"LEGACY_MODULES_WITHOUT_CLEANUP with reviewer approval."
    )


def test_legacy_cleanup_set_has_no_stale_entries():
    existing = {path.stem for path in TESTS_DIR.glob("test_*.py")}
    assert not LEGACY_MODULES_WITHOUT_CLEANUP - existing, (
        f"stale legacy entries for modules that no longer exist or now "
        f"take clean_database: "
        f"{sorted(LEGACY_MODULES_WITHOUT_CLEANUP - existing)}"
    )
    adopted = {
        stem
        for stem in LEGACY_MODULES_WITHOUT_CLEANUP
        if stem in existing
        and _module_status(TESTS_DIR / f"{stem}.py") != (True, False)
    }
    assert not adopted, (
        f"modules adopted clean_database or stopped touching the shared DB "
        f"but are still listed as legacy: {sorted(adopted)}. Remove them "
        f"from LEGACY_MODULES_WITHOUT_CLEANUP."
    )
