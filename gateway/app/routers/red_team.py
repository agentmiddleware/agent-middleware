"""
PROOF SURFACE — frozen. Do not expand; see docs/PROOF_SURFACES.md.
Red-team scan modeling router
-----------------------------
Endpoints for triggering, monitoring, and reviewing SIMULATED security
scans. The backing service models a scan lifecycle across 7 attack
categories (ACL bypass, auth probes, injection, rate limit evasion,
privilege escalation, schema abuse, enumeration) but sends no traffic
and attacks nothing; it refuses to run unless its simulation flag is
explicitly enabled. The repository's real adversarial tooling is
scripts/invariant_attacks/.

Findings are structured and machine-readable.

Scans are wallet-scoped: a scan belongs to the wallet whose key launched it,
and a wallet-scoped key can read and list only its own scans. Bootstrap
admins launch ownerless scans and can read every scan.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from ..core.auth import AuthContext, get_auth_context
from ..core.dependencies import get_red_team_swarm
from ..services.red_team import RedTeamSwarm
from ..schemas.red_team import (
    ScanRequest,
    ScanResponse,
    ScanReport,
    ScanListResponse,
    VulnerabilityListResponse,
    Severity,
)

router = APIRouter(
    prefix="/v1/security",
    tags=["Red Team Security Swarm"],
)


def _scan_owner_scope(auth: AuthContext) -> str | None:
    """Wallet a caller's scans are owned by and its reads are confined to.

    ``None`` only for a bootstrap admin: its scans are ownerless and its reads
    are unscoped. Every other caller is pinned to its own wallet, so one tenant
    can never read, list, or enumerate another tenant's scan reports.
    """
    if auth.is_bootstrap_admin:
        return None
    if auth.wallet_id is None:
        # A non-admin key always carries a wallet; refuse rather than fall
        # through to the unscoped (admin) read path.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "wallet_access_denied",
                "message": "API key is not authorized for this wallet.",
            },
        )
    return auth.wallet_id


@router.post(
    "/scans",
    response_model=ScanResponse,
    status_code=202,
    summary="Run a simulated Red Team scan",
    description=(
        "Model the scan lifecycle for the selected services and attack categories. "
        "Returns a simulated report synchronously. Sends no attack traffic and "
        "provides no evidence of security or release readiness."
    ),
)
async def launch_scan(
    request: ScanRequest,
    auth: AuthContext = Depends(get_auth_context),
    swarm: RedTeamSwarm = Depends(get_red_team_swarm),
):
    report = await swarm.run_scan(
        target_services=request.target_services,
        attack_categories=request.attack_categories,
        intensity=request.intensity,
        auto_remediate=request.auto_remediate,
        owner_wallet_id=_scan_owner_scope(auth),
    )
    return ScanResponse(
        scan_id=report.scan_id,
        status=report.status,
        target_services=report.target_services,
        attack_categories=report.attack_categories,
        intensity=report.intensity,
        estimated_duration_seconds=int(report.duration_seconds or 0),
        total_attack_vectors=report.total_tests_run,
    )


@router.get(
    "/scans",
    response_model=ScanListResponse,
    summary="List simulated security scans",
    description=(
        "Returns the caller's historical simulated scan reports, newest first "
        "(every scan for a bootstrap admin)."
    ),
)
async def list_scans(
    auth: AuthContext = Depends(get_auth_context),
    swarm: RedTeamSwarm = Depends(get_red_team_swarm),
):
    scans = await swarm.list_scans(owner_wallet_id=_scan_owner_scope(auth))
    scans.sort(key=lambda s: s.started_at, reverse=True)
    return ScanListResponse(scans=scans, total=len(scans))


@router.get(
    "/scans/{scan_id}",
    response_model=ScanReport,
    summary="Get simulated scan report",
    description=(
        "Returns the simulated security scan report including all modeled "
        "vulnerabilities, severity breakdown, security score, and "
        "prioritized remediation recommendations."
    ),
)
async def get_scan_report(
    scan_id: str,
    auth: AuthContext = Depends(get_auth_context),
    swarm: RedTeamSwarm = Depends(get_red_team_swarm),
):
    # Another wallet's scan is reported as not found: no existence oracle.
    report = await swarm.get_scan(scan_id, owner_wallet_id=_scan_owner_scope(auth))
    if not report:
        raise HTTPException(status_code=404, detail="Scan not found")
    return report


@router.get(
    "/scans/{scan_id}/vulnerabilities",
    response_model=VulnerabilityListResponse,
    summary="Get simulated findings from a scan",
    description=(
        "Returns modeled findings from a simulated scan, optionally filtered "
        "by severity. These are not findings from live security testing."
    ),
)
async def get_vulnerabilities(
    scan_id: str,
    severity: Severity | None = None,
    auth: AuthContext = Depends(get_auth_context),
    swarm: RedTeamSwarm = Depends(get_red_team_swarm),
):
    report = await swarm.get_scan(scan_id, owner_wallet_id=_scan_owner_scope(auth))
    if not report:
        raise HTTPException(status_code=404, detail="Scan not found")

    vulns = report.vulnerabilities
    if severity:
        vulns = [v for v in vulns if v.severity == severity]

    return VulnerabilityListResponse(
        vulnerabilities=vulns,
        total=len(vulns),
        critical_count=sum(1 for v in vulns if v.severity == Severity.CRITICAL),
        high_count=sum(1 for v in vulns if v.severity == Severity.HIGH),
    )


@router.post(
    "/scans/quick",
    response_model=ScanReport,
    summary="Quick simulated security scan",
    description=(
        "Models CRITICAL and HIGH severity vectors (ACL bypass, auth probes, "
        "privilege escalation) and returns a simulated report synchronously. "
        "Sends no attack traffic. This report cannot validate security or "
        "serve as a release gate."
    ),
)
async def quick_scan(
    auth: AuthContext = Depends(get_auth_context),
    swarm: RedTeamSwarm = Depends(get_red_team_swarm),
):
    from ..schemas.red_team import AttackCategory

    report = await swarm.run_scan(
        target_services=["iot", "telemetry", "media", "comms", "factory"],
        attack_categories=[
            AttackCategory.ACL_BYPASS,
            AttackCategory.AUTH_PROBE,
            AttackCategory.PRIVILEGE_ESCALATION,
        ],
        intensity="quick",
        owner_wallet_id=_scan_owner_scope(auth),
    )
    return report
