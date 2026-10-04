"""Guards the first five minutes of a new user's experience.

Each assertion here corresponds to a failure that a skeptic actually hit when
following the documented setup: copy `.env.example`, start the API, run the
gates. These are cheap to re-break in a docs edit, so they are pinned.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from app.main import (
    _SIGNING_KEY_REMEDIATION,
    _SIGNING_KEY_REMEDIATION_DEFAULT,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = REPO_ROOT / ".env.example"


def _uncommented_assignments() -> dict[str, str]:
    """Parse `.env.example` the way `cp .env.example .env` + a loader would."""

    values: dict[str, str] = {}
    for raw in ENV_EXAMPLE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def test_signing_seed_is_a_visible_required_key() -> None:
    """The seed is required in every environment, so it must not be buried.

    It previously appeared only inside a commented-out block labelled
    "production checklist", so copying the file and starting the API failed with
    `trust_signing_private_key_required`.
    """

    assignments = _uncommented_assignments()
    assert "TRUST_SIGNING_PRIVATE_KEY_B64" in assignments, (
        "TRUST_SIGNING_PRIVATE_KEY_B64 must be an uncommented key in "
        ".env.example: TRUST_MODE_ENABLED defaults to true, so the app cannot "
        "start without it."
    )


def test_signing_seed_ships_empty_rather_than_with_a_real_value() -> None:
    assignments = _uncommented_assignments()
    assert assignments["TRUST_SIGNING_PRIVATE_KEY_B64"] == "", (
        ".env.example must never carry real or placeholder key material; "
        "operators fill it from the documented generation command."
    )


def test_env_example_documents_how_to_generate_the_seed() -> None:
    text = ENV_EXAMPLE.read_text()
    assert "secrets.token_bytes(32)" in text, (
        ".env.example must include the seed generation command inline — telling "
        "a user a value is required without telling them how to produce it is "
        "the failure this guards."
    )


def test_default_state_backend_boots_locally() -> None:
    """Defaults must not be a placeholder PostgreSQL DSN under ENVIRONMENT=local.

    That combination previously failed startup with `socket.gaierror` because
    the example host does not resolve.
    """

    assignments = _uncommented_assignments()
    assert assignments.get("ENVIRONMENT") == "local"

    database_url = assignments.get("DATABASE_URL", "")
    assert database_url, "DATABASE_URL must have a working default"
    assert "user:password@host" not in database_url, (
        "DATABASE_URL default is an unresolvable placeholder; a fresh copy of "
        ".env.example must boot without hand-editing the datastore."
    )
    assert database_url.startswith("sqlite"), (
        "the shipped default should be the local SQLite path documented in the "
        "README; PostgreSQL belongs in the commented production block"
    )
    assert assignments.get("STATE_BACKEND") == "sqlite"


@pytest.mark.parametrize(
    "error_code",
    ["trust_signing_private_key_required", "invalid_trust_signing_private_key"],
)
def test_signing_key_failures_carry_actionable_remediation(error_code: str) -> None:
    """A failed first boot must say how to fix itself, not just what broke."""

    remediation = _SIGNING_KEY_REMEDIATION[error_code]
    assert "TRUST_SIGNING_PRIVATE_KEY_B64" in remediation
    assert "secrets.token_bytes(32)" in remediation, (
        "remediation must include the exact command that produces a valid seed"
    )


def test_remediation_lookup_has_a_safe_default() -> None:
    assert _SIGNING_KEY_REMEDIATION_DEFAULT
    assert "TRUST_SIGNING_PRIVATE_KEY_B64" in _SIGNING_KEY_REMEDIATION_DEFAULT


# --- Documentation honesty -------------------------------------------------
#
# `tests/test_wedge_honesty.py` already guards the runtime discovery surfaces
# (`agent.json`, `/llm.txt`) against advertising unpublished installs. These
# extend the same rule to the docs, READMEs, and examples a reader copies from.

#: Distributions this repository builds but does NOT publish to any index.
#: Every one of these was documented as a plain `pip install <name>` that exits
#: 1 with "No matching distribution found".
UNPUBLISHED_DISTRIBUTIONS = (
    "agent-middleware-api",
    "agent-middleware-awi",
    "langchain-agent-middleware",
    "crewai-agent-middleware",
    "autogen-agent-middleware",
    "openai-agent-middleware",
    "b2a-sdk",
)

#: Paths excluded from the docs sweep: vendored/marketing site content, and the
#: two files whose whole job is to record or assert these strings.
_DOC_SWEEP_EXCLUDES = (
    "site/",
    "node_modules/",
    # Gitignored agent scratch: `.claude/worktrees/` holds full checkouts, so a
    # sweep that walks it re-finds every excluded file under a second path and
    # fails on a copy of a file this list already forgave.
    ".claude/",
    "tests/test_wedge_honesty.py",
    "tests/test_onboarding_contract.py",
    "docs/tech-debt-remediation-plan.md",
)


def _sweepable_files() -> list[Path]:
    files: list[Path] = []
    for pattern in ("*.md", "*.py"):
        for path in REPO_ROOT.rglob(pattern):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if any(rel.startswith(x) or rel == x for x in _DOC_SWEEP_EXCLUDES):
                continue
            if "/.venv/" in f"/{rel}" or rel.startswith(".venv/"):
                continue
            files.append(path)
    return files


@pytest.mark.parametrize("distribution", UNPUBLISHED_DISTRIBUTIONS)
def test_docs_do_not_advertise_unpublished_installs(distribution: str) -> None:
    """No doc may tell a reader to `pip install` something that does not exist."""

    needle = f"pip install {distribution}"
    offenders = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in _sweepable_files()
        if needle in path.read_text(errors="ignore")
    ]
    assert not offenders, (
        f"`{needle}` is not installable — {distribution} is not published to "
        f"PyPI. Document the local path install instead. Offenders: {offenders}"
    )


def test_framework_integrations_import_the_module_that_exists() -> None:
    """The importable module is `framework_integrations`, not `agent_middleware`.

    Every README under framework_integrations/ documented
    `from agent_middleware import ...`, which raises ModuleNotFoundError.
    """

    offenders = []
    for path in (REPO_ROOT.parent / "integrations" / "framework_integrations").rglob(
        "*"
    ):
        if path.suffix not in {".md", ".py"}:
            continue
        for lineno, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            if line.strip().startswith(
                ("from agent_middleware", "import agent_middleware")
            ):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    assert not offenders, (
        "there is no `agent_middleware` package; import `framework_integrations`. "
        f"Offenders: {offenders}"
    )


def test_gate_scripts_share_interpreter_resolution() -> None:
    """Gates must not resolve an interpreter by name alone.

    Selecting `python3.12` by existence (never trying `python3`) made both trust
    gates fail with `No module named pytest` on any machine where a bare system
    python3.12 is on PATH.
    """

    helper = REPO_ROOT / "scripts" / "lib" / "python_env.sh"
    assert helper.is_file(), "scripts/lib/python_env.sh is the shared resolver"
    helper_text = helper.read_text()
    assert helper_text.index("elif command -v uv") < helper_text.index(
        "for _candidate in python3.12"
    ), "gates must prefer the requirements-resolved uv environment"

    for name in (
        "trust_coverage_gate.sh",
        "trust_release_gate.sh",
        "core_quality_gate.sh",
    ):
        script = REPO_ROOT / "scripts" / name
        assert script.is_file(), f"scripts/{name} must exist"
        text = script.read_text()
        assert "lib/python_env.sh" in text, (
            f"scripts/{name} must source the shared resolver"
        )
        assert 'PYTHON_BIN="${PYTHON:-python3.12}"' not in text, (
            f"scripts/{name} reintroduced the hardcoded interpreter"
        )


# --- Runnable examples -----------------------------------------------------


def test_dry_run_example_uses_the_wallet_it_creates() -> None:
    """The example must bill against the server-assigned wallet id.

    It previously created a sponsor wallet and then billed `sponsor-0`, an id
    the server never issues, so every dry-run call returned
    `404 wallet_not_found` and the example died on its first scenario.
    """

    source = (REPO_ROOT.parent / "examples" / "dry_run_example.py").read_text()
    assert "sponsor-0" not in source, (
        "wallet ids are server-assigned (e.g. spn-bde42b5c4606); a hardcoded id "
        "returns 404 wallet_not_found"
    )
    assert 'wallet["wallet_id"]' in source, (
        "the example must read the wallet id out of the creation response"
    )


def test_dry_run_example_states_its_proof_surface_prerequisite() -> None:
    """Dry-run endpoints are on the billing router, which is a proof surface."""

    source = (REPO_ROOT.parent / "examples" / "dry_run_example.py").read_text()
    assert "ENABLE_PROOF_SURFACES=true" in source, (
        "without proof surfaces enabled the billing router is not mounted and "
        "every dry-run call 404s; the example must say so"
    )


# --- Framework wrapper SDK floor --------------------------------------------

#: First b2a-sdk release with the typed async `AgentMiddlewareClient` trust
#: loop (`create_permit` / `invoke_tool` with caller-owned idempotency keys)
#: that every framework wrapper subclasses and calls (b2a_sdk/CHANGELOG.md).
B2A_SDK_TRUST_LOOP_FLOOR = Version("0.4.0")


def test_wrappers_require_an_sdk_that_ships_the_trust_loop() -> None:
    """A wrapper's `b2a-sdk` lower bound must exclude SDKs it cannot run on.

    The wrappers declared `b2a-sdk>=0.3.0`, so a resolver could pick a 0.3.x
    SDK that has no `AgentMiddlewareClient` and every governed call failed.
    The wrapper CI job installs the in-tree SDK, so nothing else exercises
    the declared floor.
    """

    sdk_version = Version(
        tomllib.loads(
            (REPO_ROOT.parent / "sdk" / "python" / "pyproject.toml").read_text()
        )["project"]["version"]
    )
    pyprojects = sorted((REPO_ROOT.parent / "integrations").glob("*/pyproject.toml"))
    assert pyprojects, "expected framework wrappers under wrappers/"

    offenders = []
    for pyproject in pyprojects:
        rel = pyproject.relative_to(REPO_ROOT.parent).as_posix()
        dependencies = tomllib.loads(pyproject.read_text())["project"]["dependencies"]
        sdk_requirements = [
            requirement
            for requirement in map(Requirement, dependencies)
            if canonicalize_name(requirement.name) == "b2a-sdk"
        ]
        if len(sdk_requirements) != 1:
            offenders.append(f"{rel}: expected one b2a-sdk dependency")
            continue
        specifier = sdk_requirements[0].specifier
        floors = [Version(spec.version) for spec in specifier if spec.operator == ">="]
        if not floors or max(floors) < B2A_SDK_TRUST_LOOP_FLOOR:
            offenders.append(f"{rel}: b2a-sdk{specifier} admits a pre-trust-loop SDK")
        if not specifier.contains(sdk_version):
            offenders.append(
                f"{rel}: b2a-sdk{specifier} excludes in-tree {sdk_version}"
            )
    assert not offenders, (
        f"wrappers must require b2a-sdk>={B2A_SDK_TRUST_LOOP_FLOOR}: {offenders}"
    )


# --- Deployment posture is auditable ---------------------------------------


@pytest.mark.anyio
async def test_health_payload_exposes_guardrail_posture() -> None:
    """`ENVIRONMENT` decides whether production guardrails engage.

    It defaults to "local", so a deploy that never sets it runs unguarded. The
    resolved value must be observable without reading the host's secret store.
    """

    from app.core.health import gather_dependency_report

    report = await gather_dependency_report()

    assert "environment" in report, "health must report the resolved ENVIRONMENT"
    assert "production_like" in report, (
        "health must report whether production trust guardrails engage"
    )
    assert isinstance(report["production_like"], bool)

    from app.core.config import get_settings
    from app.core.trust_mode import is_production_like_environment

    settings = get_settings()
    assert report["environment"] == settings.ENVIRONMENT
    assert report["production_like"] is is_production_like_environment(
        settings.ENVIRONMENT
    )


# --- Operator docs quote what the code does --------------------------------

_ERROR_HEADING = re.compile(r"^### `([A-Za-z_][\w.]*Error): (.+)`$", re.MULTILINE)
# Raised by Python or third-party tooling, not by this codebase.
_EXTERNAL_ERRORS = frozenset({"ModuleNotFoundError", "alembic.util.exc.CommandError"})


def _app_source_with_joined_literals() -> str:
    """All of app/, with adjacent string literals joined across line breaks."""

    source = "\n".join(
        path.read_text() for path in sorted((REPO_ROOT / "app").rglob("*.py"))
    )
    return re.sub(r'"\s*\n\s*"', "", source)


def test_troubleshooting_error_headings_match_raised_errors() -> None:
    """Each `XxxError: message` heading must be an error the app raises.

    The headings once quoted a `ValueError` and a `RuntimeError` text that no
    code raised, so an operator searching for their traceback found nothing.
    """

    doc = (REPO_ROOT / "TROUBLESHOOTING.md").read_text()
    app_errors = [
        (name, message)
        for name, message in _ERROR_HEADING.findall(doc)
        if name not in _EXTERNAL_ERRORS
    ]
    assert app_errors, "expected startup-error headings in TROUBLESHOOTING.md"
    source = _app_source_with_joined_literals()
    for name, message in app_errors:
        assert re.search(rf"^class {re.escape(name)}\(", source, re.MULTILINE), (
            f"TROUBLESHOOTING.md names {name}, which app/ does not define"
        )
        assert message in source, (
            f"TROUBLESHOOTING.md quotes `{name}: {message}`, which app/ never raises"
        )
