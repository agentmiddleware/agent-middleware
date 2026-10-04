"""
Conversion utilities between SQLModel database rows and Pydantic API schemas.
"""

import json
from typing import Any

from datetime import datetime

from ..core.time import to_naive_utc
from ..schemas.billing import (
    WalletResponse,
    WalletType,
    WalletStatus,
    LedgerEntry,
    LedgerAction,
    ServiceCategory,
    BillingAlert,
    AlertType,
    AlertSeverity,
    KYCStatus,
)
from ..schemas.content_factory import (
    ContentFormat,
    ContentStatus,
    GeneratedContent,
    ScheduleRecommendation,
)
from ..schemas.oracle import (
    CompatibilityTier,
    DirectoryType,
    IndexedAPI,
    IndexedCapability,
    OracleStatus,
    RegistrationResult,
)
from ..schemas.red_team import (
    AttackCategory,
    RemediationStatus,
    ScanReport,
    ScanStatus,
    Severity as RTSeverity,
    Vulnerability,
)
from ..schemas.telemetry import (
    TelemetryEvent,
    TelemetryEventType,
    Severity,
)
from .models import (
    WalletModel,
    LedgerEntryModel,
    BillingAlertModel,
    ContentPieceModel,
    ContentScheduleModel,
    OracleIndexedAPIModel,
    OracleRegistrationModel,
    SecurityScanModel,
    SecurityVulnerabilityModel,
    TelemetryEventModel,
)


def wallet_model_to_response(
    wallet: WalletModel,
) -> WalletResponse:
    """Convert a WalletModel to a WalletResponse Pydantic schema."""
    metadata = {}
    if wallet.metadata_json:
        try:
            metadata = json.loads(wallet.metadata_json)
        except json.JSONDecodeError:
            pass

    kyc_status_str = wallet.kyc_status or "not_required"
    try:
        kyc_status = KYCStatus(kyc_status_str)
    except ValueError:
        kyc_status = KYCStatus.NOT_REQUIRED

    return WalletResponse(
        wallet_id=wallet.wallet_id,
        wallet_type=WalletType(wallet.wallet_type),
        owner_name=wallet.owner_name,
        email=wallet.email,
        # Decimal -> float for the display field; the paired *_exact field
        # keeps the full-precision decimal string so no precision is lost.
        balance=float(wallet.balance),
        balance_exact=str(wallet.balance),
        lifetime_credits=float(wallet.lifetime_credits),
        lifetime_credits_exact=str(wallet.lifetime_credits),
        lifetime_debits=float(wallet.lifetime_debits),
        lifetime_debits_exact=str(wallet.lifetime_debits),
        sponsor_wallet_id=wallet.parent_wallet_id,
        agent_id=wallet.agent_id,
        child_agent_id=wallet.child_agent_id,
        max_spend=float(wallet.max_spend) if wallet.max_spend is not None else None,
        max_spend_exact=str(wallet.max_spend) if wallet.max_spend is not None else None,
        task_description=wallet.task_description,
        ttl_seconds=wallet.ttl_seconds,
        daily_limit=(
            float(wallet.daily_limit) if wallet.daily_limit is not None else None
        ),
        daily_limit_exact=(
            str(wallet.daily_limit) if wallet.daily_limit is not None else None
        ),
        auto_refill=wallet.auto_refill,
        auto_refill_threshold=float(wallet.auto_refill_threshold),
        auto_refill_threshold_exact=str(wallet.auto_refill_threshold),
        auto_refill_amount=float(wallet.auto_refill_amount),
        auto_refill_amount_exact=str(wallet.auto_refill_amount),
        status=WalletStatus(wallet.status),
        kyc_status=kyc_status,
        created_at=wallet.created_at,
        metadata=metadata,
    )


