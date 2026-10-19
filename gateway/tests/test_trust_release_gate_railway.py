"""Pins the trust release gate's Railway IaC step to a real Makefile target.

``scripts/trust_release_gate.sh`` invoked ``make check-railway-iac`` while the
Makefile defined no such target, so ``set -e`` ended the gate before any trust
test ran. These checks make the script-to-Makefile contract explicit: every
``make`` target the gate invokes must be defined, and the Railway check must
keep covering the files the ``railway up`` Docker build needs.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE_SCRIPT = REPO_ROOT / "scripts" / "trust_release_gate.sh"
MAKEFILE = REPO_ROOT / "Makefile"

# Files the `railway up` Docker build COPYs or executes; mirrors the
# `check-railway-iac` Makefile target.
RAILWAY_CONTRACT_FILES = (
    "Dockerfile",
    "requirements.txt",
    "scripts/docker_entrypoint.sh",
)


def _invoked_make_targets(script_text: str) -> list[str]:
    """Bare ``make <target>`` invocations, flag arguments skipped."""
    # Flags that consume the following token (`make -C dir target`).
    _ARG_FLAGS = frozenset({"-C", "-f"})
    targets = []
    for line in script_text.splitlines():
        match = re.match(r"\s*make\s+(.*)", line)
        if not match:
            continue
        skip_next = False
        for token in match.group(1).split():
            if skip_next:
                skip_next = False
                continue
            if token in _ARG_FLAGS:
                skip_next = True
                continue
            if token.startswith("-"):
                continue
            if "=" in token:
                continue
            targets.append(token)
            break
    return targets


def _defined_make_targets(makefile_text: str) -> set[str]:
    """Targets defined as ``name:`` at column zero (recipes are indented)."""
    return set(
        re.findall(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)\s*::?", makefile_text, re.MULTILINE)
    )


def test_release_gate_still_runs_the_railway_iac_step() -> None:
    """The Railway contract step must not be silently dropped from the gate."""
    assert "check-railway-iac" in _invoked_make_targets(GATE_SCRIPT.read_text())


def test_every_make_target_invoked_by_release_gate_is_defined() -> None:
    """The exact SEL-289 regression: the gate invoked an undefined target."""
    invoked = _invoked_make_targets(GATE_SCRIPT.read_text())
    assert invoked, "release gate invokes no make targets at all"
    defined = _defined_make_targets(MAKEFILE.read_text())
    missing = [target for target in invoked if target not in defined]
    assert not missing, (
        f"trust_release_gate.sh invokes undefined make targets: {missing}. "
        "Add each to gateway/Makefile or point the gate at an existing checker."
    )


def test_railway_iac_contract_files_exist() -> None:
    missing = [name for name in RAILWAY_CONTRACT_FILES if not (REPO_ROOT / name).is_file()]
    assert not missing, f"Railway deploy contract files missing: {missing}"


def test_railway_entrypoint_is_executable() -> None:
    entrypoint = REPO_ROOT / "scripts" / "docker_entrypoint.sh"
    assert os.access(entrypoint, os.X_OK), (
        "scripts/docker_entrypoint.sh must stay executable: "
        "the Dockerfile runs chmod +x on it and uses it as ENTRYPOINT"
    )


def test_make_dry_run_resolves_railway_target() -> None:
    """End to end: `make -n` proves the target resolves without running it."""
    if shutil.which("make") is None:
        import pytest

        pytest.skip("make is not installed")
    result = subprocess.run(
        ["make", "-n", "check-railway-iac"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"`make -n check-railway-iac` failed: {result.stderr.strip()}"
    )


def test_invoked_targets_parser_skips_flags_and_assignments() -> None:
    script = "make -C other check-railway-iac\nmake FOO=1 trust-release-gate\n"
    assert _invoked_make_targets(script) == ["check-railway-iac", "trust-release-gate"]
