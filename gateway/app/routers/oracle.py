"""
PROOF SURFACE — frozen. Do not expand; see docs/PROOF_SURFACES.md.
Agent Oracle Infiltration Router
----------------------------------
Crawl agent directories, index external API capabilities,
register our API for inbound traffic, and monitor visibility.

This is SEO for the agentic web. If agents can't find you, you don't exist.
"""

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status

import hashlib
import json

from ..audit.lightweight import record_audit
from ..core.auth import AuthContext, get_auth_context, verify_api_key
from ..core.dependencies import get_agent_oracle
from ..services.oracle import AgentOracle
from ..schemas.oracle import (
    CrawlTargetRequest,
    IndexedAPI,
    IndexedAPIListResponse,
    OracleCrawlTargetRecord,
    CompatibilityTier,
    DirectoryType,
    RegistrationRequest,
    RegistrationResponse,
    OracleStatus,
    VisibilityScore,
    NetworkGraphResponse,
)

router = APIRouter(
    prefix="/v1/oracle",
    tags=["Agent Oracle & Network Infiltration"],
    responses={
        401: {"description": "Missing API key"},
        403: {"description": "Invalid API key or access denied"},
    },
)

# Each batch URL is crawled concurrently and writes a durable crawl row, so an
# unbounded body is an unbounded fan-out of writes to shared state.
MAX_BATCH_CRAWL_URLS = 25


# --- Crawling ---


@router.post(
    "/crawl",
    response_model=IndexedAPI,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Crawl and index an external API",
    description=(
        "Submit a URL for the Oracle to crawl. It will attempt to discover "
        "the API's capabilities via /.well-known/agent.json, /llm.txt, or "
        "OpenAPI specs, then compute a compatibility score indicating how "
        "well it fits this control plane."
    ),
)
async def crawl_target(
    request: CrawlTargetRequest,
    auth: AuthContext = Depends(get_auth_context),
    oracle: AgentOracle = Depends(get_agent_oracle),
):
    audit_ctx = {
        "source": auth.source,
        "key_id": auth.key_id,
        "wallet_id": auth.wallet_id,
    }
    result = await oracle.crawl(
        url=request.url,
        directory_type=request.directory_type,
        tags=request.tags,
        priority=request.priority,
        audit_context=audit_ctx,
    )
    if not result:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "crawl_failed",
                "message": f"Failed to crawl {request.url}",
            },
        )
    return result


@router.post(
    "/crawl/batch",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Batch crawl multiple URLs",
    description="Submit multiple URLs for concurrent crawling and indexing.",
)
async def batch_crawl(
    urls: list[str] = Body(..., max_length=MAX_BATCH_CRAWL_URLS),
    auth: AuthContext = Depends(get_auth_context),
    oracle: AgentOracle = Depends(get_agent_oracle),
):
    audit_ctx = {
        "source": auth.source,
        "key_id": auth.key_id,
        "wallet_id": auth.wallet_id,
    }
    results = await oracle.batch_crawl(urls, audit_context=audit_ctx)
    return {
        "crawled": len(results),
        "submitted": len(urls),
        "apis": [
            {
                "api_id": r.api_id,
                "name": r.name,
                "url": r.url,
                "compatibility_tier": r.compatibility_tier.value,
                "compatibility_score": r.compatibility_score,
            }
            for r in results
        ],
    }


# --- Indexed API Directory ---


