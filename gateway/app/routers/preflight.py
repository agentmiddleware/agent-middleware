"""
Pre-Flight Readiness Check — Router
=====================================
Validates that the system is production-ready: real API keys, a real
BASE_URL, reachable agent directories, and non-placeholder content assets.

Endpoints:
- POST /v1/launch/preflight — Run the readiness sweep, get a GO/NO-GO verdict

Operator-only: the report describes deployment configuration (bootstrap-key
hygiene, DEBUG, rate limit), so it requires a bootstrap admin key. A
wallet-scoped tenant key is refused with 403 admin_access_denied.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from datetime import datetime

from ..core.auth import AuthContext, get_auth_context
from ..services.preflight import PreflightEngine

router = APIRouter(
    prefix="/v1/launch",
    tags=["Preflight"],
)


# ---------------------------------------------------------------------------
# Request / Response Schemas
# ---------------------------------------------------------------------------


class PreflightRequest(BaseModel):
    """Optional overrides for preflight validation."""

    base_url: str = Field(
        default="",
        description="Production BASE_URL to validate (e.g., https://api.mycompany.com).",
    )
    stripe_secret_key: str = Field(
        default="",
        description="Stripe secret key to validate (sk_live_... for production).",
    )
    campaign_source_url: str = Field(
        default="",
        description="Campaign content source URL to validate (e.g., a real launch video URL).",
    )


class PreflightCheckResult(BaseModel):
    """Single preflight check outcome."""

    name: str
    passed: bool
    severity: str = Field(..., description="critical, warning, or info")
    message: str
    detail: str = ""


class PreflightResponse(BaseModel):
    """Pre-flight readiness report — the checklist before you turn the key."""

    checked_at: datetime
    verdict: str = Field(..., description="GO or NO-GO")
    total_checks: int
    passed: int
    failed: int
    warnings: int
    critical_failures: int
    checks: list[PreflightCheckResult]
    summary: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/preflight",
    response_model=PreflightResponse,
    summary="Pre-flight Readiness Check",
    description=(
        "Runs a comprehensive validation sweep before production launch:\n\n"
        "1. **KEYS** — Reject test-key/placeholder API keys, validate Stripe tokens\n"
        "2. **DOMAIN** — Verify BASE_URL is a real domain, manifests will resolve\n"
        "3. **ORACLE** — Validate agent directory registration URLs\n"
        "4. **ASSETS** — Check content source URLs and crawl targets\n\n"
        "Returns a GO/NO-GO verdict with per-check details. "
        "Requires bootstrap admin authentication."
    ),
)
async def run_preflight(
    request: PreflightRequest = PreflightRequest(),
    auth: AuthContext = Depends(get_auth_context),
):
    # The report is operator configuration, not tenant data: a wallet-scoped
    # key must not learn the deployment's key hygiene, DEBUG state, or rate
    # limit, nor drive a sweep that operators use as a launch gate.
    auth.require_bootstrap_admin()
    engine = PreflightEngine()

    config_overrides = {}
    if request.base_url:
        config_overrides["base_url"] = request.base_url
    if request.stripe_secret_key:
        config_overrides["stripe_secret_key"] = request.stripe_secret_key
    if request.campaign_source_url:
        config_overrides["campaign_source_url"] = request.campaign_source_url

    report = await engine.run(config_overrides)

    return PreflightResponse(
        checked_at=report.checked_at,
        verdict=report.verdict,
        total_checks=report.total_checks,
        passed=report.passed,
        failed=report.failed,
        warnings=report.warnings,
        critical_failures=report.critical_failures,
        checks=[PreflightCheckResult(**c) for c in report.checks],
        summary=report.summary,
    )
