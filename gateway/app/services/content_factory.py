"""
Programmatic Content Factory — Service Layer
==============================================
Turns a single source asset into a swarm of format-adapted content pieces.

Pipeline: Source → Hook Extraction → Format Adaptation → Render → Schedule → Distribute

Live Mode (Week 6+):
One long-form video becomes:
- 3 targeted hooks × 7 formats each = 21 content pieces
- All rendered in 9:16 vertical with animated captions
- Auto-scheduled across TikTok, YouTube Shorts, Instagram Reels

Production wiring:
- FFmpeg for video/image rendering
- Whisper for transcription
- ML models for key-frame extraction and text summarization
"""

import asyncio
import json
import uuid
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.sql.elements import ColumnElement

from ..core.runtime_mode import require_simulation
from ..db.converters import (
    content_piece_model_to_schema,
    content_piece_to_model,
)
from ..db.database import get_session_factory, is_database_configured
from ..db.models import (
    ContentCampaignModel,
    ContentPieceModel,
    ContentPipelineModel,
)
from ..schemas.content_factory import (
    ContentFormat,
    ContentStatus,
    GeneratedContent,
    PlatformAnalytics,
    ScheduleRecommendation,
    ContentHook,
    CaptionStyle,
    CampaignHookResult,
    LiveCampaignResponse,
)

from .content_factory_generation import ContentGenerationStore, generate_text

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Content Store
# ---------------------------------------------------------------------------


@dataclass
class ContentPipeline:
    """A content generation pipeline instance."""

    pipeline_id: str
    title: str
    source_clip_id: str | None
    source_url: str | None
    target_formats: list[ContentFormat]
    brand_config: dict
    language: str
    auto_schedule: bool
    # Owning wallet id ("" = bootstrap-admin only). Never an API key: the
    # column name predates the fix that stopped persisting raw credentials.
    owner_key: str = ""
    status: str = "queued"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    content_pieces: list[GeneratedContent] = field(default_factory=list)
    # Live mode fields
    hook: ContentHook | None = None
    caption_style: CaptionStyle = CaptionStyle.BOLD_IMPACT
    aspect_ratio: str = "9:16"


@dataclass
class LiveCampaign:
    """A live content campaign spanning multiple hooks and pipelines."""

    campaign_id: str
    campaign_title: str
    source_url: str
    hooks: list[ContentHook]
    pipeline_ids: list[str] = field(default_factory=list)
    status: str = "running"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    owner_key: str = ""  # Owning wallet id, as on ContentPipeline.


def _pipeline_to_row(pipeline: ContentPipeline) -> ContentPipelineModel:
    return ContentPipelineModel(
        pipeline_id=pipeline.pipeline_id,
        title=pipeline.title,
        source_clip_id=pipeline.source_clip_id,
        source_url=pipeline.source_url,
        target_formats_json=json.dumps([f.value for f in pipeline.target_formats]),
        brand_config_json=json.dumps(pipeline.brand_config or {}, default=str),
        language=pipeline.language,
        auto_schedule=pipeline.auto_schedule,
        owner_key=pipeline.owner_key,
        status=pipeline.status,
        hook_json=(
            pipeline.hook.model_dump_json() if pipeline.hook is not None else None
        ),
        caption_style=pipeline.caption_style.value,
        aspect_ratio=pipeline.aspect_ratio,
        created_at=pipeline.created_at,
    )


def _row_to_pipeline(
    row: ContentPipelineModel,
    pieces: list[ContentPieceModel],
) -> ContentPipeline:
    target_formats: list[ContentFormat] = []
    if row.target_formats_json:
        try:
            for f in json.loads(row.target_formats_json) or []:
                try:
                    target_formats.append(ContentFormat(f))
                except ValueError:
                    continue
        except json.JSONDecodeError:
            pass

    brand_config: dict = {}
    if row.brand_config_json:
        try:
            parsed = json.loads(row.brand_config_json)
            if isinstance(parsed, dict):
                brand_config = parsed
        except json.JSONDecodeError:
            pass

    hook: ContentHook | None = None
    if row.hook_json:
        try:
            hook = ContentHook.model_validate_json(row.hook_json)
        except Exception:
            hook = None

    try:
        caption_style = CaptionStyle(row.caption_style)
    except ValueError:
        caption_style = CaptionStyle.BOLD_IMPACT

    return ContentPipeline(
        pipeline_id=row.pipeline_id,
        title=row.title,
        source_clip_id=row.source_clip_id,
        source_url=row.source_url,
        target_formats=target_formats,
        brand_config=brand_config,
        language=row.language,
        auto_schedule=row.auto_schedule,
        owner_key=row.owner_key,
        status=row.status,
        created_at=row.created_at,
        content_pieces=[content_piece_model_to_schema(p) for p in pieces],
        hook=hook,
        caption_style=caption_style,
        aspect_ratio=row.aspect_ratio,
    )


