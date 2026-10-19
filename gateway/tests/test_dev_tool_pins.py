"""Keep the local lint and type-check pins identical to CI.

mypy used to be `>=2.4.0` in requirements.txt, and the CI type-check step
installed whatever `uv pip install mypy` resolved that day. ruff was pinned
in CI (`RUFF_VERSION`) but not for local runs. These checks fail if either
tool can change versions without a repo change.
"""

from __future__ import annotations

import re
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parent.parent.parent
GATEWAY = ROOT / "gateway"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _requirements_from_lines(lines: list[str]) -> dict[str, Requirement]:
    requirements: dict[str, Requirement] = {}
    for line in lines:
        value = line.partition("#")[0].strip()
        if not value or value.startswith("-r ") or value.startswith("-c "):
            continue
        requirement = Requirement(value)
        requirements[requirement.name.lower()] = requirement
    return requirements


def _workflow_env(workflow: str, name: str) -> str:
    match = re.search(
        rf'^[ \t]*{name}:[ \t]*"([^"]+)"',
        workflow,
        re.MULTILINE,
    )
    assert match is not None, f"{name} missing from CI workflow"
    return match.group(1)


def _exact_version(requirement: Requirement) -> str:
    specifiers = list(requirement.specifier)
    assert len(specifiers) == 1, requirement
    specifier = specifiers[0]
    assert specifier.operator == "==", requirement
    return specifier.version


def test_mypy_and_ruff_pins_match_ci_env() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    ruff_version = _workflow_env(workflow, "RUFF_VERSION")
    mypy_version = _workflow_env(workflow, "MYPY_VERSION")

    runtime = _requirements_from_lines(
        (GATEWAY / "requirements.txt").read_text(encoding="utf-8").splitlines()
    )
    dev = _requirements_from_lines(
        (GATEWAY / "requirements-dev.txt").read_text(encoding="utf-8").splitlines()
    )

    assert _exact_version(runtime["mypy"]) == mypy_version
    assert _exact_version(dev["mypy"]) == mypy_version
    assert _exact_version(dev["ruff"]) == ruff_version
    # The runtime image installs requirements.txt. Keep the linter out of it.
    assert "ruff" not in runtime


def test_ci_installs_pinned_mypy_and_ruff() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    installs = re.findall(r"^.*uv pip install.*$", workflow, re.MULTILINE)
    mypy_installs = [line for line in installs if "mypy" in line]
    ruff_installs = [line for line in installs if "ruff" in line]

    assert mypy_installs, "CI never installs mypy"
    assert ruff_installs, "CI never installs ruff"
    for line in mypy_installs:
        assert "mypy==${MYPY_VERSION}" in line
    for line in ruff_installs:
        assert "ruff==${RUFF_VERSION}" in line
    assert "uv pip install mypy &&" not in workflow
    assert "uv pip install mypy\n" not in workflow


def test_local_gate_uses_the_dev_lock() -> None:
    gate = (GATEWAY / "scripts" / "core_quality_gate.sh").read_text(encoding="utf-8")
    env_script = (GATEWAY / "scripts" / "lib" / "python_env.sh").read_text(
        encoding="utf-8"
    )

    assert 'PYTHON_ENV_EXTRA_REQUIREMENTS="requirements-dev.txt"' in gate
    assert 'PYTHON_ENV_EXTRA_WITH="ruff"' not in gate
    assert "--with ruff" not in gate
    assert "--with mypy" not in gate
    assert "PYTHON_ENV_EXTRA_REQUIREMENTS" in env_script
    assert '--with-requirements "${_python_env_extra_requirements}"' in env_script


def test_contributing_documents_blessed_versions() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")

    assert _workflow_env(workflow, "RUFF_VERSION") in contributing
    assert _workflow_env(workflow, "MYPY_VERSION") in contributing
    assert "gateway/requirements-dev.txt" in contributing
    assert "blessed" in contributing
