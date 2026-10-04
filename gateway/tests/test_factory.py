"""
Tests for the Content Factory & Algorithmic Scheduling endpoints.
Validates pipeline creation, content retrieval, analytics ingestion,
schedule recommendation, and Live Campaign Mode with hook-based
1-to-20 multiplication.
"""

import asyncio
import pytest
from httpx import AsyncClient, ASGITransport
from app.db.database import get_session_factory
from app.db.models import ContentCampaignModel, ContentPipelineModel
from app.main import app
from app.schemas.content_factory import ContentFormat
from app.services.content_factory import FORMAT_ADAPTERS
from tests.test_trust_helpers import provision_agent_wallet


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def api_headers():
    return {"X-API-Key": "test-key"}


# --- Pipeline Creation ---


@pytest.mark.anyio
async def test_create_pipeline(client, api_headers):
    resp = await client.post(
        "/v1/factory/pipelines",
        json={
            "title": "Q1 Launch Video",
            "target_formats": ["short_video", "static_image", "text_post"],
            "source_url": "https://storage.example.com/launch.mp4",
        },
        headers=api_headers,
    )
    assert resp.status_code == 202
    data = resp.json()
    assert "pipeline_id" in data
    assert data["title"] == "Q1 Launch Video"
    assert data["source_type"] == "url"
    assert data["status"] == "queued"
    # short_video=5 + static_image=5 + text_post=5 = 15
    assert data["estimated_pieces"] == 15


