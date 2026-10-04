"""Every schema migration from 040 onward must ship a rollout note.

Production boot refuses to start when the database's Alembic revision differs
from the packaged head in either direction. Applying a new migration therefore
retires the previously serving image: it can no longer restart against the
upgraded database. Migration 040 crossed that boundary without a plan and
needed a same-night compatibility release, recorded in
docs/schema-040-rollout.md. This test turns that lesson into a merge
requirement: a migration lands together with a note that names its Alembic
revision id and has a heading for its rollback boundary.

The gate reads every module in migrations/versions. A file outside the
three-digit naming convention fails the test instead of slipping past a glob,
because the alembic revision command generates timestamped names by default.
The note must name the revision id the module declares, which is what the
alembic_version table records, rather than the filename stem; the two have
differed before in this repository.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "migrations" / "versions"
DOCS_DIR = REPO_ROOT / "docs"
FIRST_GATED_REVISION = 40
NUMBERED_NAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.py$")
REVISION_ASSIGNMENT = re.compile(
    r"^revision(?:\s*:\s*str)?\s*=\s*['\"]([^'\"]+)['\"]", re.MULTILINE
)
# A Markdown heading that opens a rollback section: "## Rollback after
# activation", "### Rolling back", "## Roll-back boundary". Requiring a heading
# gates a section an operator can find, not a token anywhere in the prose.
ROLLBACK_HEADING = re.compile(
    r"^#{1,6}\s+.*\broll(?:ing)?[\s-]?back", re.IGNORECASE | re.MULTILINE
)


def _migration_modules() -> list[Path]:
    return sorted(
        path for path in MIGRATIONS_DIR.glob("*.py") if path.name != "__init__.py"
    )


def _alembic_revision(path: Path) -> str:
    match = REVISION_ASSIGNMENT.search(path.read_text(encoding="utf-8"))
    assert match, f"{path.name} declares no revision"
    return match.group(1)


def _gated_migrations() -> list[Path]:
    gated: list[Path] = []
    for path in _migration_modules():
        match = NUMBERED_NAME.match(path.name)
        if match and int(match.group(1)) >= FIRST_GATED_REVISION:
            gated.append(path)
    return gated


def test_every_migration_follows_the_numbered_naming_convention() -> None:
    offenders = [
        path.name for path in _migration_modules() if not NUMBERED_NAME.match(path.name)
    ]
    assert not offenders, (
        "Migrations must be named NNN_snake_case.py so the rollout-note gate sees "
        f"them; the timestamped default of alembic revision is not accepted: {offenders}"
    )


def test_gate_covers_migration_040() -> None:
    names = [path.name for path in _gated_migrations()]
    assert any(name.startswith("040_") for name in names), names


@pytest.mark.parametrize("migration", _gated_migrations(), ids=lambda path: path.stem)
def test_migration_ships_a_rollout_note(migration: Path) -> None:
    note = DOCS_DIR / f"schema-{int(migration.name[:3]):03d}-rollout.md"
    assert note.exists(), (
        f"{migration.name} has no rollout note. Add {note.relative_to(REPO_ROOT)} "
        "stating the compatibility release that precedes it and the rollback "
        "boundary it creates; see docs/schema-040-rollout.md for the shape."
    )
    text = note.read_text(encoding="utf-8")
    revision = _alembic_revision(migration)
    assert revision in text, (
        f"{note.relative_to(REPO_ROOT)} must name the Alembic revision {revision}"
    )
    assert ROLLBACK_HEADING.search(text), (
        f"{note.relative_to(REPO_ROOT)} must have a heading for its rollback "
        "boundary, for example '## Rollback after activation'"
    )


@pytest.mark.parametrize(
    ("note", "accepted"),
    [
        ("## Rollback after activation\n", True),
        ("### Rolling back\n", True),
        ("## Roll-back boundary\n", True),
        ("## Roll back to 039 is unsupported\n", True),
        ("We considered no rollback here.\n", False),
        ("## Compatibility release\nrollback is fine\n", False),
    ],
)
def test_rollback_heading_rule(note: str, accepted: bool) -> None:
    assert bool(ROLLBACK_HEADING.search(note)) is accepted
