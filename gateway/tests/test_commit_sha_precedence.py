"""
Test commit SHA precedence: Railway runtime env > baked build arg > stale vars.

Verifies that RAILWAY_GIT_COMMIT_SHA (Railway's automatic deployment metadata)
always wins over BUILD_COMMIT_SHA (from Docker build arg or stale service vars),
preventing stale SHA issues when service variables persist across deploys.
"""

import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.core.config import get_settings
from app.core.build_metadata import get_build_commit_sha


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def test_railway_git_commit_sha_wins_over_build_commit_sha(monkeypatch):
    """RAILWAY_GIT_COMMIT_SHA must take precedence over BUILD_COMMIT_SHA."""
    fresh_railway_sha = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    stale_build_sha = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    get_settings.cache_clear()
    settings = get_settings()
    settings.BUILD_COMMIT_SHA = stale_build_sha
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", fresh_railway_sha)

    result = get_build_commit_sha()
    assert result == fresh_railway_sha, (
        "RAILWAY_GIT_COMMIT_SHA (fresh) must win over BUILD_COMMIT_SHA (stale)"
    )

    get_settings.cache_clear()


def test_returns_none_when_railway_unset_and_only_build_commit_sha(monkeypatch):
    """Returns None when only BUILD_COMMIT_SHA env exists (no Railway, no baked file).

    BUILD_COMMIT_SHA env is no longer trusted to prevent stale service variables.
    """
    build_sha = "cccccccccccccccccccccccccccccccccccccccc"

    get_settings.cache_clear()
    settings = get_settings()
    settings.BUILD_COMMIT_SHA = build_sha
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)

    result = get_build_commit_sha()
    assert result is None, (
        "Must return None (not BUILD_COMMIT_SHA env) to prevent stale service vars"
    )

    get_settings.cache_clear()


def test_railway_empty_returns_none_not_build_commit_sha(monkeypatch):
    """Empty RAILWAY_GIT_COMMIT_SHA returns None (not BUILD_COMMIT_SHA env)."""
    build_sha = "dddddddddddddddddddddddddddddddddddddddd"

    get_settings.cache_clear()
    settings = get_settings()
    settings.BUILD_COMMIT_SHA = build_sha
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "")

    result = get_build_commit_sha()
    assert result is None, (
        "Empty Railway env + no baked file = None (not BUILD_COMMIT_SHA env)"
    )

    get_settings.cache_clear()


def test_railway_invalid_returns_none_not_build_commit_sha(monkeypatch):
    """Invalid RAILWAY_GIT_COMMIT_SHA returns None (not BUILD_COMMIT_SHA env)."""
    build_sha = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    invalid_railway = "not-a-valid-sha"

    get_settings.cache_clear()
    settings = get_settings()
    settings.BUILD_COMMIT_SHA = build_sha
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", invalid_railway)

    result = get_build_commit_sha()
    assert result is None, (
        "Invalid Railway env + no baked file = None (not BUILD_COMMIT_SHA env)"
    )

    get_settings.cache_clear()


def test_both_invalid_returns_none(monkeypatch):
    """When both sources are invalid, return None."""
    get_settings.cache_clear()
    settings = get_settings()
    settings.BUILD_COMMIT_SHA = "not-a-sha"
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "also-not-a-sha")

    result = get_build_commit_sha()
    assert result is None, (
        "When both RAILWAY_GIT_COMMIT_SHA and BUILD_COMMIT_SHA are invalid, return None"
    )

    get_settings.cache_clear()


def test_both_empty_returns_none(monkeypatch):
    """When both sources are empty, return None."""
    get_settings.cache_clear()
    settings = get_settings()
    settings.BUILD_COMMIT_SHA = ""
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "")

    result = get_build_commit_sha()
    assert result is None, (
        "When both RAILWAY_GIT_COMMIT_SHA and BUILD_COMMIT_SHA are empty, return None"
    )

    get_settings.cache_clear()


@pytest.mark.anyio
async def test_health_reports_railway_sha_over_stale_build_sha(client, monkeypatch):
    """Health endpoints must report RAILWAY_GIT_COMMIT_SHA over stale BUILD_COMMIT_SHA."""
    fresh_railway_sha = "1111111111111111111111111111111111111111"
    stale_build_sha = "a928fd6f00000000000000000000000000000000"

    get_settings.cache_clear()
    settings = get_settings()
    settings.BUILD_COMMIT_SHA = stale_build_sha
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", fresh_railway_sha)

    liveness = await client.get("/health")
    dependencies = await client.get("/health/dependencies")

    assert liveness.status_code == 200
    assert dependencies.status_code == 200

    assert liveness.json()["commit_sha"] == fresh_railway_sha
    assert dependencies.json()["commit_sha"] == fresh_railway_sha

    assert stale_build_sha not in liveness.text
    assert stale_build_sha not in dependencies.text

    get_settings.cache_clear()


@pytest.mark.anyio
async def test_health_returns_null_when_railway_absent_and_no_baked_file(
    client, monkeypatch
):
    """Health returns null when Railway absent and no baked file (not BUILD_COMMIT_SHA env)."""
    build_sha = "2222222222222222222222222222222222222222"

    get_settings.cache_clear()
    settings = get_settings()
    settings.BUILD_COMMIT_SHA = build_sha
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)

    liveness = await client.get("/health")
    dependencies = await client.get("/health/dependencies")

    assert liveness.status_code == 200
    assert dependencies.status_code == 200

    # Must return null, not BUILD_COMMIT_SHA env
    assert liveness.json()["commit_sha"] is None
    assert dependencies.json()["commit_sha"] is None

    get_settings.cache_clear()
