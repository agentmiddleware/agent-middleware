"""
Service-level tests for app.services.media_engine.

test_media.py already covers the HTTP surface. These tests exercise the
service layer in isolation: the video store, hook detection, clip
rendering, distribution stubs, scheduling, and the MediaEngine
orchestrator, including edge and error cases. No network access.
"""

import random
from datetime import datetime, timezone

import pytest

from app.core.config import get_settings
from app.schemas.media import (
    AspectRatio,
    CaptionStyle,
    Platform,
    ViralHook,
)
from app.services.media_engine import (
    ClipRenderer,
    HookDetector,
    MediaEngine,
    PlatformDistributor,
    SchedulingEngine,
    StoredVideo,
    VideoStatus,
    VideoStore,
)


def _video(**kwargs) -> StoredVideo:
    defaults = {
        "video_id": "vid-1",
        "title": "Test video",
        "source_url": None,
        "language": "en",
        "status": VideoStatus.READY,
        "metadata": {},
        "created_at": datetime.now(timezone.utc),
        "transcript": "Hello world, this is a test transcript.",
        "duration_seconds": 300.0,
    }
    defaults.update(kwargs)
    return StoredVideo(**defaults)


def _hook(**kwargs) -> ViralHook:
    defaults = {
        "hook_id": "hook-1",
        "start_time_seconds": 10.0,
        "end_time_seconds": 40.0,
        "confidence_score": 0.8,
        "trigger_type": "emotional_peak",
        "transcript_snippet": "snippet",
    }
    defaults.update(kwargs)
    return ViralHook(**defaults)


# --- VideoStore ---


async def test_video_store_round_trip():
    store = VideoStore()
    assert await store.get("missing") is None

    video = _video()
    await store.create(video)
    assert await store.get("vid-1") is video

    await store.update_status("vid-1", VideoStatus.PROCESSING)
    assert (await store.get("vid-1")).status == VideoStatus.PROCESSING

    # Updating an unknown id is a silent no-op, not an error.
    await store.update_status("nope", VideoStatus.READY)


# --- HookDetector ---


async def test_detect_hooks_ranked_and_bounded():
    random.seed(7)
    detector = HookDetector()
    video = _video(duration_seconds=300.0)
    hooks = await detector.detect_hooks(video)

    assert 3 <= len(hooks) <= 5
    scores = [h.confidence_score for h in hooks]
    assert scores == sorted(scores, reverse=True)
    for hook in hooks:
        assert 0.0 <= hook.confidence_score <= 1.0
        assert 0.0 <= hook.start_time_seconds <= hook.end_time_seconds
        assert hook.end_time_seconds <= 300.0
        duration = hook.end_time_seconds - hook.start_time_seconds
        assert 15.0 <= duration <= 60.0
    # Detected hooks are stored back on the video.
    assert video.hooks == hooks


async def test_detect_hooks_without_transcript_still_returns_hooks(caplog):
    random.seed(11)
    detector = HookDetector()
    video = _video(transcript=None)
    hooks = await detector.detect_hooks(video)
    assert len(hooks) >= 3


async def test_detect_hooks_refuses_non_simulation_mode(monkeypatch):
    # get_settings() is lru-cached, so setenv has no effect after startup;
    # toggle the cached object like test_iot.py does.
    monkeypatch.setattr(get_settings(), "SIMULATION_MODE_MEDIA_ENGINE", False)
    detector = HookDetector()
    with pytest.raises(NotImplementedError):
        await detector.detect_hooks(_video())


# --- ClipRenderer ---


async def test_render_clip_derives_duration_and_urls():
    renderer = ClipRenderer()
    video = _video()
    hook = _hook(start_time_seconds=10.0, end_time_seconds=42.5)
    clip = await renderer.render_clip(
        video, hook, AspectRatio.PORTRAIT_9_16, CaptionStyle.KARAOKE
    )
    assert clip.video_id == "vid-1"
    assert clip.hook_id == "hook-1"
    assert clip.duration_seconds == pytest.approx(32.5)
    assert clip.aspect_ratio == AspectRatio.PORTRAIT_9_16
    assert clip.caption_style == CaptionStyle.KARAOKE
    assert clip.download_url == f"/v1/media/clips/{clip.clip_id}/download"
    assert clip.thumbnail_url == f"/v1/media/clips/{clip.clip_id}/thumbnail"


async def test_render_batch_limits_hooks_and_fans_out_ratios():
    renderer = ClipRenderer()
    video = _video()
    hooks = [_hook(hook_id=f"hook-{i}") for i in range(4)]
    clips = await renderer.render_batch(
        video,
        hooks,
        [AspectRatio.PORTRAIT_9_16, AspectRatio.SQUARE_1_1],
        CaptionStyle.WORD_BY_WORD,
        max_clips=2,
    )
    # Only the first 2 hooks are used, each rendered in both ratios.
    assert len(clips) == 4
    assert {c.hook_id for c in clips} == {"hook-0", "hook-1"}
    assert {c.aspect_ratio for c in clips} == {
        AspectRatio.PORTRAIT_9_16,
        AspectRatio.SQUARE_1_1,
    }


async def test_render_batch_empty_hooks_returns_empty():
    renderer = ClipRenderer()
    clips = await renderer.render_batch(
        _video(), [], [AspectRatio.PORTRAIT_9_16], CaptionStyle.WORD_BY_WORD
    )
    assert clips == []


# --- PlatformDistributor ---