def _campaign_to_row(campaign: LiveCampaign) -> ContentCampaignModel:
    return ContentCampaignModel(
        campaign_id=campaign.campaign_id,
        campaign_title=campaign.campaign_title,
        source_url=campaign.source_url,
        hooks_json=json.dumps([h.model_dump(mode="json") for h in campaign.hooks]),
        pipeline_ids_json=json.dumps(list(campaign.pipeline_ids)),
        status=campaign.status,
        owner_key=campaign.owner_key,
        created_at=campaign.created_at,
    )


def _row_to_campaign(row: ContentCampaignModel) -> LiveCampaign:
    hooks: list[ContentHook] = []
    if row.hooks_json:
        try:
            for h in json.loads(row.hooks_json) or []:
                try:
                    hooks.append(ContentHook.model_validate(h))
                except Exception:
                    continue
        except json.JSONDecodeError:
            pass

    pipeline_ids: list[str] = []
    if row.pipeline_ids_json:
        try:
            parsed = json.loads(row.pipeline_ids_json)
            if isinstance(parsed, list):
                pipeline_ids = [str(p) for p in parsed]
        except json.JSONDecodeError:
            pass

    return LiveCampaign(
        campaign_id=row.campaign_id,
        campaign_title=row.campaign_title,
        source_url=row.source_url,
        hooks=hooks,
        pipeline_ids=pipeline_ids,
        status=row.status,
        created_at=row.created_at,
        owner_key=row.owner_key,
    )


class ContentStore:
    """PostgreSQL-backed pipeline, piece, and campaign store. See #31.

    Blob storage for generated bytes is a separate concern — see
    app/core/blob.py. download_url on ContentPieceModel holds the blob
    reference; the store doesn't touch bytes directly.
    """

    @staticmethod
    def _require_db() -> None:
        if not is_database_configured():
            raise RuntimeError(
                "content_factory.ContentStore requires a configured database. "
                "Set DATABASE_URL."
            )

    async def create_pipeline(self, pipeline: ContentPipeline) -> ContentPipeline:
        self._require_db()
        factory = get_session_factory()
        async with factory() as session:
            existing = await session.get(ContentPipelineModel, pipeline.pipeline_id)
            if existing is None:
                session.add(_pipeline_to_row(pipeline))
            else:
                new_row = _pipeline_to_row(pipeline)
                for field_name in (
                    "title",
                    "source_clip_id",
                    "source_url",
                    "target_formats_json",
                    "brand_config_json",
                    "language",
                    "auto_schedule",
                    "owner_key",
                    "status",
                    "hook_json",
                    "caption_style",
                    "aspect_ratio",
                ):
                    setattr(existing, field_name, getattr(new_row, field_name))
            await session.commit()
        return pipeline

    async def get_pipeline(self, pipeline_id: str) -> ContentPipeline | None:
        self._require_db()
        factory = get_session_factory()
        async with factory() as session:
            row = await session.get(ContentPipelineModel, pipeline_id)
            if row is None:
                return None
            pieces_result = await session.execute(
                select(ContentPieceModel)
                .where(
                    cast(
                        ColumnElement[bool],
                        ContentPieceModel.pipeline_id == pipeline_id,
                    )
                )
                .order_by(
                    cast(ColumnElement[Any], ContentPieceModel.generated_at).asc()
                )
            )
            pieces = list(pieces_result.scalars().all())
        return _row_to_pipeline(row, pieces)

    async def store_content(self, content: GeneratedContent) -> None:
        """Insert (or replace on matching content_id) a generated piece.

        The old in-memory store mutated pipeline.content_pieces in place;
        the PG-backed equivalent queries pieces fresh on each
        get_pipeline/list_by_pipeline call, so no in-place mutation is
        needed.
        """
        self._require_db()
        factory = get_session_factory()
        async with factory() as session:
            existing = await session.get(ContentPieceModel, content.content_id)
            new_row = content_piece_to_model(content)
            if existing is None:
                session.add(new_row)
            else:
                for field_name in (
                    "pipeline_id",
                    "format",
                    "title",
                    "description",
                    "download_url",
                    "thumbnail_url",
                    "duration_seconds",
                    "dimensions",
                    "file_size_bytes",
                    "status",
                    "metadata_json",
                    "generated_at",
                ):
                    setattr(existing, field_name, getattr(new_row, field_name))
            await session.commit()

    async def get_content(self, content_id: str) -> GeneratedContent | None:
        self._require_db()
        factory = get_session_factory()
        async with factory() as session:
            row = await session.get(ContentPieceModel, content_id)
        return content_piece_model_to_schema(row) if row else None

    async def list_by_pipeline(self, pipeline_id: str) -> list[GeneratedContent]:
        self._require_db()
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(ContentPieceModel)
                .where(
                    cast(
                        ColumnElement[bool],
                        ContentPieceModel.pipeline_id == pipeline_id,
                    )
                )
                .order_by(
                    cast(ColumnElement[Any], ContentPieceModel.generated_at).asc()
                )
            )
            rows = list(result.scalars().all())
        return [content_piece_model_to_schema(r) for r in rows]

    async def create_campaign(self, campaign: LiveCampaign) -> LiveCampaign:
        self._require_db()
        factory = get_session_factory()
        async with factory() as session:
            existing = await session.get(ContentCampaignModel, campaign.campaign_id)
            new_row = _campaign_to_row(campaign)
            if existing is None:
                session.add(new_row)
            else:
                for field_name in (
                    "campaign_title",
                    "source_url",
                    "hooks_json",
                    "pipeline_ids_json",
                    "status",
                    "owner_key",
                ):
                    setattr(existing, field_name, getattr(new_row, field_name))
            await session.commit()
        return campaign

    async def get_campaign(self, campaign_id: str) -> LiveCampaign | None:
        self._require_db()
        factory = get_session_factory()
        async with factory() as session:
            row = await session.get(ContentCampaignModel, campaign_id)
        return _row_to_campaign(row) if row else None

    async def list_campaigns(self, owner_key: str | None = None) -> list[LiveCampaign]:
        """List campaigns, newest first; ``owner_key`` restricts to one owner."""
        self._require_db()
        factory = get_session_factory()
        query = select(ContentCampaignModel)
        if owner_key is not None:
            query = query.where(
                cast(ColumnElement[bool], ContentCampaignModel.owner_key == owner_key)
            )
        async with factory() as session:
            result = await session.execute(
                query.order_by(
                    cast(ColumnElement[Any], ContentCampaignModel.created_at).desc()
                )
            )
            rows = list(result.scalars().all())
        return [_row_to_campaign(r) for r in rows]


