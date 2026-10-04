"""
PROOF SURFACE — frozen. Do not expand; see docs/PROOF_SURFACES.md.
Programmatic Content Factory Router
-------------------------------------
Turn one source asset into dozens of format-adapted content pieces.
Then let the algorithmic scheduler pick optimal posting windows.

Week 5: Distribution at superhuman speed.
Week 6+: Live Campaign Mode — hook-based 1-to-20 multiplication
with 9:16 vertical rendering and animated captions.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from ..core.auth import AuthContext, get_auth_context, verify_api_key
from ..core.dependencies import get_content_factory
from ..services.content_factory import ContentFactory, ContentPipeline, LiveCampaign
from ..schemas.content_factory import (
    ContentPipelineRequest,
    ContentPipelineResponse,
    GeneratedContent,
    ContentListResponse,
    AnalyticsIngestRequest,
    AnalyticsIngestResponse,
    ScheduleRequest,
    ScheduleResponse,
    LiveCampaignRequest,
    LiveCampaignResponse,
)

router = APIRouter(
    prefix="/v1/factory",
    tags=["Content Factory & Scheduling"],
    responses={
        401: {"description": "Missing API key"},
        403: {"description": "Invalid API key or access denied"},
    },
)


# --- Ownership ---
#
# Pipelines and campaigns record their owner in ``owner_key``. That column used
# to hold the caller's raw API key -- a live credential persisted in plaintext
# -- and no read ever compared it, so any authenticated caller could read every
# tenant's pipelines, content pieces, and campaigns, and list all campaigns.
# It now holds the owning wallet id: non-secret, shared by every key of that
# wallet, and the identity ``AuthContext.require_wallet_access`` enforces.
# Records written by a bootstrap admin (no wallet) are ownerless ("") and so
# visible only to bootstrap admins, as are legacy rows still holding a raw key.


def _owner_wallet_id(auth: AuthContext) -> str:
    """Return the non-secret owner identity to record for the caller."""
    return auth.wallet_id or ""


def _caller_owns(owner_wallet_id: str, auth: AuthContext) -> bool:
    try:
        auth.require_wallet_access(owner_wallet_id or None)
    except HTTPException:
        return False
    return True


def _not_found(error: str) -> HTTPException:
    # A record the caller may not see answers exactly like a missing one: no
    # existence oracle, and the owner's wallet id is never echoed back.
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": error},
    )


async def _load_owned_pipeline(
    pipeline_id: str,
    factory: ContentFactory,
    auth: AuthContext,
    *,
    error: str = "pipeline_not_found",
) -> ContentPipeline:
    pipeline = await factory.store.get_pipeline(pipeline_id)
    if not pipeline or not _caller_owns(pipeline.owner_key, auth):
        raise _not_found(error)
    return pipeline


async def _load_owned_campaign(
    campaign_id: str,
    factory: ContentFactory,
    auth: AuthContext,
) -> LiveCampaign:
    campaign = await factory.store.get_campaign(campaign_id)
    if not campaign or not _caller_owns(campaign.owner_key, auth):
        raise _not_found("campaign_not_found")
    return campaign


# --- Content Pipeline ---


@router.post(
    "/pipelines",
    response_model=ContentPipelineResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Create a content generation pipeline",
    description=(
        "Submit a source asset and target formats. The factory will "
        "generate multiple content pieces adapted to each format: "
        "short videos, static images, text posts, audiograms, carousels, "
        "blog excerpts, and email snippets. Processing is async."
    ),
)
async def create_pipeline(
    request: ContentPipelineRequest,
    auth: AuthContext = Depends(get_auth_context),
    factory: ContentFactory = Depends(get_content_factory),
):
    pipeline = await factory.create_pipeline(
        title=request.title,
        target_formats=request.target_formats,
        source_clip_id=request.source_clip_id,
        source_url=request.source_url,
        brand_config=request.brand_config,
        language=request.language,
        auto_schedule=request.auto_schedule,
        owner_key=_owner_wallet_id(auth),
    )

    return ContentPipelineResponse(
        pipeline_id=pipeline.pipeline_id,
        title=pipeline.title,
        source_type=(
            "clip"
            if pipeline.source_clip_id
            else "url"
            if pipeline.source_url
            else "none"
        ),
        target_formats=pipeline.target_formats,
        status=pipeline.status,
        estimated_pieces=factory.estimate_pieces(pipeline.target_formats),
    )


@router.get(
    "/pipelines/{pipeline_id}",
    summary="Get pipeline status",
    description="Check the status of a content generation pipeline.",
)
async def get_pipeline(
    pipeline_id: str,
    auth: AuthContext = Depends(get_auth_context),
    factory: ContentFactory = Depends(get_content_factory),
):
    pipeline = await _load_owned_pipeline(pipeline_id, factory, auth)
    return {
        "pipeline_id": pipeline.pipeline_id,
        "title": pipeline.title,
        "status": pipeline.status,
        "target_formats": [f.value for f in pipeline.target_formats],
        "pieces_generated": len(pipeline.content_pieces),
        "created_at": pipeline.created_at.isoformat(),
    }


@router.get(
    "/pipelines/{pipeline_id}/content",
    response_model=ContentListResponse,
    summary="List generated content pieces",
    description="Retrieve all content pieces generated by a pipeline.",
)
async def list_pipeline_content(
    pipeline_id: str,
    auth: AuthContext = Depends(get_auth_context),
    factory: ContentFactory = Depends(get_content_factory),
):
    await _load_owned_pipeline(pipeline_id, factory, auth)

    content = await factory.list_pipeline_content(pipeline_id)
    return ContentListResponse(
        content=content,
        total=len(content),
        pipeline_id=pipeline_id,
    )


@router.get(
    "/content/{content_id}",
    response_model=GeneratedContent,
    summary="Get content piece details",
    description="Retrieve metadata and download URL for a specific content piece.",
)
async def get_content(
    content_id: str,
    auth: AuthContext = Depends(get_auth_context),
    factory: ContentFactory = Depends(get_content_factory),
):
    content = await factory.get_content(content_id)
    if not content:
        raise _not_found("content_not_found")
    # A piece carries no owner of its own; it belongs to its pipeline's owner.
    await _load_owned_pipeline(
        content.pipeline_id, factory, auth, error="content_not_found"
    )
    return content


# --- Live Campaign Mode ---


@router.post(
    "/campaigns",
    response_model=LiveCampaignResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Launch a live content campaign",
    description=(
        "The 'Big Red Button' — submit a source video URL and targeted hooks. "
        "Each hook is a specific segment (e.g., 30-45s reaction clip, 60s "
        "explainer). The factory applies the 1-to-N multiplication rule: "
        "each hook produces multiple format-adapted pieces (short videos, "
        "quote cards, carousels, text posts) all rendered in 9:16 vertical "
        "with animated captions. Then the AlgorithmicScheduler staggers posts "
        "across platforms (TikTok, YouTube Shorts, Instagram Reels) to maximize "
        "view velocity."
    ),
)
async def launch_campaign(
    request: LiveCampaignRequest,
    auth: AuthContext = Depends(get_auth_context),
    factory: ContentFactory = Depends(get_content_factory),
):
    result = await factory.launch_campaign(
        campaign_title=request.campaign_title,
        source_url=request.source_url,
        hooks=request.hooks,
        brand_config=request.brand_config,
        caption_style=request.caption_style,
        aspect_ratio=request.aspect_ratio,
        platforms=request.platforms,
        max_posts_per_day=request.max_posts_per_day,
        language=request.language,
        auto_schedule=request.auto_schedule,
        owner_key=_owner_wallet_id(auth),
    )
    return result


@router.get(
    "/campaigns/{campaign_id}",
    summary="Get campaign status",
    description="Check the status and results of a live content campaign.",
)
async def get_campaign(
    campaign_id: str,
    auth: AuthContext = Depends(get_auth_context),
    factory: ContentFactory = Depends(get_content_factory),
):
    campaign = await _load_owned_campaign(campaign_id, factory, auth)
    return {
        "campaign_id": campaign.campaign_id,
        "campaign_title": campaign.campaign_title,
        "source_url": campaign.source_url,
        "status": campaign.status,
        "hooks_count": len(campaign.hooks),
        "pipeline_ids": campaign.pipeline_ids,
        "created_at": campaign.created_at.isoformat(),
    }


@router.get(
    "/campaigns",
    summary="List all campaigns",
    description="Retrieve all live content campaigns.",
)
async def list_campaigns(
    auth: AuthContext = Depends(get_auth_context),
    factory: ContentFactory = Depends(get_content_factory),
):
    # A wallet-scoped caller only ever lists its own wallet's campaigns;
    # bootstrap admins keep the unfiltered view. A non-admin without a wallet
    # owns nothing (and must not match the ownerless "" admin records).
    if auth.is_bootstrap_admin:
        campaigns = await factory.store.list_campaigns()
    elif auth.wallet_id:
        campaigns = await factory.store.list_campaigns(owner_key=auth.wallet_id)
    else:
        campaigns = []
    return {
        "campaigns": [
            {
                "campaign_id": c.campaign_id,
                "campaign_title": c.campaign_title,
                "status": c.status,
                "hooks_count": len(c.hooks),
                "created_at": c.created_at.isoformat(),
            }
            for c in campaigns
        ],
        "total": len(campaigns),
    }


# --- Algorithmic Scheduling ---


@router.post(
    "/analytics",
    response_model=AnalyticsIngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest engagement analytics",
    description=(
        "Feed engagement data from platform APIs into the scheduling engine. "
        "The scheduler uses this data to learn optimal posting windows. "
        "More data = smarter scheduling = higher view velocity."
    ),
)
async def ingest_analytics(
    request: AnalyticsIngestRequest,
    api_key: str = Depends(verify_api_key),
    factory: ContentFactory = Depends(get_content_factory),
):
    summary = await factory.scheduler.ingest_analytics(request.metrics)
    return AnalyticsIngestResponse(
        ingested=len(request.metrics),
        platform_summary=summary,
    )


@router.get(
    "/analytics/summary",
    summary="Get analytics summary",
    description="View summary of ingested engagement data.",
)
async def get_analytics_summary(
    api_key: str = Depends(verify_api_key),
    factory: ContentFactory = Depends(get_content_factory),
):
    return await factory.scheduler.get_analytics_summary()


@router.post(
    "/schedule",
    response_model=ScheduleResponse,
    summary="Get optimal posting schedule",
    description=(
        "Generate an algorithmically-optimized posting schedule for content pieces. "
        "The scheduler analyzes historical engagement data per platform to pick "
        "the highest-engagement time slots. Posts are spread across days to avoid "
        "audience fatigue (configurable via max_posts_per_day)."
    ),
)
async def get_schedule(
    request: ScheduleRequest,
    api_key: str = Depends(verify_api_key),
    factory: ContentFactory = Depends(get_content_factory),
):
    recommendations = await factory.scheduler.recommend(
        content_ids=request.content_ids,
        platforms=request.platforms,
        earliest=request.earliest,
        latest=request.latest,
        max_per_day=request.max_posts_per_day,
    )

    # Build date range string
    if recommendations:
        start = recommendations[0].recommended_time.strftime("%Y-%m-%d")
        end = recommendations[-1].recommended_time.strftime("%Y-%m-%d")
        date_range = f"{start} to {end}"
    else:
        date_range = "no recommendations"

    return ScheduleResponse(
        recommendations=recommendations,
        total_scheduled=len(recommendations),
        date_range=date_range,
    )
