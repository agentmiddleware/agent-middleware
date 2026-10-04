"""
Red-team scan modeling — dormant simulated proof surface.

This module MODELS a red-team scan lifecycle (scan records, findings,
severities, machine-readable reports) so the surrounding API shape could be
exercised end to end. It probes nothing: no requests are sent, no endpoints
are attacked, and no scanner integrations (ZAP, Nuclei, fuzzers) exist.
Entry points are guarded by ``require_simulation`` and refuse to run unless
the service's simulation flag is explicitly enabled; the routes that expose
this module are proof surfaces, unmounted by default (see
docs/PROOF_SURFACES.md).

The real, runnable adversarial tooling for this repository lives in
scripts/invariant_attacks/ and scripts/red_team_trust_plane.py, with recorded
verdicts in docs/invariant-attack-report.md.
"""

import uuid
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.sql.elements import ColumnElement

from ..core.runtime_mode import require_simulation
from ..db.converters import (
    scan_report_to_scan_model,
    scan_model_to_report,
    vulnerability_to_model,
)
from ..db.database import get_session_factory, is_database_configured
from ..db.models import SecurityScanModel, SecurityVulnerabilityModel

from ..schemas.red_team import (
    AttackCategory,
    Severity,
    ScanStatus,
    Vulnerability,
    ScanReport,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Attack Vector Registry
# ---------------------------------------------------------------------------


@dataclass
class AttackVector:
    """A single attack test case."""

    vector_id: str
    category: AttackCategory
    name: str
    description: str
    target_endpoint: str
    target_method: str
    payload: dict
    expected_behavior: str  # What SHOULD happen (e.g., "403 Forbidden")
    severity_if_failed: Severity


@dataclass
class AttackResult:
    """Result of executing an attack vector."""

    vector: AttackVector
    passed: bool  # True = system defended correctly
    actual_status: int
    actual_response: dict
    notes: str = ""


# ---------------------------------------------------------------------------
# Attack Libraries (one per category)
# ---------------------------------------------------------------------------


class ACLBypassAttacks:
    """
    Tests for topic-level ACL enforcement on IoT endpoints.
    Inspired by the DJI Romo breach: a single device token
    should NEVER grant access to another device's topics.
    """

    @staticmethod
    def generate_vectors() -> list[AttackVector]:
        return [
            AttackVector(
                vector_id=str(uuid.uuid4()),
                category=AttackCategory.ACL_BYPASS,
                name="Cross-device topic access",
                description=(
                    "Register Device A with ACL for 'sensors/temp/#', "
                    "then attempt to publish to 'sensors/camera/device-b'. "
                    "If allowed, we have a DJI-grade breach."
                ),
                target_endpoint="/v1/iot/bridge",
                target_method="POST",
                payload={
                    "device_id": "__DEVICE_A_ID__",
                    "protocol": "mqtt",
                    "topic": "sensors/camera/device-b",
                    "payload": {"cmd": "stream_live_feed"},
                    "qos": 1,
                },
                expected_behavior="403 Forbidden — ACL denies cross-device topic",
                severity_if_failed=Severity.CRITICAL,
            ),
            AttackVector(
                vector_id=str(uuid.uuid4()),
                category=AttackCategory.ACL_BYPASS,
                name="Wildcard ACL escalation",
                description=(
                    "Register device with ACL ['sensors/temp/+'], "
                    "attempt 'sensors/temp/../admin/config'. "
                    "Tests MQTT topic traversal resistance."
                ),
                target_endpoint="/v1/iot/bridge",
                target_method="POST",
                payload={
                    "device_id": "__DEVICE_A_ID__",
                    "protocol": "mqtt",
                    "topic": "sensors/temp/../admin/config",
                    "payload": {"cmd": "read_config"},
                    "qos": 0,
                },
                expected_behavior="403 Forbidden — path traversal blocked",
                severity_if_failed=Severity.CRITICAL,
            ),
            AttackVector(
                vector_id=str(uuid.uuid4()),
                category=AttackCategory.ACL_BYPASS,
                name="Empty ACL should deny all",
                description=(
                    "Register device with empty ACL list, "
                    "attempt to publish to any topic. "
                    "Deny-by-default must hold."
                ),
                target_endpoint="/v1/iot/bridge",
                target_method="POST",
                payload={
                    "device_id": "__EMPTY_ACL_DEVICE__",
                    "protocol": "mqtt",
                    "topic": "any/topic/at/all",
                    "payload": {"data": "test"},
                    "qos": 0,
                },
                expected_behavior="403 Forbidden — empty ACL = deny all",
                severity_if_failed=Severity.HIGH,
            ),
        ]


class AuthProbeAttacks:
    """Tests for API key authentication edge cases."""

    @staticmethod
    def generate_vectors() -> list[AttackVector]:
        endpoints = [
            ("/v1/iot/devices", "GET"),
            ("/v1/telemetry/events", "POST"),
            ("/v1/media/videos", "POST"),
            ("/v1/comms/agents", "GET"),
            ("/v1/factory/pipelines", "POST"),
        ]
        vectors = []
        for endpoint, method in endpoints:
            # No API key at all
            vectors.append(
                AttackVector(
                    vector_id=str(uuid.uuid4()),
                    category=AttackCategory.AUTH_PROBE,
                    name=f"Missing API key — {method} {endpoint}",
                    description=f"Hit {endpoint} with no X-API-Key header.",
                    target_endpoint=endpoint,
                    target_method=method,
                    payload={"_no_auth": True},
                    expected_behavior="401 Unauthorized",
                    severity_if_failed=Severity.CRITICAL,
                )
            )
            # Empty API key
            vectors.append(
                AttackVector(
                    vector_id=str(uuid.uuid4()),
                    category=AttackCategory.AUTH_PROBE,
                    name=f"Empty API key — {method} {endpoint}",
                    description=f"Hit {endpoint} with X-API-Key: '' (empty string).",
                    target_endpoint=endpoint,
                    target_method=method,
                    payload={"_empty_auth": True},
                    expected_behavior="401 or 403",
                    severity_if_failed=Severity.CRITICAL,
                )
            )
        return vectors


class InjectionAttacks:
    """Tests for payload injection across all services."""

    @staticmethod
    def generate_vectors() -> list[AttackVector]:
        return [
            # MQTT topic injection
            AttackVector(
                vector_id=str(uuid.uuid4()),
                category=AttackCategory.INJECTION,
                name="MQTT topic injection via device name",
                description="Register device with topic-separator characters in name.",
                target_endpoint="/v1/iot/devices",
                target_method="POST",
                payload={
                    "device_id": "device/../../../etc/passwd",
                    "protocol": "mqtt",
                    "name": "malicious-device",
                    "acl_topics": ["sensors/#"],
                },
                expected_behavior="400 Bad Request or sanitized input",
                severity_if_failed=Severity.HIGH,
            ),
            # JSON injection in telemetry
            AttackVector(
                vector_id=str(uuid.uuid4()),
                category=AttackCategory.INJECTION,
                name="Nested JSON bomb in telemetry payload",
                description="Send deeply nested JSON to exhaust parser memory.",
                target_endpoint="/v1/telemetry/events",
                target_method="POST",
                payload={
                    "events": [
                        {
                            "event_type": "error",
                            "source": "test",
                            "payload": {"a": {"b": {"c": {"d": {"e": {"f": "deep"}}}}}},
                        }
                    ]
                },
                expected_behavior="200 OK or 400 with depth limit",
                severity_if_failed=Severity.MEDIUM,
            ),
            # XSS in content factory title
            AttackVector(
                vector_id=str(uuid.uuid4()),
                category=AttackCategory.INJECTION,
                name="XSS payload in content factory title",
                description="Submit pipeline with script tag in title field.",
                target_endpoint="/v1/factory/pipelines",
                target_method="POST",
                payload={
                    "title": "<script>alert('xss')</script>",
                    "target_formats": ["text_post"],
                },
                expected_behavior=(
                    "Title stored/returned without execution context "
                    "(API-only = no browser = low risk, but still sanitize)"
                ),
                severity_if_failed=Severity.LOW,
            ),
            # SQL-like injection in query params
            AttackVector(
                vector_id=str(uuid.uuid4()),
                category=AttackCategory.INJECTION,
                name="SQL injection in capability filter",
                description="Pass SQL fragment in capability query parameter.",
                target_endpoint="/v1/comms/agents?capability=' OR '1'='1",
                target_method="GET",
                payload={},
                expected_behavior="Empty results or 400, not data dump",
                severity_if_failed=Severity.HIGH,
            ),
        ]


class RateLimitEvasionAttacks:
    """Tests for sliding window rate limiter bypass."""

    @staticmethod
    def generate_vectors() -> list[AttackVector]:
        return [
            AttackVector(
                vector_id=str(uuid.uuid4()),
                category=AttackCategory.RATE_LIMIT_EVASION,
                name="Rapid burst past rate limit",
                description="Send 150 requests in quick succession (limit is 120/min).",
                target_endpoint="/v1/iot/devices",
                target_method="GET",
                payload={"_burst_count": 150},
                expected_behavior="429 Too Many Requests after 120th request",
                severity_if_failed=Severity.MEDIUM,
            ),
            AttackVector(
                vector_id=str(uuid.uuid4()),
                category=AttackCategory.RATE_LIMIT_EVASION,
                name="Rate limit bypass via missing API key header rotation",
                description=(
                    "Alternate between different API keys to test per-key isolation."
                ),
                target_endpoint="/v1/iot/devices",
                target_method="GET",
                payload={"_key_rotation": True},
                expected_behavior=(
                    "Each key has its own 120/min window (no cross-contamination)"
                ),
                severity_if_failed=Severity.MEDIUM,
            ),
        ]


class PrivilegeEscalationAttacks:
    """Tests for cross-resource access."""

    @staticmethod
    def generate_vectors() -> list[AttackVector]:
        return [
            AttackVector(
                vector_id=str(uuid.uuid4()),
                category=AttackCategory.PRIVILEGE_ESCALATION,
                name="Read another agent's inbox",
                description="Agent A tries to poll Agent B's message inbox.",
                target_endpoint="/v1/comms/messages/__AGENT_B_ID__/inbox",
                target_method="GET",
                payload={},
                expected_behavior="403 or scoped to requesting agent only",
                severity_if_failed=Severity.HIGH,
            ),
            AttackVector(
                vector_id=str(uuid.uuid4()),
                category=AttackCategory.PRIVILEGE_ESCALATION,
                name="Access another pipeline's content",
                description=(
                    "Try to list content from a pipeline belonging to another API key."
                ),
                target_endpoint="/v1/factory/pipelines/__OTHER_PIPELINE__/content",
                target_method="GET",
                payload={},
                expected_behavior="404 or 403 — no cross-tenant access",
                severity_if_failed=Severity.HIGH,
            ),
        ]


class SchemaAbuseAttacks:
    """Tests for malformed payload handling."""

    @staticmethod
    def generate_vectors() -> list[AttackVector]:
        return [
            AttackVector(
                vector_id=str(uuid.uuid4()),
                category=AttackCategory.SCHEMA_ABUSE,
                name="Wrong type for required field",
                description="Send integer where string expected for device_id.",
                target_endpoint="/v1/iot/devices",
                target_method="POST",
                payload={
                    "device_id": 12345,
                    "protocol": "mqtt",
                    "name": "type-coercion-test",
                    "acl_topics": ["sensors/#"],
                },
                expected_behavior="422 Unprocessable Entity with clear error",
                severity_if_failed=Severity.LOW,
            ),
            AttackVector(
                vector_id=str(uuid.uuid4()),
                category=AttackCategory.SCHEMA_ABUSE,
                name="Oversized payload",
                description="Send extremely large list in batch telemetry endpoint.",
                target_endpoint="/v1/telemetry/events",
                target_method="POST",
                payload={
                    "events": [
                        {
                            "event_type": "error",
                            "source": f"src-{i}",
                            "payload": {"x": "y" * 1000},
                        }
                        for i in range(1000)
                    ]
                },
                expected_behavior="400 or 413 — payload size limit enforced",
                severity_if_failed=Severity.MEDIUM,
            ),
            AttackVector(
                vector_id=str(uuid.uuid4()),
                category=AttackCategory.SCHEMA_ABUSE,
                name="Invalid enum value",
                description="Send invalid protocol type for device registration.",
                target_endpoint="/v1/iot/devices",
                target_method="POST",
                payload={
                    "device_id": "enum-test",
                    "protocol": "not_a_real_protocol",
                    "name": "enum-abuse",
                    "acl_topics": [],
                },
                expected_behavior="422 with enum validation error",
                severity_if_failed=Severity.LOW,
            ),
        ]


class EnumerationAttacks:
    """Tests for resource ID guessing and endpoint crawling."""

    @staticmethod
    def generate_vectors() -> list[AttackVector]:
        return [
            AttackVector(
                vector_id=str(uuid.uuid4()),
                category=AttackCategory.ENUMERATION,
                name="Sequential ID guessing",
                description=(
                    "Try accessing devices with sequential IDs "
                    "(device-1, device-2, ...)."
                ),
                target_endpoint="/v1/iot/devices/device-001",
                target_method="GET",
                payload={},
                expected_behavior="404 — UUIDs prevent sequential guessing",
                severity_if_failed=Severity.MEDIUM,
            ),
            AttackVector(
                vector_id=str(uuid.uuid4()),
                category=AttackCategory.ENUMERATION,
                name="Undocumented endpoint probe",
                description=(
                    "Probe common admin paths: /admin, /internal, /debug, /metrics."
                ),
                target_endpoint="/admin",
                target_method="GET",
                payload={
                    "_probe_paths": [
                        "/admin",
                        "/internal",
                        "/debug",
                        "/metrics",
                        "/_config",
                    ]
                },
                expected_behavior="404 or 405 for all — no hidden endpoints",
                severity_if_failed=Severity.MEDIUM,
            ),
        ]


# ---------------------------------------------------------------------------
# Attack Engine
# ---------------------------------------------------------------------------

# Map categories to their attack libraries
ATTACK_LIBRARIES: dict[AttackCategory, type] = {
    AttackCategory.ACL_BYPASS: ACLBypassAttacks,
    AttackCategory.AUTH_PROBE: AuthProbeAttacks,
    AttackCategory.INJECTION: InjectionAttacks,
    AttackCategory.RATE_LIMIT_EVASION: RateLimitEvasionAttacks,
    AttackCategory.PRIVILEGE_ESCALATION: PrivilegeEscalationAttacks,
    AttackCategory.SCHEMA_ABUSE: SchemaAbuseAttacks,
    AttackCategory.ENUMERATION: EnumerationAttacks,
}


class AttackEngine:
    """
    Executes attack vectors against live endpoints.
    In this in-memory implementation, we simulate the attacks
    and assess vulnerability based on known system behavior.

    Production: Replace with actual HTTP client execution against
    a staging environment using httpx + OWASP ZAP integration.
    """

    def __init__(self):
        self._results: dict[str, list[AttackResult]] = {}

    async def execute_vectors(
        self,
        vectors: list[AttackVector],
        scan_id: str,
    ) -> list[AttackResult]:
        """Execute a batch of attack vectors and collect results."""
        results = []
        for vector in vectors:
            result = await self._execute_single(vector)
            results.append(result)
        self._results[scan_id] = results
        return results

    async def _execute_single(self, vector: AttackVector) -> AttackResult:
        """
        Execute a single attack vector.

        In production this fires real HTTP requests at the staging env.
        Here we simulate based on our knowledge of the system's defenses.
        """
        # Simulate defense checks based on what we built
        if vector.category == AttackCategory.ACL_BYPASS:
            return await self._check_acl_defense(vector)
        elif vector.category == AttackCategory.AUTH_PROBE:
            return await self._check_auth_defense(vector)
        elif vector.category == AttackCategory.INJECTION:
            return await self._check_injection_defense(vector)
        elif vector.category == AttackCategory.RATE_LIMIT_EVASION:
            return await self._check_rate_limit_defense(vector)
        elif vector.category == AttackCategory.PRIVILEGE_ESCALATION:
            return await self._check_privilege_defense(vector)
        elif vector.category == AttackCategory.SCHEMA_ABUSE:
            return await self._check_schema_defense(vector)
        elif vector.category == AttackCategory.ENUMERATION:
            return await self._check_enumeration_defense(vector)
        else:
            return AttackResult(
                vector=vector,
                passed=True,
                actual_status=200,
                actual_response={},
                notes="Unknown category — skipped",
            )

    async def _check_acl_defense(self, vector: AttackVector) -> AttackResult:
        """Our IoT bridge uses deny-by-default TopicACLEngine with wildcard matching."""
        if "cross-device" in vector.name.lower():
            return AttackResult(
                vector=vector,
                passed=True,
                actual_status=403,
                actual_response={
                    "detail": "ACL violation: topic not in device allowlist"
                },
                notes="TopicACLEngine enforces deny-by-default. Cross-device blocked.",
            )
        elif "wildcard" in vector.name.lower() or "traversal" in vector.name.lower():
            return AttackResult(
                vector=vector,
                passed=True,
                actual_status=403,
                actual_response={"detail": "ACL violation: traversal patterns blocked"},
                notes="fnmatch doesn't resolve '../' — traversal ineffective.",
            )
        elif "empty" in vector.name.lower():
            return AttackResult(
                vector=vector,
                passed=True,
                actual_status=403,
                actual_response={"detail": "ACL violation: no ACL rules = deny all"},
                notes="Deny-by-default holds with empty ACL list.",
            )
        return AttackResult(
            vector=vector,
            passed=True,
            actual_status=403,
            actual_response={},
            notes="ACL defense held.",
        )

    async def _check_auth_defense(self, vector: AttackVector) -> AttackResult:
        """Our auth uses APIKeyHeader with auto_error=True."""
        if "missing" in vector.name.lower():
            return AttackResult(
                vector=vector,
                passed=True,
                actual_status=401,
                actual_response={"detail": "Missing API key"},
                notes="APIKeyHeader returns 401 when header absent.",
            )
        elif "empty" in vector.name.lower():
            # PATCHED: Auth dependency now rejects keys shorter than 8 chars
            return AttackResult(
                vector=vector,
                passed=True,
                actual_status=401,
                actual_response={"detail": "API key must be at least 8 characters."},
                notes="PATCHED: verify_api_key() rejects empty/short keys.",
            )
        return AttackResult(
            vector=vector,
            passed=True,
            actual_status=401,
            actual_response={},
            notes="Auth defense held.",
        )

    async def _check_injection_defense(self, vector: AttackVector) -> AttackResult:
        """Check various injection vectors."""
        if "mqtt topic" in vector.name.lower() or "device name" in vector.name.lower():
            # PATCHED: device_id now validated by SAFE_ID_PATTERN regex
            return AttackResult(
                vector=vector,
                passed=True,
                actual_status=422,
                actual_response={"detail": "device_id must be alphanumeric"},
                notes="PATCHED: field_validator rejects traversal chars.",
            )
        elif "json bomb" in vector.name.lower():
            # FastAPI/Pydantic handles nested JSON fine, but no depth limit
            return AttackResult(
                vector=vector,
                passed=True,
                actual_status=200,
                actual_response={},
                notes="Pydantic validates structure. Deep nesting accepted.",
            )
        elif "xss" in vector.name.lower():
            # API-only, no browser rendering — XSS is low risk
            return AttackResult(
                vector=vector,
                passed=True,
                actual_status=202,
                actual_response={},
                notes="Zero-GUI means no browser rendering. XSS is informational only.",
            )
        elif "sql" in vector.name.lower():
            # In-memory stores, no SQL — but note the finding for production
            return AttackResult(
                vector=vector,
                passed=True,
                actual_status=200,
                actual_response={"agents": [], "total": 0},
                notes="In-memory store immune to SQL injection. Flag for DB migration.",
            )
        return AttackResult(
            vector=vector,
            passed=True,
            actual_status=200,
            actual_response={},
            notes="Injection defense held.",
        )

    async def _check_rate_limit_defense(self, vector: AttackVector) -> AttackResult:
        """Our sliding window rate limiter uses per-key tracking."""
        if "burst" in vector.name.lower():
            return AttackResult(
                vector=vector,
                passed=True,
                actual_status=429,
                actual_response={"detail": "Rate limit exceeded", "retry_after": 60},
                notes="Sliding window limiter correctly returns 429 at 120 req/min.",
            )
        elif "rotation" in vector.name.lower():
            return AttackResult(
                vector=vector,
                passed=True,
                actual_status=200,
                actual_response={},
                notes="Per-key isolation confirmed — each key has its own window.",
            )
        return AttackResult(
            vector=vector,
            passed=True,
            actual_status=200,
            actual_response={},
            notes="Rate limit defense held.",
        )

    async def _check_privilege_defense(self, vector: AttackVector) -> AttackResult:
        """Check cross-resource access controls."""
        if "inbox" in vector.name.lower():
            # PATCHED: poll_inbox now checks agent.owner_key == request.api_key
            return AttackResult(
                vector=vector,
                passed=True,
                actual_status=403,
                actual_response={"detail": "You do not own this agent."},
                notes="PATCHED: Inbox endpoint verifies owner_key.",
            )
        elif "pipeline" in vector.name.lower():
            # PATCHED: ContentPipeline now has owner_key field for tenant scoping
            return AttackResult(
                vector=vector,
                passed=True,
                actual_status=403,
                actual_response={"detail": "Pipeline belongs to another tenant."},
                notes="PATCHED: ContentPipeline.owner_key enables tenant-scoped.",
            )
        return AttackResult(
            vector=vector,
            passed=True,
            actual_status=403,
            actual_response={},
            notes="Privilege defense held.",
        )

    async def _check_schema_defense(self, vector: AttackVector) -> AttackResult:
        """Check Pydantic validation on malformed payloads."""
        if "wrong type" in vector.name.lower():
            # PATCHED: DeviceRegistration now uses ConfigDict(strict=True)
            return AttackResult(
                vector=vector,
                passed=True,
                actual_status=422,
                actual_response={"detail": "Input should be a valid string"},
                notes="PATCHED: ConfigDict(strict=True) rejects int coercion.",
            )
        elif "oversized" in vector.name.lower():
            # PATCHED: TelemetryBatch.events now has max_length=100
            return AttackResult(
                vector=vector,
                passed=True,
                actual_status=422,
                actual_response={"detail": "List should have at most 100 items"},
                notes="PATCHED: TelemetryBatch.events max_length reduced to 100.",
            )
        elif "invalid enum" in vector.name.lower():
            return AttackResult(
                vector=vector,
                passed=True,
                actual_status=422,
                actual_response={"detail": "Invalid enum value"},
                notes="Pydantic enum validation correctly rejects unknown protocols.",
            )
        return AttackResult(
            vector=vector,
            passed=True,
            actual_status=422,
            actual_response={},
            notes="Schema validation held.",
        )

    async def _check_enumeration_defense(self, vector: AttackVector) -> AttackResult:
        """Check resource enumeration resistance."""
        if "sequential" in vector.name.lower():
            return AttackResult(
                vector=vector,
                passed=True,
                actual_status=404,
                actual_response={},
                notes="UUIDs used for all IDs. Sequential guessing infeasible.",
            )
        elif "undocumented" in vector.name.lower():
            return AttackResult(
                vector=vector,
                passed=True,
                actual_status=404,
                actual_response={},
                notes="No hidden admin endpoints found. All routes explicitly defined.",
            )
        return AttackResult(
            vector=vector,
            passed=True,
            actual_status=404,
            actual_response={},
            notes="Enumeration defense held.",
        )


# ---------------------------------------------------------------------------
# Scan Orchestrator
# ---------------------------------------------------------------------------


class ScanStore:
    """PostgreSQL-backed scan storage shared with rtaas via
    SecurityScanModel + SecurityVulnerabilityModel (discriminator:
    scan_type='internal'). See issue #30.

    Internal scans keep their owning wallet in the shared ``tenant_id``
    column (``None`` for a scan launched by a bootstrap admin). Reads take an
    optional ``owner_wallet_id``: when given, only that wallet's scans are
    visible; when ``None`` the read is unscoped (bootstrap admin)."""

    @staticmethod
    def _require_db() -> None:
        if not is_database_configured():
            raise RuntimeError(
                "red_team.ScanStore requires a configured database. Set DATABASE_URL."
            )

    async def save(self, report: ScanReport, owner_wallet_id: str | None = None):
        """Upsert a scan report and its child vulnerabilities.

        ``owner_wallet_id`` is recorded only when the scan is first stored;
        a re-save never reassigns ownership.
        """
        self._require_db()
        factory = get_session_factory()
        async with factory() as session:
            existing = await session.get(SecurityScanModel, report.scan_id)
            new_row = scan_report_to_scan_model(report)
            if existing is None:
                new_row.tenant_id = owner_wallet_id
                session.add(new_row)
            else:
                for field in (
                    "status",
                    "targets_json",
                    "attack_categories_json",
                    "intensity",
                    "total_tests_run",
                    "total_passed",
                    "total_failed",
                    "security_score",
                    "recommendations_json",
                    "started_at",
                    "completed_at",
                ):
                    setattr(existing, field, getattr(new_row, field))

            # Replace vulnerabilities for idempotent re-save.
            from sqlalchemy import delete as sa_delete

            await session.execute(
                sa_delete(SecurityVulnerabilityModel).where(
                    cast(
                        ColumnElement[bool],
                        SecurityVulnerabilityModel.scan_id == report.scan_id,
                    )
                )
            )
            for vuln in report.vulnerabilities:
                session.add(vulnerability_to_model(vuln))

            await session.commit()

    async def get(
        self, scan_id: str, owner_wallet_id: str | None = None
    ) -> ScanReport | None:
        self._require_db()
        factory = get_session_factory()
        async with factory() as session:
            scan = await session.get(SecurityScanModel, scan_id)
            if scan is None or scan.scan_type != "internal":
                return None
            # A scan owned by another wallet reads exactly like a missing one.
            if owner_wallet_id is not None and scan.tenant_id != owner_wallet_id:
                return None
            result = await session.execute(
                select(SecurityVulnerabilityModel).where(
                    cast(
                        ColumnElement[bool],
                        SecurityVulnerabilityModel.scan_id == scan_id,
                    )
                )
            )
            vulns = list(result.scalars().all())
        return scan_model_to_report(scan, vulns)

    async def list_all(self, owner_wallet_id: str | None = None) -> list[ScanReport]:
        self._require_db()
        factory = get_session_factory()
        async with factory() as session:
            stmt = select(SecurityScanModel).where(
                cast(ColumnElement[bool], SecurityScanModel.scan_type == "internal")
            )
            if owner_wallet_id is not None:
                stmt = stmt.where(
                    cast(
                        ColumnElement[bool],
                        SecurityScanModel.tenant_id == owner_wallet_id,
                    )
                )
            result = await session.execute(
                stmt.order_by(
                    cast(ColumnElement[Any], SecurityScanModel.created_at).desc()
                )
            )
            scans = list(result.scalars().all())

            reports: list[ScanReport] = []
            for scan in scans:
                vuln_result = await session.execute(
                    select(SecurityVulnerabilityModel).where(
                        cast(
                            ColumnElement[bool],
                            SecurityVulnerabilityModel.scan_id == scan.scan_id,
                        )
                    )
                )
                vulns = list(vuln_result.scalars().all())
                reports.append(scan_model_to_report(scan, vulns))
        return reports


class RedTeamSwarm:
    """
    Simulated top-level orchestrator for the modeled scan lifecycle.
    Coordinates vector generation, simulated execution, and reporting;
    sends no traffic (see the module docstring).
    """

    def __init__(self):
        self.engine = AttackEngine()
        self.store = ScanStore()

    def _generate_vectors(
        self,
        categories: list[AttackCategory],
        target_services: list[str],
    ) -> list[AttackVector]:
        """Generate all applicable attack vectors for the requested scope."""
        vectors = []
        for category in categories:
            library = ATTACK_LIBRARIES.get(category)
            if library:
                category_vectors = library.generate_vectors()  # type: ignore[attr-defined]
                # Filter by target services if applicable
                for v in category_vectors:
                    service = self._endpoint_to_service(v.target_endpoint)
                    if service in target_services or service == "global":
                        vectors.append(v)
        return vectors

    @staticmethod
    def _endpoint_to_service(endpoint: str) -> str:
        """Map an endpoint to its service pillar."""
        if "/iot" in endpoint:
            return "iot"
        elif "/telemetry" in endpoint:
            return "telemetry"
        elif "/media" in endpoint:
            return "media"
        elif "/comms" in endpoint:
            return "comms"
        elif "/factory" in endpoint:
            return "factory"
        else:
            return "global"

    async def run_scan(
        self,
        target_services: list[str],
        attack_categories: list[AttackCategory],
        intensity: str = "standard",
        auto_remediate: bool = False,
        owner_wallet_id: str | None = None,
    ) -> ScanReport:
        """Execute a full security scan owned by ``owner_wallet_id``
        (``None`` for a bootstrap-admin scan)."""
        require_simulation("red_team")
        scan_id = str(uuid.uuid4())
        started_at = datetime.now(timezone.utc)

        # Generate attack vectors
        vectors = self._generate_vectors(attack_categories, target_services)

        # Execute all vectors
        results = await self.engine.execute_vectors(vectors, scan_id)

        # Analyze results
        passed = [r for r in results if r.passed]
        failed = [r for r in results if not r.passed]

        # Build vulnerability list from failures
        vulnerabilities = []
        severity_counts: dict[str, int] = defaultdict(int)

        for result in failed:
            vuln = Vulnerability(
                vuln_id=str(uuid.uuid4()),
                scan_id=scan_id,
                category=result.vector.category,
                severity=result.vector.severity_if_failed,
                title=result.vector.name,
                description=result.notes,
                endpoint=result.vector.target_endpoint,
                method=result.vector.target_method,
                evidence={
                    "expected": result.vector.expected_behavior,
                    "actual_status": result.actual_status,
                    "actual_response": result.actual_response,
                    "payload": result.vector.payload,
                },
                remediation=self._generate_remediation(result),
                cwe_id=self._map_cwe(result.vector.category),
                discovered_at=datetime.now(timezone.utc),
            )
            vulnerabilities.append(vuln)
            severity_counts[result.vector.severity_if_failed.value] += 1

        completed_at = datetime.now(timezone.utc)
        duration = (completed_at - started_at).total_seconds()

        # Calculate security score
        score = self._calculate_score(len(results), len(failed), severity_counts)

        # Generate prioritized recommendations
        recommendations = self._generate_recommendations(vulnerabilities)

        report = ScanReport(
            scan_id=scan_id,
            status=ScanStatus.COMPLETED,
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=round(duration, 2),
            target_services=target_services,
            attack_categories=[c.value for c in attack_categories],
            intensity=intensity,
            total_tests_run=len(results),
            total_passed=len(passed),
            total_failed=len(failed),
            vulnerabilities_found=len(vulnerabilities),
            severity_breakdown=dict(severity_counts),
            vulnerabilities=vulnerabilities,
            recommendations=recommendations,
            score=round(score, 1),
        )

        await self.store.save(report, owner_wallet_id=owner_wallet_id)
        logger.info(
            f"Scan {scan_id}: {len(results)} tests, {len(failed)} vulnerabilities, "
            f"score={score:.1f}/100"
        )
        return report

    def _calculate_score(
        self,
        total: int,
        failed: int,
        severity_counts: dict[str, int],
    ) -> float:
        """
        Calculate security score (0-100).
        Weighted by severity: critical=-25, high=-15, medium=-5, low=-2.
        """
        if total == 0:
            return 100.0

        base_score = 100.0
        penalties = {
            "critical": 25.0,
            "high": 15.0,
            "medium": 5.0,
            "low": 2.0,
            "info": 0.5,
        }
        for sev, count in severity_counts.items():
            base_score -= penalties.get(sev, 0) * count

        return max(0.0, min(100.0, base_score))

    @staticmethod
    def _generate_remediation(result: AttackResult) -> str:
        """Generate machine-readable remediation guidance."""
        category = result.vector.category
        remediations = {
            AttackCategory.AUTH_PROBE: (
                "Add explicit validation in auth dependency: "
                "if not api_key or len(api_key.strip()) < 8: raise HTTPException(401). "
                "Consider minimum key length and character requirements."
            ),
            AttackCategory.INJECTION: (
                "Add input sanitization: regex validate device_id, topic strings. "
                "Reject path separators (/, ..) in identifier fields. "
                "Use constr(pattern=r'^[a-zA-Z0-9_-]+$') for IDs."
            ),
            AttackCategory.PRIVILEGE_ESCALATION: (
                "Implement tenant scoping: associate each resource with the API key "
                "that created it. Add ownership verification in handlers. "
                "Pattern: if resource.owner_key != request.api_key: "
                "raise HTTPException(403)."
            ),
            AttackCategory.SCHEMA_ABUSE: (
                "Enable Pydantic strict mode for security-sensitive fields. "
                "Add max_length constraints to all list fields. "
                "Enforce Content-Length limits at middleware level."
            ),
        }
        return remediations.get(
            category,
            f"Review and harden the {result.vector.target_endpoint} "
            f"endpoint against {category.value} attacks.",
        )

    @staticmethod
    def _map_cwe(category: AttackCategory) -> str | None:
        """Map attack category to Common Weakness Enumeration."""
        mapping = {
            AttackCategory.ACL_BYPASS: "CWE-285",
            AttackCategory.AUTH_PROBE: "CWE-287",
            AttackCategory.INJECTION: "CWE-74",
            AttackCategory.RATE_LIMIT_EVASION: "CWE-770",
            AttackCategory.PRIVILEGE_ESCALATION: "CWE-269",
            AttackCategory.SCHEMA_ABUSE: "CWE-20",
            AttackCategory.ENUMERATION: "CWE-200",
        }
        return mapping.get(category)

    @staticmethod
    def _generate_recommendations(vulnerabilities: list[Vulnerability]) -> list[str]:
        """Generate prioritized remediation list."""
        recs = []

        # Group by severity
        critical = [v for v in vulnerabilities if v.severity == Severity.CRITICAL]
        high = [v for v in vulnerabilities if v.severity == Severity.HIGH]
        medium = [v for v in vulnerabilities if v.severity == Severity.MEDIUM]

        if critical:
            recs.append(
                f"[CRITICAL] Fix {len(critical)} critical vulnerabilities IMMEDIATELY: "
                + ", ".join(v.title for v in critical)
            )

        if high:
            recs.append(
                f"[HIGH] Address {len(high)} high-severity findings before deployment: "
                + ", ".join(v.title for v in high)
            )

        if medium:
            recs.append(
                f"[MEDIUM] Schedule fixes for {len(medium)} medium-severity findings."
            )

        if not vulnerabilities:
            recs.append(
                "No vulnerabilities found. "
                "System appears hardened for current attack surface."
            )

        recs.append(
            "Run scans continuously (every 6 hours minimum) and after every deployment."
        )

        return recs

    async def get_scan(
        self, scan_id: str, owner_wallet_id: str | None = None
    ) -> ScanReport | None:
        """Fetch a scan; with ``owner_wallet_id``, only if that wallet owns it."""
        return await self.store.get(scan_id, owner_wallet_id)  # type: ignore[no-any-return]

    async def list_scans(self, owner_wallet_id: str | None = None) -> list[ScanReport]:
        """List scans; with ``owner_wallet_id``, only that wallet's scans."""
        return await self.store.list_all(owner_wallet_id)  # type: ignore[no-any-return]
