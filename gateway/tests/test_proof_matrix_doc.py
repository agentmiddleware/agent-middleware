"""Documents that name CI jobs or key-storage controls must match the code.

PROOF_MATRIX.md, TESTING_TIERS.md and the failure lab all point readers at
jobs in .github/workflows/ci.yml. A renamed or removed job left a dead
reference in the proof index (``postgres_permit_concurrency``) and the threat
model kept describing SHA-256 key storage after the move to scrypt. These
tests parse the workflow and the source files so the prose cannot drift
silently.
"""

from __future__ import annotations

import ast
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


def _parametrize_cases(decorator: ast.expr) -> int | None:
    """Case count of a ``pytest.mark.parametrize`` decorator, None if not one."""
    if not (
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr == "parametrize"
    ):
        return None
    values: ast.expr | None = decorator.args[1] if len(decorator.args) > 1 else None
    for keyword in decorator.keywords:
        if keyword.arg == "argvalues":
            values = keyword.value
    # An unpacked entry ([*CASES]) is a list node whose length is not its case count.
    assert isinstance(values, (ast.List, ast.Tuple)) and not any(
        isinstance(entry, ast.Starred) for entry in values.elts
    ), "parametrize with non-literal argvalues: count its cases here explicitly"
    return len(values.elts)


def _cases_from(decorators: list[ast.expr]) -> int:
    """Product of the parametrize case counts among ``decorators``."""
    cases = 1
    for decorator in decorators:
        cases *= _parametrize_cases(decorator) or 1
    return cases


def _collected_case_count(source: str) -> int:
    """Number of cases pytest collects from a module, without importing it."""
    total = 0
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            # Class-level parametrize multiplies every test method in the class.
            per_method = _cases_from(node.decorator_list)
            total += sum(
                per_method * _cases_from(method.decorator_list)
                for method in node.body
                if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
                and method.name.startswith("test_")
            )
        elif isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ) and node.name.startswith("test_"):
            total += _cases_from(node.decorator_list)
    return total


def test_proof_matrix_row_lock_count_matches_the_suite():
    """The matrix quotes the row-lock suite size; recount it from the file."""
    text = (GATEWAY / "docs" / "PROOF_MATRIX.md").read_text()
    suite = (GATEWAY / "tests" / "test_permit_postgres_concurrency.py").read_text()
    match = re.search(r"\*\*(\d+)\*\* row-lock\s+concurrency tests", text)
    assert match, "PROOF_MATRIX.md no longer states the row-lock test count"
    collected = _collected_case_count(suite)
    assert collected > 0
    assert int(match.group(1)) == collected, (
        f"PROOF_MATRIX.md says {match.group(1)} row-lock tests, the suite collects {collected}"
    )


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
