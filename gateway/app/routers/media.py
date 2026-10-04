"""
PROOF SURFACE — frozen. Do not expand; see docs/PROOF_SURFACES.md.
Programmatic Media Engine Router
---------------------------------
Ingest long-form video -> detect viral hooks -> reframe for vertical ->
generate animated captions -> distribute to platforms via API.

Wired to MediaEngine service via FastAPI dependency injection.

Videos and clips are wallet-scoped: each records the wallet whose key created
it, and every handler answers a foreign resource exactly like a missing one.
"""

from functools import partial

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..core.auth import AuthContext, get_auth_context
from ..core.dependencies import get_media_engine
from ..services.media_engine import MediaEngine, StoredVideo
from ..schemas.media import (
    VideoUploadRequest,
    VideoUploadResponse,
    ViralHook,
    ClipGenerationRequest,
    ClipGenerationResponse,
    GeneratedClip,
    DistributionRequest,
    DistributionResponse,
)

router = APIRouter(
    prefix="/v1/media",
    tags=["Programmatic Media Engine"],
    responses={
        401: {"description": "Missing API key"},
        403: {"description": "Invalid API key"},
    },
)


def _owner_allowed(auth: AuthContext, owner_wallet_id: str | None) -> bool:
    """Whether the caller may touch a resource owned by ``owner_wallet_id``.

    Same rule as ``AuthContext.require_wallet_access`` (bootstrap admins, or
    the exact owning wallet), returned as a bool so handlers can answer with
    their existing 404 instead of a 403 that would confirm the id exists and
    echo the owner's wallet id.
    """
    try:
        auth.require_wallet_access(owner_wallet_id)
    except HTTPException:
        return False
    return True


async def _load_owned_video(
    video_id: str,
    engine: MediaEngine,
    auth: AuthContext,
) -> StoredVideo | None:
    """Fetch a video the caller owns; None when it is missing or foreign."""
    video = await engine.video_store.get(video_id)
    if video is None or not _owner_allowed(auth, video.owner_wallet_id):
        return None
    return video


@router.post(
    "/videos",
    response_model=VideoUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a video for processing",
    description=(
        "Submit a video for the media engine pipeline. Provide either a "
        "source_url for the engine to fetch, or use the returned upload_url "
        "to push the file directly. Processing begins automatically after upload."
    ),
)
async def upload_video(
    request: VideoUploadRequest,
    auth: AuthContext = Depends(get_auth_context),
    engine: MediaEngine = Depends(get_media_engine),
):
    video = await engine.ingest_video(
        title=request.title,
        source_url=request.source_url,
        language=request.language,
        metadata=request.metadata,
        owner_wallet_id=auth.wallet_id,
    )

    return VideoUploadResponse(
        video_id=video.video_id,
        upload_url=(
            None if request.source_url else f"/v1/media/videos/{video.video_id}/upload"
        ),
        status=video.status.value,
        estimated_processing_seconds=120 if request.source_url else None,
    )


@router.get(
    "/videos/{video_id}",
    summary="Get video processing status",
    description="Check the current status of a video in the processing pipeline.",
)
async def get_video_status(
    video_id: str,
    auth: AuthContext = Depends(get_auth_context),
    engine: MediaEngine = Depends(get_media_engine),
):
    video = await _load_owned_video(video_id, engine, auth)
    if not video:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "video_not_found",
                "message": f"Video '{video_id}' not found.",
            },
        )
    return {
        "video_id": video.video_id,
        "title": video.title,
        "source_url": video.source_url,
        "language": video.language,
        "status": video.status.value,
        "created_at": video.created_at.isoformat(),
        "duration_seconds": video.duration_seconds,
        "hook_count": len(video.hooks),
    }


