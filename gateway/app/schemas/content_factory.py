"""
Schemas for the Programmatic Content Factory.
Handles automated content repurposing, format adaptation,
engagement-driven scheduling, and live campaign orchestration.

Week 6+ Live Mode: Hook-based ingestion turns one source video
into 20+ platform-adapted content pieces with animated captions,
then feeds them through the AlgorithmicScheduler for staggered
cross-platform distribution.
"""

from pydantic import BaseModel, Field, field_validator, model_validator
from enum import Enum
from datetime import datetime


class ContentFormat(str, Enum):
    """Output content formats."""

    SHORT_VIDEO = "short_video"  # 15-60s vertical (Shorts/Reels/TikTok)
    LONG_VIDEO = "long_video"  # 2-15min horizontal (YouTube)
    AUDIOGRAM = "audiogram"  # Audio waveform video for podcasts
    CAROUSEL = "carousel"  # Multi-image slideshow
    STATIC_IMAGE = "static_image"  # Single thumbnail/poster
    TEXT_POST = "text_post"  # Platform-native text (X/LinkedIn)
    BLOG_EXCERPT = "blog_excerpt"  # SEO-optimized text snippet
    EMAIL_SNIPPET = "email_snippet"  # Newsletter-ready HTML block
    QUOTE_CARD = "quote_card"  # Pull-quote image for debate/reaction
    DEBATE_CLIP = "debate_clip"  # Side-by-side argument clip


class ContentStatus(str, Enum):
    QUEUED = "queued"
    RENDERING = "rendering"
    READY = "ready"
    DISTRIBUTED = "distributed"
    FAILED = "failed"


class HookType(str, Enum):
    """Types of content hooks extracted from source material."""

    REACTION = "reaction"  # Hot-take / emotional reaction clip
    EDUCATIONAL = "educational"  # Concept explainer segment
    QUOTE_CARD = "quote_card"  # Pull-quote for static image
    DEBATE = "debate"  # Contrasting viewpoints
    MONTAGE = "montage"  # Rapid-fire highlights
    COLD_OPEN = "cold_open"  # Pattern-interrupt opener


class CaptionStyle(str, Enum):
    """Animated caption rendering styles for vertical video."""

    WORD_BY_WORD = "word_by_word"  # Each word pops on screen
    SENTENCE_HIGHLIGHT = "sentence_highlight"
    KARAOKE = "karaoke"  # Bouncing ball style
    BOLD_IMPACT = "bold_impact"  # Large centered bold text
    DUAL_COLOR = "dual_color"  # Key words in accent color


class EngagementMetricType(str, Enum):
    """Metrics the scheduling engine tracks."""

    VIEWS = "views"
    WATCH_TIME = "watch_time"
    LIKES = "likes"
    SHARES = "shares"
    COMMENTS = "comments"
    CLICK_THROUGH = "click_through"
    SAVE_RATE = "save_rate"
    FOLLOWER_GAIN = "follower_gain"


# --- Content Hook Schemas ---


class ContentHook(BaseModel):
    """A targeted content hook extracted from source material.

    Hooks are the atomic units of the 1-to-20 multiplication rule:
    each hook produces multiple format-adapted content pieces.
    """

    hook_id: str | None = Field(
        default=None,
        description="Auto-assigned if omitted. Pass existing hook_id to reuse.",
    )
    title: str = Field(
        ...,
        description="Working title for this hook.",
        examples=["The Zero-GUI Thesis"],
    )
    hook_type: HookType = Field(
        ...,
        description="Type of content piece this hook produces.",
    )
    start_seconds: float = Field(
        ...,
        ge=0,
        description="Start timestamp in source video (seconds).",
    )
    end_seconds: float = Field(
        ...,
        gt=0,
        description="End timestamp in source video (seconds).",
    )
    transcript_snippet: str = Field(
        default="",
        description="Key quote or transcript from this segment.",
    )
    talking_points: list[str] = Field(
        default_factory=list,
        description="Key talking points for text-based formats.",
    )
    target_formats: list[ContentFormat] = Field(
        default=[
            ContentFormat.SHORT_VIDEO,
            ContentFormat.STATIC_IMAGE,
            ContentFormat.TEXT_POST,
            ContentFormat.CAROUSEL,
        ],
        description=(
            "Formats to generate from this hook. Defaults to the 4-format spread."
        ),
    )

    @field_validator("end_seconds")
    @classmethod
    def end_after_start(cls, v: float, info) -> float:
        start = info.data.get("start_seconds", 0)
        if v <= start:
            raise ValueError("end_seconds must be after start_seconds")
        return v