def ledger_entry_model_to_schema(
    entry: LedgerEntryModel,
) -> LedgerEntry:
    """Convert a LedgerEntryModel to a LedgerEntry Pydantic schema."""
    metadata = {}
    if entry.metadata_json:
        try:
            metadata = json.loads(entry.metadata_json)
        except json.JSONDecodeError:
            pass

    return LedgerEntry(
        entry_id=entry.entry_id,
        wallet_id=entry.wallet_id,
        action=LedgerAction(entry.action),
        # Decimal -> float for the display field; the paired *_exact field
        # keeps the full-precision decimal string so no precision is lost.
        amount=float(entry.amount),
        amount_exact=str(entry.amount),
        balance_after=float(entry.balance_after),
        balance_after_exact=str(entry.balance_after),
        service_category=(
            ServiceCategory(entry.service_category) if entry.service_category else None
        ),
        description=entry.description,
        request_path=entry.request_path,
        compute_cost=float(entry.compute_cost)
        if entry.compute_cost is not None
        else None,
        compute_cost_exact=(
            str(entry.compute_cost) if entry.compute_cost is not None else None
        ),
        margin=float(entry.margin) if entry.margin is not None else None,
        margin_exact=str(entry.margin) if entry.margin is not None else None,
        timestamp=entry.timestamp,
        metadata=metadata,
    )


def billing_alert_model_to_schema(
    alert: BillingAlertModel,
) -> BillingAlert:
    """Convert a BillingAlertModel to a BillingAlert Pydantic schema."""
    return BillingAlert(
        alert_id=alert.alert_id,
        wallet_id=alert.wallet_id,
        alert_type=AlertType(alert.alert_type),
        # Decimal -> float for the display field; the paired *_exact field
        # keeps the full-precision decimal string so no precision is lost.
        threshold_amount=(
            float(alert.threshold_amount)
            if alert.threshold_amount is not None
            else None
        ),
        threshold_amount_exact=(
            str(alert.threshold_amount) if alert.threshold_amount is not None else None
        ),
        current_balance=(
            float(alert.current_balance) if alert.current_balance is not None else None
        ),
        current_balance_exact=(
            str(alert.current_balance) if alert.current_balance is not None else None
        ),
        message=alert.message,
        severity=AlertSeverity(alert.severity),
        acknowledged=alert.acknowledged,
        created_at=alert.created_at,
    )


def metadata_dict_to_json(metadata: dict[str, Any] | None) -> str | None:
    """Convert a metadata dict to JSON string for database storage."""
    if not metadata:
        return None
    return json.dumps(metadata, default=str)


def parse_metadata_json(metadata_json: str | None) -> dict[str, Any]:
    """Parse metadata JSON string to dict."""
    if not metadata_json:
        return {}
    try:
        return json.loads(metadata_json)
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


def telemetry_event_to_model(
    event_id: str,
    batch_id: str,
    event: TelemetryEvent,
    ingested_at: datetime,
) -> TelemetryEventModel:
    """Convert a TelemetryEvent API schema + storage metadata into a row."""
    return TelemetryEventModel(
        event_id=event_id,
        batch_id=batch_id,
        event_type=event.event_type.value,
        severity=event.severity.value,
        source=event.source,
        message=event.message,
        stack_trace=event.stack_trace,
        payload_json=metadata_dict_to_json(event.metadata),
        event_timestamp=(to_naive_utc(event.timestamp) if event.timestamp else None),
        ingested_at=to_naive_utc(ingested_at),
    )


def telemetry_event_model_to_schema(row: TelemetryEventModel) -> TelemetryEvent:
    """Rebuild a TelemetryEvent from its stored row."""
    return TelemetryEvent(
        event_type=TelemetryEventType(row.event_type),
        source=row.source,
        message=row.message,
        severity=Severity(row.severity),
        stack_trace=row.stack_trace,
        metadata=parse_metadata_json(row.payload_json),
        timestamp=row.event_timestamp,
    )


# ---------------------------------------------------------------------------
# Oracle
# ---------------------------------------------------------------------------


def indexed_api_to_model(api: IndexedAPI) -> OracleIndexedAPIModel:
    """Flatten an IndexedAPI schema into its stored row."""
    return OracleIndexedAPIModel(
        api_id=api.api_id,
        url=api.url,
        name=api.name,
        description=api.description,
        directory_type=api.directory_type.value,
        compatibility_tier=api.compatibility_tier.value,
        compatibility_score=api.compatibility_score,
        capabilities_json=json.dumps(
            [c.model_dump() for c in api.capabilities], default=str
        ),
        tags_json=json.dumps(api.tags, default=str),
        status=api.status.value,
        last_crawled=api.last_crawled,
    )