# ---------------------------------------------------------------------------
# Format Adapters (upgraded for Live Mode)
# ---------------------------------------------------------------------------

# 1-to-20 multiplication rule: pieces per format per hook
HOOK_FORMAT_MULTIPLIERS = {
    ContentFormat.SHORT_VIDEO: 3,  # 3 clip variants per hook
    ContentFormat.STATIC_IMAGE: 3,  # 3 thumbnail variants
    ContentFormat.TEXT_POST: 3,  # 3 platform-adapted text posts
    ContentFormat.CAROUSEL: 1,  # 1 deep-dive carousel
    ContentFormat.AUDIOGRAM: 1,  # 1 audio waveform
    ContentFormat.BLOG_EXCERPT: 1,  # 1 SEO excerpt
    ContentFormat.EMAIL_SNIPPET: 1,  # 1 newsletter block
    ContentFormat.QUOTE_CARD: 2,  # 2 pull-quote images
    ContentFormat.DEBATE_CLIP: 1,  # 1 side-by-side debate
    ContentFormat.LONG_VIDEO: 1,  # 1 long-form version
}

# Standard pipeline multipliers (non-hook mode)
FORMAT_MULTIPLIERS = {
    ContentFormat.SHORT_VIDEO: 5,
    ContentFormat.LONG_VIDEO: 1,
    ContentFormat.AUDIOGRAM: 1,
    ContentFormat.CAROUSEL: 2,
    ContentFormat.STATIC_IMAGE: 5,
    ContentFormat.TEXT_POST: 5,
    ContentFormat.BLOG_EXCERPT: 1,
    ContentFormat.EMAIL_SNIPPET: 1,
    ContentFormat.QUOTE_CARD: 2,
    ContentFormat.DEBATE_CLIP: 1,
}

# Caption style configurations
CAPTION_CONFIGS = {
    CaptionStyle.WORD_BY_WORD: {
        "animation": "pop",
        "font_size": 48,
        "position": "center",
        "bg_opacity": 0.7,
    },
    CaptionStyle.SENTENCE_HIGHLIGHT: {
        "animation": "highlight",
        "font_size": 36,
        "position": "bottom_third",
        "bg_opacity": 0.5,
    },
    CaptionStyle.KARAOKE: {
        "animation": "bounce",
        "font_size": 42,
        "position": "center",
        "bg_opacity": 0.6,
    },
    CaptionStyle.BOLD_IMPACT: {
        "animation": "slam",
        "font_size": 64,
        "position": "center",
        "bg_opacity": 0.0,
        "stroke_width": 3,
        "font_weight": "900",
    },
    CaptionStyle.DUAL_COLOR: {
        "animation": "fade_swap",
        "font_size": 48,
        "position": "center",
        "primary_color": "#FFFFFF",
        "accent_color": "#FF6B00",
    },
}


