"""Build provenance reporting.

`get_build_commit_sha` answers which SHA is running; `get_build_provenance`
answers where that answer came from. Only an operator build through
the documented archive-stamped release context writes /app/.build_commit_sha,
so the absence of that stamp — or its disagreement with Railway's control-plane
metadata — identifies an image that did not come through the release path.

These tests must not depend on a real /app/.build_commit_sha existing, so the
module-level path is monkeypatched in every case.
"""

import pytest

from app.core import build_metadata
from app.core.build_metadata import get_build_provenance

SHA_A = "a" * 40
SHA_B = "b" * 40


@pytest.fixture
def baked(tmp_path, monkeypatch):
    """Return a setter for the baked stamp file's contents (None = absent)."""

    target = tmp_path / ".build_commit_sha"
    monkeypatch.setattr(build_metadata, "_BUILD_SHA_FILE", target)

    def _set(contents: str | None) -> None:
        if contents is None:
            if target.exists():
                target.unlink()
        else:
            target.write_text(contents, encoding="utf-8")

    _set(None)
    return _set


def test_stamped_when_both_present_and_agree(baked, monkeypatch):
    baked(SHA_A)
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", SHA_A)
    assert get_build_provenance() == "stamped"


def test_stamped_is_case_insensitive(baked, monkeypatch):
    baked(SHA_A.upper())
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", SHA_A.lower())
    assert get_build_provenance() == "stamped"


def test_stamped_tolerates_trailing_newline(baked, monkeypatch):
    # The Dockerfile writes the stamp with `echo`, so it carries a newline.
    baked(SHA_A + "\n")
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", SHA_A)
    assert get_build_provenance() == "stamped"


def test_stamped_when_only_baked_present(baked, monkeypatch):
    baked(SHA_A)
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    assert get_build_provenance() == "stamped"


def test_mismatch_when_sources_disagree(baked, monkeypatch):
    baked(SHA_A)
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", SHA_B)
    assert get_build_provenance() == "mismatch"


def test_control_plane_only_without_baked_stamp(baked, monkeypatch):
    """The signature of the 2026-08-26 incident: Railway rebuilt from branch
    HEAD without an immutable staged release context, so no baked stamp exists."""
    baked(None)
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", SHA_A)
    assert get_build_provenance() == "control_plane_only"


def test_unstamped_when_neither_source_present(baked, monkeypatch):
    baked(None)
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    assert get_build_provenance() == "unstamped"


@pytest.mark.parametrize("garbage", ["", "   ", "not-a-sha", "zz" * 20])
def test_invalid_baked_stamp_is_not_trusted(baked, monkeypatch, garbage):
    baked(garbage)
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", SHA_A)
    assert get_build_provenance() == "control_plane_only"


@pytest.mark.parametrize("garbage", ["", "   ", "not-a-sha"])
def test_invalid_control_plane_value_is_not_trusted(baked, monkeypatch, garbage):
    baked(None)
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", garbage)
    assert get_build_provenance() == "unstamped"


@pytest.mark.parametrize(
    "error",
    [
        OSError("unreadable"),
        PermissionError("permission denied"),
        IsADirectoryError("is a directory"),
    ],
)
def test_filesystem_errors_never_propagate(baked, monkeypatch, error):
    """Provenance is a reporting surface: it must never take a service down.

    Path.exists() can itself raise on the Python versions this project
    supports, so the whole probe - not just the read - has to be guarded.
    A raising filesystem degrades to "no stamp", never to an exception
    escaping into gather_dependency_report().
    """
    baked(SHA_A)

    def _boom(*args, **kwargs):
        raise error

    monkeypatch.setattr(
        type(build_metadata._BUILD_SHA_FILE), "read_text", _boom, raising=True
    )
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", SHA_A)
    assert get_build_provenance() == "control_plane_only"


def test_missing_stamp_file_is_not_an_error(baked, monkeypatch):
    """FileNotFoundError is an OSError subclass - absence is the common path."""
    baked(None)
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", SHA_A)
    assert get_build_provenance() == "control_plane_only"


def test_reported_sha_precedence_is_unchanged(baked, monkeypatch):
    """This change must not alter which SHA is reported — only how it is
    explained. Railway's value still wins, per test_commit_sha_precedence."""
    baked(SHA_A)
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", SHA_B)
    assert build_metadata.get_build_commit_sha() == SHA_B
    assert get_build_provenance() == "mismatch"


# ---------------------------------------------------------------------------
# The health -> preflight contract.
#
# The classifier is only useful if its value actually reaches the surface the
# release gate reads. If the public projection ever drops or renames this key,
# check_live() silently takes its older-image note path and the gate passes
# without verifying provenance at all - so pin both ends.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_full_dependency_report_publishes_build_provenance(baked, monkeypatch):
    from app.core import health

    baked(SHA_A)
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", SHA_A)
    report = await health.gather_dependency_report()
    assert report["build_provenance"] == "stamped"


@pytest.mark.anyio
async def test_public_dependency_report_publishes_build_provenance(baked, monkeypatch):
    from app.core import health

    baked(None)
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", SHA_A)
    full = await health.gather_dependency_report()
    public = health.build_public_dependency_report(full)
    assert public["build_provenance"] == "control_plane_only"


async def test_liveness_probe_publishes_build_provenance(baked, monkeypatch):
    """``/health`` carries the same provenance answer as ``/health/dependencies``.

    An operator comparing a live SHA against ``main`` needs to know whether the
    SHA is trustworthy before deciding the deployment is merely behind; the
    cheapest public probe must answer that on its own.
    """
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "")
    baked("a" * 40)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        liveness = await client.get("/health")
        dependencies = await client.get("/health/dependencies")
    assert liveness.status_code == 200
    assert liveness.json()["commit_sha"] == "a" * 40
    assert liveness.json()["build_provenance"] == "stamped"
    assert (
        liveness.json()["build_provenance"] == dependencies.json()["build_provenance"]
    )
