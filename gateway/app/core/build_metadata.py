"""Public, non-secret build metadata for runtime health surfaces."""

from __future__ import annotations

import os
import re
from pathlib import Path


_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_BUILD_SHA_FILE = Path("/app/.build_commit_sha")


def get_build_commit_sha() -> str | None:
    """Return a validated deployed Git SHA, or ``None`` when unavailable.

    Precedence (highest to lowest):
    1. ``RAILWAY_GIT_COMMIT_SHA`` — Railway's automatic deployment metadata
    2. ``/app/.build_commit_sha`` — SHA baked into image at build time
    3. ``None`` — Do not trust BUILD_COMMIT_SHA env (stale service variable)

    Railway's automatic env is checked first. The baked file is trusted when
    present. If neither exists, return None instead of reading BUILD_COMMIT_SHA
    from env to prevent stale COMMIT_SHA/BUILD_COMMIT_SHA service variables
    from winning.

    For local dev without Docker, set RAILWAY_GIT_COMMIT_SHA explicitly.
    """

    candidates = [
        os.getenv("RAILWAY_GIT_COMMIT_SHA", ""),
    ]

    # Read baked SHA file if it exists
    if _BUILD_SHA_FILE.exists():
        try:
            candidates.append(_BUILD_SHA_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Do NOT read BUILD_COMMIT_SHA from env - it may be a stale service variable
    # If neither Railway env nor baked file is available, return None

    for candidate in candidates:
        normalized = candidate.strip()
        if _GIT_SHA_RE.fullmatch(normalized):
            return normalized.lower()
    return None


def get_build_provenance() -> str:
    """Report how this image's commit SHA was established.

    ``get_build_commit_sha`` answers *which* SHA is running. This answers
    *where that answer came from*, which is what distinguishes a release built
    through the documented operator path from one Railway rebuilt on its own.

    The documented Railway release path builds a temporary context from
    ``git archive`` at the exact release SHA and adds a stamp file before
    upload. The Dockerfile requires that file at build time. Railway sets
    ``RAILWAY_GIT_COMMIT_SHA`` only for GitHub-triggered deployments, so a
    local ``railway up`` release relies on the baked stamp. A disagreement —
    or a missing stamp — is the signature of a build that did not come through
    the SOP.

    Returns one of:

    - ``"stamped"`` — a valid baked stamp exists and, when the control plane
      also reports a SHA, the two agree. This is the only value a release built
      through the documented path can produce.
    - ``"mismatch"`` — both sources are valid and disagree. The image was built
      from one commit and deployed as another.
    - ``"control_plane_only"`` — no valid baked stamp; the SHA comes solely from
      Railway's deployment metadata. An operator build cannot produce this.
    - ``"unstamped"`` — neither source yields a valid SHA.

    This is a reporting surface, not a guardrail: it never raises and never
    changes which SHA is reported, so enabling it cannot take a running service
    down. Gate on it from the release preflight instead.
    """

    # Read without an exists() pre-check: on the Python versions this project
    # supports, Path.exists() can itself propagate an OSError rather than
    # returning False, which would break the never-raises guarantee above.
    # A missing file raises FileNotFoundError, an OSError subclass, so one
    # guarded read covers absence and inaccessibility alike.
    baked = ""
    try:
        baked = _BUILD_SHA_FILE.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        baked = ""
    if not _GIT_SHA_RE.fullmatch(baked):
        baked = ""

    control_plane = os.getenv("RAILWAY_GIT_COMMIT_SHA", "").strip()
    if not _GIT_SHA_RE.fullmatch(control_plane):
        control_plane = ""

    if baked and control_plane:
        return "stamped" if baked.lower() == control_plane.lower() else "mismatch"
    if baked:
        return "stamped"
    if control_plane:
        return "control_plane_only"
    return "unstamped"