class FormatAdapter:
    """
    Generates content in a specific format from source material.
    Each adapter handles one output format.
    Upgraded for Live Mode: hook-aware metadata, 9:16 vertical, animated captions.
    """

    @staticmethod
    async def adapt_short_video(
        pipeline: ContentPipeline,
        index: int,
    ) -> GeneratedContent:
        """Generate a vertical short-form video clip with animated captions."""
        content_id = str(uuid.uuid4())
        hook = pipeline.hook
        caption_config = CAPTION_CONFIGS.get(
            pipeline.caption_style, CAPTION_CONFIGS[CaptionStyle.BOLD_IMPACT]
        )
        dimensions = "1080x1920" if pipeline.aspect_ratio == "9:16" else "1920x1080"

        metadata = {
            "brand_config": pipeline.brand_config,
            "caption_style": pipeline.caption_style.value,
            "caption_config": caption_config,
            "aspect_ratio": pipeline.aspect_ratio,
            "variant": index + 1,
        }
        if hook:
            metadata.update(
                {
                    "hook_id": hook.hook_id or "",
                    "hook_type": hook.hook_type.value,
                    "source_segment": f"{hook.start_seconds}s-{hook.end_seconds}s",
                    "transcript_snippet": hook.transcript_snippet[:200],
                }
            )

        return GeneratedContent(
            content_id=content_id,
            pipeline_id=pipeline.pipeline_id,
            format=ContentFormat.SHORT_VIDEO,
            title=f"{pipeline.title} — Clip {index + 1}",
            description=(
                f"9:16 vertical clip with {pipeline.caption_style.value} captions. "
                f"Hook: {hook.title if hook else 'auto-detected'}"
            ),
            download_url=f"/v1/factory/content/{content_id}/download",
            thumbnail_url=f"/v1/factory/content/{content_id}/thumbnail",
            duration_seconds=hook.end_seconds - hook.start_seconds if hook else 30.0,
            dimensions=dimensions,
            file_size_bytes=5_000_000,
            status=ContentStatus.READY,
            generated_at=datetime.now(timezone.utc),
            metadata=metadata,
        )

    @staticmethod
    async def adapt_static_image(
        pipeline: ContentPipeline,
        index: int,
    ) -> GeneratedContent:
        """Generate a key-frame thumbnail with text overlay and pull quote."""
        content_id = str(uuid.uuid4())
        hook = pipeline.hook

        metadata = {"brand_config": pipeline.brand_config, "variant": index + 1}
        if hook:
            metadata.update(
                {
                    "hook_id": hook.hook_id,
                    "pull_quote": (
                        hook.transcript_snippet[:140] if hook.transcript_snippet else ""
                    ),
                    "talking_points": hook.talking_points[:3],
                }
            )

        return GeneratedContent(
            content_id=content_id,
            pipeline_id=pipeline.pipeline_id,
            format=ContentFormat.STATIC_IMAGE,
            title=f"{pipeline.title} — Image {index + 1}",
            description=f"Key-frame with pull quote from '{pipeline.title}'",
            download_url=f"/v1/factory/content/{content_id}/download",
            thumbnail_url=f"/v1/factory/content/{content_id}/download",
            dimensions="1080x1080",
            file_size_bytes=500_000,
            status=ContentStatus.READY,
            generated_at=datetime.now(timezone.utc),
            metadata=metadata,
        )

    @staticmethod
    async def adapt_text_post(
        pipeline: ContentPipeline,
        index: int,
    ) -> GeneratedContent:
        """Generate a platform-native text post with pull quote."""
        content_id = str(uuid.uuid4())
        hook = pipeline.hook

        # Platform-specific adaptations
        platforms = ["twitter", "linkedin", "threads"]
        target_platform = platforms[index % len(platforms)]

        talking_points = hook.talking_points if hook else ["Agent economy insight"]
        text = (
            hook.transcript_snippet[:200]
            if hook and hook.transcript_snippet
            else f"Key insight from {pipeline.title}: [auto-generated pull quote]"
        )

        return GeneratedContent(
            content_id=content_id,
            pipeline_id=pipeline.pipeline_id,
            format=ContentFormat.TEXT_POST,
            title=f"{pipeline.title} — {target_platform.title()} Post {index + 1}",
            description=f"Auto-generated {target_platform} post",
            download_url=f"/v1/factory/content/{content_id}/download",
            status=ContentStatus.READY,
            generated_at=datetime.now(timezone.utc),
            metadata={
                "text": text,
                "platform": target_platform,
                "hashtags": ["agenteconomy", "b2a", "automation", "aiagents"],
                "talking_points": talking_points,
                "hook_id": hook.hook_id if hook else None,
            },
        )

    @staticmethod
    async def adapt_audiogram(
        pipeline: ContentPipeline,
        index: int,
    ) -> GeneratedContent:
        """Generate audio waveform video for podcast distribution."""
        content_id = str(uuid.uuid4())
        hook = pipeline.hook
        return GeneratedContent(
            content_id=content_id,
            pipeline_id=pipeline.pipeline_id,
            format=ContentFormat.AUDIOGRAM,
            title=f"{pipeline.title} — Audiogram",
            download_url=f"/v1/factory/content/{content_id}/download",
            duration_seconds=hook.end_seconds - hook.start_seconds if hook else 60.0,
            dimensions="1080x1080",
            file_size_bytes=3_000_000,
            status=ContentStatus.READY,
            generated_at=datetime.now(timezone.utc),
            metadata={"hook_id": hook.hook_id if hook else None},
        )

    @staticmethod
    async def adapt_carousel(
        pipeline: ContentPipeline,
        index: int,
    ) -> GeneratedContent:
        """Generate multi-image carousel for Instagram/LinkedIn."""
        content_id = str(uuid.uuid4())
        hook = pipeline.hook
        slide_count = max(3, len(hook.talking_points) + 2) if hook else 5

        return GeneratedContent(
            content_id=content_id,
            pipeline_id=pipeline.pipeline_id,
            format=ContentFormat.CAROUSEL,
            title=f"{pipeline.title} — Carousel",
            download_url=f"/v1/factory/content/{content_id}/download",
            dimensions="1080x1080",
            status=ContentStatus.READY,
            generated_at=datetime.now(timezone.utc),
            metadata={
                "slide_count": slide_count,
                "hook_id": hook.hook_id if hook else None,
                "talking_points": hook.talking_points if hook else [],
            },
        )

    @staticmethod
    async def adapt_blog_excerpt(
        pipeline: ContentPipeline,
        index: int,
    ) -> GeneratedContent:
        """Generate SEO-optimized blog excerpt."""
        content_id = str(uuid.uuid4())
        hook = pipeline.hook
        return GeneratedContent(
            content_id=content_id,
            pipeline_id=pipeline.pipeline_id,
            format=ContentFormat.BLOG_EXCERPT,
            title=f"{pipeline.title} — Blog Excerpt",
            download_url=f"/v1/factory/content/{content_id}/download",
            status=ContentStatus.READY,
            generated_at=datetime.now(timezone.utc),
            metadata={
                "word_count": 250,
                "seo_keywords": [
                    "agent middleware",
                    "b2a",
                    "api automation",
                    "ai agents",
                ],
                "hook_id": hook.hook_id if hook else None,
            },
        )

    @staticmethod
    async def adapt_email_snippet(
        pipeline: ContentPipeline,
        index: int,
    ) -> GeneratedContent:
        """Generate newsletter-ready HTML block."""
        content_id = str(uuid.uuid4())
        return GeneratedContent(
            content_id=content_id,
            pipeline_id=pipeline.pipeline_id,
            format=ContentFormat.EMAIL_SNIPPET,
            title=f"{pipeline.title} — Email Block",
            download_url=f"/v1/factory/content/{content_id}/download",
            status=ContentStatus.READY,
            generated_at=datetime.now(timezone.utc),
            metadata={"html_preview": "<div>...</div>"},
        )

    @staticmethod
    async def adapt_quote_card(
        pipeline: ContentPipeline,
        index: int,
    ) -> GeneratedContent:
        """Generate a pull-quote image card for debate/reaction sharing."""
        content_id = str(uuid.uuid4())
        hook = pipeline.hook
        quote = (
            hook.transcript_snippet[:140]
            if hook and hook.transcript_snippet
            else f"Key quote from {pipeline.title}"
        )

        return GeneratedContent(
            content_id=content_id,
            pipeline_id=pipeline.pipeline_id,
            format=ContentFormat.QUOTE_CARD,
            title=f"{pipeline.title} — Quote Card {index + 1}",
            description=f'Pull-quote card: "{quote[:60]}..."',
            download_url=f"/v1/factory/content/{content_id}/download",
            thumbnail_url=f"/v1/factory/content/{content_id}/download",
            dimensions="1080x1080",
            file_size_bytes=400_000,
            status=ContentStatus.READY,
            generated_at=datetime.now(timezone.utc),
            metadata={
                "quote": quote,
                "hook_id": hook.hook_id if hook else None,
                "brand_config": pipeline.brand_config,
                "variant": index + 1,
            },
        )

    @staticmethod
    async def adapt_debate_clip(
        pipeline: ContentPipeline,
        index: int,
    ) -> GeneratedContent:
        """Generate a side-by-side debate/contrast clip."""
        content_id = str(uuid.uuid4())
        hook = pipeline.hook
        dimensions = "1080x1920" if pipeline.aspect_ratio == "9:16" else "1920x1080"

        return GeneratedContent(
            content_id=content_id,
            pipeline_id=pipeline.pipeline_id,
            format=ContentFormat.DEBATE_CLIP,
            title=f"{pipeline.title} — Debate Clip",
            description="Side-by-side contrast clip with animated captions",
            download_url=f"/v1/factory/content/{content_id}/download",
            thumbnail_url=f"/v1/factory/content/{content_id}/thumbnail",
            duration_seconds=hook.end_seconds - hook.start_seconds if hook else 45.0,
            dimensions=dimensions,
            file_size_bytes=6_000_000,
            status=ContentStatus.READY,
            generated_at=datetime.now(timezone.utc),
            metadata={
                "hook_id": hook.hook_id if hook else None,
                "caption_style": pipeline.caption_style.value,
                "aspect_ratio": pipeline.aspect_ratio,
                "talking_points": hook.talking_points if hook else [],
            },
        )


