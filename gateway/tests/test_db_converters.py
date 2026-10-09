"""Focused unit tests for app/db/converters.py.

Pure in-memory round trips between SQLModel rows and Pydantic schemas.
No database, no network.
"""

from datetime import datetime, timezone
from decimal import Decimal

from app.db import converters as c
from app.db.models import (
    BillingAlertModel,
    ContentPieceModel,
    ContentScheduleModel,
    LedgerEntryModel,
    OracleIndexedAPIModel,
    OracleRegistrationModel,
    SecurityScanModel,
    SecurityVulnerabilityModel,
    TelemetryEventModel,
    WalletModel,
)
from app.schemas.billing import AlertSeverity, AlertType, LedgerAction
from app.schemas.content_factory import (
    ContentFormat,
    ContentStatus,
    GeneratedContent,
    ScheduleRecommendation,
)
from app.schemas.oracle import (
    CompatibilityTier,
    DirectoryType,
    IndexedAPI,
    IndexedCapability,
    OracleStatus,
    RegistrationResult,
)
from app.schemas.red_team import (
    AttackCategory,
    RemediationStatus,
    ScanReport,
    ScanStatus,
    Severity as RTSeverity,
    Vulnerability,
)
from app.schemas.telemetry import Severity, TelemetryEvent, TelemetryEventType


def _utc(year=2026, month=1, day=2, hour=3, minute=4, second=5):
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


# --- metadata helpers -------------------------------------------------------


def test_metadata_dict_to_json_none_and_empty():
    assert c.metadata_dict_to_json(None) is None
    assert c.metadata_dict_to_json({}) is None


def test_metadata_dict_to_json_round_trip_and_str_fallback():
    import json

    payload = {"a": 1, "when": _utc()}
    raw = c.metadata_dict_to_json(payload)
    assert raw is not None
    assert json.loads(raw)["a"] == 1
    assert isinstance(json.loads(raw)["when"], str)


def test_parse_metadata_json_edge_cases():
    assert c.parse_metadata_json(None) == {}
    assert c.parse_metadata_json("") == {}
    assert c.parse_metadata_json("{not json") == {}
    assert c.parse_metadata_json('{"k": "v"}') == {"k": "v"}


# --- wallet -----------------------------------------------------------------


def _wallet(**overrides):
    base = {"wallet_id": "w-1", "wallet_type": "sponsor"}
    base.update(overrides)
    return WalletModel(**base)


def test_wallet_model_to_response_happy_path():
    wallet = _wallet(
        owner_name="Ada",
        balance=Decimal("12.5"),
        lifetime_credits=Decimal("20"),
        lifetime_debits=Decimal("7.5"),
        metadata_json='{"tier": "gold"}',
        kyc_status="verified",
    )
    resp = c.wallet_model_to_response(wallet)
    assert resp.wallet_id == "w-1"
    assert resp.balance == 12.5
    assert resp.balance_exact == "12.5"
    assert resp.metadata == {"tier": "gold"}
    assert resp.kyc_status.value == "verified"


def test_wallet_model_to_response_bad_metadata_and_kyc_fall_back():
    wallet = _wallet(metadata_json="{broken", kyc_status="mystery-status")
    resp = c.wallet_model_to_response(wallet)
    assert resp.metadata == {}
    assert resp.kyc_status.value == "not_required"


def test_wallet_model_to_response_none_kyc_and_none_optionals():
    wallet = _wallet(kyc_status=None, max_spend=None, daily_limit=None)
    resp = c.wallet_model_to_response(wallet)
    assert resp.kyc_status.value == "not_required"
    assert resp.max_spend is None
    assert resp.max_spend_exact is None
    assert resp.daily_limit is None


# --- ledger and billing alerts ----------------------------------------------


def test_ledger_entry_model_to_schema_happy_path():
    entry = LedgerEntryModel(
        entry_id="e-1",
        wallet_id="w-1",
        action="credit",
        amount=Decimal("3.25"),
        balance_after=Decimal("10.25"),
        service_category="oracle",
        description="top up",
        compute_cost=Decimal("1.5"),
        margin=Decimal("0.5"),
        metadata_json='{"src": "t"}',
    )
    schema = c.ledger_entry_model_to_schema(entry)
    assert schema.action == LedgerAction.CREDIT
    assert schema.amount == 3.25
    assert schema.amount_exact == "3.25"
    assert schema.compute_cost == 1.5
    assert schema.margin_exact == "0.5"
    assert schema.metadata == {"src": "t"}