# --- Live Campaign Schemas ---


class LiveCampaignRequest(BaseModel):
    """Launch a full live content campaign from a single source.

    This is the 'Big Red Button' — submit a source URL and a set of
    targeted hooks. The factory will:
    1. Extract each hook segment
    2. Apply the 1-to-N multiplication rule per hook
    3. Render all pieces in 9:16 vertical with animated captions
    4. Auto-schedule across platforms via the AlgorithmicScheduler
    """

    source_url: str = Field(
        ...,
        description="URL of the source video/audio asset.",
        examples=["https://www.youtube.com/watch?v=s9utHnNAOSA"],
    )
    campaign_title: str = Field(
        ...,
        description="Campaign name for tracking.",
        examples=["B2A Launch — Why Agents Need Their Own API Layer"],
    )
    hooks: list[ContentHook] = Field(
        ...,
        min_length=1,
        max_length=20,
        description="Targeted hooks to extract. Each hook produces N content pieces.",
    )
    brand_config: dict = Field(
        default_factory=dict,
        description="Brand colors, fonts, logo, watermark settings.",
    )
    caption_style: CaptionStyle = Field(
        default=CaptionStyle.BOLD_IMPACT,
        description="Animated caption style for all video outputs.",
    )
    aspect_ratio: str = Field(
        default="9:16",
        description="Target aspect ratio for video content. 9:16 for vertical-first.",
    )
    platforms: list[str] = Field(
        default=["youtube_shorts", "tiktok", "instagram_reels"],
        description="Target platforms for scheduling.",
    )
    max_posts_per_day: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Max posts per platform per day to avoid audience fatigue.",
    )
    language: str = Field(default="en")
    auto_schedule: bool = Field(
        default=True,
        description="Automatically generate optimal posting schedule.",
    )


class CampaignHookResult(BaseModel):
    """Results for a single hook within a campaign."""

    hook_id: str
    hook_title: str
    hook_type: HookType
    content_pieces: list[str] = Field(
        ...,
        description="Content IDs of all pieces generated from this hook.",
    )
    pieces_by_format: dict[str, int] = Field(
        ...,
        description="Count of pieces per format type.",
    )
    total_pieces: int


class LiveCampaignResponse(BaseModel):
    """Full results of a live content campaign."""

    campaign_id: str
    campaign_title: str
    source_url: str
    status: str
    hooks_processed: int
    total_content_pieces: int
    hook_results: list[CampaignHookResult]
    schedule_generated: bool
    schedule_summary: dict = Field(
        default_factory=dict,
        description="Summary of the auto-generated posting schedule.",
    )
    pipeline_ids: list[str] = Field(
        default_factory=list,
        description="Pipeline IDs created for each hook.",
    )


# --- Content Factory Schemas ---