FORMAT_ADAPTERS = {
    ContentFormat.SHORT_VIDEO: FormatAdapter.adapt_short_video,
    ContentFormat.STATIC_IMAGE: FormatAdapter.adapt_static_image,
    ContentFormat.TEXT_POST: FormatAdapter.adapt_text_post,
    ContentFormat.AUDIOGRAM: FormatAdapter.adapt_audiogram,
    ContentFormat.CAROUSEL: FormatAdapter.adapt_carousel,
    ContentFormat.BLOG_EXCERPT: FormatAdapter.adapt_blog_excerpt,
    ContentFormat.EMAIL_SNIPPET: FormatAdapter.adapt_email_snippet,
    ContentFormat.QUOTE_CARD: FormatAdapter.adapt_quote_card,
    ContentFormat.DEBATE_CLIP: FormatAdapter.adapt_debate_clip,
}


# ---------------------------------------------------------------------------
# Algorithmic Scheduler
# ---------------------------------------------------------------------------


class AlgorithmicScheduler:
    """
    Learns optimal posting times from engagement analytics.
    Uses a simple historical-peak model: finds the time windows
    that historically produced the highest engagement per platform+format.

    Production: replace with a proper ML model (XGBoost, etc.) trained
    on the analytics data.
    """

    def __init__(self):
        self._analytics: list[PlatformAnalytics] = []
        self._lock = asyncio.Lock()

        # Default engagement curves (hour of day UTC → relative score)
        # Learned from historical data in production
        self._default_curves: dict[str, dict[int, float]] = {
            "youtube_shorts": {9: 0.5, 12: 0.7, 14: 0.8, 17: 1.0, 20: 0.9, 22: 0.6},
            "tiktok": {8: 0.4, 11: 0.7, 15: 0.9, 19: 1.0, 22: 0.8},
            "instagram_reels": {9: 0.5, 12: 0.8, 17: 1.0, 21: 0.9},
            "x_video": {8: 0.6, 13: 0.8, 17: 1.0, 20: 0.7},
            "linkedin_video": {8: 0.9, 10: 1.0, 12: 0.8, 17: 0.7},
        }

    async def ingest_analytics(
        self, metrics: list[PlatformAnalytics]
    ) -> dict[str, int]:
        """Ingest engagement metrics to improve scheduling model."""
        summary: dict[str, int] = defaultdict(int)
        async with self._lock:
            for m in metrics:
                self._analytics.append(m)
                summary[m.platform] += 1
        logger.info(f"Ingested {len(metrics)} analytics data points")
        return dict(summary)

    async def recommend(
        self,
        content_ids: list[str],
        platforms: list[str],
        earliest: datetime | None = None,
        latest: datetime | None = None,
        max_per_day: int = 3,
    ) -> list[ScheduleRecommendation]:
        """
        Generate optimal posting schedule based on engagement data.
        Spreads posts across days to avoid audience fatigue.
        """
        require_simulation("content_factory", issue="#31")
        now = datetime.now(timezone.utc)
        start = earliest or now
        end = latest or (now + timedelta(days=7))

        recommendations = []
        # Track posts per platform per day
        slots_used: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

        for content_id in content_ids:
            for platform in platforms:
                curve = self._default_curves.get(platform, {12: 0.7, 17: 1.0})

                # Find best available slot
                best_slot = await self._find_best_slot(
                    platform, curve, start, end, slots_used[platform], max_per_day
                )

                if best_slot:
                    slot_time, confidence = best_slot
                    day_key = slot_time.strftime("%Y-%m-%d")
                    slots_used[platform][day_key] += 1

                    recommendations.append(
                        ScheduleRecommendation(
                            content_id=content_id,
                            platform=platform,
                            recommended_time=slot_time,
                            confidence=round(confidence, 2),
                            reasoning=self._explain_recommendation(
                                platform, slot_time, confidence
                            ),
                            estimated_views=self._estimate_views(platform, confidence),
                        )
                    )

        # Sort by time
        recommendations.sort(key=lambda r: r.recommended_time)
        return recommendations

    async def _find_best_slot(
        self,
        platform: str,
        curve: dict[int, float],
        start: datetime,
        end: datetime,
        used: dict[str, int],
        max_per_day: int,
    ) -> tuple[datetime, float] | None:
        """Find the highest-engagement available slot."""
        # Sort hours by engagement score (descending)
        ranked_hours = sorted(curve.items(), key=lambda x: x[1], reverse=True)

        current_day = start.replace(hour=0, minute=0, second=0, microsecond=0)
        while current_day <= end:
            day_key = current_day.strftime("%Y-%m-%d")
            if used.get(day_key, 0) >= max_per_day:
                current_day += timedelta(days=1)
                continue

            for hour, score in ranked_hours:
                candidate = current_day.replace(hour=hour)
                if start <= candidate <= end:
                    return candidate, score

            current_day += timedelta(days=1)

        return None

    def _explain_recommendation(
        self, platform: str, time: datetime, confidence: float
    ) -> str:
        """Generate human/agent-readable explanation."""
        day_name = time.strftime("%A")
        hour = time.strftime("%I%p").lstrip("0")
        return (
            f"Historical peak for {platform}: {day_name}s at {hour} UTC "
            f"(confidence: {confidence:.0%}). Based on engagement curve analysis."
        )

    def _estimate_views(self, platform: str, confidence: float) -> int:
        """Rough view estimate based on platform and confidence."""
        base_views = {
            "youtube_shorts": 5000,
            "tiktok": 8000,
            "instagram_reels": 3000,
            "x_video": 2000,
            "linkedin_video": 1500,
        }
        base = base_views.get(platform, 1000)
        return int(base * confidence)

    async def get_analytics_summary(self) -> dict:
        """Return summary of ingested analytics data."""
        by_platform: dict[str, int] = defaultdict(int)
        by_metric: dict[str, int] = defaultdict(int)
        for m in self._analytics:
            by_platform[m.platform] += 1
            by_metric[m.metric_type.value] += 1

        return {
            "total_data_points": len(self._analytics),
            "by_platform": dict(by_platform),
            "by_metric": dict(by_metric),
        }