def test_ledger_entry_model_to_schema_none_optionals_and_bad_metadata():
    entry = LedgerEntryModel(
        entry_id="e-2",
        wallet_id="w-1",
        action="debit",
        amount=Decimal("1"),
        balance_after=Decimal("9"),
        service_category=None,
        compute_cost=None,
        margin=None,
        metadata_json="{oops",
    )
    schema = c.ledger_entry_model_to_schema(entry)
    assert schema.service_category is None
    assert schema.compute_cost is None
    assert schema.margin is None
    assert schema.metadata == {}


def test_billing_alert_model_to_schema_none_amounts():
    alert = BillingAlertModel(
        alert_id="a-1",
        wallet_id="w-1",
        alert_type="low_balance",
        message="low",
        severity="warning",
        threshold_amount=None,
        current_balance=None,
    )
    schema = c.billing_alert_model_to_schema(alert)
    assert schema.alert_type == AlertType.LOW_BALANCE
    assert schema.severity == AlertSeverity.WARNING
    assert schema.threshold_amount is None
    assert schema.threshold_amount_exact is None
    assert schema.current_balance_exact is None


# --- telemetry --------------------------------------------------------------


def test_telemetry_event_round_trip():
    event = TelemetryEvent(
        event_type=TelemetryEventType.ERROR,
        source="iot-bridge",
        message="boom",
        severity=Severity.HIGH,
        stack_trace="trace",
        metadata={"k": "v"},
        timestamp=_utc(),
    )
    row = c.telemetry_event_to_model("ev-1", "b-1", event, _utc())
    assert isinstance(row, TelemetryEventModel)
    assert row.event_type == "error"
    assert row.severity == "high"
    assert row.event_timestamp is not None
    assert row.event_timestamp.tzinfo is None

    back = c.telemetry_event_model_to_schema(row)
    assert back.event_type == TelemetryEventType.ERROR
    assert back.message == "boom"
    assert back.metadata == {"k": "v"}


def test_telemetry_event_model_to_schema_bad_payload_and_no_timestamp():
    row = TelemetryEventModel(
        event_id="ev-2",
        batch_id="b-1",
        event_type="warning",
        severity="low",
        source="s",
        message="m",
        payload_json="{broken",
        event_timestamp=None,
    )
    back = c.telemetry_event_model_to_schema(row)
    assert back.metadata == {}
    assert back.timestamp is None


# --- oracle -----------------------------------------------------------------


def _indexed_api():
    return IndexedAPI(
        api_id="api-1",
        url="https://example.com/api",
        name="Example",
        description="desc",
        directory_type=DirectoryType.OPENAPI,
        capabilities=[
            IndexedCapability(name="cap", description="d", endpoint="/x", method="GET")
        ],
        compatibility_tier=CompatibilityTier.NATIVE,
        compatibility_score=0.9,
        tags=["a", "b"],
        last_crawled=_utc(),
        status=OracleStatus.INDEXED,
    )


def test_indexed_api_round_trip():
    row = c.indexed_api_to_model(_indexed_api())
    assert isinstance(row, OracleIndexedAPIModel)
    back = c.indexed_api_model_to_schema(row)
    assert back.api_id == "api-1"
    assert back.capabilities[0].name == "cap"
    assert back.tags == ["a", "b"]
    assert back.compatibility_tier == CompatibilityTier.NATIVE


def test_indexed_api_model_to_schema_corrupt_json_gives_empties():
    row = OracleIndexedAPIModel(
        api_id="api-9",
        url="https://example.com/api",
        name="Example",
        directory_type="openapi",
        compatibility_tier="native",
        status="indexed",
        capabilities_json="{broken",
        tags_json='{"not": "a-list"}',
    )
    back = c.indexed_api_model_to_schema(row)
    assert back.capabilities == []
    assert back.tags == []


def test_registration_result_round_trip_and_empty_id():
    result = RegistrationResult(
        directory_url="https://example.com/dir",
        directory_type=DirectoryType.WELL_KNOWN,
        status=OracleStatus.REGISTERED,
        registration_id=None,
        message="ok",
    )
    row = c.registration_result_to_model(result)
    assert isinstance(row, OracleRegistrationModel)
    assert row.registration_id == ""
    back = c.registration_model_to_schema(row)
    assert back.registration_id is None
    assert back.status == OracleStatus.REGISTERED


# --- security scans ----------------------------------------------------------