def indexed_api_model_to_schema(row: OracleIndexedAPIModel) -> IndexedAPI:
    """Rebuild an IndexedAPI from its row."""
    caps: list[IndexedCapability] = []
    if row.capabilities_json:
        try:
            for item in json.loads(row.capabilities_json):
                caps.append(IndexedCapability.model_validate(item))
        except (json.JSONDecodeError, ValueError):
            pass

    tags: list[str] = []
    if row.tags_json:
        try:
            parsed = json.loads(row.tags_json)
            if isinstance(parsed, list):
                tags = [str(t) for t in parsed]
        except json.JSONDecodeError:
            pass

    return IndexedAPI(
        api_id=row.api_id,
        url=row.url,
        name=row.name,
        description=row.description,
        directory_type=DirectoryType(row.directory_type),
        capabilities=caps,
        compatibility_tier=CompatibilityTier(row.compatibility_tier),
        compatibility_score=row.compatibility_score,
        tags=tags,
        last_crawled=row.last_crawled,
        status=OracleStatus(row.status),
    )


def registration_result_to_model(
    result: RegistrationResult,
) -> OracleRegistrationModel:
    """Store a RegistrationResult (caller synthesizes id for failures)."""
    return OracleRegistrationModel(
        registration_id=result.registration_id or "",
        directory_url=result.directory_url,
        directory_type=result.directory_type.value,
        status=result.status.value,
        message=result.message,
    )


def registration_model_to_schema(
    row: OracleRegistrationModel,
) -> RegistrationResult:
    return RegistrationResult(
        directory_url=row.directory_url,
        directory_type=DirectoryType(row.directory_type),
        status=OracleStatus(row.status),
        registration_id=row.registration_id or None,
        message=row.message,
    )


# ---------------------------------------------------------------------------
# Security scans (shared by red_team + rtaas)
# ---------------------------------------------------------------------------


def _str_list_to_json(values: list | None) -> str | None:
    if not values:
        return None
    return json.dumps(list(values), default=str)


def _parse_json_list(raw: str | None) -> list:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


def scan_report_to_scan_model(report: ScanReport) -> SecurityScanModel:
    """Internal red_team ScanReport → row (no tenant)."""
    return SecurityScanModel(
        scan_id=report.scan_id,
        scan_type="internal",
        tenant_id=None,
        targets_json=_str_list_to_json(report.target_services),
        attack_categories_json=_str_list_to_json(report.attack_categories),
        intensity=report.intensity,
        status=report.status.value,
        total_tests_run=report.total_tests_run,
        total_passed=report.total_passed,
        total_failed=report.total_failed,
        security_score=report.score,
        recommendations_json=_str_list_to_json(report.recommendations),
        started_at=report.started_at,
        completed_at=report.completed_at,
    )


def vulnerability_to_model(vuln: Vulnerability) -> SecurityVulnerabilityModel:
    return SecurityVulnerabilityModel(
        vuln_id=vuln.vuln_id,
        scan_id=vuln.scan_id,
        category=vuln.category.value,
        severity=vuln.severity.value,
        title=vuln.title,
        description=vuln.description,
        endpoint=vuln.endpoint,
        method=vuln.method,
        evidence_json=(
            json.dumps(vuln.evidence, default=str) if vuln.evidence else None
        ),
        remediation=vuln.remediation,
        remediation_status=vuln.remediation_status.value,
        cwe_id=vuln.cwe_id,
        discovered_at=vuln.discovered_at,
    )