# ---------------------------------------------------------------------------
# Content Factory Orchestrator
# ---------------------------------------------------------------------------


class ContentFactory:
    """
    Top-level orchestrator for the Programmatic Content Factory.
    Coordinates: source analysis → hook extraction → multi-format adaptation
    → rendering → scheduling → distribution.

    Live Mode: Accepts targeted hooks and applies the 1-to-N multiplication
    rule with 9:16 vertical rendering and animated captions.
    """

    def __init__(self):
        self.store = ContentStore()
        self.scheduler = AlgorithmicScheduler()
        self.generation_store = ContentGenerationStore()
        # The event loop holds only weak references to tasks; keep in-flight
        # pipeline runs alive until they finish.
        self._pipeline_tasks: set[asyncio.Task[None]] = set()

    async def generate_llm_text(
        self, prompt: str, model: str | None = None
    ) -> dict[str, Any]:
        """Simulated or OpenAI-compatible text generation with optional DB persistence."""
        return await generate_text(
            store=self.generation_store, prompt=prompt, model=model
        )

    async def get_llm_generation(self, content_id: str) -> dict[str, Any] | None:
        """Fetch a persisted generation row (real mode only)."""
        return await self.generation_store.get_record(content_id)

    async def create_pipeline(
        self,
        title: str,
        target_formats: list[ContentFormat],
        source_clip_id: str | None = None,
        source_url: str | None = None,
        brand_config: dict | None = None,
        language: str = "en",
        auto_schedule: bool = True,
        owner_key: str = "",
        hook: ContentHook | None = None,
        caption_style: CaptionStyle = CaptionStyle.BOLD_IMPACT,
        aspect_ratio: str = "9:16",
        run_inline: bool = False,
    ) -> ContentPipeline:
        """Create a new content generation pipeline (standard or hook-based).

        Generation runs in the background unless ``run_inline`` is set, in
        which case it has finished (``ready`` or ``failed``) on return.
        """
        pipeline = ContentPipeline(
            pipeline_id=str(uuid.uuid4()),
            title=title,
            source_clip_id=source_clip_id,
            source_url=source_url,
            target_formats=target_formats,
            brand_config=brand_config or {},
            language=language,
            auto_schedule=auto_schedule,
            owner_key=owner_key,
            hook=hook,
            caption_style=caption_style,
            aspect_ratio=aspect_ratio,
        )
        await self.store.create_pipeline(pipeline)

        if run_inline:
            await self._run_pipeline(pipeline.pipeline_id)
            # The durable store returns copies; _run_pipeline updates its own
            # copy, so return the saved terminal status rather than "queued".
            finished = await self.store.get_pipeline(pipeline.pipeline_id)
            assert finished is not None
            return finished

        # Kick off async generation
        task = asyncio.create_task(self._run_pipeline(pipeline.pipeline_id))
        self._pipeline_tasks.add(task)
        task.add_done_callback(self._pipeline_tasks.discard)

        return pipeline

    def estimate_pieces(self, formats: list[ContentFormat]) -> int:
        """Estimate total content pieces for given formats."""
        return sum(FORMAT_MULTIPLIERS.get(f, 1) for f in formats)

    def estimate_hook_pieces(self, hooks: list[ContentHook]) -> int:
        """Estimate total pieces across all hooks using hook multipliers."""
        total = 0
        for hook in hooks:
            for fmt in hook.target_formats:
                total += HOOK_FORMAT_MULTIPLIERS.get(fmt, 1)
        return total

    async def _run_pipeline(self, pipeline_id: str) -> None:
        """Execute the content generation pipeline.

        Every status transition is written back through the store. The stored
        row is the only status any reader sees (GET /pipelines/{id});
        setting it on the local copy alone left every pipeline reporting
        "queued" forever.
        """
        pipeline = await self.store.get_pipeline(pipeline_id)
        if not pipeline:
            return

        logger.info(
            f"Pipeline {pipeline_id}: rendering {len(pipeline.target_formats)} formats"
        )

        try:
            pipeline.status = "rendering"
            await self.store.create_pipeline(pipeline)

            tasks = []
            multipliers = (
                HOOK_FORMAT_MULTIPLIERS if pipeline.hook else FORMAT_MULTIPLIERS
            )

            for fmt in pipeline.target_formats:
                adapter = FORMAT_ADAPTERS.get(fmt)
                if not adapter:
                    continue
                count = multipliers.get(fmt, 1)
                for i in range(count):
                    tasks.append(adapter(pipeline, i))

            pieces = await asyncio.gather(*tasks)

            for piece in pieces:
                await self.store.store_content(piece)

            pipeline.status = "ready"
            await self.store.create_pipeline(pipeline)
            logger.info(f"Pipeline {pipeline_id}: {len(pieces)} pieces generated")

        except Exception as e:
            pipeline.status = "failed"
            logger.error(f"Pipeline {pipeline_id} failed: {e}")
            try:
                await self.store.create_pipeline(pipeline)
            except Exception:
                logger.exception(f"Pipeline {pipeline_id}: could not record failure")

    async def launch_campaign(
        self,
        campaign_title: str,
        source_url: str,
        hooks: list[ContentHook],
        brand_config: dict | None = None,
        caption_style: CaptionStyle = CaptionStyle.BOLD_IMPACT,
        aspect_ratio: str = "9:16",
        platforms: list[str] | None = None,
        max_posts_per_day: int = 3,
        language: str = "en",
        auto_schedule: bool = True,
        owner_key: str = "",
    ) -> LiveCampaignResponse:
        """
        Launch a full live content campaign.

        This is the 1-to-20 multiplication engine:
        1. For each hook, create a pipeline with hook-aware format adapters
        2. Generate all content pieces (9:16 vertical, animated captions)
        3. Auto-schedule across platforms via AlgorithmicScheduler
        """
        campaign_id = str(uuid.uuid4())

        # Assign hook IDs
        for i, hook in enumerate(hooks):
            if not hook.hook_id:
                hook.hook_id = f"hook-{campaign_id[:8]}-{i}"

        campaign = LiveCampaign(
            campaign_id=campaign_id,
            campaign_title=campaign_title,
            source_url=source_url,
            hooks=hooks,
            owner_key=owner_key,
        )
        await self.store.create_campaign(campaign)

        # Create a pipeline per hook
        hook_results: list[CampaignHookResult] = []
        all_content_ids: list[str] = []
        all_pipelines_ready = True

        for hook in hooks:
            pipeline = await self.create_pipeline(
                title=f"{campaign_title} — {hook.title}",
                target_formats=hook.target_formats,
                source_url=source_url,
                brand_config=brand_config,
                language=language,
                auto_schedule=False,  # We schedule at campaign level
                owner_key=owner_key,
                hook=hook,
                caption_style=caption_style,
                aspect_ratio=aspect_ratio,
                # Generate before gathering: the old bounded poll for "ready"
                # reported whatever pieces existed when it gave up.
                run_inline=True,
            )
            campaign.pipeline_ids.append(pipeline.pipeline_id)
            all_pipelines_ready = all_pipelines_ready and pipeline.status == "ready"
            if not all_pipelines_ready:
                campaign.status = "failed"
            # Preserve completed work if a later hook or scheduling step fails.
            await self.store.create_campaign(campaign)

            # Gather results
            content = await self.store.list_by_pipeline(pipeline.pipeline_id)
            content_ids = [c.content_id for c in content]
            all_content_ids.extend(content_ids)

            pieces_by_format: dict[str, int] = defaultdict(int)
            for c in content:
                pieces_by_format[c.format.value] += 1

            hook_results.append(
                CampaignHookResult(
                    hook_id=hook.hook_id or "",
                    hook_title=hook.title,
                    hook_type=hook.hook_type,
                    content_pieces=content_ids,
                    pieces_by_format=dict(pieces_by_format),
                    total_pieces=len(content_ids),
                )
            )

        # Auto-schedule across platforms
        schedule_summary: dict = {}
        if auto_schedule and all_content_ids and platforms and all_pipelines_ready:
            recommendations = await self.scheduler.recommend(
                content_ids=all_content_ids,
                platforms=platforms,
                max_per_day=max_posts_per_day,
            )
            schedule_summary = {
                "total_scheduled": len(recommendations),
                "platforms": list(set(r.platform for r in recommendations)),
                "date_range": (
                    f"{recommendations[0].recommended_time.strftime('%Y-%m-%d')} to "
                    f"{recommendations[-1].recommended_time.strftime('%Y-%m-%d')}"
                    if recommendations
                    else "none"
                ),
                "estimated_total_views": sum(
                    r.estimated_views or 0 for r in recommendations
                ),
                "recommendations_preview": [
                    {
                        "content_id": r.content_id,
                        "platform": r.platform,
                        "time": r.recommended_time.isoformat(),
                        "confidence": r.confidence,
                        "estimated_views": r.estimated_views,
                    }
                    for r in recommendations[:10]  # First 10 as preview
                ],
            }

        campaign.status = "completed" if all_pipelines_ready else "failed"
        await self.store.create_campaign(campaign)
        total_pieces = sum(hr.total_pieces for hr in hook_results)

        return LiveCampaignResponse(
            campaign_id=campaign_id,
            campaign_title=campaign_title,
            source_url=source_url,
            status=campaign.status,
            hooks_processed=len(hooks),
            total_content_pieces=total_pieces,
            hook_results=hook_results,
            schedule_generated=bool(schedule_summary),
            schedule_summary=schedule_summary,
            pipeline_ids=campaign.pipeline_ids,
        )

    async def get_content(self, content_id: str) -> GeneratedContent | None:
        return await self.store.get_content(content_id)  # type: ignore[no-any-return]

    async def list_pipeline_content(self, pipeline_id: str) -> list[GeneratedContent]:
        return await self.store.list_by_pipeline(pipeline_id)  # type: ignore[no-any-return]
