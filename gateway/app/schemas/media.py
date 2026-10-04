"""
Schemas for the Programmatic Media Engine service.
Handles video ingestion, viral hook detection, reframing, captioning,
and cross-platform distribution.
"""

from pydantic import BaseModel, Field
from enum import Enum
from datetime import datetime


class AspectRatio(str, Enum):
    """Target aspect ratios for reframing."""

    LANDSCAPE_16_9 = "16:9"
    PORTRAIT_9_16 = "9:16"
    SQUARE_1_1 = "1:1"
    CINEMATIC_21_9 = "21:9"


class Platform(str, Enum):
    """Supported distribution platforms."""

    YOUTUBE_SHORTS = "youtube_shorts"
    TIKTOK = "tiktok"
    INSTAGRAM_REELS = "instagram_reels"
    X_VIDEO = "x_video"
    LINKEDIN_VIDEO = "linkedin_video"


class CaptionStyle(str, Enum):
    """Animated caption styles (80% of social video is watched muted)."""

    WORD_BY_WORD = "word_by_word"
    SENTENCE_HIGHLIGHT = "sentence_highlight"
    KARAOKE = "karaoke"
    MINIMAL_SUBTITLE = "minimal_subtitle"


class VideoUploadRequest(BaseModel):
    """Initiate a video upload for processing."""

    source_url: str | None = Field(
        None,
        description="URL of the source video to fetch. Mutually exclusive with upload.",
    )
    title: str = Field(
        ...,
        description="Title for internal tracking and metadata tagging.",
        examples=["Q1 Product Demo - Full Length"],
    )
    language: str = Field(
        default="en",
        description="ISO 639-1 language code for transcription and captioning.",
    )
    metadata: dict = Field(
        default_factory=dict,
        description="Arbitrary metadata for downstream processing.",
    )


class VideoUploadResponse(BaseModel):
    """Response after initiating video upload."""

    video_id: str
    upload_url: str | None = Field(
        None,
        description=(
            "Pre-signed URL for direct upload (if source_url was not provided)."
        ),
    )
    status: str = Field(default="pending")
    estimated_processing_seconds: int | None = None


class ViralHook(BaseModel):
    """A detected high-engagement moment in the source video."""

    hook_id: str
    start_time_seconds: float
    end_time_seconds: float
    confidence_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Model confidence that this segment will drive engagement.",
    )
    trigger_type: str = Field(
        ...,
        description=(
            "What makes this moment engaging (e.g., 'emotional_peak', "
            "'speech_pattern', 'visual_surprise')."
        ),
    )
    transcript_snippet: str = Field(
        ...,
        description="Text of what is being said during this hook.",
    )


class ClipGenerationRequest(BaseModel):
    """Request to generate platform-ready clips from a processed video."""

    video_id: str
    hooks: list[str] | None = Field(
        None,
        description="Specific hook_ids to use. If null, uses top hooks by confidence.",
    )
    max_clips: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Maximum number of clips to generate.",
    )
    aspect_ratios: list[AspectRatio] = Field(
        default=[AspectRatio.PORTRAIT_9_16],
        description="Target aspect ratios for reframing.",
    )
    caption_style: CaptionStyle = Field(
        default=CaptionStyle.WORD_BY_WORD,
        description="Animated caption style to apply.",
    )
    caption_language: str = Field(
        default="en",
        description="Language for generated captions.",
    )


class GeneratedClip(BaseModel):
    """A single generated clip ready for distribution."""

    clip_id: str
    video_id: str
    hook_id: str
    aspect_ratio: AspectRatio
    duration_seconds: float
    download_url: str = Field(
        ...,
        description="Temporary URL to download the rendered clip.",
    )
    caption_style: CaptionStyle
    thumbnail_url: str | None = None
    generated_at: datetime


class ClipGenerationResponse(BaseModel):
    """Result of clip generation."""

    video_id: str
    clips: list[GeneratedClip]
    total_generated: int
    status: str


class DistributionRequest(BaseModel):
    """Push clips directly to social platforms via API."""

    clip_ids: list[str] = Field(
        ...,
        description="IDs of clips to distribute.",
    )
    platforms: list[Platform] = Field(
        ...,
        description="Target platforms for distribution.",
    )
    schedule_at: datetime | None = Field(
        None,
        description="Schedule for optimal posting time. Null = post immediately.",
    )
    title: str = Field(
        ...,
        description="Title/caption for the social post.",
    )
    hashtags: list[str] = Field(
        default_factory=list,
        description="Hashtags to include (without # prefix).",
    )
    optimize_schedule: bool = Field(
        default=True,
        description=(
            "Let the algorithm pick optimal posting windows based on engagement data."
        ),
    )


class DistributionResult(BaseModel):
    """Result of a single platform distribution."""

    clip_id: str
    platform: Platform
    status: str
    platform_post_id: str | None = None
    platform_url: str | None = None
    scheduled_at: datetime | None = None
    error: str | None = None


class DistributionResponse(BaseModel):
    """Aggregated distribution results."""

    results: list[DistributionResult]
    total_distributed: int
    total_failed: int