async def test_distribute_marks_simulated_or_scheduled():
    distributor = PlatformDistributor()
    video = _video()
    renderer = ClipRenderer()
    clip = await renderer.render_clip(
        video, _hook(), AspectRatio.PORTRAIT_9_16, CaptionStyle.WORD_BY_WORD
    )

    immediate = await distributor.distribute(
        clip, Platform.TIKTOK, "title", ["#a"], schedule_at=None
    )
    assert immediate.status == "simulated"
    assert immediate.platform_post_id is not None
    assert "tiktok.com" in immediate.platform_url
    assert immediate.scheduled_at is None

    when = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    scheduled = await distributor.distribute(
        clip, Platform.YOUTUBE_SHORTS, "title", [], schedule_at=when
    )
    assert scheduled.status == "simulated_scheduled"
    assert scheduled.scheduled_at == when
    assert "youtube.com/shorts/" in scheduled.platform_url


async def test_platform_urls_cover_every_platform():
    distributor = PlatformDistributor()
    for platform in Platform:
        url = distributor._build_platform_url(platform, "abc123")
        assert "abc123" in url


async def test_distribute_refuses_non_simulation_mode(monkeypatch):
    monkeypatch.setattr(get_settings(), "SIMULATION_MODE_MEDIA_ENGINE", False)
    distributor = PlatformDistributor()
    renderer = ClipRenderer()
    clip = await renderer.render_clip(
        _video(), _hook(), AspectRatio.PORTRAIT_9_16, CaptionStyle.WORD_BY_WORD
    )
    with pytest.raises(NotImplementedError):
        await distributor.distribute(clip, Platform.TIKTOK, "t", [])


# --- SchedulingEngine ---


async def test_optimal_time_next_window_today():
    scheduler = SchedulingEngine()
    after = datetime(2026, 5, 1, 10, 30, tzinfo=timezone.utc)
    result = await scheduler.get_optimal_time(Platform.TIKTOK, after=after)
    assert result == datetime(2026, 5, 1, 11, 0, tzinfo=timezone.utc)


async def test_optimal_time_rolls_to_tomorrow_after_last_window():
    scheduler = SchedulingEngine()
    after = datetime(2026, 5, 1, 23, 30, tzinfo=timezone.utc)
    result = await scheduler.get_optimal_time(Platform.TIKTOK, after=after)
    assert result == datetime(2026, 5, 2, 11, 0, tzinfo=timezone.utc)


# --- MediaEngine orchestrator ---


async def test_ingest_without_url_awaits_upload():
    engine = MediaEngine()
    video = await engine.ingest_video("Direct upload")
    assert video.status == VideoStatus.AWAITING_UPLOAD
    assert video.owner_wallet_id is None
    assert await engine.video_store.get(video.video_id) is video


async def test_ingest_with_url_processes_in_background():
    import asyncio

    engine = MediaEngine()
    video = await engine.ingest_video("URL video", source_url="https://x/y.mp4")
    assert video.status == VideoStatus.PROCESSING
    for _ in range(200):
        if video.status in (VideoStatus.READY, VideoStatus.FAILED):
            break
        await asyncio.sleep(0.01)
    assert video.status == VideoStatus.READY
    assert video.transcript
    assert len(video.hooks) >= 3


async def test_generate_clips_unknown_video_raises():
    engine = MediaEngine()
    with pytest.raises(ValueError, match="not found"):
        await engine.generate_clips("missing-video")


async def test_generate_clips_filters_hooks_and_records_owner():
    engine = MediaEngine()
    video = await engine.ingest_video("Owned", owner_wallet_id="wallet-1")
    video.hooks = [_hook(hook_id="h1"), _hook(hook_id="h2")]
    clips = await engine.generate_clips(video.video_id, hook_ids=["h2"])
    assert len(clips) == 1
    assert clips[0].hook_id == "h2"
    assert await engine.get_clip(clips[0].clip_id) is clips[0]
    assert await engine.get_clip_owner(clips[0].clip_id) == "wallet-1"
    assert await engine.get_clip("unknown") is None


async def test_distribute_unknown_clip_reports_failed_per_platform():
    engine = MediaEngine()
    results = await engine.distribute_clips(
        ["ghost-clip"],
        [Platform.TIKTOK, Platform.X_VIDEO],
        "title",
        owner_allowed=lambda owner: True,
    )
    assert len(results) == 2
    for result in results:
        assert result.status == "failed"
        assert "ghost-clip" in (result.error or "")


async def test_distribute_denied_owner_matches_unknown_clip_response():
    engine = MediaEngine()
    video = await engine.ingest_video("Owned", owner_wallet_id="wallet-1")
    video.hooks = [_hook(hook_id="h1")]
    (clip,) = await engine.generate_clips(video.video_id)

    denied = await engine.distribute_clips(
        [clip.clip_id],
        [Platform.TIKTOK],
        "title",
        optimize_schedule=False,
        owner_allowed=lambda owner: False,
    )
    unknown = await engine.distribute_clips(
        ["no-such-clip"],
        [Platform.TIKTOK],
        "title",
        optimize_schedule=False,
        owner_allowed=lambda owner: True,
    )
    # The denied response never confirms the clip exists: same template as
    # the unknown-id response, echoing only the caller-supplied id.
    assert denied[0].status == unknown[0].status == "failed"
    assert denied[0].error == f"Clip '{clip.clip_id}' not found"
    assert unknown[0].error == "Clip 'no-such-clip' not found"


async def test_distribute_allowed_owner_succeeds():
    engine = MediaEngine()
    video = await engine.ingest_video("Owned", owner_wallet_id="wallet-1")
    video.hooks = [_hook(hook_id="h1")]
    (clip,) = await engine.generate_clips(video.video_id)
    results = await engine.distribute_clips(
        [clip.clip_id],
        [Platform.INSTAGRAM_REELS],
        "title",
        hashtags=["#fun"],
        optimize_schedule=False,
        owner_allowed=lambda owner: owner == "wallet-1",
    )
    assert results[0].status == "simulated"
    assert "instagram.com/reel/" in results[0].platform_url