@router.get(
    "/videos/{video_id}/hooks",
    response_model=list[ViralHook],
    summary="Get detected viral hooks",
    description=(
        "Retrieve the viral hooks detected in a processed video. "
        "Hooks are ranked by confidence_score. Each hook identifies "
        "a high-engagement moment based on speech patterns, emotional "
        "peaks, visual surprises, or audience retention signals."
    ),
)
async def get_viral_hooks(
    video_id: str,
    min_confidence: float = Query(
        0.0, ge=0.0, le=1.0, description="Minimum confidence threshold"
    ),
    auth: AuthContext = Depends(get_auth_context),
    engine: MediaEngine = Depends(get_media_engine),
):
    video = await _load_owned_video(video_id, engine, auth)
    if not video:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "video_not_found"},
        )

    hooks = video.hooks
    if min_confidence > 0:
        hooks = [h for h in hooks if h.confidence_score >= min_confidence]
    return sorted(hooks, key=lambda h: h.confidence_score, reverse=True)


@router.post(
    "/videos/{video_id}/clips",
    response_model=ClipGenerationResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Generate platform-ready clips",
    description=(
        "Generate reframed, captioned clips from detected viral hooks. "
        "Clips are automatically reframed to the target aspect ratio(s) "
        "and overlaid with animated captions in the chosen style. "
        "80%% of social video is consumed muted — captions are not optional."
    ),
)
async def generate_clips(
    video_id: str,
    request: ClipGenerationRequest,
    auth: AuthContext = Depends(get_auth_context),
    engine: MediaEngine = Depends(get_media_engine),
):
    if await _load_owned_video(video_id, engine, auth) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "video_not_found",
                "message": f"Video '{video_id}' not found",
            },
        )
    try:
        clips = await engine.generate_clips(
            video_id=video_id,
            hook_ids=request.hooks,
            max_clips=request.max_clips,
            aspect_ratios=request.aspect_ratios,
            caption_style=request.caption_style,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "video_not_found", "message": str(e)},
        )

    return ClipGenerationResponse(
        video_id=video_id,
        clips=clips,
        total_generated=len(clips),
        status="completed",
    )


@router.post(
    "/distribute",
    response_model=DistributionResponse,
    summary="Distribute clips to social platforms",
    description=(
        "Simulated distribution of your generated clips: nothing is posted to "
        "any platform. Each result carries status 'simulated' (or "
        "'simulated_scheduled') and a placeholder post URL for YouTube Shorts, "
        "TikTok, Instagram Reels, X Video, or LinkedIn Video. Clip ids you do "
        "not own are reported as not found. Set optimize_schedule=true to pick "
        "a posting window from default per-platform engagement windows."
    ),
)
async def distribute_clips(
    request: DistributionRequest,
    auth: AuthContext = Depends(get_auth_context),
    engine: MediaEngine = Depends(get_media_engine),
):
    results = await engine.distribute_clips(
        clip_ids=request.clip_ids,
        platforms=request.platforms,
        title=request.title,
        hashtags=request.hashtags,
        schedule_at=request.schedule_at,
        optimize_schedule=request.optimize_schedule,
        owner_allowed=partial(_owner_allowed, auth),
    )

    # The distributor is simulation-only, so its simulated statuses count as
    # distributed; counting only real publishes always reported zero.
    distributed = {"published", "scheduled", "simulated", "simulated_scheduled"}
    published = sum(1 for r in results if r.status in distributed)
    failed = sum(1 for r in results if r.status == "failed")

    return DistributionResponse(
        results=results,
        total_distributed=published,
        total_failed=failed,
    )


@router.get(
    "/clips/{clip_id}",
    response_model=GeneratedClip,
    summary="Get clip details",
    description="Retrieve metadata and download URL for a generated clip.",
)
async def get_clip(
    clip_id: str,
    auth: AuthContext = Depends(get_auth_context),
    engine: MediaEngine = Depends(get_media_engine),
):
    clip = await engine.get_clip(clip_id)
    if not clip or not _owner_allowed(auth, await engine.get_clip_owner(clip_id)):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "clip_not_found"},
        )
    return clip
