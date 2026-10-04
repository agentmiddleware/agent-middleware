"""
Schemas for the Autonomous Product Manager / Telemetry service.
Ingests raw telemetry, identifies anomalies, and outputs actionable code fixes.
"""

from pydantic import BaseModel, Field
from enum import Enum
from datetime import datetime


class TelemetryEventType(str, Enum):
    """Categories of telemetry events the system can ingest."""

    ERROR = "error"
    WARNING = "warning"
    SESSION = "session"
    API_CALL = "api_call"
    LLM_TRACE = "llm_trace"
    PERFORMANCE = "performance"
    CUSTOM = "custom"


class Severity(str, Enum):
    """Severity levels for anomalies and issues."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class TelemetryEvent(BaseModel):
    """A single telemetry event to ingest."""

    event_type: TelemetryEventType
    source: str = Field(
        ...,
        description="Originating service or module name.",
        examples=["iot-bridge", "media-engine", "auth-service"],
    )
    message: str = Field(
        ...,
        description="Human/agent-readable description of the event.",
    )
    severity: Severity = Field(default=Severity.INFO)
    stack_trace: str | None = Field(
        None,
        description="Full stack trace for error events.",
    )
    metadata: dict = Field(
        default_factory=dict,
        description="Structured context (request_id, user_agent, latency_ms, etc.).",
    )
    timestamp: datetime | None = Field(
        None,
        description="Event timestamp. Server will assign one if omitted.",
    )


class TelemetryBatch(BaseModel):
    """Batch submission of telemetry events for efficient ingestion."""

    events: list[TelemetryEvent] = Field(
        ...,
        min_length=1,
        max_length=100,
        description=(
            "Array of telemetry events. Max 100 per batch. "
            "RED TEAM FIX: Reduced from 1000 to prevent memory exhaustion attacks. "
            "For higher throughput, send multiple batches."
        ),
    )


class TelemetryBatchResponse(BaseModel):
    """Confirmation of batch ingestion."""

    ingested: int
    failed: int
    batch_id: str
    errors: list[dict] = Field(
        default_factory=list,
        description="Details of any events that failed validation.",
    )


class AnomalyReport(BaseModel):
    """An anomaly detected by the Autonomous PM from telemetry analysis."""

    anomaly_id: str
    severity: Severity
    category: str = Field(
        ...,
        description=(
            "Classification of the anomaly (e.g., 'error_spike', "
            "'latency_regression', 'missing_feature')."
        ),
    )
    summary: str = Field(
        ...,
        description="Agent-readable summary of the issue.",
    )
    affected_endpoints: list[str] = Field(
        default_factory=list,
        description="API endpoints or services affected.",
    )
    event_count: int = Field(
        ...,
        description="Number of telemetry events contributing to this anomaly.",
    )
    first_seen: datetime
    last_seen: datetime
    suggested_fix: str | None = Field(
        default=None,
        description="LLM-generated code fix suggestion in diff format.",
    )
    auto_pr_url: str | None = Field(
        default=None,
        description="URL of the auto-generated pull request, if AUTO_PR_ENABLED.",
    )


class AnomalyListResponse(BaseModel):
    """Paginated anomaly listing."""

    anomalies: list[AnomalyReport]
    total: int
    page: int
    per_page: int


class AutoPRRequest(BaseModel):
    """Request the Autonomous PM to generate and push a fix."""

    anomaly_id: str = Field(
        ...,
        description="The anomaly to generate a fix for.",
    )
    target_repo: str | None = Field(
        None,
        description="Git remote URL. Uses system default if omitted.",
    )
    branch_name: str | None = Field(
        None,
        description="Branch name for the PR. Auto-generated if omitted.",
    )
    dry_run: bool = Field(
        default=True,
        description="If true, returns the proposed diff without pushing.",
    )


class AutoPRResponse(BaseModel):
    """Result of an autonomous pull request generation."""

    anomaly_id: str
    pr_url: str | None = Field(
        None,
        description=(
            "URL of a created PR. Always null: the generator is simulated "
            "and never opens a PR."
        ),
    )
    diff: str = Field(
        ...,
        description="The proposed code changes in unified diff format.",
    )
    files_changed: list[str]
    tests_passed: bool | None = Field(
        None,
        description="Whether tests passed. Null: no test suite is run.",
    )
    status: str = Field(
        ...,
        description='"dry_run", or "simulated" when dry_run=false.',
    )
