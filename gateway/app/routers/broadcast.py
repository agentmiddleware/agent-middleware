"""
PROOF SURFACE — frozen. Do not expand; see docs/PROOF_SURFACES.md.
Oracle Mass-Broadcast — Router
================================
Push published APIs into the agent discovery network.

Endpoints:
- POST /v1/broadcast                        — Broadcast a published API
- GET  /v1/broadcast/jobs                    — List the caller's broadcast jobs
- GET  /v1/broadcast/jobs/{job_id}           — Get broadcast job details
- GET  /v1/broadcast/jobs/{job_id}/metrics   — Get discovery metrics
- POST /v1/broadcast/jobs/{job_id}/events    — Simulate discovery event
- GET  /v1/broadcast/directories             — List available directories
"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from datetime import datetime
from dataclasses import asdict

from ..core.auth import AuthContext, get_auth_context
from ..core.dependencies import get_broadcast_engine
from ..services.oracle_broadcast import (
    AGENT_DIRECTORIES,
    BroadcastJob,
    OracleBroadcastEngine,
)

# Router-level auth keeps every endpoint (including /directories) behind a
# credential; handlers that need the caller re-declare get_auth_context, which
# FastAPI resolves once per request.
router = APIRouter(
    prefix="/v1/broadcast",
    tags=["Oracle Broadcast"],
    dependencies=[Depends(get_auth_context)],
)


def _job_not_found(job_id: str) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, f"Broadcast job {job_id} not found")


def _owns_job(auth: AuthContext, job: BroadcastJob) -> bool:
    """Bootstrap admins see every job; a wallet key only the jobs it created."""
    if auth.is_bootstrap_admin:
        return True
    return auth.wallet_id is not None and job.owner_wallet_id == auth.wallet_id


async def _load_owned_job(
    job_id: str, engine: OracleBroadcastEngine, auth: AuthContext
) -> BroadcastJob:
    """Load a broadcast job, or 404 — whether it is missing or not the caller's.

    Every job-scoped handler goes through this so one tenant cannot read
    another tenant's job or metrics, or forge discovery events into them. A
    foreign job is indistinguishable from a missing one, so the response is
    neither an existence oracle nor a leak of the owning wallet id.
    """
    job = await engine.get_job(job_id)
    if job is None or not _owns_job(auth, job):
        raise _job_not_found(job_id)
    return job


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class BroadcastRequest(BaseModel):
    """Request to broadcast a published API to agent directories."""

    service_name: str = Field(..., description="Name of the API to broadcast.")
    service_version: str = Field(default="1.0.0", description="API version.")
    base_url: str = Field(..., description="Production base URL of the API.")
    generation_id: str = Field(
        ...,
        description="Protocol Engine generation ID (from POST /v1/protocol/generate).",
    )
    llm_txt: str | None = Field(None, description="Generated llm.txt content.")
    openapi_spec: dict | None = Field(None, description="Generated OpenAPI 3.1 spec.")
    agent_json: dict | None = Field(None, description="Generated agent.json manifest.")
    target_directories: list[str] | None = Field(
        None, description="Directory IDs to target (default: all)."
    )


class BroadcastTargetDetail(BaseModel):
    directory_id: str
    directory_name: str
    url: str
    format: str
    tier: str
    status: str
    response_code: int | None = None
    registered_at: datetime | None = None


class DiscoveryMetricsResponse(BaseModel):
    impressions: int
    lookups: int
    integrations: int
    last_lookup_at: datetime | None = None
    referral_sources: dict[str, int]


class BroadcastJobResponse(BaseModel):
    job_id: str
    service_name: str
    service_version: str
    base_url: str
    generation_id: str
    oracle_registration_id: str | None = None
    directories_contacted: int
    directories_confirmed: int
    directories_failed: int
    targets: list[dict]
    discovery_metrics: dict
    status: str
    created_at: datetime
    completed_at: datetime | None = None


class DiscoveryEventRequest(BaseModel):
    """Simulate an inbound discovery event."""

    event_type: str = Field(
        ...,
        description="Event type: impression, lookup, or integration.",
    )
    source: str = Field(
        ..., description="Source of the event (directory name or agent ID)."
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=BroadcastJobResponse,
    status_code=201,
    summary="Broadcast API to Agent Directories",
    description=(
        "Pushes discovery artifacts (llm.txt, OpenAPI, agent.json) "
        "to all registered agent directories. The network effects engine."
    ),
)
async def broadcast_api(
    request: BroadcastRequest,
    auth: AuthContext = Depends(get_auth_context),
    engine: OracleBroadcastEngine = Depends(get_broadcast_engine),
):
    job = await engine.broadcast(
        owner_wallet_id=auth.wallet_id,
        service_name=request.service_name,
        service_version=request.service_version,
        base_url=request.base_url,
        generation_id=request.generation_id,
        llm_txt=request.llm_txt,
        openapi_spec=request.openapi_spec,
        agent_json=request.agent_json,
        directories=request.target_directories,
    )

    return BroadcastJobResponse(
        job_id=job.job_id,
        service_name=job.service_name,
        service_version=job.service_version,
        base_url=job.base_url,
        generation_id=job.generation_id,
        oracle_registration_id=job.oracle_registration_id,
        directories_contacted=job.directories_contacted,
        directories_confirmed=job.directories_confirmed,
        directories_failed=job.directories_failed,
        targets=[asdict(t) for t in job.targets],
        discovery_metrics=asdict(job.discovery_metrics),
        status=job.status,
        created_at=job.created_at,
        completed_at=job.completed_at,
    )


@router.get(
    "/jobs",
    summary="List Broadcast Jobs",
)
async def list_jobs(
    service_name: str | None = None,
    auth: AuthContext = Depends(get_auth_context),
    engine: OracleBroadcastEngine = Depends(get_broadcast_engine),
):
    # A wallet-scoped key only ever lists its own jobs; bootstrap admins see all.
    jobs = [
        j
        for j in await engine.list_jobs(service_name=service_name)
        if _owns_job(auth, j)
    ]
    return {
        "jobs": [
            {
                "job_id": j.job_id,
                "service_name": j.service_name,
                "directories_confirmed": j.directories_confirmed,
                "status": j.status,
                "created_at": str(j.created_at),
            }
            for j in jobs
        ],
        "total": len(jobs),
    }


@router.get(
    "/jobs/{job_id}",
    response_model=BroadcastJobResponse,
    summary="Get Broadcast Job Details",
)
async def get_job(
    job_id: str,
    auth: AuthContext = Depends(get_auth_context),
    engine: OracleBroadcastEngine = Depends(get_broadcast_engine),
):
    job = await _load_owned_job(job_id, engine, auth)

    return BroadcastJobResponse(
        job_id=job.job_id,
        service_name=job.service_name,
        service_version=job.service_version,
        base_url=job.base_url,
        generation_id=job.generation_id,
        oracle_registration_id=job.oracle_registration_id,
        directories_contacted=job.directories_contacted,
        directories_confirmed=job.directories_confirmed,
        directories_failed=job.directories_failed,
        targets=[asdict(t) for t in job.targets],
        discovery_metrics=asdict(job.discovery_metrics),
        status=job.status,
        created_at=job.created_at,
        completed_at=job.completed_at,
    )


@router.get(
    "/jobs/{job_id}/metrics",
    response_model=DiscoveryMetricsResponse,
    summary="Get Discovery Metrics",
    description="Real-time discovery tracking: impressions, lookups, integrations.",
)
async def get_metrics(
    job_id: str,
    auth: AuthContext = Depends(get_auth_context),
    engine: OracleBroadcastEngine = Depends(get_broadcast_engine),
):
    await _load_owned_job(job_id, engine, auth)
    metrics = await engine.get_discovery_metrics(job_id)
    if not metrics:
        raise _job_not_found(job_id)
    return DiscoveryMetricsResponse(**asdict(metrics))


@router.post(
    "/jobs/{job_id}/events",
    response_model=DiscoveryMetricsResponse,
    summary="Simulate Discovery Event",
    description="Simulate an impression, lookup, or integration event.",
)
async def record_discovery_event(
    job_id: str,
    request: DiscoveryEventRequest,
    auth: AuthContext = Depends(get_auth_context),
    engine: OracleBroadcastEngine = Depends(get_broadcast_engine),
):
    if request.event_type not in ("impression", "lookup", "integration"):
        raise HTTPException(
            400, "event_type must be impression, lookup, or integration"
        )

    await _load_owned_job(job_id, engine, auth)
    metrics = await engine.simulate_discovery_event(
        job_id, request.event_type, request.source
    )
    if not metrics:
        raise _job_not_found(job_id)
    return DiscoveryMetricsResponse(**asdict(metrics))


@router.get(
    "/directories",
    summary="List Available Directories",
    description="All agent directories we can broadcast to.",
)
async def list_directories():
    return {
        "directories": AGENT_DIRECTORIES,
        "total": len(AGENT_DIRECTORIES),
    }
