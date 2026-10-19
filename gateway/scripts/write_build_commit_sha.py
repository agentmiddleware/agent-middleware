"""Bake /app/.build_commit_sha during the production image build.

``.build_commit_sha`` is gitignored. A named ``COPY`` of that path fails on a
clean tree before the build starts, and no workflow in this repo creates the
file. The production Dockerfile runs this script instead.

Sources:

1. A stamp already copied into the image (release archive). It is authoritative.
2. The ``RELEASE_COMMIT_SHA`` build arg, for ``docker build`` on a clean tree.

The build fails when neither source is a 7–64 character hex SHA, when the
staged file is present but invalid, or when both sources are valid and
disagree. The arg is not named ``COMMIT_SHA`` or ``BUILD_COMMIT_SHA`` so a
stale Railway service variable is not picked up as the stamp.

``BUILD_SHA_PATH`` overrides the output path (tests only).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Same shape as app.core.build_metadata._GIT_SHA_RE. Kept local so image
# build does not import application settings.
_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_DEFAULT_PATH = Path("/app/.build_commit_sha")
_MISSING = (
    "Release commit stamp missing. Stage .build_commit_sha in the build "
    "context or pass --build-arg RELEASE_COMMIT_SHA=$(git rev-parse HEAD)."
)


def _valid(value: str) -> bool:
    return _GIT_SHA_RE.fullmatch(value) is not None


def resolve_stamp(staged: str | None, build_arg: str) -> str:
    """Return the lowercase SHA to write.

    ``staged`` is the file contents, or ``None`` when the file is absent.
    """

    arg = build_arg.strip()
    if arg and not _valid(arg):
        raise SystemExit(
            "RELEASE_COMMIT_SHA is not a 7-64 character hex git SHA."
        )

    if staged is None:
        if not _valid(arg):
            raise SystemExit(_MISSING)
        return arg.lower()

    staged_sha = staged.strip()
    if not _valid(staged_sha):
        raise SystemExit(
            "Staged .build_commit_sha is not a 7-64 character hex git SHA."
        )
    if arg and arg.lower() != staged_sha.lower():
        raise SystemExit(
            "RELEASE_COMMIT_SHA disagrees with staged .build_commit_sha."
        )
    return staged_sha.lower()


def main() -> int:
    path = Path(os.environ.get("BUILD_SHA_PATH", str(_DEFAULT_PATH)))
    staged: str | None
    try:
        staged = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        staged = None
    except OSError as exc:
        raise SystemExit(f"Cannot read {path}: {exc}") from exc

    sha = resolve_stamp(staged, os.environ.get("RELEASE_COMMIT_SHA", ""))
    path.write_text(sha + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
