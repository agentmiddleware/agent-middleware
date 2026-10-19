"""Runtime dependencies must be installable metadata, not just a lockfile.

`pip install -e .` / `uv sync` resolve only `[project].dependencies`, so every
runtime floor in requirements.txt has to be declared there too. requirements.txt
stays the install target for Docker/CI/`--with-requirements` runs; these tests
keep the two files in sync in both directions.
"""

from __future__ import annotations

import configparser
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Installed only to run the suite; never imported by `app/`.
DEV_ONLY = {"pytest", "pytest-asyncio", "pytest-cov", "mypy"}


def _requirements() -> list[Requirement]:
    requirements: list[Requirement] = []
    for raw in (REPO_ROOT / "requirements.txt").read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        requirements.append(Requirement(line))
    return requirements


def _pyproject_dependencies() -> list[Requirement]:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]
    return [Requirement(dep) for dep in project["dependencies"]]


def _missing_requirements(
    required: list[Requirement], declared: list[Requirement]
) -> list[str]:
    declarations = set(declared)
    return [str(req) for req in required if req not in declarations]


def test_runtime_requirements_declared_in_pyproject() -> None:
    """Every non-dev requirement, including its constraints, must be declared.

    Previously `[project]` declared no `dependencies` at all, so a fresh
    `pip install -e .` / `uv sync` installed nothing and imports resolved
    only via ambient installs.
    """
    runtime = [
        req for req in _requirements() if canonicalize_name(req.name) not in DEV_ONLY
    ]
    missing = _missing_requirements(runtime, _pyproject_dependencies())
    assert not missing, (
        "requirements.txt entries missing from pyproject [project].dependencies: "
        f"{missing}"
    )


def test_pyproject_has_no_extra_runtime_dependencies() -> None:
    """The sync runs the other way too: pyproject must not drift from the lock."""
    runtime = [
        req for req in _requirements() if canonicalize_name(req.name) not in DEV_ONLY
    ]
    extra = _missing_requirements(_pyproject_dependencies(), runtime)
    assert not extra, (
        "pyproject [project].dependencies entries missing from requirements.txt: "
        f"{extra}"
    )


@pytest.mark.parametrize(
    ("required", "declared"),
    [
        ("pydantic>=2.14.1,<2.15", "pydantic>=2.14.1"),
        ("uvicorn[standard]>=0.54.0", "uvicorn>=0.54.0"),
        ("httpx>=0.28.1; python_version < '3.13'", "httpx>=0.28.1"),
    ],
)
def test_sync_detects_dropped_bounds_extras_and_markers(
    required: str, declared: str
) -> None:
    requirement = Requirement(required)
    assert _missing_requirements([requirement], [Requirement(declared)]) == [
        str(requirement)
    ]


def test_mcp_major_bound_preserved() -> None:
    """The mcp<2 ceiling is deliberate: 2.x renamed FastMCP to MCPServer and
    moved McpError. Dependabot major bumps are ignored (#460)."""
    for req in _pyproject_dependencies():
        if canonicalize_name(req.name) == "mcp":
            assert req.specifier.contains("1.99.0"), f"mcp lower bound slipped: {req}"
            assert not req.specifier.contains("2.0.0"), (
                f"mcp<2 ceiling lifted without the dedicated migration PR: {req}"
            )
            return
    raise AssertionError("mcp missing from pyproject [project].dependencies")


def test_mypy_has_no_global_missing_imports_ignore() -> None:
    """The global `ignore_missing_imports = true` hid unresolved imports; only
    explicit per-module overrides for stub-less or optional dependencies may
    remain (asyncpg ships no stubs; webauthn/playwright/openai are optional
    integrations absent from requirements.txt installs)."""
    config = configparser.ConfigParser()
    config.read(REPO_ROOT / "mypy.ini")
    assert config.getboolean("mypy", "ignore_missing_imports") is False, (
        "mypy.ini must not ignore missing imports globally; use per-module "
        "overrides for the documented exceptions"
    )
    for module in ("asyncpg", "webauthn", "playwright", "openai"):
        section = f"mypy-{module}.*"
        assert config.has_section(section), (
            f"mypy.ini needs an explicit [{section}] override"
        )
        assert config.getboolean(section, "ignore_missing_imports") is True, (
            f"[{section}] must set ignore_missing_imports = true"
        )
