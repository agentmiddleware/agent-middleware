"""
PROOF SURFACE — frozen. Do not expand; see docs/PROOF_SURFACES.md.
Red-Team-as-a-Service modeling router
-------------------------------------
Tenant-scoped SIMULATED scan jobs for agent-built tools. The backing
service models the job lifecycle only: it contacts no external service
and refuses to run unless its simulation flag is explicitly enabled.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from datetime import datetime

from ..core.auth import AuthContext, get_auth_context
from ..core.dependencies import get_rtaas_engine
from ..schemas.red_team import AttackCategory
from ..services.rtaas import RTaaSEngine, RTaaSJob

router = APIRouter(
    prefix="/v1/rtaas",
    tags=["Red-Team-as-a-Service"],
)


async def _load_owned_job(
    job_id: str,
    engine: RTaaSEngine,
    auth: AuthContext,
) -> RTaaSJob:
    """Fetch a job the caller may read.

    Jobs are wallet-scoped: ``tenant_id`` is the owning wallet. A job owned by
    another wallet answers exactly like a missing one (404), so job ids cannot
    be probed across tenants. Bootstrap admins may read any job.
    """
    job = await engine.get_job(job_id)
    if job is None or not (
        auth.is_bootstrap_admin
        or (auth.wallet_id is not None and job.tenant_id == auth.wallet_id)
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "job_not_found"},
        )
    return job


# --- Schemas ---


class RTaaSTargetSchema(BaseModel):
    """An endpoint description used to generate simulated findings."""

    url: str = Field(..., description="Full URL of the target endpoint.")
    method: str = Field(default="GET", description="HTTP method.")
    auth_header: str | None = Field(
        None,
        description=(
            "Accepted for compatibility only. The simulation contacts no "
            "target, so this value is never forwarded and never stored."
        ),
    )
    description: str = Field(default="", description="What this endpoint does.")


class CreateJobRequest(BaseModel):
    """Describe targets for a simulated Red Team job."""

    tenant_id: str = Field(
        ...,
        description=(
            "Owning wallet ID. Must be the caller's own wallet unless the "
            "caller is a bootstrap admin."
        ),
    )
    targets: list[RTaaSTargetSchema] = Field(
        ...,
        min_length=1,
        description="Endpoint descriptions for simulation; no targets are contacted.",
    )
    attack_categories: list[AttackCategory] | None = Field(
        None,
        description="Attack categories to run. None = all categories.",
    )
    intensity: str = Field(
        default="standard",
        description="Scan intensity: quick, standard, or thorough.",
    )


class VulnerabilitySchema(BaseModel):
    vuln_id: str
    severity: str
    category: str
    target_url: str
    title: str
    description: str
    evidence: str = ""
    cwe_id: str | None = None
    remediation: str = ""


class JobResponse(BaseModel):
    """RTaaS scanning job result."""

    job_id: str
    tenant_id: str
    status: str
    targets_count: int
    total_tests_run: int
    vulnerabilities_found: int
    security_score: float
    vulnerabilities: list[VulnerabilitySchema]
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime


class JobListResponse(BaseModel):
    jobs: list[dict]
    total: int


# --- Endpoints ---


@router.post(
    "/jobs",
    response_model=JobResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a simulated Red Team job",
    description=(
        "Model a scan job using endpoint descriptions and return deterministic "
        "simulated findings with CWE mappings and remediation examples. "
        "No target is contacted or attacked. These findings provide no evidence "
        "of security or release readiness."
    ),
)
async def create_job(
    request: CreateJobRequest,
    auth: AuthContext = Depends(get_auth_context),
    engine: RTaaSEngine = Depends(get_rtaas_engine),
):
    # A caller may only create a job for its own wallet; bootstrap admins may
    # target any tenant.
    auth.require_wallet_access(request.tenant_id)
    job = await engine.create_job(
        tenant_id=request.tenant_id,
        targets=[t.model_dump() for t in request.targets],
        attack_categories=(
            [category.value for category in request.attack_categories]
            if request.attack_categories is not None
            else None
        ),
        intensity=request.intensity,
    )
    return _job_to_response(job)


@router.get(
    "/jobs",
    response_model=JobListResponse,
    summary="List simulated scanning jobs",
    description=(
        "View your wallet's RTaaS jobs. Bootstrap admins may filter by any "
        "tenant, or omit the filter to list all."
    ),
)
async def list_jobs(
    tenant_id: str | None = Query(None),
    auth: AuthContext = Depends(get_auth_context),
    engine: RTaaSEngine = Depends(get_rtaas_engine),
):
    # A wallet-scoped key may only ever list its own jobs: ignore any
    # client-supplied tenant_id and force it to the caller's wallet, so this
    # can't be used to enumerate other tenants. A non-admin caller without a
    # wallet owns nothing (and must never reach the unfiltered list-all path).
    # Bootstrap admins may filter by any tenant_id, or omit it to list all.
    if not auth.is_bootstrap_admin:
        if not auth.wallet_id:
            return JobListResponse(jobs=[], total=0)
        tenant_id = auth.wallet_id
    jobs = await engine.list_jobs(tenant_id)
    return JobListResponse(
        jobs=[
            {
                "job_id": j.job_id,
                "tenant_id": j.tenant_id,
                "status": j.status,
                "targets_count": len(j.targets),
                "vulnerabilities_found": len(j.vulnerabilities),
                "security_score": j.security_score,
                "created_at": j.created_at,
            }
            for j in jobs
        ],
        total=len(jobs),
    )


@router.get(
    "/jobs/{job_id}",
    response_model=JobResponse,
    summary="Get job details",
    description="Retrieve the simulated findings report for an RTaaS job.",
)
async def get_job(
    job_id: str,
    auth: AuthContext = Depends(get_auth_context),
    engine: RTaaSEngine = Depends(get_rtaas_engine),
):
    job = await _load_owned_job(job_id, engine, auth)
    return _job_to_response(job)


@router.get(
    "/jobs/{job_id}/vulnerabilities",
    summary="Get simulated findings for a job",
    description="Retrieve modeled findings with remediation examples; no live scan was run.",
)
async def get_vulnerabilities(
    job_id: str,
    severity: str | None = Query(None, description="Filter by severity"),
    auth: AuthContext = Depends(get_auth_context),
    engine: RTaaSEngine = Depends(get_rtaas_engine),
):
    job = await _load_owned_job(job_id, engine, auth)

    vulns = job.vulnerabilities
    if severity:
        vulns = [v for v in vulns if v.severity.value == severity]

    return {
        "job_id": job_id,
        "total": len(vulns),
        "vulnerabilities": [
            {
                "vuln_id": v.vuln_id,
                "severity": v.severity.value,
                "category": v.category.value,
                "target_url": v.target_url,
                "title": v.title,
                "description": v.description,
                "evidence": v.evidence,
                "cwe_id": v.cwe_id,
                "remediation": v.remediation,
            }
            for v in vulns
        ],
    }


def _job_to_response(job) -> JobResponse:
    return JobResponse(
        job_id=job.job_id,
        tenant_id=job.tenant_id,
        status=job.status,
        targets_count=len(job.targets),
        total_tests_run=job.total_tests_run,
        vulnerabilities_found=len(job.vulnerabilities),
        security_score=job.security_score,
        vulnerabilities=[
            VulnerabilitySchema(
                vuln_id=v.vuln_id,
                severity=v.severity.value,
                category=v.category.value,
                target_url=v.target_url,
                title=v.title,
                description=v.description,
                evidence=v.evidence,
                cwe_id=v.cwe_id,
                remediation=v.remediation,
            )
            for v in job.vulnerabilities
        ],
        started_at=job.started_at,
        completed_at=job.completed_at,
        created_at=job.created_at,
    )
