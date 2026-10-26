"""Validate a repository-root release stamp for the production image.

``.build_commit_sha`` is gitignored. A named Docker COPY of that path fails on a
clean tree before the build starts, and no workflow in this repo creates the
file. The production Dockerfile runs this script instead.

Sources, in precedence order:

1. ``RELEASE_COMMIT_SHA`` build arg: explicit operator override (kept so a
   pinned staging service variable keeps working).
2. ``RAILWAY_GIT_COMMIT_SHA`` build arg: Railway's own deployment metadata,
   passed in because the Dockerfile declares it as an ARG.
3. ``GIT_COMMIT_SHA`` build arg: passed by CI (``git rev-parse HEAD``).
4. A stamp at the repository build-context root (release archive).

Every source that is set must be a 7-40 character hex SHA, and every set
source must agree (case-insensitively); otherwise the build fails. A stale
pinned value therefore cannot win over the real commit: it fails the build.
The build also fails when no source is set. ``BUILD_COMMIT_SHA`` and
``COMMIT_SHA`` are deliberately not read, because they are commonly stale
Railway service variables.

``STAGED_SHA_PATH`` and ``BUILD_SHA_PATH`` select the input and output paths
for the image metadata stage and tests. The two paths must be distinct in the
image so a stray ``gateway/.build_commit_sha`` cannot become an input.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Git object names (SHA-1, abbreviated to at least 7). Kept local so the image
# build does not import application settings.
_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_DEFAULT_PATH = Path("/app/.build_commit_sha")
_MISSING = (
    "Release commit stamp missing. Stage .build_commit_sha in the build "
    "context or pass --build-arg GIT_COMMIT_SHA=$(git rev-parse HEAD) "
    "(Railway supplies RAILWAY_GIT_COMMIT_SHA for Git-triggered deploys)."
)


def _valid(value: str) -> bool:
    return _GIT_SHA_RE.fullmatch(value) is not None


def resolve_stamp(
    staged: str | None,
    build_arg: str = "",
    railway_sha: str = "",
    ci_sha: str = "",
) -> str:
    """Return the lowercase SHA to write.

    ``staged`` is the file contents, or ``None`` when the file is absent.
    ``build_arg`` is ``RELEASE_COMMIT_SHA``, ``railway_sha`` is
    ``RAILWAY_GIT_COMMIT_SHA`` and ``ci_sha`` is ``GIT_COMMIT_SHA``.
    """

    sources: list[tuple[str, str]] = []
    for name, raw in (
        ("RELEASE_COMMIT_SHA", build_arg),
        ("RAILWAY_GIT_COMMIT_SHA", railway_sha),
        ("GIT_COMMIT_SHA", ci_sha),
    ):
        value = (raw or "").strip()
        if not value:
            continue
        if not _valid(value):
            raise SystemExit(f"{name} is not a 7-40 character hex git SHA.")
        sources.append((name, value.lower()))
    if staged is not None:
        staged_sha = staged.strip()
        if not _valid(staged_sha):
            raise SystemExit(
                "Staged .build_commit_sha is not a 7-40 character hex git SHA."
            )
        sources.append(("staged .build_commit_sha", staged_sha.lower()))

    if not sources:
        raise SystemExit(_MISSING)
    winner_name, winner = sources[0]
    for name, value in sources[1:]:
        if value != winner:
            raise SystemExit(f"{winner_name} disagrees with {name}.")
    return winner


def main() -> int:
    path = Path(os.environ.get("BUILD_SHA_PATH", str(_DEFAULT_PATH)))
    staged_path = Path(os.environ.get("STAGED_SHA_PATH", str(path)))
    staged: str | None
    try:
        staged = staged_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        staged = None
    except OSError as exc:
        raise SystemExit(f"Cannot read {staged_path}: {exc}") from exc

    sha = resolve_stamp(
        staged,
        os.environ.get("RELEASE_COMMIT_SHA", ""),
        os.environ.get("RAILWAY_GIT_COMMIT_SHA", ""),
        os.environ.get("GIT_COMMIT_SHA", ""),
    )
    path.write_text(sha + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
