"""
PROOF SURFACE — frozen. Do not expand; see docs/PROOF_SURFACES.md.
Autonomous Product Manager / Telemetry Router
----------------------------------------------
Ingests raw telemetry, detects anomalies, generates autonomous PRs.
Wired to AutonomousPM service via FastAPI dependency injection.

Tenant-scoped: the tenant is the caller's wallet. Events are recorded under
the ingesting wallet, and anomalies, auto-PR context and stats only ever cover
the caller's own telemetry. Bootstrap admins see every tenant.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
import uuid

from ..core.auth import AuthContext, get_auth_context
from ..core.dependencies import get_autonomous_pm
from ..services.telemetry_pm import (
    ANY_OWNER,
    AutonomousPM,
    OwnerScope,
    owner_for_wallet,
)
from ..schemas.telemetry import (
    TelemetryEvent,
    TelemetryBatch,
    TelemetryBatchResponse,
    AnomalyReport,
    AnomalyListResponse,
    AutoPRRequest,
    AutoPRResponse,
    Severity,
    TelemetryEventType,
)

router = APIRouter(
    prefix="/v1/telemetry",
    tags=["Autonomous Product Manager"],
    responses={
        401: {"description": "Missing API key"},
        403: {"description": "Invalid API key"},
    },
)


def _caller_owner(auth: AuthContext) -> str | None:
    """Partition the caller's ingested events are recorded under.

    A wallet-scoped key writes into its own wallet's partition. Bootstrap
    admins write into the unowned partition, which only admins can read.
    """
    if auth.is_bootstrap_admin:
        return None
    if not auth.wallet_id:
        # Every non-admin credential is wallet-bound; fail closed if not, so
        # it can never fall into the admin-only unowned partition.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "wallet_access_denied",
                "message": "API key is not bound to a wallet.",
            },
        )
    return owner_for_wallet(auth.wallet_id)


def _read_scope(auth: AuthContext) -> OwnerScope:
    """Telemetry the caller may read: every tenant for bootstrap admins,
    otherwise only its own wallet's."""
    if auth.is_bootstrap_admin:
        return ANY_OWNER
    return _caller_owner(auth)


def _anomaly_not_found(anomaly_id: str) -> HTTPException:
    # Same answer for a missing and a foreign anomaly: no existence oracle.
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "error": "anomaly_not_found",
            "message": f"Anomaly '{anomaly_id}' not found.",
        },
    )


@router.post(
    "/events",
    response_model=TelemetryBatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest telemetry events",
    description=(
        "Submit a batch of telemetry events for anomaly detection. "
        "Events are processed asynchronously. The Autonomous PM will "
        "analyze patterns across events to identify error spikes, "
        "latency regressions, and missing feature patterns. Events are "
        "recorded under the caller's wallet."
    ),
)
async def ingest_events(
    batch: TelemetryBatch,
    auth: AuthContext = Depends(get_auth_context),
    pm: AutonomousPM = Depends(get_autonomous_pm),
):
    owner = _caller_owner(auth)
    batch_id = str(uuid.uuid4())
    ingested, errors = await pm.event_store.ingest(batch.events, batch_id, owner)

    return TelemetryBatchResponse(
        ingested=ingested,
        failed=len(errors),
        batch_id=batch_id,
        errors=errors,
    )


@router.post(
    "/events/single",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest a single telemetry event",
    description="Convenience endpoint for submitting one event at a time.",
)
async def ingest_single_event(
    event: TelemetryEvent,
    auth: AuthContext = Depends(get_auth_context),
    pm: AutonomousPM = Depends(get_autonomous_pm),
):
    owner = _caller_owner(auth)
    batch_id = str(uuid.uuid4())
    ingested, errors = await pm.event_store.ingest([event], batch_id, owner)
    return TelemetryBatchResponse(
        ingested=ingested,
        failed=len(errors),
        batch_id=batch_id,
        errors=errors,
    )


@router.get(
    "/anomalies",
    response_model=AnomalyListResponse,
    summary="List detected anomalies",
    description=(
        "Retrieve anomalies the Autonomous PM detected in the caller's "
        "telemetry. Anomalies are classified by severity and category, with "
        "optional LLM-generated fix suggestions and auto-PR links."
    ),
)
async def list_anomalies(
    severity: Severity | None = Query(None, description="Filter by severity level"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    auth: AuthContext = Depends(get_auth_context),
    pm: AutonomousPM = Depends(get_autonomous_pm),
):
    anomalies, total = await pm.detector.get_anomalies(
        severity, page, per_page, owner=_read_scope(auth)
    )
    return AnomalyListResponse(
        anomalies=anomalies,
        total=total,
        page=page,
        per_page=per_page,
    )


@router.get(
    "/anomalies/{anomaly_id}",
    response_model=AnomalyReport,
    summary="Get anomaly details",
    description=(
        "Retrieve detailed information about a specific anomaly, "
        "including suggested fixes."
    ),
)
async def get_anomaly(
    anomaly_id: str,
    auth: AuthContext = Depends(get_auth_context),
    pm: AutonomousPM = Depends(get_autonomous_pm),
):
    anomaly = await pm.detector.get_anomaly(anomaly_id, owner=_read_scope(auth))
    if not anomaly:
        raise _anomaly_not_found(anomaly_id)
    return anomaly


@router.post(
    "/anomalies/{anomaly_id}/auto-pr",
    response_model=AutoPRResponse,
    summary="Generate an autonomous pull request",
    description=(
        "Instruct the Autonomous PM to generate a code fix for the given anomaly "
        "and optionally push it as a pull request. Use dry_run=true to preview "
        "the proposed diff without committing."
    ),
)
async def generate_auto_pr(
    anomaly_id: str,
    request: AutoPRRequest,
    auth: AuthContext = Depends(get_auth_context),
    pm: AutonomousPM = Depends(get_autonomous_pm),
):
    found = await pm.detector.get_owned_anomaly(anomaly_id, owner=_read_scope(auth))
    if not found:
        raise _anomaly_not_found(anomaly_id)
    anomaly, anomaly_owner = found

    # Fix context comes only from the anomaly owner's events, never another
    # tenant's, even when a bootstrap admin triggers the PR.
    related = await pm.event_store.query(
        event_type=TelemetryEventType.ERROR, limit=20, owner=anomaly_owner
    )

    result = await pm.pr_generator.generate_fix(
        anomaly=anomaly,
        related_events=related,
        dry_run=request.dry_run,
    )

    return AutoPRResponse(
        anomaly_id=anomaly_id,
        pr_url=result.get("pr_url"),
        diff=result["diff"],
        files_changed=result["files_changed"],
        tests_passed=result.get("tests_passed"),
        status=result["status"],
    )


@router.get(
    "/stats",
    summary="Get telemetry statistics",
    description=(
        "Aggregate statistics across the caller's ingested telemetry events "
        "(every tenant's for bootstrap admins). "
        "Useful for agents monitoring system health at a glance."
    ),
)
async def get_stats(
    auth: AuthContext = Depends(get_auth_context),
    pm: AutonomousPM = Depends(get_autonomous_pm),
):
    scope = _read_scope(auth)
    event_stats = await pm.event_store.stats(owner=scope)
    _, total_anomalies = await pm.detector.get_anomalies(owner=scope)

    return {
        "total_events": event_stats["total"],
        "events_by_type": event_stats["by_type"],
        "events_by_severity": event_stats["by_severity"],
        "events_by_source": event_stats["by_source"],
        "total_anomalies": total_anomalies,
    }
