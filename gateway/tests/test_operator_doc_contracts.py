"""Operator recipes must preserve the current release and schema boundaries."""

from pathlib import Path

import pytest


DOCS = Path(__file__).resolve().parents[1] / "docs"


@pytest.mark.parametrize("name", ["human-onboarding.md"])
def test_legacy_database_guidance_requires_schema_and_data_review(name):
    text = (DOCS / name).read_text()
    assert "manual review" in text
    assert "schema and data-migration history" in text
    assert "proven matching historical revision" in text
    assert "run `alembic stamp head` once" not in text
    assert "run **`alembic stamp head`** once" not in text


