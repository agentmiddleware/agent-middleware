"""
PROOF SURFACE — frozen. Do not expand; see docs/PROOF_SURFACES.md.
Programmatic Media Engine — Service Layer
==========================================
Handles the full video-to-viral-clip pipeline:
1. INGEST — Accept video uploads or URL fetches
2. ANALYZE — Transcribe, detect viral hooks via speech/emotion/visual signals
3. RENDER — Reframe to target aspect ratios, generate animated captions
4. DISTRIBUTE — Push to social platforms via their APIs

In production, wire up:
- Whisper / Deepgram for transcription
- FFmpeg for video processing
- Platform APIs (YouTube Data API, TikTok for Business, etc.)
"""

import asyncio
import uuid
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum

from ..core.runtime_mode import require_simulation
from ..schemas.media import (
    AspectRatio,
    CaptionStyle,
    Platform,
    ViralHook,
    GeneratedClip,
    DistributionResult,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Video Store
# ---------------------------------------------------------------------------


class VideoStatus(str, Enum):
    PENDING = "pending"
    AWAITING_UPLOAD = "awaiting_upload"
    UPLOADING = "uploading"
    PROCESSING = "processing"
    TRANSCRIBING = "transcribing"
    ANALYZING = "analyzing"
    READY = "ready"
    FAILED = "failed"


@dataclass
class StoredVideo:
    """Internal video representation."""

    video_id: str
    title: str
    source_url: str | None
    language: str
    status: VideoStatus
    metadata: dict
    created_at: datetime
    transcript: str | None = None
    duration_seconds: float | None = None
    hooks: list[ViralHook] = field(default_factory=list)
    # Wallet whose key ingested the video. None for a bootstrap-admin upload,
    # which only bootstrap admins can reach.
    owner_wallet_id: str | None = None


class VideoStore:
    """In-memory video store. Production: S3 + PostgreSQL."""

    def __init__(self):
        self._videos: dict[str, StoredVideo] = {}
        self._lock = asyncio.Lock()

    async def create(self, video: StoredVideo) -> StoredVideo:
        async with self._lock:
            self._videos[video.video_id] = video
        return video

    async def get(self, video_id: str) -> StoredVideo | None:
        return self._videos.get(video_id)

    async def update_status(self, video_id: str, status: VideoStatus):
        async with self._lock:
            if video_id in self._videos:
                self._videos[video_id].status = status


# ---------------------------------------------------------------------------
# Hook Detector
# ---------------------------------------------------------------------------


@dataclass
class HookSignal:
    """A raw signal that contributes to hook detection."""

    timestamp: float  # seconds into video
    signal_type: str  # speech_pattern, emotional_peak, visual_surprise, etc.
    strength: float  # 0.0 - 1.0
    context: str  # what was happening


class HookDetector:
    """
    Detects viral hooks in video content by analyzing:
    - Speech patterns (pace changes, emphasis, rhetorical questions)
    - Emotional peaks (sentiment analysis on transcript)
    - Visual surprises (scene change detection)
    - Audience retention proxies (based on content structure)

    In production, this orchestrates multiple ML models.
    Currently provides structured stubs for integration testing.
    """

    def __init__(self):
        self._min_hook_duration = 15.0  # seconds
        self._max_hook_duration = 60.0  # seconds
        self._min_confidence = 0.3

    async def detect_hooks(self, video: StoredVideo) -> list[ViralHook]:
        """
        Analyze a video and return ranked viral hooks.
        """
        require_simulation("media_engine")
        if not video.transcript:
            logger.warning(
                f"No transcript for {video.video_id}, using duration-based detection"
            )

        # Production: run ML pipeline
        # Stub: generate synthetic hooks for testing
        hooks = await self._generate_synthetic_hooks(video)

        # Sort by confidence
        hooks.sort(key=lambda h: h.confidence_score, reverse=True)

        # Store on video
        video.hooks = hooks
        return hooks

    async def _generate_synthetic_hooks(self, video: StoredVideo) -> list[ViralHook]:
        """Generate synthetic hooks for testing. Replace with ML pipeline."""
        duration = video.duration_seconds or 300.0
        hooks = []

        # Simulate 3-5 hooks per video
        import random

        num_hooks = random.randint(3, 5)

        trigger_types = [
            ("emotional_peak", "Speaker reaches emotional climax discussing key topic"),
            ("speech_pattern", "Rapid-fire delivery with rhetorical question"),
            ("visual_surprise", "Unexpected visual transition or reveal"),
            ("retention_signal", "Strong opening hook with pattern interrupt"),
        ]

        for i in range(num_hooks):
            start = random.uniform(0, max(0, duration - self._max_hook_duration))
            hook_dur = random.uniform(self._min_hook_duration, self._max_hook_duration)
            trigger = random.choice(trigger_types)

            hooks.append(
                ViralHook(
                    hook_id=str(uuid.uuid4()),
                    start_time_seconds=round(start, 1),
                    end_time_seconds=round(start + hook_dur, 1),
                    confidence_score=round(random.uniform(0.4, 0.95), 2),
                    trigger_type=trigger[0],
                    transcript_snippet=trigger[1],
                )
            )

        return hooks


# ---------------------------------------------------------------------------
# Clip Renderer
# ---------------------------------------------------------------------------


class ClipRenderer:
    """
    Renders platform-ready clips from viral hooks.
    Handles reframing (aspect ratio) and animated caption generation.

    Production stack:
    - FFmpeg for video processing
    - Subject detection for intelligent reframing
    - Custom caption renderer with animation styles
    """

    async def render_clip(
        self,
        video: StoredVideo,
        hook: ViralHook,
        aspect_ratio: AspectRatio,
        caption_style: CaptionStyle,
    ) -> GeneratedClip:
        """Render a single clip from a hook."""
        clip_id = str(uuid.uuid4())

        # Production: FFmpeg pipeline
        # 1. Extract segment from start_time to end_time
        # 2. Apply subject-aware reframing to target aspect ratio
        # 3. Generate animated captions from transcript
        # 4. Render final clip

        duration = hook.end_time_seconds - hook.start_time_seconds

        clip = GeneratedClip(
            clip_id=clip_id,
            video_id=video.video_id,
            hook_id=hook.hook_id,
            aspect_ratio=aspect_ratio,
            duration_seconds=round(duration, 1),
            download_url=f"/v1/media/clips/{clip_id}/download",
            caption_style=caption_style,
            thumbnail_url=f"/v1/media/clips/{clip_id}/thumbnail",
            generated_at=datetime.now(timezone.utc),
        )

        logger.info(
            f"Rendered clip {clip_id}: {duration:.1f}s, "
            f"{aspect_ratio.value}, {caption_style.value}"
        )
        return clip

    async def render_batch(
        self,
        video: StoredVideo,
        hooks: list[ViralHook],
        aspect_ratios: list[AspectRatio],
        caption_style: CaptionStyle,
        max_clips: int = 5,
    ) -> list[GeneratedClip]:
        """Render multiple clips concurrently."""
        tasks = []
        for hook in hooks[:max_clips]:
            for ratio in aspect_ratios:
                tasks.append(self.render_clip(video, hook, ratio, caption_style))

        clips = await asyncio.gather(*tasks)
        return list(clips)


# ---------------------------------------------------------------------------
# Platform Distributor
# ---------------------------------------------------------------------------


class PlatformDistributor:
    """
    Pushes clips to social platforms via their APIs.
    No app is opened. Ever.

    Production: wire up each platform's API client:
    - YouTube Data API v3 (Shorts upload)
    - TikTok for Business API
    - Instagram Graph API (Reels)
    - X API v2 (media upload + tweet)
    - LinkedIn Marketing API
    """

    async def distribute(
        self,
        clip: GeneratedClip,
        platform: Platform,
        title: str,
        hashtags: list[str],
        schedule_at: datetime | None = None,
    ) -> DistributionResult:
        """Distribute a single clip to a platform."""

        # Stub path only — real platform APIs are outside the frozen surface.
        require_simulation("media_engine")

        post_id = uuid.uuid4().hex[:12]

        result = DistributionResult(
            clip_id=clip.clip_id,
            platform=platform,
            status="simulated_scheduled" if schedule_at else "simulated",
            platform_post_id=post_id,
            platform_url=self._build_platform_url(platform, post_id),
            scheduled_at=schedule_at,
        )

        logger.info(
            f"Simulated distribute {clip.clip_id} to {platform.value}: "
            f"{result.platform_url}"
        )
        return result

    def _build_platform_url(self, platform: Platform, post_id: str) -> str:
        """Construct the expected platform URL."""
        base_urls = {
            Platform.YOUTUBE_SHORTS: f"https://youtube.com/shorts/{post_id}",
            Platform.TIKTOK: f"https://tiktok.com/@handle/video/{post_id}",
            Platform.INSTAGRAM_REELS: f"https://instagram.com/reel/{post_id}",
            Platform.X_VIDEO: f"https://x.com/handle/status/{post_id}",
            Platform.LINKEDIN_VIDEO: f"https://linkedin.com/feed/update/{post_id}",
        }
        return base_urls.get(platform, f"https://{platform.value}/{post_id}")


# ---------------------------------------------------------------------------
# Scheduling Engine
# ---------------------------------------------------------------------------


class SchedulingEngine:
    """
    Determines optimal posting times based on historical engagement data.
    In production, this analyzes per-platform engagement curves.
    """

    # Default optimal windows (UTC) — production would learn these per-account
    _default_windows: dict[Platform, list[int]] = {
        Platform.YOUTUBE_SHORTS: [14, 17, 20],  # 2PM, 5PM, 8PM UTC
        Platform.TIKTOK: [11, 15, 19, 22],  # High engagement windows
        Platform.INSTAGRAM_REELS: [12, 17, 21],
        Platform.X_VIDEO: [13, 17, 20],
        Platform.LINKEDIN_VIDEO: [8, 12, 17],  # Business hours
    }

    async def get_optimal_time(
        self,
        platform: Platform,
        after: datetime | None = None,
    ) -> datetime:
        """
        Calculate the next optimal posting time for a platform.
        """
        now = after or datetime.now(timezone.utc)
        windows = self._default_windows.get(platform, [12, 18])

        # Find the next optimal hour
        for hour in sorted(windows):
            candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            if candidate > now:
                return candidate

        # All windows passed today — use first window tomorrow
        tomorrow = now + timedelta(days=1)
        return tomorrow.replace(hour=windows[0], minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Media Engine Orchestrator
# ---------------------------------------------------------------------------


class MediaEngine:
    """
    Top-level orchestrator for the Programmatic Media Engine.
    Coordinates the full pipeline: ingest → analyze → render → distribute.
    """

    def __init__(self):
        self.video_store = VideoStore()
        self.hook_detector = HookDetector()
        self.clip_renderer = ClipRenderer()
        self.distributor = PlatformDistributor()
        self.scheduler = SchedulingEngine()
        self._clip_store: dict[str, GeneratedClip] = {}
        # clip_id -> owning wallet, inherited from the source video.
        self._clip_owners: dict[str, str | None] = {}

    async def ingest_video(
        self,
        title: str,
        source_url: str | None = None,
        language: str = "en",
        metadata: dict | None = None,
        owner_wallet_id: str | None = None,
    ) -> StoredVideo:
        """Start the video ingestion pipeline."""
        video = StoredVideo(
            video_id=str(uuid.uuid4()),
            title=title,
            source_url=source_url,
            language=language,
            status=(
                VideoStatus.PROCESSING if source_url else VideoStatus.AWAITING_UPLOAD
            ),
            metadata=metadata or {},
            created_at=datetime.now(timezone.utc),
            owner_wallet_id=owner_wallet_id,
        )
        await self.video_store.create(video)

        if source_url:
            # Production: kick off async download + processing
            asyncio.create_task(self._process_video(video.video_id))

        return video

    async def _process_video(self, video_id: str):
        """Background video processing pipeline."""
        video = await self.video_store.get(video_id)
        if not video:
            return

        try:
            # Step 1: Download (if URL)
            await self.video_store.update_status(video_id, VideoStatus.PROCESSING)

            # Step 2: Transcribe
            await self.video_store.update_status(video_id, VideoStatus.TRANSCRIBING)
            # Production: call Whisper/Deepgram API
            video.transcript = "Placeholder transcript for processing pipeline."
            video.duration_seconds = 300.0  # Placeholder

            # Step 3: Detect hooks
            await self.video_store.update_status(video_id, VideoStatus.ANALYZING)
            await self.hook_detector.detect_hooks(video)

            # Done
            await self.video_store.update_status(video_id, VideoStatus.READY)
            logger.info(
                f"Video {video_id} processed: {len(video.hooks)} hooks detected"
            )

        except Exception as e:
            await self.video_store.update_status(video_id, VideoStatus.FAILED)
            logger.error(f"Video processing failed for {video_id}: {e}")

    async def generate_clips(
        self,
        video_id: str,
        hook_ids: list[str] | None = None,
        max_clips: int = 5,
        aspect_ratios: list[AspectRatio] | None = None,
        caption_style: CaptionStyle = CaptionStyle.WORD_BY_WORD,
    ) -> list[GeneratedClip]:
        """Generate platform-ready clips from a video's hooks."""
        video = await self.video_store.get(video_id)
        if not video:
            raise ValueError(f"Video '{video_id}' not found")

        # Select hooks
        if hook_ids:
            hooks = [h for h in video.hooks if h.hook_id in hook_ids]
        else:
            hooks = sorted(video.hooks, key=lambda h: h.confidence_score, reverse=True)

        clips = await self.clip_renderer.render_batch(
            video=video,
            hooks=hooks,
            aspect_ratios=aspect_ratios or [AspectRatio.PORTRAIT_9_16],
            caption_style=caption_style,
            max_clips=max_clips,
        )

        # Store clips
        for clip in clips:
            self._clip_store[clip.clip_id] = clip
            self._clip_owners[clip.clip_id] = video.owner_wallet_id

        return clips  # type: ignore[no-any-return]

    async def distribute_clips(
        self,
        clip_ids: list[str],
        platforms: list[Platform],
        title: str,
        hashtags: list[str] | None = None,
        schedule_at: datetime | None = None,
        optimize_schedule: bool = True,
        *,
        owner_allowed: Callable[[str | None], bool],
    ) -> list[DistributionResult]:
        """Distribute clips to social platforms.

        ``owner_allowed`` decides, from a clip's owning wallet, whether the
        caller may distribute it. A clip it rejects is reported exactly like
        an unknown clip id, so the response never confirms that another
        tenant's clip exists.
        """
        results = []

        for clip_id in clip_ids:
            clip = self._clip_store.get(clip_id)
            if not clip or not owner_allowed(self._clip_owners.get(clip_id)):
                for platform in platforms:
                    results.append(
                        DistributionResult(
                            clip_id=clip_id,
                            platform=platform,
                            status="failed",
                            error=f"Clip '{clip_id}' not found",
                        )
                    )
                continue

            for platform in platforms:
                post_time = schedule_at
                if optimize_schedule and not schedule_at:
                    post_time = await self.scheduler.get_optimal_time(platform)

                result = await self.distributor.distribute(
                    clip=clip,
                    platform=platform,
                    title=title,
                    hashtags=hashtags or [],
                    schedule_at=post_time,
                )
                results.append(result)

        return results

    async def get_clip(self, clip_id: str) -> GeneratedClip | None:
        return self._clip_store.get(clip_id)

    async def get_clip_owner(self, clip_id: str) -> str | None:
        return self._clip_owners.get(clip_id)
