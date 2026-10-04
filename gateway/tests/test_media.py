"""
Tests for the Programmatic Media Engine endpoints.
Validates video upload, hook detection, clip generation, and distribution.
"""

import asyncio

import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app
from tests.test_trust_helpers import BOOTSTRAP_HEADERS, provision_agent_wallet


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def api_headers():
    return {"X-API-Key": "test-key"}


# --- Video Upload ---


@pytest.mark.anyio
async def test_upload_video_with_url(client, api_headers):
    resp = await client.post(
        "/v1/media/videos",
        json={
            "source_url": "https://storage.example.com/demo.mp4",
            "title": "Test Video",
            "language": "en",
        },
        headers=api_headers,
    )
    assert resp.status_code == 202
    data = resp.json()
    assert "video_id" in data
    assert data["status"] == "processing"
    assert data["upload_url"] is None  # URL provided, no upload needed


@pytest.mark.anyio
async def test_upload_video_without_url(client, api_headers):
    resp = await client.post(
        "/v1/media/videos",
        json={"title": "Direct Upload"},
        headers=api_headers,
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "awaiting_upload"
    assert data["upload_url"] is not None


@pytest.mark.anyio
async def test_get_video_status(client, api_headers):
    upload = await client.post(
        "/v1/media/videos",
        json={"title": "Status Check", "source_url": "https://example.com/v.mp4"},
        headers=api_headers,
    )
    video_id = upload.json()["video_id"]

    resp = await client.get(f"/v1/media/videos/{video_id}", headers=api_headers)
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_video_not_found(client, api_headers):
    resp = await client.get("/v1/media/videos/nonexistent", headers=api_headers)
    assert resp.status_code == 404


# --- Hooks ---


@pytest.mark.anyio
async def test_hooks_empty_for_new_video(client, api_headers):
    upload = await client.post(
        "/v1/media/videos",
        json={"title": "Hook Test", "source_url": "https://example.com/v.mp4"},
        headers=api_headers,
    )
    video_id = upload.json()["video_id"]
    resp = await client.get(f"/v1/media/videos/{video_id}/hooks", headers=api_headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# --- Distribution ---


@pytest.mark.anyio
async def test_distribute_nonexistent_clips(client, api_headers):
    resp = await client.post(
        "/v1/media/distribute",
        json={
            "clip_ids": ["fake-clip-1"],
            "platforms": ["youtube_shorts"],
            "title": "Test Post",
        },
        headers=api_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_failed"] >= 1


# --- Clip Not Found ---


@pytest.mark.anyio
async def test_clip_not_found(client, api_headers):
    resp = await client.get("/v1/media/clips/nonexistent", headers=api_headers)
    assert resp.status_code == 404


# --- Tenant isolation ---
# Videos and clips are owned by the wallet whose key created them. Another
# wallet-scoped key must not read, render from, or distribute them, and must
# get the same answer it would for an id that does not exist (no existence
# oracle). Bootstrap admins keep full access.

_SECRET_TITLE = "Tenant A confidential launch cut"
_SECRET_SOURCE = "https://a-private.example/launch-cut.mp4"


async def _create_ready_video(client, headers) -> str:
    upload = await client.post(
        "/v1/media/videos",
        json={"title": _SECRET_TITLE, "source_url": _SECRET_SOURCE},
        headers=headers,
    )
    assert upload.status_code == 202, upload.text
    video_id = upload.json()["video_id"]
    # Hook detection runs as a background task; wait for it to finish.
    for _ in range(100):
        resp = await client.get(f"/v1/media/videos/{video_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        if resp.json()["status"] == "ready":
            return video_id
        await asyncio.sleep(0.01)
    raise AssertionError(f"video {video_id} never reached ready")


async def _create_clip(client, headers, video_id: str) -> str:
    resp = await client.post(
        f"/v1/media/videos/{video_id}/clips",
        json={"video_id": video_id, "max_clips": 1},
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    clips = resp.json()["clips"]
    assert clips
    return clips[0]["clip_id"]


def _assert_no_leak(body: str, *secrets: str) -> None:
    for secret in secrets:
        assert secret not in body


@pytest.mark.proof
@pytest.mark.anyio
async def test_media_video_and_clip_not_readable_across_tenants(client, clean_database):
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)
    video_id = await _create_ready_video(client, a["agent_headers"])
    clip_id = await _create_clip(client, a["agent_headers"], video_id)

    missing_video = await client.get(
        "/v1/media/videos/does-not-exist", headers=b["agent_headers"]
    )
    missing_clip = await client.get(
        "/v1/media/clips/does-not-exist", headers=b["agent_headers"]
    )
    assert missing_video.status_code == 404
    assert missing_clip.status_code == 404

    # Wallet B gets the same 404 it would for an unknown id, with none of A's
    # data (title, source, owner wallet, hooks, clip lineage) in the body.
    video = await client.get(f"/v1/media/videos/{video_id}", headers=b["agent_headers"])
    hooks = await client.get(
        f"/v1/media/videos/{video_id}/hooks", headers=b["agent_headers"]
    )
    clip = await client.get(f"/v1/media/clips/{clip_id}", headers=b["agent_headers"])
    assert video.status_code == 404
    assert video.json()["detail"]["error"] == missing_video.json()["detail"]["error"]
    assert hooks.status_code == 404
    assert hooks.json()["detail"]["error"] == "video_not_found"
    assert clip.status_code == 404
    assert clip.json() == missing_clip.json()
    for resp in (video, hooks, clip):
        _assert_no_leak(
            resp.text,
            _SECRET_TITLE,
            _SECRET_SOURCE,
            a["agent_wallet_id"],
            "hook_id",
        )
    _assert_no_leak(clip.text, video_id)

    # The owner still has full access.
    owner_video = await client.get(
        f"/v1/media/videos/{video_id}", headers=a["agent_headers"]
    )
    assert owner_video.status_code == 200
    assert owner_video.json()["title"] == _SECRET_TITLE
    owner_hooks = await client.get(
        f"/v1/media/videos/{video_id}/hooks", headers=a["agent_headers"]
    )
    assert owner_hooks.status_code == 200
    assert owner_hooks.json()
    owner_clip = await client.get(
        f"/v1/media/clips/{clip_id}", headers=a["agent_headers"]
    )
    assert owner_clip.status_code == 200
    assert owner_clip.json()["video_id"] == video_id


@pytest.mark.proof
@pytest.mark.anyio
async def test_media_generate_clips_from_foreign_video_denied(client, clean_database):
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)
    video_id = await _create_ready_video(client, a["agent_headers"])

    resp = await client.post(
        f"/v1/media/videos/{video_id}/clips",
        json={"video_id": video_id, "max_clips": 1},
        headers=b["agent_headers"],
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "video_not_found"
    _assert_no_leak(
        resp.text, "clip_id", "hook_id", _SECRET_TITLE, a["agent_wallet_id"]
    )

    # The owner can still render clips from its own video.
    await _create_clip(client, a["agent_headers"], video_id)


@pytest.mark.proof
@pytest.mark.anyio
async def test_media_distribute_foreign_clip_denied(client, clean_database):
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)
    video_id = await _create_ready_video(client, a["agent_headers"])
    clip_id = await _create_clip(client, a["agent_headers"], video_id)
    body = {
        "clip_ids": [clip_id],
        "platforms": ["youtube_shorts"],
        "title": "Hijacked post",
    }

    # Wallet B cannot publish A's clip: it reads exactly like an unknown id
    # and nothing is distributed.
    foreign = await client.post(
        "/v1/media/distribute", json=body, headers=b["agent_headers"]
    )
    assert foreign.status_code == 200
    data = foreign.json()
    assert data["total_distributed"] == 0
    assert data["total_failed"] == 1
    [result] = data["results"]
    assert result["status"] == "failed"
    assert result["platform_post_id"] is None
    assert result["platform_url"] is None
    assert result["error"] == f"Clip '{clip_id}' not found"
    _assert_no_leak(foreign.text, video_id, a["agent_wallet_id"])

    # The owner's (simulated) distribution succeeds and is counted.
    owner = await client.post(
        "/v1/media/distribute", json=body, headers=a["agent_headers"]
    )
    assert owner.status_code == 200
    owner_data = owner.json()
    assert owner_data["total_failed"] == 0
    assert owner_data["total_distributed"] == 1
    [owner_result] = owner_data["results"]
    assert owner_result["status"] in ("simulated", "simulated_scheduled")
    assert owner_result["platform_url"]


@pytest.mark.proof
@pytest.mark.anyio
async def test_media_bootstrap_admin_access_and_ownerless_resources(
    client, clean_database
):
    a = await provision_agent_wallet(client)

    # A bootstrap admin can still read and render from a wallet-owned video.
    video_id = await _create_ready_video(client, a["agent_headers"])
    clip_id = await _create_clip(client, a["agent_headers"], video_id)
    assert (
        await client.get(f"/v1/media/videos/{video_id}", headers=BOOTSTRAP_HEADERS)
    ).status_code == 200
    assert (
        await client.get(f"/v1/media/clips/{clip_id}", headers=BOOTSTRAP_HEADERS)
    ).status_code == 200
    await _create_clip(client, BOOTSTRAP_HEADERS, video_id)

    # A video (and its clips) created by a bootstrap admin has no owning
    # wallet, so no wallet-scoped key can reach it.
    admin_video = await _create_ready_video(client, BOOTSTRAP_HEADERS)
    admin_clip = await _create_clip(client, BOOTSTRAP_HEADERS, admin_video)
    assert (
        await client.get(f"/v1/media/videos/{admin_video}", headers=a["agent_headers"])
    ).status_code == 404
    assert (
        await client.get(f"/v1/media/clips/{admin_clip}", headers=a["agent_headers"])
    ).status_code == 404


@pytest.mark.proof
@pytest.mark.anyio
async def test_media_requires_authentication(client):
    assert (await client.get("/v1/media/videos/anything")).status_code == 401
    assert (
        await client.post(
            "/v1/media/distribute",
            json={"clip_ids": ["x"], "platforms": ["youtube_shorts"], "title": "t"},
        )
    ).status_code == 401