class ContentPipelineRequest(BaseModel):
    """Submit a source asset for multi-format content generation."""

    source_clip_id: str | None = Field(
        None,
        description=(
            "ID of an existing clip from the Media Engine. "
            "Mutually exclusive with source_url."
        ),
    )
    source_url: str | None = Field(
        None,
        description="URL of raw source content (video, audio, image).",
    )
    title: str = Field(
        ...,
        description="Working title for the content batch.",
        examples=["Q1 Product Launch Highlights"],
    )
    target_formats: list[ContentFormat] = Field(
        default=[
            ContentFormat.SHORT_VIDEO,
            ContentFormat.STATIC_IMAGE,
            ContentFormat.TEXT_POST,
        ],
        description=(
            "Output formats to generate. Each source becomes multiple "
            "format-adapted pieces."
        ),
    )
    brand_config: dict = Field(
        default_factory=dict,
        description="Brand customization: colors, fonts, logo_url, watermark settings.",
        examples=[
            {
                "primary_color": "#FF6B00",
                "font_family": "Inter",
                "logo_url": "https://cdn.example.com/logo.png",
                "watermark_position": "bottom_right",
            }
        ],
    )
    language: str = Field(default="en")
    auto_schedule: bool = Field(
        default=True,
        description=(
            "Automatically schedule distribution using the algorithmic scheduler."
        ),
    )

    @model_validator(mode="after")
    def single_source(self) -> "ContentPipelineRequest":
        # Documented as mutually exclusive; accepting both silently reported
        # the pipeline as clip-sourced and ignored the URL.
        if self.source_clip_id is not None and self.source_url is not None:
            raise ValueError("source_clip_id and source_url are mutually exclusive")
        return self


class ContentPipelineResponse(BaseModel):
    """Response after initiating a content pipeline."""

    pipeline_id: str
    title: str
    source_type: str
    target_formats: list[ContentFormat]
    status: str
    estimated_pieces: int = Field(
        ...,
        description="Estimated number of content pieces that will be generated.",
    )


class GeneratedContent(BaseModel):
    """A single generated content piece."""

    content_id: str
    pipeline_id: str
    format: ContentFormat
    title: str
    description: str | None = None
    download_url: str
    thumbnail_url: str | None = None
    duration_seconds: float | None = None
    dimensions: str | None = Field(
        default=None,
        description="WxH for image/video formats.",
        examples=["1080x1920"],
    )
    file_size_bytes: int | None = None
    status: ContentStatus
    generated_at: datetime
    metadata: dict = Field(default_factory=dict)


class ContentListResponse(BaseModel):
    """Paginated content listing."""

    content: list[GeneratedContent]
    total: int
    pipeline_id: str


# --- Scheduling Schemas ---


class PlatformAnalytics(BaseModel):
    """Engagement analytics for a specific platform."""

    platform: str
    metric_type: EngagementMetricType
    value: float
    recorded_at: datetime
    content_id: str | None = None


class AnalyticsIngestRequest(BaseModel):
    """Submit engagement data to improve scheduling."""

    metrics: list[PlatformAnalytics] = Field(
        ...,
        min_length=1,
        max_length=500,
        description=(
            "Engagement metrics from platform APIs. The scheduler "
            "learns from this data."
        ),
    )


class AnalyticsIngestResponse(BaseModel):
    ingested: int
    platform_summary: dict = Field(
        ...,
        description="Per-platform ingestion counts.",
    )


class ScheduleRecommendation(BaseModel):
    """Algorithmic recommendation for when/where to post."""

    content_id: str
    platform: str
    recommended_time: datetime
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Model confidence in this time slot's engagement potential.",
    )
    reasoning: str = Field(
        ...,
        description="Explanation of why this slot was chosen.",
        examples=[
            "Historical peak engagement for short_video on tiktok: Tuesdays 7-9pm UTC"
        ],
    )
    estimated_views: int | None = None


class ScheduleRequest(BaseModel):
    """Request optimal scheduling for content pieces."""

    content_ids: list[str] = Field(
        ...,
        description="Content piece IDs to schedule.",
    )
    platforms: list[str] = Field(
        ...,
        description="Target platforms.",
    )
    earliest: datetime | None = Field(
        None,
        description="Earliest acceptable posting time. Defaults to now.",
    )
    latest: datetime | None = Field(
        None,
        description="Latest acceptable posting time. Defaults to 7 days from now.",
    )
    max_posts_per_day: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum posts per platform per day to avoid audience fatigue.",
    )


class ScheduleResponse(BaseModel):
    """Full schedule with per-content, per-platform recommendations."""

    recommendations: list[ScheduleRecommendation]
    total_scheduled: int
    date_range: str
