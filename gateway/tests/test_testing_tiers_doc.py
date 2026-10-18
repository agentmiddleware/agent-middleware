"""The test-tier guide must keep up with the suite it describes.

Prospects stall when a skipped or red local run has no explanation, so
docs/TESTING_TIERS.md names the three levels (plain pytest, Postgres
opt-in, live batteries) with the exact flags each needs. This test pins
the guide to the mechanisms: if the flags move, the guide must move.
"""

from __future__ import annotations

from pathlib import Path

DOC = Path(__file__).resolve().parent.parent / "docs" / "TESTING_TIERS.md"


def _text() -> str:
    assert DOC.exists(), "docs/TESTING_TIERS.md is missing"
    return DOC.read_text()


def test_tier_guide_names_plain_pytest_level():
    text = _text()
    assert "make test-all" in text
    assert "production_trust" in text


def test_tier_guide_names_postgres_opt_in_flags():
    text = _text()
    for token in (
        "RUN_POSTGRES_CONCURRENCY_TESTS",
        "ENVIRONMENT=test",
        "alembic upgrade head",
        "test_action_postgres.py",
    ):
        assert token in text, f"tier guide no longer documents {token}"


def test_tier_guide_names_live_level_and_ci_job():
    text = _text()
    for token in (
        "trust-conformance-live",
        "adversarial-battery-live",
        "postgres-concurrency",
    ):
        assert token in text, f"tier guide no longer documents {token}"


def test_tier_guide_warns_about_unproven_levels():
    """A green tier 1 must not be quotable as live or money proof."""
    text = _text()
    assert "does not prove" in text