@pytest.mark.anyio
async def test_create_pipeline_with_clip_source(client, api_headers):
    resp = await client.post(
        "/v1/factory/pipelines",
        json={
            "title": "From Existing Clip",
            "target_formats": ["blog_excerpt", "email_snippet"],
            "source_clip_id": "clip-abc-123",
        },
        headers=api_headers,
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["source_type"] == "clip"
    # blog_excerpt=1 + email_snippet=1 = 2
    assert data["estimated_pieces"] == 2


@pytest.mark.anyio
async def test_get_pipeline_status(client, api_headers):
    # Create pipeline first
    create_resp = await client.post(
        "/v1/factory/pipelines",
        json={"title": "Status Check", "target_formats": ["text_post"]},
        headers=api_headers,
    )
    pipeline_id = create_resp.json()["pipeline_id"]

    resp = await client.get(
        f"/v1/factory/pipelines/{pipeline_id}",
        headers=api_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["pipeline_id"] == pipeline_id
    assert data["title"] == "Status Check"
    assert "created_at" in data


@pytest.mark.anyio
async def test_get_pipeline_not_found(client, api_headers):
    resp = await client.get(
        "/v1/factory/pipelines/nonexistent-id",
        headers=api_headers,
    )
    assert resp.status_code == 404


# --- Content Retrieval ---


@pytest.mark.anyio
async def test_list_pipeline_content(client, api_headers):
    # Create pipeline and wait for async rendering
    create_resp = await client.post(
        "/v1/factory/pipelines",
        json={
            "title": "Content List Test",
            "target_formats": ["email_snippet"],
        },
        headers=api_headers,
    )
    pipeline_id = create_resp.json()["pipeline_id"]

    # Give async task time to complete
    await asyncio.sleep(0.2)

    resp = await client.get(
        f"/v1/factory/pipelines/{pipeline_id}/content",
        headers=api_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["pipeline_id"] == pipeline_id
    assert data["total"] >= 1
    assert len(data["content"]) >= 1
    piece = data["content"][0]
    assert piece["format"] == "email_snippet"
    assert "download_url" in piece


@pytest.mark.anyio
async def test_get_content_not_found(client, api_headers):
    resp = await client.get(
        "/v1/factory/content/nonexistent-content-id",
        headers=api_headers,
    )
    assert resp.status_code == 404


# --- Analytics Ingestion ---


@pytest.mark.anyio
async def test_ingest_analytics(client, api_headers):
    resp = await client.post(
        "/v1/factory/analytics",
        json={
            "metrics": [
                {
                    "platform": "tiktok",
                    "metric_type": "views",
                    "value": 15000,
                    "recorded_at": "2026-02-20T14:00:00Z",
                    "content_id": "content-abc",
                },
                {
                    "platform": "youtube_shorts",
                    "metric_type": "watch_time",
                    "value": 3600,
                    "recorded_at": "2026-02-20T14:00:00Z",
                },
            ]
        },
        headers=api_headers,
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["ingested"] == 2
    assert "tiktok" in data["platform_summary"]
    assert "youtube_shorts" in data["platform_summary"]


@pytest.mark.anyio
async def test_analytics_summary(client, api_headers):
    resp = await client.get(
        "/v1/factory/analytics/summary",
        headers=api_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "total_data_points" in data
    assert "by_platform" in data


# --- Scheduling ---


@pytest.mark.anyio
async def test_get_schedule(client, api_headers):
    resp = await client.post(
        "/v1/factory/schedule",
        json={
            "content_ids": ["content-1", "content-2"],
            "platforms": ["tiktok", "youtube_shorts"],
            "max_posts_per_day": 2,
        },
        headers=api_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_scheduled"] > 0
    assert "date_range" in data
    rec = data["recommendations"][0]
    assert "content_id" in rec
    assert "platform" in rec
    assert "recommended_time" in rec
    assert 0.0 <= rec["confidence"] <= 1.0
    assert "reasoning" in rec


@pytest.mark.anyio
async def test_schedule_empty_content(client, api_headers):
    resp = await client.post(
        "/v1/factory/schedule",
        json={
            "content_ids": [],
            "platforms": ["tiktok"],
        },
        headers=api_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["total_scheduled"] == 0
    assert resp.json()["date_range"] == "no recommendations"


# --- Live Campaign Mode ---


@pytest.mark.anyio
async def test_launch_b2a_campaign(client, api_headers):
    """Full integration: source video -> 3 hooks -> 1-to-N multiplication -> scheduling."""
    resp = await client.post(
        "/v1/factory/campaigns",
        json={
            "source_url": "https://yourdomain.com/content/b2a-launch-video",
            "campaign_title": "B2A Launch — Why Agents Need Their Own API Layer",
            "hooks": [
                {
                    "title": "The Zero-GUI Thesis",
                    "hook_type": "reaction",
                    "start_seconds": 30.0,
                    "end_seconds": 90.0,
                    "transcript_snippet": "Every API today was built for humans clicking buttons. But your next million customers don't have screens — they're autonomous agents that consume JSON, not pixels.",
                    "talking_points": [
                        "GUIs are a tax on automation",
                        "Agent-native APIs eliminate the UI bottleneck",
                        "B2A replaces B2B for programmatic buyers",
                    ],
                    "target_formats": [
                        "short_video",
                        "static_image",
                        "text_post",
                        "quote_card",
                    ],
                },
                {
                    "title": "The Liability Sink",
                    "hook_type": "educational",
                    "start_seconds": 120.0,
                    "end_seconds": 240.0,
                    "transcript_snippet": "Agents can't hold credit cards, sign contracts, or pass 2FA. Every agent needs a human sponsor — a liability sink — who provisions its budget and sets the guardrails.",
                    "talking_points": [
                        "Two-tier wallet solves agent payment",
                        "Per-action micro-metering at sub-cent precision",
                        "402 responses teach agents to self-fund",
                        "Zero-GUI product design",
                    ],
                    "target_formats": [
                        "short_video",
                        "static_image",
                        "text_post",
                        "carousel",
                        "blog_excerpt",
                    ],
                },
                {
                    "title": "Swarm Beats Monolith",
                    "hook_type": "debate",
                    "start_seconds": 300.0,
                    "end_seconds": 420.0,
                    "transcript_snippet": "One massive model that does everything, or a fleet of specialized agents that coordinate through middleware? Swarms are cheaper, more resilient, and infinitely scalable.",
                    "talking_points": [
                        "Specialized agents outperform generalist models",
                        "Middleware is the connective tissue of swarms",
                        "Redundancy beats single points of failure",
                    ],
                    "target_formats": [
                        "short_video",
                        "quote_card",
                        "debate_clip",
                        "text_post",
                    ],
                },
            ],
            "brand_config": {
                "primary_color": "#FF6B00",
                "font_family": "Inter",
                "watermark_position": "bottom_right",
            },
            "caption_style": "bold_impact",
            "aspect_ratio": "9:16",
            "platforms": ["youtube_shorts", "tiktok", "instagram_reels"],
            "max_posts_per_day": 3,
        },
        headers=api_headers,
    )
    assert resp.status_code == 202
    data = resp.json()

    # Campaign structure
    assert "campaign_id" in data
    assert data["campaign_title"] == "B2A Launch — Why Agents Need Their Own API Layer"
    assert data["status"] == "completed"
    assert data["hooks_processed"] == 3

    # Content multiplication: each hook should produce multiple pieces
    assert data["total_content_pieces"] > 0
    assert len(data["hook_results"]) == 3

    # Verify hook-level results
    hook1 = data["hook_results"][0]
    assert hook1["hook_title"] == "The Zero-GUI Thesis"
    assert hook1["hook_type"] == "reaction"
    assert hook1["total_pieces"] > 0
    assert len(hook1["content_pieces"]) > 0
    assert "short_video" in hook1["pieces_by_format"]

    hook2 = data["hook_results"][1]
    assert hook2["hook_title"] == "The Liability Sink"
    assert hook2["hook_type"] == "educational"
    assert "blog_excerpt" in hook2["pieces_by_format"]

    hook3 = data["hook_results"][2]
    assert hook3["hook_title"] == "Swarm Beats Monolith"
    assert hook3["hook_type"] == "debate"
    assert "debate_clip" in hook3["pieces_by_format"]

    # Scheduling was generated
    assert data["schedule_generated"] is True
    sched = data["schedule_summary"]
    assert sched["total_scheduled"] > 0
    assert "youtube_shorts" in sched["platforms"]
    assert "tiktok" in sched["platforms"]
    assert sched["estimated_total_views"] > 0

    # Pipeline IDs were created
    assert len(data["pipeline_ids"]) == 3


@pytest.mark.anyio
async def test_campaign_single_hook(client, api_headers):
    """Minimal campaign with one hook."""
    resp = await client.post(
        "/v1/factory/campaigns",
        json={
            "source_url": "https://example.com/video.mp4",
            "campaign_title": "Single Hook Test",
            "hooks": [
                {
                    "title": "Test Hook",
                    "hook_type": "educational",
                    "start_seconds": 0,
                    "end_seconds": 30,
                    "target_formats": ["short_video", "text_post"],
                },
            ],
            "platforms": ["tiktok"],
        },
        headers=api_headers,
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["hooks_processed"] == 1
    assert data["total_content_pieces"] > 0
    assert data["schedule_generated"] is True


@pytest.mark.anyio
async def test_campaign_no_schedule(client, api_headers):
    """Campaign with auto_schedule=false."""
    resp = await client.post(
        "/v1/factory/campaigns",
        json={
            "source_url": "https://example.com/video.mp4",
            "campaign_title": "No Schedule Test",
            "hooks": [
                {
                    "title": "Raw Hook",
                    "hook_type": "reaction",
                    "start_seconds": 10,
                    "end_seconds": 40,
                    "target_formats": ["quote_card"],
                },
            ],
            "auto_schedule": False,
        },
        headers=api_headers,
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["schedule_generated"] is False
    assert data["total_content_pieces"] > 0


@pytest.mark.anyio
async def test_get_campaign(client, api_headers):
    """Retrieve a campaign by ID."""
    create_resp = await client.post(
        "/v1/factory/campaigns",
        json={
            "source_url": "https://example.com/vid.mp4",
            "campaign_title": "Lookup Test",
            "hooks": [
                {
                    "title": "H1",
                    "hook_type": "cold_open",
                    "start_seconds": 0,
                    "end_seconds": 15,
                    "target_formats": ["short_video"],
                },
            ],
        },
        headers=api_headers,
    )
    campaign_id = create_resp.json()["campaign_id"]

    resp = await client.get(
        f"/v1/factory/campaigns/{campaign_id}",
        headers=api_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["campaign_id"] == campaign_id
    assert data["campaign_title"] == "Lookup Test"


@pytest.mark.anyio
async def test_get_campaign_not_found(client, api_headers):
    resp = await client.get(
        "/v1/factory/campaigns/nonexistent",
        headers=api_headers,
    )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_list_campaigns(client, api_headers):
    # Create a campaign first
    await client.post(
        "/v1/factory/campaigns",
        json={
            "source_url": "https://example.com/v.mp4",
            "campaign_title": "List Test",
            "hooks": [
                {
                    "title": "H",
                    "hook_type": "montage",
                    "start_seconds": 0,
                    "end_seconds": 20,
                    "target_formats": ["text_post"],
                },
            ],
        },
        headers=api_headers,
    )

    resp = await client.get("/v1/factory/campaigns", headers=api_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert len(data["campaigns"]) >= 1


@pytest.mark.anyio
async def test_hook_validates_end_after_start(client, api_headers):
    """end_seconds must be after start_seconds."""
    resp = await client.post(
        "/v1/factory/campaigns",
        json={
            "source_url": "https://example.com/v.mp4",
            "campaign_title": "Bad Hook",
            "hooks": [
                {
                    "title": "Invalid",
                    "hook_type": "reaction",
                    "start_seconds": 60,
                    "end_seconds": 30,
                    "target_formats": ["short_video"],
                },
            ],
        },
        headers=api_headers,
    )
    assert resp.status_code == 422  # Pydantic validation error


@pytest.mark.anyio
async def test_campaign_content_has_hook_metadata(client, api_headers):
    """Content pieces from a campaign should carry hook metadata."""
    resp = await client.post(
        "/v1/factory/campaigns",
        json={
            "source_url": "https://example.com/v.mp4",
            "campaign_title": "Metadata Check",
            "hooks": [
                {
                    "title": "Meta Hook",
                    "hook_type": "educational",
                    "start_seconds": 0,
                    "end_seconds": 45,
                    "transcript_snippet": "AI agents are the future of software distribution.",
                    "target_formats": ["short_video"],
                },
            ],
            "caption_style": "bold_impact",
            "aspect_ratio": "9:16",
            "platforms": ["tiktok"],
        },
        headers=api_headers,
    )
    data = resp.json()

    # Get a content piece via its pipeline
    pipeline_id = data["pipeline_ids"][0]
    await asyncio.sleep(0.1)

    content_resp = await client.get(
        f"/v1/factory/pipelines/{pipeline_id}/content",
        headers=api_headers,
    )
    content = content_resp.json()["content"]
    assert len(content) > 0

    # Check hook metadata on a video piece
    video_pieces = [c for c in content if c["format"] == "short_video"]
    assert len(video_pieces) > 0
    piece = video_pieces[0]
    assert piece["dimensions"] == "1080x1920"  # 9:16 vertical
    assert piece["metadata"]["caption_style"] == "bold_impact"
    assert piece["metadata"]["aspect_ratio"] == "9:16"
    assert "hook_id" in piece["metadata"]
    assert piece["metadata"]["hook_type"] == "educational"


# --- Auth ---


@pytest.mark.anyio
async def test_factory_requires_api_key(client):
    resp = await client.post(
        "/v1/factory/pipelines",
        json={"title": "No Key", "target_formats": ["text_post"]},
    )
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_campaign_requires_api_key(client):
    resp = await client.post(
        "/v1/factory/campaigns",
        json={
            "source_url": "https://example.com/v.mp4",
            "campaign_title": "No Key",
            "hooks": [
                {
                    "title": "H",
                    "hook_type": "reaction",
                    "start_seconds": 0,
                    "end_seconds": 30,
                    "target_formats": ["short_video"],
                },
            ],
        },
    )
    assert resp.status_code == 401


# --- Tenant isolation (owner enforced on every read) ---
#
# Pipelines, their content pieces, and campaigns used to be tagged with the
# caller's raw API key and then served to any authenticated caller: no GET
# compared the stored owner, and the campaign list returned every tenant's
# rows. Ownership is now the caller's wallet id, enforced on every read. A
# foreign record is indistinguishable from a missing one (same 404 body), so
# the surface is not an existence oracle and never echoes the owner's wallet.


async def _create_owned_pipeline(client, headers, title="Tenant A pipeline"):
    resp = await client.post(
        "/v1/factory/pipelines",
        json={
            "title": title,
            "target_formats": ["text_post", "email_snippet"],
            "source_url": "https://a.example/secret-source.mp4",
        },
        headers=headers,
    )
    assert resp.status_code == 202
    return resp.json()["pipeline_id"]


async def _wait_for_pipeline(client, pipeline_id, headers, *, attempts=100):
    """Poll the pipeline until it reaches a terminal status (or give up)."""
    data = {}
    for _ in range(attempts):
        resp = await client.get(f"/v1/factory/pipelines/{pipeline_id}", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        if data["status"] in {"ready", "failed"}:
            return data
        await asyncio.sleep(0.02)
    return data


async def _launch_owned_campaign(client, headers, title="Tenant A campaign"):
    resp = await client.post(
        "/v1/factory/campaigns",
        json={
            "source_url": "https://a.example/secret-campaign.mp4",
            "campaign_title": title,
            "hooks": [
                {
                    "title": "A hook",
                    "hook_type": "educational",
                    "start_seconds": 0,
                    "end_seconds": 30,
                    "target_formats": ["text_post", "quote_card"],
                },
            ],
            "auto_schedule": False,
        },
        headers=headers,
    )
    assert resp.status_code == 202
    return resp.json()


def _assert_no_leak(resp, *secrets):
    for secret in secrets:
        assert secret not in resp.text


@pytest.mark.proof
@pytest.mark.anyio
async def test_pipeline_not_readable_across_tenants(
    client, api_headers, clean_database
):
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)
    pipeline_id = await _create_owned_pipeline(client, a["agent_headers"])
    await _wait_for_pipeline(client, pipeline_id, a["agent_headers"])

    missing = await client.get(
        "/v1/factory/pipelines/does-not-exist", headers=b["agent_headers"]
    )
    assert missing.status_code == 404

    # Wallet B cannot read A's pipeline status or enumerate its content, and
    # the denial is byte-identical to "does not exist".
    foreign = await client.get(
        f"/v1/factory/pipelines/{pipeline_id}", headers=b["agent_headers"]
    )
    assert foreign.status_code == 404
    assert foreign.json() == missing.json()
    _assert_no_leak(foreign, a["agent_wallet_id"], "Tenant A pipeline")

    foreign_content = await client.get(
        f"/v1/factory/pipelines/{pipeline_id}/content", headers=b["agent_headers"]
    )
    assert foreign_content.status_code == 404
    assert foreign_content.json() == missing.json()
    _assert_no_leak(foreign_content, a["agent_wallet_id"], "Tenant A pipeline")

    # The owner keeps full access, and so does a bootstrap admin.
    own = await client.get(
        f"/v1/factory/pipelines/{pipeline_id}", headers=a["agent_headers"]
    )
    assert own.status_code == 200
    assert own.json()["title"] == "Tenant A pipeline"
    own_content = await client.get(
        f"/v1/factory/pipelines/{pipeline_id}/content", headers=a["agent_headers"]
    )
    assert own_content.status_code == 200
    assert own_content.json()["total"] >= 1
    admin = await client.get(
        f"/v1/factory/pipelines/{pipeline_id}", headers=api_headers
    )
    assert admin.status_code == 200


@pytest.mark.proof
@pytest.mark.anyio
async def test_content_piece_not_readable_across_tenants(
    client, api_headers, clean_database
):
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)
    pipeline_id = await _create_owned_pipeline(client, a["agent_headers"])
    await _wait_for_pipeline(client, pipeline_id, a["agent_headers"])
    listing = await client.get(
        f"/v1/factory/pipelines/{pipeline_id}/content", headers=a["agent_headers"]
    )
    assert listing.status_code == 200
    content_id = listing.json()["content"][0]["content_id"]

    missing = await client.get(
        "/v1/factory/content/does-not-exist", headers=b["agent_headers"]
    )
    assert missing.status_code == 404
    foreign = await client.get(
        f"/v1/factory/content/{content_id}", headers=b["agent_headers"]
    )
    assert foreign.status_code == 404
    assert foreign.json() == missing.json()
    _assert_no_leak(foreign, a["agent_wallet_id"], pipeline_id, "Tenant A pipeline")

    own = await client.get(
        f"/v1/factory/content/{content_id}", headers=a["agent_headers"]
    )
    assert own.status_code == 200
    assert own.json()["pipeline_id"] == pipeline_id
    admin = await client.get(f"/v1/factory/content/{content_id}", headers=api_headers)
    assert admin.status_code == 200


@pytest.mark.proof
@pytest.mark.anyio
async def test_campaign_not_readable_or_listable_across_tenants(
    client, api_headers, clean_database
):
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)
    launched = await _launch_owned_campaign(client, a["agent_headers"])
    campaign_id = launched["campaign_id"]

    missing = await client.get(
        "/v1/factory/campaigns/does-not-exist", headers=b["agent_headers"]
    )
    assert missing.status_code == 404
    foreign = await client.get(
        f"/v1/factory/campaigns/{campaign_id}", headers=b["agent_headers"]
    )
    assert foreign.status_code == 404
    assert foreign.json() == missing.json()
    _assert_no_leak(
        foreign,
        a["agent_wallet_id"],
        "Tenant A campaign",
        *launched["pipeline_ids"],
    )

    # B's campaign list never contains A's campaign.
    b_list = await client.get("/v1/factory/campaigns", headers=b["agent_headers"])
    assert b_list.status_code == 200
    assert campaign_id not in {c["campaign_id"] for c in b_list.json()["campaigns"]}
    assert b_list.json()["total"] == 0
    _assert_no_leak(b_list, campaign_id, "Tenant A campaign")

    # The owner sees and lists its own campaign; a bootstrap admin sees all.
    own = await client.get(
        f"/v1/factory/campaigns/{campaign_id}", headers=a["agent_headers"]
    )
    assert own.status_code == 200
    assert own.json()["campaign_title"] == "Tenant A campaign"
    a_list = await client.get("/v1/factory/campaigns", headers=a["agent_headers"])
    assert a_list.status_code == 200
    assert campaign_id in {c["campaign_id"] for c in a_list.json()["campaigns"]}
    admin_list = await client.get("/v1/factory/campaigns", headers=api_headers)
    assert campaign_id in {c["campaign_id"] for c in admin_list.json()["campaigns"]}

    # The campaign's per-hook pipelines belong to A as well.
    for pipeline_id in launched["pipeline_ids"]:
        resp = await client.get(
            f"/v1/factory/pipelines/{pipeline_id}", headers=b["agent_headers"]
        )
        assert resp.status_code == 404


@pytest.mark.proof
@pytest.mark.anyio
async def test_owner_recorded_as_wallet_never_raw_api_key(client, clean_database):
    a = await provision_agent_wallet(client)
    raw_key = a["agent_headers"]["X-API-Key"]
    pipeline_id = await _create_owned_pipeline(client, a["agent_headers"])
    await _wait_for_pipeline(client, pipeline_id, a["agent_headers"])
    launched = await _launch_owned_campaign(client, a["agent_headers"])

    session_factory = get_session_factory()
    async with session_factory() as session:
        rows = [
            await session.get(ContentPipelineModel, pipeline_id),
            await session.get(ContentCampaignModel, launched["campaign_id"]),
        ]
        for hook_pipeline_id in launched["pipeline_ids"]:
            rows.append(await session.get(ContentPipelineModel, hook_pipeline_id))

    for row in rows:
        assert row is not None
        assert row.owner_key == a["agent_wallet_id"]
        assert raw_key not in row.owner_key


# --- Request validation ---


@pytest.mark.proof
@pytest.mark.anyio
async def test_create_pipeline_rejects_both_sources(client, api_headers):
    """source_clip_id and source_url are documented as mutually exclusive."""
    resp = await client.post(
        "/v1/factory/pipelines",
        json={
            "title": "Ambiguous Source",
            "target_formats": ["text_post"],
            "source_clip_id": "clip-abc-123",
            "source_url": "https://storage.example.com/launch.mp4",
        },
        headers=api_headers,
    )
    assert resp.status_code == 422


# --- Pipeline status persistence ---


@pytest.mark.proof
@pytest.mark.anyio
async def test_pipeline_status_reaches_ready(client, api_headers):
    create = await client.post(
        "/v1/factory/pipelines",
        json={"title": "Persisted Status", "target_formats": ["text_post"]},
        headers=api_headers,
    )
    assert create.status_code == 202
    pipeline_id = create.json()["pipeline_id"]
    data = await _wait_for_pipeline(client, pipeline_id, api_headers)
    assert data["status"] == "ready"
    assert data["pieces_generated"] == create.json()["estimated_pieces"]


@pytest.mark.proof
@pytest.mark.anyio
async def test_pipeline_failure_is_persisted(client, api_headers, monkeypatch):
    async def _boom(pipeline, index):
        raise RuntimeError("adapter exploded")

    monkeypatch.setitem(FORMAT_ADAPTERS, ContentFormat.TEXT_POST, _boom)
    create = await client.post(
        "/v1/factory/pipelines",
        json={"title": "Failing Pipeline", "target_formats": ["text_post"]},
        headers=api_headers,
    )
    assert create.status_code == 202
    pipeline_id = create.json()["pipeline_id"]
    data = await _wait_for_pipeline(client, pipeline_id, api_headers)
    assert data["status"] == "failed"


@pytest.mark.proof
@pytest.mark.anyio
async def test_campaign_reports_every_piece_of_a_slow_render(
    client, api_headers, monkeypatch
):
    """launch_campaign used to poll ~1s for a "ready" status that was never
    stored, then silently report whatever pieces existed by then."""
    real_adapter = FORMAT_ADAPTERS[ContentFormat.TEXT_POST]

    async def _slow(pipeline, index):
        await asyncio.sleep(1.5)
        return await real_adapter(pipeline, index)

    monkeypatch.setitem(FORMAT_ADAPTERS, ContentFormat.TEXT_POST, _slow)
    launched = await _launch_owned_campaign(client, api_headers, title="Slow")

    # One hook: text_post x3 + quote_card x2.
    assert launched["total_content_pieces"] == 5
    for pipeline_id in launched["pipeline_ids"]:
        resp = await client.get(
            f"/v1/factory/pipelines/{pipeline_id}", headers=api_headers
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"
        assert resp.json()["pieces_generated"] == 5