@router.get(
    "/index",
    response_model=IndexedAPIListResponse,
    summary="List indexed APIs",
    description=(
        "Browse the Oracle's index of external APIs, or list durable crawl rows. "
        "Without ``domain``: returns ``apis`` (indexed APIs), filterable by tier "
        "and directory type. With ``domain``: returns ``crawl_targets`` from the "
        "database (substring match on URL). Sorted by compatibility score (apis) "
        "or queued_at descending (crawl targets)."
    ),
)
async def list_indexed(
    tier: CompatibilityTier | None = Query(
        None,
        description="Filter indexed APIs by compatibility tier (ignored when domain is set)",
    ),
    directory_type: DirectoryType | None = Query(
        None,
        description="Filter indexed APIs by directory type (ignored when domain is set)",
    ),
    domain: str | None = Query(
        None,
        description="When set, list durable crawl targets whose URL contains this host fragment (e.g. example.com).",
    ),
    limit: int = Query(50, ge=1, le=200, description="Page size"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    auth: AuthContext = Depends(get_auth_context),
    oracle: AgentOracle = Depends(get_agent_oracle),
):
    filters: dict = {}
    audit_basis: dict

    if domain:
        filters["domain"] = domain
        rows, total = await oracle.store.list_crawl_targets(
            domain=domain, limit=limit, offset=offset
        )
        audit_basis = {
            "mode": "crawl_targets",
            **filters,
            "limit": limit,
            "offset": offset,
        }
        record_audit(
            "oracle.index.list",
            actor_source=auth.source,
            key_id=auth.key_id,
            wallet_id=auth.wallet_id,
            outcome="ok",
            payload_hash=hashlib.sha256(
                json.dumps(audit_basis, sort_keys=True).encode()
            ).hexdigest(),
        )
        crawl_targets = [
            OracleCrawlTargetRecord(
                target_id=r["target_id"],
                url=r["url"],
                domain=r["domain"],
                directory_type=r["directory_type"],
                status=r["status"],
                api_id=r["api_id"],
                queued_at=r["queued_at"],
                crawled_at=r["crawled_at"],
                raw_payload_hash=r["raw_payload_hash"],
            )
            for r in rows
        ]
        return IndexedAPIListResponse(
            apis=[],
            total=total,
            filters_applied=filters,
            limit=limit,
            offset=offset,
            crawl_targets=crawl_targets,
        )

    if tier:
        filters["tier"] = tier.value
    if directory_type:
        filters["directory_type"] = directory_type.value
    apis, total = await oracle.store.list_indexed(
        tier=tier, directory_type=directory_type, limit=limit, offset=offset
    )
    audit_basis = {"mode": "indexed_apis", **filters, "limit": limit, "offset": offset}
    record_audit(
        "oracle.index.list",
        actor_source=auth.source,
        key_id=auth.key_id,
        wallet_id=auth.wallet_id,
        outcome="ok",
        payload_hash=hashlib.sha256(
            json.dumps(audit_basis, sort_keys=True).encode()
        ).hexdigest(),
    )
    return IndexedAPIListResponse(
        apis=apis,
        total=total,
        filters_applied=filters,
        limit=limit,
        offset=offset,
        crawl_targets=[],
    )


@router.get(
    "/index/{api_id}",
    response_model=IndexedAPI,
    summary="Get indexed API details",
    description="Retrieve full details for a specific indexed API.",
)
async def get_indexed_api(
    api_id: str,
    api_key: str = Depends(verify_api_key),
    oracle: AgentOracle = Depends(get_agent_oracle),
):
    api = await oracle.store.get_indexed(api_id)
    if not api:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "api_not_found"},
        )
    return api


# --- Registration ---


@router.post(
    "/register",
    response_model=RegistrationResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Register in external agent directories",
    description=(
        "Push our API profile into external agent directories and registries. "
        "This is how agents find us — by being listed in the directories they "
        "already crawl. Supports /.well-known, MCP server listings, plugin stores, "
        "and centralized agent registries."
    ),
)
async def register_in_directories(
    request: RegistrationRequest,
    api_key: str = Depends(verify_api_key),
    oracle: AgentOracle = Depends(get_agent_oracle),
):
    targets = [
        {
            "directory_url": t.directory_url,
            "directory_type": t.directory_type.value,
            "registration_payload": t.registration_payload,
        }
        for t in request.targets
    ]
    results = await oracle.register_in_directories(
        targets=targets,
        profile_overrides=request.profile if request.profile else None,
    )

    registered = sum(1 for r in results if r.status == OracleStatus.REGISTERED)
    failed = sum(1 for r in results if r.status == OracleStatus.FAILED)

    return RegistrationResponse(
        results=results,
        total_attempted=len(results),
        total_registered=registered,
        total_failed=failed,
    )


@router.get(
    "/registrations",
    summary="List all registrations",
    description="View all directories where our API has been registered.",
)
async def list_registrations(
    api_key: str = Depends(verify_api_key),
    oracle: AgentOracle = Depends(get_agent_oracle),
):
    regs = await oracle.store.get_registrations()
    return {
        "registrations": [
            {
                "directory_url": r.directory_url,
                "directory_type": r.directory_type.value,
                "status": r.status.value,
                "registration_id": r.registration_id,
            }
            for r in regs
        ],
        "total": len(regs),
    }


# --- Visibility & Analytics ---


@router.get(
    "/visibility",
    response_model=VisibilityScore,
    summary="Get visibility score",
    description=(
        "Compute our API's overall visibility across agent networks. "
        "Includes registration count, discovery hit rate, compatibility "
        "distribution, and actionable recommendations."
    ),
)
async def get_visibility(
    api_key: str = Depends(verify_api_key),
    oracle: AgentOracle = Depends(get_agent_oracle),
):
    return await oracle.compute_visibility()


@router.get(
    "/network",
    response_model=NetworkGraphResponse,
    summary="Get agent network graph",
    description=(
        "Returns the agent network graph centered on our API. "
        "Shows all indexed APIs, registered directories, and their "
        "compatibility relationships. Use this to visualize our position "
        "in the agent ecosystem."
    ),
)
async def get_network_graph(
    api_key: str = Depends(verify_api_key),
    oracle: AgentOracle = Depends(get_agent_oracle),
):
    return await oracle.get_network_graph()


@router.post(
    "/discovery",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Record inbound discovery hit",
    description=(
        "Called when an external agent discovers our API. "
        "Tracks referrer for analytics. This endpoint should be called "
        "by this service whenever an agent first hits / or /.well-known/agent.json."
    ),
)
async def record_discovery(
    referrer: str = Query(default="direct", description="Referring directory URL"),
    auth: AuthContext = Depends(get_auth_context),
    oracle: AgentOracle = Depends(get_agent_oracle),
):
    # Discovery hits feed the shared, global visibility metrics and are meant
    # to be recorded by this service itself, so tenant keys may not write them.
    auth.require_bootstrap_admin()
    await oracle.record_discovery(referrer)
