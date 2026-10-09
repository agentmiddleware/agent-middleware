"""Deployment image builds must install a hash-pinned lockfile.

requirements.txt records direct packages and the oldest versions we accept.
It is not a reproducible install: pip and uv can resolve a newer release on
the next build. Image builds have to install gateway/requirements.lock, where
every distribution is an exact pin with a sha256 hash, and pip must be told
to reject anything that lacks one.
"""

from __future__ import annotations

import re
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

REPO_ROOT = Path(__file__).resolve().parent.parent
_IMAGE_DOCKERFILES = ("Dockerfile", "Dockerfile.dev")
_HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _logical_lines(text: str) -> list[str]:
    logical: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        code = raw.split("#", 1)[0].strip()
        if not code:
            continue
        if code.endswith("\\"):
            buffer += code[:-1].strip() + " "
            continue
        buffer += code
        logical.append(" ".join(buffer.split()))
        buffer = ""
    if buffer.strip():
        logical.append(" ".join(buffer.split()))
    return logical


def _requirements_from_lines(lines: list[str]) -> dict[str, Requirement]:
    requirements: dict[str, Requirement] = {}
    for line in lines:
        requirement = Requirement(line)
        requirements[canonicalize_name(requirement.name)] = requirement
    return requirements


def _locked_distributions(text: str) -> dict[str, list[Requirement]]:
    locked: dict[str, list[Requirement]] = {}
    for line in _logical_lines(text):
        requirement_text, separator, hash_text = line.partition("--hash=")
        assert separator, f"locked distribution has no hash: {line}"
        hashes = [
            piece.strip().split()[0]
            for piece in f"--hash={hash_text}".split("--hash=")
            if piece.strip()
        ]
        assert hashes, f"locked distribution has no hash: {requirement_text.strip()}"
        for digest in hashes:
            assert _HASH.fullmatch(digest), f"hash is not sha256: {digest}"
        requirement = Requirement(requirement_text.strip())
        specifiers = list(requirement.specifier)
        assert len(specifiers) == 1 and specifiers[0].operator == "==", requirement
        locked.setdefault(canonicalize_name(requirement.name), []).append(requirement)
    return locked


def test_lockfile_pins_direct_requirements_with_hashes() -> None:
    lock_path = REPO_ROOT / "requirements.lock"
    assert lock_path.is_file(), "image builds need gateway/requirements.lock"

    direct = _requirements_from_lines(
        _logical_lines((REPO_ROOT / "requirements.txt").read_text(encoding="utf-8"))
    )
    locked = _locked_distributions(lock_path.read_text(encoding="utf-8"))

    # The lock is a closed set. Direct pins alone are not enough: pip in
    # hash-checking mode refuses dependencies that are not listed with hashes.
    assert len(locked) > len(direct)

    missing = sorted(set(direct) - set(locked))
    assert not missing, f"requirements.txt packages missing from the lock: {missing}"

    for name, requirement in direct.items():
        for locked_requirement in locked[name]:
            version = next(iter(locked_requirement.specifier)).version
            assert version in requirement.specifier, (
                f"{name}=={version} does not satisfy {requirement}"
            )


def test_image_builds_install_the_hashed_lock() -> None:
    for name in _IMAGE_DOCKERFILES:
        instructions = _logical_lines((REPO_ROOT / name).read_text(encoding="utf-8"))
        installs = [line for line in instructions if "pip install" in line]
        assert len(installs) == 1, name
        install = installs[0]
        assert install.startswith("RUN pip install"), install
        assert "--require-hashes" in install, install
        assert "-r requirements.lock" in install, install
        assert "requirements.txt" not in install, install
        assert any(
            line.startswith("COPY ") and "requirements.lock" in line
            for line in instructions
        ), name
