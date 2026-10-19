"""Pins the scope of ``scripts/trust_coverage_gate.sh``.

The gate enforces its 80% floor only over the hand-curated
``TRUST_COVERAGE_MODULES`` list. A trust-plane module that is never added to
that list is never measured, so the gate stays green whatever its coverage —
``app.routers.mcp_standard``, ``app.core.auth`` and the audit hash chain all
sat outside it while their tests ran inside it. These checks make the list's
scope an explicit decision: every module in ``CORE_TRUST_ROUTERS``, plus the
services behind each step of the governed loop, must either be measured by
the gate or be exempted below with a written reason.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

from app.main import CORE_TRUST_ROUTERS

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE_SCRIPT = REPO_ROOT / "scripts" / "trust_coverage_gate.sh"

# Security-critical modules outside CORE_TRUST_ROUTERS, one group per step of
# discover → authenticate → authorize → invoke → meter → receipt → audit →
# govern. Removing one of these from the gate is the drift this file exists to
# catch, so they are required just like the routers that call them.
SECURITY_CRITICAL_MODULES = frozenset(
    {
        # authenticate: credential validation, key issuance, self-serve mint
        "app.core.auth",
        "app.core.jwt",
        "app.services.api_key_service",
        "app.routers.dev_keys",
        # authorize
        "app.services.permits",
        "app.services.policies",
        # invoke: exactly-once dispatch
        "app.services.idempotency",
        "app.services.mcp_dispatch_attempts",
        "app.services.upstream_mcp",
        # meter
        "app.services.billing_engine",
        "app.services.governed_metering",
        # receipt
        "app.services.receipts",
        "app.services.signing_keys",
        "app.trust.evidence",
        # audit
        "app.services.audit_chain",
        # govern
        "app.core.trust_mode",
    }
)

# Modules the gate deliberately does not measure. Each one must say why, so the
# omission is a reviewed decision rather than drift. Listing a module in the
# gate requires removing it from here.
EXEMPT: dict[str, str] = {
    "app.routers.static": (
        "serves llms.txt and advertised markdown files read from disk; makes no "
        "authentication, authorization, metering or receipt decision"
    ),
    "app.routers.docs": (
        "agent-readable documentation index and llms.txt aliases built from "
        "static metadata; makes no trust decision"
    ),
}


def _bash_array(script_text: str, name: str) -> list[str]:
    """Entries of a top-level ``NAME=( ... )`` array, comments dropped."""

    match = re.search(
        rf"^{re.escape(name)}=\(\n(.*?)^\)", script_text, re.MULTILINE | re.DOTALL
    )
    assert match, f"{name}=( ... ) not found in {GATE_SCRIPT.name}"
    entries = []
    for line in match.group(1).splitlines():
        entry = line.split("#", 1)[0].strip()
        if entry:
            entries.append(entry)
    return entries


def _measured_modules() -> list[str]:
    return _bash_array(GATE_SCRIPT.read_text(), "TRUST_COVERAGE_MODULES")


def _required_modules() -> set[str]:
    return {module.__name__ for module in CORE_TRUST_ROUTERS} | set(
        SECURITY_CRITICAL_MODULES
    )


def _unaccounted(
    required: Iterable[str], measured: Iterable[str], exempt: Mapping[str, str]
) -> list[str]:
    return sorted(set(required) - set(measured) - set(exempt))


def test_every_trust_module_is_measured_or_exempted_with_a_reason() -> None:
    unaccounted = _unaccounted(_required_modules(), _measured_modules(), EXEMPT)
    assert not unaccounted, (
        "trust modules outside the coverage gate's scope: "
        f"{unaccounted}. Add each to TRUST_COVERAGE_MODULES in "
        "scripts/trust_coverage_gate.sh (with the tests that exercise it in "
        "TRUST_COVERAGE_TESTS), or exempt it in EXEMPT here with a reason."
    )


def test_exemptions_are_justified_and_not_stale() -> None:
    measured = set(_measured_modules())
    required = _required_modules()
    for module, reason in EXEMPT.items():
        assert len(reason.split()) >= 8, f"{module}: exemption needs a real reason"
        assert module in required, (
            f"{module} is exempt but no longer a core trust module; drop the entry"
        )
        assert module not in measured, (
            f"{module} is both measured and exempt; drop the exemption"
        )


def test_measured_modules_resolve_and_are_unique() -> None:
    # coverage.py only warns ("module-not-imported") for a --cov target that
    # never loads, so a misspelled entry would silently measure nothing.
    measured = _measured_modules()
    assert len(measured) == len(set(measured)), "duplicate TRUST_COVERAGE_MODULES"
    unresolved = [name for name in measured if importlib.util.find_spec(name) is None]
    assert not unresolved, f"TRUST_COVERAGE_MODULES names no module: {unresolved}"


def test_listed_test_files_exist() -> None:
    tests = _bash_array(GATE_SCRIPT.read_text(), "TRUST_COVERAGE_TESTS")
    assert len(tests) == len(set(tests)), "duplicate TRUST_COVERAGE_TESTS"
    missing = [path for path in tests if not (REPO_ROOT / path).is_file()]
    assert not missing, f"TRUST_COVERAGE_TESTS lists missing files: {missing}"


def test_gate_keeps_its_floor_over_the_listed_modules() -> None:
    text = GATE_SCRIPT.read_text()
    assert 'for module in "${TRUST_COVERAGE_MODULES[@]}"' in text
    assert 'COV_ARGS+=("--cov=$module")' in text
    floors = [int(value) for value in re.findall(r"--cov-fail-under=(\d+)", text)]
    assert floors and min(floors) >= 80, "trust coverage floor dropped below 80%"


def test_scope_check_flags_an_unlisted_unexempted_module() -> None:
    """The check itself must catch a module that is neither listed nor exempt."""

    script = (
        "TRUST_COVERAGE_MODULES=(\n"
        "  app.routers.permits  # inline note\n"
        "  # app.routers.receipts\n"
        "\n"
        ")\n"
    )
    measured = _bash_array(script, "TRUST_COVERAGE_MODULES")
    assert measured == ["app.routers.permits"]
    required = {"app.routers.permits", "app.routers.receipts", "app.routers.static"}
    assert _unaccounted(required, measured, {"app.routers.static": "reason"}) == [
        "app.routers.receipts"
    ]
    assert _unaccounted(required, measured, {}) == [
        "app.routers.receipts",
        "app.routers.static",
    ]


def test_coverage_gate_preserves_distinct_pytest_override(tmp_path: Path) -> None:
    """The coverage preloader must run through PYTEST, not the PYTHON override."""

    pytest_stub = tmp_path / "pytest-stub"
    pytest_stub.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$GATE_CAPTURE"\nexit "$GATE_EXIT"\n'
    )
    pytest_stub.chmod(0o755)
    python_stub = tmp_path / "python-stub"
    python_stub.write_text("#!/bin/sh\nexit 42\n")
    python_stub.chmod(0o755)

    environment = os.environ.copy()
    for name in (
        "DATABASE_URL",
        "RUN_POSTGRES_CONCURRENCY_TESTS",
        "RUN_MCP_MULTIPROCESS_TESTS",
    ):
        environment.pop(name, None)
    environment.update(
        PYTHON=str(python_stub),
        PYTEST=str(pytest_stub),
        GATE_CAPTURE=str(tmp_path / "arguments"),
        GATE_EXIT="0",
    )
    result = subprocess.run(
        ["bash", str(GATE_SCRIPT)],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    arguments = (tmp_path / "arguments").read_text().splitlines()
    assert arguments[:3] == ["-p", "sqlalchemy", "-q"]
    assert "--cov=app.trust.evidence" in arguments
    assert "--cov-fail-under=80" in arguments
    environment["GATE_EXIT"] = "17"
    failed = subprocess.run(
        ["bash", str(GATE_SCRIPT)], cwd=REPO_ROOT, env=environment, check=False
    )
    assert failed.returncode == 17


def test_coverage_preload_keeps_sqlalchemy_cache_traversal(tmp_path: Path) -> None:
    """The real pytest plugin keeps cache keys valid and evidence measured."""

    probe = tmp_path / "test_cache_key.py"
    probe.write_text(
        "from sqlalchemy import select\n"
        "from app.db.models import WalletModel\n"
        "from app.trust.evidence import _metadata_from_json\n"
        "def test_cache_key():\n"
        "    assert _metadata_from_json('{}') == {}\n"
        "    assert select(WalletModel)._generate_cache_key() is not None\n"
    )
    environment = os.environ.copy()
    for name in (
        "DATABASE_URL",
        "RUN_POSTGRES_CONCURRENCY_TESTS",
        "RUN_MCP_MULTIPROCESS_TESTS",
    ):
        environment.pop(name, None)
    for name in tuple(environment):
        if name.startswith("COV_CORE_"):
            environment.pop(name)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    environment["COVERAGE_FILE"] = str(tmp_path / ".coverage")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "sqlalchemy",
            str(probe),
            "--cov=app.trust.evidence",
            "--cov-report=term",
        ],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "app/trust/evidence.py" in result.stdout
