"""Regression checks for dependency advisory floors and removals."""

from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parent.parent

SECURITY_FLOORS = {
    "anyio": "4.14.2",
    "cryptography": "50.0.0",
    "mcp": "1.28.1",
    "pydantic-settings": "2.14.2",
    "pyasn1": "0.6.4",
}


def _requirements_from_lines(lines: list[str]) -> dict[str, Requirement]:
    requirements: dict[str, Requirement] = {}
    for line in lines:
        value = line.partition("#")[0].strip()
        if not value:
            continue
        requirement = Requirement(value)
        requirements[requirement.name.lower()] = requirement
    return requirements


def _assert_security_floor(requirement: Requirement, minimum: str) -> None:
    lower_bounds = [
        Version(specifier.version)
        for specifier in requirement.specifier
        if specifier.operator == ">="
    ]
    assert lower_bounds

    effective_floor = max(lower_bounds)
    assert effective_floor >= Version(minimum)
    assert effective_floor in requirement.specifier


def test_runtime_manifest_enforces_advisory_policy() -> None:
    requirements = _requirements_from_lines(
        (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    )

    # Keep ChromaDB out while the repository alert reports no patched release.
    assert "chromadb" not in requirements

    for package in ("anyio", "cryptography", "mcp", "pydantic-settings"):
        _assert_security_floor(requirements[package], SECURITY_FLOORS[package])


def test_optional_extras_cannot_resolve_known_vulnerable_versions() -> None:
    with (REPO_ROOT / "pyproject.toml").open("rb") as pyproject_file:
        extras = tomllib.load(pyproject_file)["project"]["optional-dependencies"]

    # Deprecated no-op extras keep existing install commands valid without
    # installing the vulnerable ChromaDB package.
    assert extras["chromadb"] == []
    assert extras["rag"] == []

    mcp_requirements = _requirements_from_lines(extras["mcp"])
    for package in ("anyio", "mcp", "pydantic-settings"):
        _assert_security_floor(mcp_requirements[package], SECURITY_FLOORS[package])

    webauthn_requirements = _requirements_from_lines(extras["webauthn"])
    for package in ("cryptography", "pyasn1"):
        _assert_security_floor(webauthn_requirements[package], SECURITY_FLOORS[package])
