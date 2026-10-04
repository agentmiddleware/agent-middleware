"""
Schemas for the Red Team Security Swarm.
Defines attack vectors, vulnerability reports, and scan orchestration
for autonomous penetration testing of all 33+ API paths.

Philosophy: If your own agents can't break it, external agents won't either.
But if they CAN break it, you find out before 7,000 vacuum cameras go live.
"""

from pydantic import BaseModel, Field
from enum import Enum
from datetime import datetime


class AttackCategory(str, Enum):
    """Categories of automated attack vectors."""

    ACL_BYPASS = "acl_bypass"
    AUTH_PROBE = "auth_probe"
    INJECTION = "injection"
    RATE_LIMIT_EVASION = "rate_limit_evasion"
    PRIVILEGE_ESCALATION = "privilege_escalation"
    SCHEMA_ABUSE = "schema_abuse"
    ENUMERATION = "enumeration"
    REPLAY = "replay"
    DOS_PATTERN = "dos_pattern"


class Severity(str, Enum):
    CRITICAL = "critical"  # Data exposure, full auth bypass
    HIGH = "high"  # Partial auth bypass, ACL leak
    MEDIUM = "medium"  # Information disclosure, enumeration
    LOW = "low"  # Best practice violations
    INFO = "info"  # Observations, no direct risk


class ScanStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class RemediationStatus(str, Enum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    PATCHED = "patched"
    ACCEPTED_RISK = "accepted_risk"


# --- Vulnerability ---


class Vulnerability(BaseModel):
    """A discovered security vulnerability."""

    vuln_id: str
    scan_id: str
    category: AttackCategory
    severity: Severity
    title: str = Field(..., description="Short description of the finding.")
    description: str = Field(
        ...,
        description=(
            "Detailed technical explanation including attack vector and impact."
        ),
    )
    endpoint: str = Field(..., description="The affected API path.")
    method: str = Field(..., description="HTTP method (GET, POST, etc.).")
    evidence: dict = Field(
        default_factory=dict,
        description="Proof-of-concept: request payload, response status, headers, etc.",
    )
    remediation: str = Field(
        ...,
        description="Recommended fix. Agents read this to auto-generate patches.",
    )
    remediation_status: RemediationStatus = RemediationStatus.OPEN
    cwe_id: str | None = Field(
        None,
        description=(
            "Common Weakness Enumeration ID (e.g. CWE-285 for improper authorization)."
        ),
    )
    discovered_at: datetime


class VulnerabilityListResponse(BaseModel):
    vulnerabilities: list[Vulnerability]
    total: int
    critical_count: int
    high_count: int


# --- Scan ---


class ScanRequest(BaseModel):
    """Initiate a Red Team scan."""

    target_services: list[str] = Field(
        default=["iot", "telemetry", "media", "comms", "factory"],
        description="Which service pillars to attack. Default: all.",
    )
    attack_categories: list[AttackCategory] = Field(
        default=[c for c in AttackCategory],
        description="Which attack vectors to deploy. Default: all.",
    )
    intensity: str = Field(
        default="standard",
        description=(
            "Scan intensity: 'quick' (surface-level), 'standard' (thorough), "
            "'aggressive' (full fuzzing)."
        ),
    )
    auto_remediate: bool = Field(
        default=False,
        description=(
            "If true, the swarm will attempt to auto-generate patches for "
            "discovered vulnerabilities."
        ),
    )


class ScanResponse(BaseModel):
    """Response after initiating a scan."""

    scan_id: str
    status: ScanStatus
    target_services: list[str]
    attack_categories: list[str]
    intensity: str
    estimated_duration_seconds: int
    total_attack_vectors: int


class ScanReport(BaseModel):
    """Full scan report."""

    scan_id: str
    status: ScanStatus
    started_at: datetime
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    target_services: list[str]
    attack_categories: list[str]
    intensity: str
    total_tests_run: int
    total_passed: int
    total_failed: int
    vulnerabilities_found: int
    severity_breakdown: dict = Field(
        default_factory=dict,
        description="Count per severity level: {critical: N, high: N, ...}",
    )
    vulnerabilities: list[Vulnerability] = Field(default_factory=list)
    recommendations: list[str] = Field(
        default_factory=list,
        description="Prioritized remediation recommendations.",
    )
    score: float = Field(
        ...,
        ge=0.0,
        le=100.0,
        description="Security score: 100 = fortress, 0 = DJI Romo vacuum.",
    )


class ScanListResponse(BaseModel):
    scans: list[ScanReport]
    total: int
