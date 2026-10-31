"""Documents that name CI jobs or key-storage controls must match the code.

PROOF_MATRIX.md, TESTING_TIERS.md and the failure lab all point readers at
jobs in .github/workflows/ci.yml. A renamed or removed job left a dead
reference in the proof index (``postgres_permit_concurrency``) and the threat
model kept describing SHA-256 key storage after the move to scrypt. These
tests parse the workflow and the source files so the prose cannot drift
silently.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

GATEWAY = Path(__file__).resolve().parent.parent
REPO = GATEWAY.parent
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"

DOCS_NAMING_CI_JOBS = (
    GATEWAY / "docs" / "PROOF_MATRIX.md",
    GATEWAY / "docs" / "TESTING_TIERS.md",
    GATEWAY / "docs" / "failure-lab.md",
    GATEWAY / "scripts" / "failure_lab.py",
)

# A Postgres CI job is spelled postgres-<word>; the underscore spelling is the
# dead name this test exists to catch, so both separators are matched.
JOB_TOKEN = re.compile(r"\bpostgres[-_][a-z]+(?:[-_][a-z]+)*")


def _ci_job_names() -> set[str]:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text())
    return set(workflow["jobs"])


def test_ci_job_names_in_docs_exist_in_the_workflow():
    jobs = _ci_job_names()
    assert {"postgres-concurrency", "postgres-proofs"} <= jobs
    for path in DOCS_NAMING_CI_JOBS:
        named = set(JOB_TOKEN.findall(path.read_text()))
        assert named, f"{path.name} no longer names a Postgres CI job; update this test"
        missing = sorted(named - jobs)
        assert not missing, f"{path.name} names CI jobs that do not exist: {missing}"


def test_proof_matrix_row_lock_count_matches_the_suite():
    """The matrix quotes the row-lock suite size; recount it from the file."""
    text = (GATEWAY / "docs" / "PROOF_MATRIX.md").read_text()
    suite = (GATEWAY / "tests" / "test_permit_postgres_concurrency.py").read_text()
    defined = len(re.findall(r"^(?:async )?def test_", suite, flags=re.MULTILINE))
    match = re.search(r"\*\*(\d+)\*\* row-lock\s+concurrency tests", text)
    assert match, "PROOF_MATRIX.md no longer states the row-lock test count"
    # Parametrized tests collect more cases than there are definitions, so the
    # documented number may exceed the definition count but never undercut it.
    assert int(match.group(1)) >= defined


def test_threat_model_describes_current_api_key_storage():
    text = (GATEWAY / "docs" / "threat-model.md").read_text()
    bullet = re.search(
        r"- DB-created keys are stored as.*?(?=\n- )", text, flags=re.DOTALL
    )
    assert bullet, "threat-model.md no longer describes how DB-created keys are stored"
    described = bullet.group(0)
    assert "scrypt" in described
    assert "SHA-256" not in described
    assert "API_KEY_HASH_PEPPER" in described
    service = (GATEWAY / "app" / "services" / "api_key_service.py").read_text()
    assert "hashlib.scrypt" in service
    assert "sha256" not in service.lower()