def _vuln(vuln_id="v-1", severity=RTSeverity.HIGH):
    return Vulnerability(
        vuln_id=vuln_id,
        scan_id="s-1",
        category=AttackCategory.INJECTION,
        severity=severity,
        title="t",
        description="d",
        endpoint="/api/x",
        method="GET",
        evidence={"payload": "x"},
        remediation="fix it",
        remediation_status=RemediationStatus.OPEN,
        discovered_at=_utc(),
    )


def test_vulnerability_round_trip():
    row = c.vulnerability_to_model(_vuln())
    assert isinstance(row, SecurityVulnerabilityModel)
    back = c.vulnerability_model_to_schema(row)
    assert back.vuln_id == "v-1"
    assert back.evidence == {"payload": "x"}
    assert back.method == "GET"


def test_vulnerability_model_to_schema_evidence_edge_cases():
    bad = SecurityVulnerabilityModel(
        vuln_id="v-bad",
        scan_id="s-1",
        category="injection",
        severity="high",
        title="t",
        endpoint="/e",
        evidence_json="{broken",
    )
    assert c.vulnerability_model_to_schema(bad).evidence == {}

    listed = SecurityVulnerabilityModel(
        vuln_id="v-list",
        scan_id="s-1",
        category="injection",
        severity="low",
        title="t",
        endpoint="/e",
        evidence_json='["a"]',
    )
    assert c.vulnerability_model_to_schema(listed).evidence == {"value": ["a"]}


def test_scan_report_to_scan_model_and_back():
    report = ScanReport(
        scan_id="s-1",
        status=ScanStatus.COMPLETED,
        started_at=_utc(2026, 1, 1, 3, 4, 5),
        completed_at=_utc(2026, 1, 2, 3, 4, 6),
        target_services=["svc-a"],
        attack_categories=["injection"],
        intensity="standard",
        total_tests_run=10,
        total_passed=8,
        total_failed=2,
        vulnerabilities_found=2,
        vulnerabilities=[_vuln("v-1", RTSeverity.HIGH), _vuln("v-2", RTSeverity.LOW)],
        recommendations=["patch"],
        score=80.0,
    )
    scan_row = c.scan_report_to_scan_model(report)
    assert scan_row.scan_type == "internal"
    assert scan_row.tenant_id is None

    vuln_rows = [c.vulnerability_to_model(v) for v in report.vulnerabilities]
    rebuilt = c.scan_model_to_report(scan_row, vuln_rows)
    assert rebuilt.vulnerabilities_found == 2
    assert rebuilt.severity_breakdown == {"high": 1, "low": 1}
    assert rebuilt.duration_seconds == 86401.0
    assert rebuilt.total_passed == 8
    assert rebuilt.recommendations == ["patch"]


def test_scan_model_to_report_missing_times_and_counts():
    scan = SecurityScanModel(
        scan_id="s-2",
        status="completed",
        started_at=None,
        completed_at=None,
        total_passed=None,
        total_failed=None,
    )
    rebuilt = c.scan_model_to_report(scan, [])
    assert rebuilt.started_at == scan.created_at
    assert rebuilt.duration_seconds is None
    assert rebuilt.total_passed == 0
    assert rebuilt.total_failed == 0
    assert rebuilt.vulnerabilities_found == 0


# --- content factory ---------------------------------------------------------


def test_content_piece_round_trip():
    piece = GeneratedContent(
        content_id="c-1",
        pipeline_id="p-1",
        format=ContentFormat.TEXT_POST,
        title="hello",
        download_url="https://example.com/f",
        status=ContentStatus.READY,
        generated_at=_utc(),
        metadata={"k": "v"},
    )
    row = c.content_piece_to_model(piece)
    assert isinstance(row, ContentPieceModel)
    back = c.content_piece_model_to_schema(row)
    assert back.content_id == "c-1"
    assert back.format == ContentFormat.TEXT_POST
    assert back.metadata == {"k": "v"}


def test_schedule_recommendation_round_trip():
    rec = ScheduleRecommendation(
        content_id="c-1",
        platform="tiktok",
        recommended_time=_utc(),
        confidence=0.7,
        reasoning="peak hour",
        estimated_views=100,
    )
    row = c.schedule_recommendation_to_model(rec, "sched-1")
    assert isinstance(row, ContentScheduleModel)
    assert row.schedule_id == "sched-1"
    back = c.schedule_model_to_recommendation(row)
    assert back.content_id == "c-1"
    assert back.confidence == 0.7
    assert back.estimated_views == 100
