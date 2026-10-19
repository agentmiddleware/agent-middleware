#!/usr/bin/env python3
"""Check the repository-owned inputs to the gateway images.

This is a source preflight, not a Docker parser or a hosted deployment check.
It checks the COPY/install paths and runs the stamp writer with isolated files.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def _instructions(path: Path) -> list[str]:
    lines: list[str] = []
    pending = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pending += " " + line.rstrip("\\").strip()
        if not line.endswith("\\"):
            lines.append(re.sub(r"\s+", " ", pending.strip()))
            pending = ""
    if pending:
        lines.append(re.sub(r"\s+", " ", pending.strip()))
    return lines


def _require(lines: list[str], pattern: str, label: str) -> int:
    for index, line in enumerate(lines):
        if re.search(pattern, line, re.IGNORECASE):
            return index
    raise ValueError(f"missing {label}")


def _check_writer(path: Path) -> None:
    valid = "a" * 40
    other = "b" * 40
    cases = (
        (None, "", False, None),
        (None, valid.upper(), True, valid),
        (valid.upper(), "", True, valid),
        (valid.upper(), valid, True, valid),
        (valid, other, False, None),
        ("not-a-sha", valid, False, None),
        (valid, "not-a-sha", False, None),
    )
    with tempfile.TemporaryDirectory(prefix="gateway-stamp-check-") as directory:
        root = Path(directory)
        staged = root / ".build_commit_sha"
        output = root / "output" / ".build_commit_sha"
        output.parent.mkdir()
        for staged_value, argument, succeeds, expected in cases:
            staged.unlink(missing_ok=True)
            output.unlink(missing_ok=True)
            if staged_value is not None:
                staged.write_text(staged_value + "\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(path)],
                env={
                    "STAGED_SHA_PATH": str(staged),
                    "BUILD_SHA_PATH": str(output),
                    "RELEASE_COMMIT_SHA": argument,
                },
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if (result.returncode == 0) != succeeds:
                raise ValueError(
                    "stamp writer accepted invalid or rejected valid provenance"
                )
            if succeeds and output.read_text(encoding="utf-8") != expected + "\n":
                raise ValueError(
                    "stamp writer did not write normalized SHA and newline"
                )
        # A gateway-local stamp must never stand in for the root context stamp.
        staged.unlink(missing_ok=True)
        (root / "gateway").mkdir()
        (root / "gateway" / ".build_commit_sha").write_text(valid + "\n")
        result = subprocess.run(
            [sys.executable, str(path)],
            env={
                "STAGED_SHA_PATH": str(staged),
                "BUILD_SHA_PATH": str(output),
                "RELEASE_COMMIT_SHA": "",
            },
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            raise ValueError("gateway-local stamp bypassed missing root provenance")


def check(root: Path) -> None:
    gateway = root / "gateway"
    files = (
        "gateway/Dockerfile",
        "gateway/Dockerfile.dev",
        "gateway/docker-compose.yml",
        "gateway/requirements.txt",
        "gateway/scripts/docker_entrypoint.sh",
        "gateway/scripts/write_build_commit_sha.py",
        "sdk/python/pyproject.toml",
        "sdk/python/src/b2a_sdk/__init__.py",
        "sdk/python/src/b2a_sdk/receipt_verifier.py",
        ".dockerignore",
    )
    missing = [name for name in files if not (root / name).is_file()]
    if missing:
        raise ValueError(f"missing image input: {', '.join(missing)}")
    if not (gateway / "scripts/docker_entrypoint.sh").stat().st_mode & 0o111:
        raise ValueError("gateway entrypoint is not executable")

    ignored = {
        line.strip()
        for line in (root / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    required_ignores = {
        ".git",
        ".env",
        ".env.*",
        "**/.env",
        "**/.env.*",
        ".railway",
        "**/.railway",
        "*.pem",
        "**/*.pem",
        "*.key",
        "**/*.key",
        "gateway/.build_commit_sha",
        "gateway/tests",
    }
    if missing_ignores := required_ignores - ignored:
        raise ValueError(
            f"root Docker context misses exclusions: {sorted(missing_ignores)}"
        )
    if any(line.startswith("!") for line in ignored):
        raise ValueError("root Docker context contains a re-inclusion rule")

    compose = (gateway / "docker-compose.yml").read_text(encoding="utf-8")
    if not re.search(r"(?m)^\s+context:\s+\.\.\s*$", compose):
        raise ValueError("compose must build from repository root")
    if not re.search(r"(?m)^\s+dockerfile:\s+gateway/Dockerfile\.dev\s*$", compose):
        raise ValueError("compose must use gateway/Dockerfile.dev from root")

    for name in ("Dockerfile", "Dockerfile.dev"):
        instructions = _instructions(gateway / name)
        stages = [
            i for i, line in enumerate(instructions) if line.upper().startswith("FROM ")
        ]
        if not stages or not re.search(
            r"^FROM\s+python:", instructions[stages[-1]], re.IGNORECASE
        ):
            raise ValueError(f"{name} has no final Python runtime stage")
        runtime = instructions[stages[-1] :]
        _require(
            runtime, r"^ENV\s+.*PYTHONPATH=/sdk/python/src(?:\s|$)", "SDK import path"
        )
        _require(runtime, r"^COPY\s+sdk/python\s+/sdk/python/?$", "SDK source copy")
        _require(
            runtime,
            r"^COPY\s+gateway/requirements\.txt\s+",
            "gateway requirements copy",
        )
        _require(
            runtime, r"^RUN\s+.*pip install\s+.* /sdk/python(?:\s|$)", "SDK install"
        )
        _require(runtime, r"^COPY\s+gateway/\s+/app/?$", "gateway-only app copy")
        _require(
            runtime, r'^ENTRYPOINT\s+\["scripts/docker_entrypoint\.sh"\]$', "entrypoint"
        )

    prod = _instructions(gateway / "Dockerfile")
    metadata_start = _require(
        prod, r"^FROM\s+\S+\s+AS\s+build-metadata$", "metadata stage"
    )
    final_start = next(
        (
            i
            for i in range(metadata_start + 1, len(prod))
            if prod[i].upper().startswith("FROM ")
        ),
        None,
    )
    if final_start is None:
        raise ValueError("metadata stage has no final runtime stage")
    metadata, runtime = prod[metadata_start:final_start], prod[final_start:]
    context_copy = _require(
        metadata, r"^COPY\s+\.\s+/context/?$", "root context staging"
    )
    argument = _require(
        metadata, r"^ARG\s+RELEASE_COMMIT_SHA(?:=|$)", "release SHA argument"
    )
    validation = _require(
        metadata,
        r"^RUN\s+.*STAGED_SHA_PATH=/context/\.build_commit_sha.*BUILD_SHA_PATH=/stamp/\.build_commit_sha.*python\s+(?:/context/)?gateway/scripts/write_build_commit_sha\.py(?:\s|$)",
        "root stamp validation",
    )
    if validation <= context_copy or validation <= argument:
        raise ValueError(
            "root context and release SHA argument must precede validation"
        )
    app_copy = _require(runtime, r"^COPY\s+gateway/\s+/app/?$", "gateway-only app copy")
    stamp_copy = _require(
        runtime,
        r"^COPY\s+--from=build-metadata\s+/stamp/\.build_commit_sha\s+/app/\.build_commit_sha$",
        "validated stamp transfer",
    )
    if stamp_copy <= app_copy:
        raise ValueError("validated stamp must replace any gateway-local stamp")
    if any(re.search(r"^COPY\s+\.\s+", line, re.IGNORECASE) for line in runtime):
        raise ValueError("runtime stage copies the full repository context")
    _check_writer(gateway / "scripts/write_build_commit_sha.py")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[2]
    )
    args = parser.parse_args()
    try:
        check(args.root.resolve())
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"gateway image source contract failed: {exc}", file=sys.stderr)
        sys.exit(1)
    print("gateway image source contract passed")
