"""
Tests for the PG-backed content_factory.ContentStore (issue #31).

test_factory.py covers the HTTP surface. These verify store-level
behavior: pipeline/campaign round-trip, content piece listing per
pipeline, JSON field fidelity (hooks, brand_config, target_formats).
"""

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.db.database import get_session_factory
from app.db.models import (
    ContentCampaignModel,
    ContentPieceModel,
    ContentPipelineModel,
)
from app.schemas.content_factory import (
    CaptionStyle,
    ContentFormat,
    ContentHook,
    ContentStatus,
    GeneratedContent,
    HookType,
)
from app.services.content_factory import (
    ContentPipeline,
    ContentStore,
    LiveCampaign,
)


@pytest_asyncio.fixture(autouse=True)
async def _clean_content_tables():
    factory = get_session_factory()
    async with factory() as session:
        # Pieces FK pipelines — purge children first.
        await session.execute(delete(ContentPieceModel))
        await session.execute(delete(ContentPipelineModel))
        await session.execute(delete(ContentCampaignModel))
        await session.commit()
    yield


def _pipeline(pipeline_id: str = "p-1", owner: str = "tenant-a") -> ContentPipeline:
    return ContentPipeline(
        pipeline_id=pipeline_id,
        title="Test pipeline",
        source_clip_id=None,
        source_url="https://example.com/video.mp4",
        target_formats=[ContentFormat.SHORT_VIDEO, ContentFormat.TEXT_POST],
        brand_config={"primary_color": "#FF6B00", "logo_url": "x.png"},
        language="en",
        auto_schedule=True,
        owner_key=owner,
    )


def _piece(content_id: str = "c-1", pipeline_id: str = "p-1") -> GeneratedContent:
    return GeneratedContent(
        content_id=content_id,
        pipeline_id=pipeline_id,
        format=ContentFormat.SHORT_VIDEO,
        title="Piece title",
        description="desc",
        download_url="file:///tmp/x.mp4",
        thumbnail_url="file:///tmp/x.jpg",
        duration_seconds=30.0,
        dimensions="1080x1920",
        file_size_bytes=5_000_000,
        status=ContentStatus.READY,
        generated_at=datetime.now(timezone.utc),
        metadata={"caption_style": "bold_impact", "hook_id": "h-1"},
    )


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_create_pipeline_round_trip_preserves_complex_fields():
    store = ContentStore()
    pipeline = _pipeline()
    pipeline.hook = ContentHook(
        hook_id="h-1",
        title="The hook",
        hook_type=HookType.REACTION,
        start_seconds=10.0,
        end_seconds=45.0,
        transcript_snippet="snippet",
    )
    pipeline.caption_style = CaptionStyle.BOLD_IMPACT

    await store.create_pipeline(pipeline)

    got = await store.get_pipeline("p-1")
    assert got is not None
    assert got.title == "Test pipeline"
    assert got.target_formats == [ContentFormat.SHORT_VIDEO, ContentFormat.TEXT_POST]
    assert got.brand_config == {"primary_color": "#FF6B00", "logo_url": "x.png"}
    assert got.hook is not None
    assert got.hook.hook_type == HookType.REACTION
    assert got.hook.transcript_snippet == "snippet"
    assert got.caption_style == CaptionStyle.BOLD_IMPACT
    assert got.content_pieces == []  # No pieces stored yet.


@pytest.mark.anyio
async def test_create_pipeline_is_upsert():
    store = ContentStore()
    await store.create_pipeline(_pipeline())
    # Re-create with a different title — should update, not duplicate.
    updated = _pipeline()
    updated.title = "Updated title"
    await store.create_pipeline(updated)

    got = await store.get_pipeline("p-1")
    assert got is not None
    assert got.title == "Updated title"

    # Confirm only one row at raw level.
    factory = get_session_factory()
    async with factory() as session:
        from sqlalchemy import select, func

        count = await session.scalar(
            select(func.count()).select_from(ContentPipelineModel)
        )
        assert count == 1


# ---------------------------------------------------------------------------
# Content pieces
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_store_and_list_pieces_per_pipeline():
    store = ContentStore()
    await store.create_pipeline(_pipeline())
    await store.store_content(_piece(content_id="c-1"))
    await store.store_content(_piece(content_id="c-2"))

    pieces = await store.list_by_pipeline("p-1")
    assert {p.content_id for p in pieces} == {"c-1", "c-2"}

    # get_pipeline should also populate pieces fresh each call.
    pipeline = await store.get_pipeline("p-1")
    assert pipeline is not None
    assert {p.content_id for p in pipeline.content_pieces} == {"c-1", "c-2"}


@pytest.mark.anyio
async def test_store_content_preserves_metadata_and_status():
    store = ContentStore()
    await store.create_pipeline(_pipeline())
    piece = _piece()
    piece.metadata = {"caption_style": "bold_impact", "dimensions": "1080x1920"}
    await store.store_content(piece)

    got = await store.get_content("c-1")
    assert got is not None
    assert got.status == ContentStatus.READY
    assert got.metadata == {"caption_style": "bold_impact", "dimensions": "1080x1920"}


@pytest.mark.anyio
async def test_store_content_is_upsert_on_content_id():
    store = ContentStore()
    await store.create_pipeline(_pipeline())
    await store.store_content(_piece())

    updated = _piece()
    updated.title = "New title"
    updated.status = ContentStatus.DISTRIBUTED
    await store.store_content(updated)

    got = await store.get_content("c-1")
    assert got is not None
    assert got.title == "New title"
    assert got.status == ContentStatus.DISTRIBUTED

    pieces = await store.list_by_pipeline("p-1")
    assert len(pieces) == 1


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_campaign_round_trip_preserves_hooks_and_pipeline_ids():
    store = ContentStore()
    campaign = LiveCampaign(
        campaign_id="camp-1",
        campaign_title="Big launch",
        source_url="https://example.com/source.mp4",
        hooks=[
            ContentHook(
                hook_id="h-1",
                title="Zero-GUI",
                hook_type=HookType.REACTION,
                start_seconds=0.0,
                end_seconds=30.0,
            ),
            ContentHook(
                hook_id="h-2",
                title="Why now",
                hook_type=HookType.EDUCATIONAL,
                start_seconds=30.0,
                end_seconds=60.0,
            ),
        ],
        pipeline_ids=["p-1", "p-2"],
        owner_key="tenant-a",
    )
    await store.create_campaign(campaign)

    got = await store.get_campaign("camp-1")
    assert got is not None
    assert got.campaign_title == "Big launch"
    assert got.pipeline_ids == ["p-1", "p-2"]
    assert len(got.hooks) == 2
    assert got.hooks[0].hook_type == HookType.REACTION
    assert got.hooks[1].title == "Why now"


@pytest.mark.anyio
async def test_list_campaigns_returns_every_row():
    store = ContentStore()
    for cid in ("camp-1", "camp-2", "camp-3"):
        await store.create_campaign(
            LiveCampaign(
                campaign_id=cid,
                campaign_title=cid,
                source_url="https://example.com/x",
                hooks=[
                    ContentHook(
                        title="t",
                        hook_type=HookType.REACTION,
                        start_seconds=0,
                        end_seconds=1,
                    )
                ],
                pipeline_ids=[],
                owner_key="",
            )
        )

    campaigns = await store.list_campaigns()
    assert {c.campaign_id for c in campaigns} == {"camp-1", "camp-2", "camp-3"}