def vulnerability_model_to_schema(
    row: SecurityVulnerabilityModel,
) -> Vulnerability:
    evidence: dict = {}
    if row.evidence_json:
        try:
            loaded = json.loads(row.evidence_json)
            evidence = loaded if isinstance(loaded, dict) else {"value": loaded}
        except json.JSONDecodeError:
            evidence = {}

    return Vulnerability(
        vuln_id=row.vuln_id,
        scan_id=row.scan_id,
        category=AttackCategory(row.category),
        severity=RTSeverity(row.severity),
        title=row.title,
        description=row.description,
        endpoint=row.endpoint,
        method=row.method or "",
        evidence=evidence,
        remediation=row.remediation,
        remediation_status=RemediationStatus(row.remediation_status),
        cwe_id=row.cwe_id,
        discovered_at=row.discovered_at,
    )


def scan_model_to_report(
    scan: SecurityScanModel,
    vulns: list[SecurityVulnerabilityModel],
) -> ScanReport:
    """Row + child vulns → internal ScanReport."""
    vulnerabilities = [vulnerability_model_to_schema(v) for v in vulns]

    severity_breakdown: dict[str, int] = {}
    for v in vulnerabilities:
        severity_breakdown[v.severity.value] = (
            severity_breakdown.get(v.severity.value, 0) + 1
        )

    duration = None
    if scan.started_at and scan.completed_at:
        duration = (scan.completed_at - scan.started_at).total_seconds()

    return ScanReport(
        scan_id=scan.scan_id,
        status=ScanStatus(scan.status),
        started_at=scan.started_at or scan.created_at,
        completed_at=scan.completed_at,
        duration_seconds=duration,
        target_services=_parse_json_list(scan.targets_json),
        attack_categories=[
            str(c) for c in _parse_json_list(scan.attack_categories_json)
        ],
        intensity=scan.intensity,
        total_tests_run=scan.total_tests_run,
        total_passed=scan.total_passed or 0,
        total_failed=scan.total_failed or 0,
        vulnerabilities_found=len(vulnerabilities),
        severity_breakdown=severity_breakdown,
        vulnerabilities=vulnerabilities,
        recommendations=[str(r) for r in _parse_json_list(scan.recommendations_json)],
        score=scan.security_score,
    )


# ---------------------------------------------------------------------------
# Content factory
# ---------------------------------------------------------------------------


def content_piece_to_model(piece: GeneratedContent) -> ContentPieceModel:
    return ContentPieceModel(
        content_id=piece.content_id,
        pipeline_id=piece.pipeline_id,
        format=piece.format.value,
        title=piece.title,
        description=piece.description,
        download_url=piece.download_url,
        thumbnail_url=piece.thumbnail_url,
        duration_seconds=piece.duration_seconds,
        dimensions=piece.dimensions,
        file_size_bytes=piece.file_size_bytes,
        status=piece.status.value,
        metadata_json=metadata_dict_to_json(piece.metadata),
        generated_at=piece.generated_at,
    )


def content_piece_model_to_schema(row: ContentPieceModel) -> GeneratedContent:
    return GeneratedContent(
        content_id=row.content_id,
        pipeline_id=row.pipeline_id,
        format=ContentFormat(row.format),
        title=row.title,
        description=row.description,
        download_url=row.download_url,
        thumbnail_url=row.thumbnail_url,
        duration_seconds=row.duration_seconds,
        dimensions=row.dimensions,
        file_size_bytes=row.file_size_bytes,
        status=ContentStatus(row.status),
        generated_at=row.generated_at,
        metadata=parse_metadata_json(row.metadata_json),
    )


def schedule_recommendation_to_model(
    rec: ScheduleRecommendation, schedule_id: str
) -> ContentScheduleModel:
    return ContentScheduleModel(
        schedule_id=schedule_id,
        content_id=rec.content_id,
        platform=rec.platform,
        recommended_time=rec.recommended_time,
        confidence=rec.confidence,
        reasoning=rec.reasoning,
        estimated_views=rec.estimated_views,
    )


def schedule_model_to_recommendation(
    row: ContentScheduleModel,
) -> ScheduleRecommendation:
    return ScheduleRecommendation(
        content_id=row.content_id,
        platform=row.platform,
        recommended_time=row.recommended_time,
        confidence=row.confidence,
        reasoning=row.reasoning,
        estimated_views=row.estimated_views,
    )
