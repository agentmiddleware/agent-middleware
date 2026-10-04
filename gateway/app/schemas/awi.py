"""
Schemas for Agentic Web Interface (AWI) — Phase 7
===================================================
Based on arXiv:2506.10953v1 - "Build the web for agents, not agents for the web"

Provides stateful, standardized interfaces for web agents with:
- Higher-level unified actions (not DOM clicks)
- Progressive information transfer (agent specifies format/resolution)
- Agentic task queues with concurrency limits and human pause/steer
"""

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class AWISessionStatus(str, Enum):
    """Status of an AWI session."""

    CREATED = "created"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AWIRepresentationType(str, Enum):
    """Types of progressive representations an agent can request."""

    FULL_DOM = "full_dom"
    SUMMARY = "summary"
    EMBEDDING = "embedding"
    LOW_RES_SCREENSHOT = "low_res_screenshot"
    ACCESSIBILITY_TREE = "accessibility_tree"
    JSON_STRUCTURE = "json_structure"
    TEXT_EXTRACTION = "text_extraction"


class AWIActionCategory(str, Enum):
    """Categories of standardized AWI actions."""

    NAVIGATION = "navigation"
    SEARCH = "search"
    INTERACTION = "interaction"
    EXTRACTION = "extraction"
    TRANSACTION = "transaction"
    AUTH = "auth"


class AWIStandardAction(str, Enum):
    """Standardized higher-level actions (from paper's unified action vocabulary)."""

    SEARCH_AND_SORT = "search_and_sort"
    ADD_TO_CART = "add_to_cart"
    CHECKOUT = "checkout"
    FILL_FORM = "fill_form"
    LOGIN = "login"
    LOGOUT = "logout"
    NAVIGATE_TO = "navigate_to"
    CLICK_BUTTON = "click_button"
    SCROLL = "scroll"
    SELECT_OPTION = "select_option"
    UPLOAD_FILE = "upload_file"
    EXTRACT_DATA = "extract_data"
    GET_REPRESENTATION = "get_representation"


class AWIActionTier(str, Enum):
    """How directly an action expresses AWI semantic intent."""

    SEMANTIC = "semantic"
    COMPATIBILITY = "compatibility"


class AWIActionStatus(str, Enum):
    """Maturity status for AWI action contracts."""

    STABLE = "stable"
    PROVISIONAL = "provisional"
    DEPRECATED = "deprecated"


class AWIActionRiskLevel(str, Enum):
    """Risk level for policy and human-approval decisions."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AWIActionDefinitionResponse(BaseModel):
    """Public action vocabulary entry exposed by AWI discovery surfaces."""

    action: AWIStandardAction
    category: AWIActionCategory
    description: str
    parameters: dict[str, dict[str, Any]]
    required_preconditions: list[str]
    postconditions: list[str]
    estimated_cost: float
    tier: AWIActionTier
    status: AWIActionStatus
    risk_level: AWIActionRiskLevel
    sensitive_parameters: list[str] = Field(default_factory=list)


class AWIManifestTransport(BaseModel):
    """Transport metadata for the AWI discovery manifest."""

    primary: str
    mcp_compatible: bool
    mcp_manifest: str


class AWIManifestSafetyCapabilities(BaseModel):
    """Safety capabilities advertised by the AWI discovery manifest."""

    wallet_scoped_authorization: bool
    human_intervention: list[str]
    passkey_high_risk_actions: bool
    signed_permits: bool
    tamper_evident_audit_chain: bool
    sensitive_parameter_redaction: bool


class AWIDiscoveryManifest(BaseModel):
    """Draft AWI-over-MCP discovery manifest."""

    schema_version: str
    awi_version: str
    status: str
    profile: str
    surface: str = Field(
        default="proof_surface",
        description=(
            "Discovery honesty label. AWI HTTP routes are proof-surface "
            "scaffolding unless invoked through governed MCP."
        ),
    )
    transport: AWIManifestTransport
    description: str
    endpoints: dict[str, str]
    representation_types: list[AWIRepresentationType]
    actions: list[AWIActionDefinitionResponse]
    safety_capabilities: AWIManifestSafetyCapabilities
    known_limitations: list[str]


class AWISessionCreate(BaseModel):
    """Request to create an AWI session."""

    target_url: str = Field(..., description="Target website URL")
    wallet_id: str | None = Field(None, description="Wallet for billing")
    max_steps: int = Field(
        default=100, ge=1, le=1000, description="Max steps per session"
    )
    timeout_seconds: int = Field(
        default=300, ge=10, le=3600, description="Session timeout"
    )
    allow_human_pause: bool = Field(
        default=True, description="Allow human intervention"
    )
    representation_preference: AWIRepresentationType = Field(
        default=AWIRepresentationType.SUMMARY,
        description="Preferred representation type for state",
    )


class AWISession(BaseModel):
    """An active AWI session with stateful context."""

    session_id: str = Field(..., description="Unique session identifier")
    target_url: str
    wallet_id: str | None = Field(None, description="Wallet that owns this session")
    status: AWISessionStatus
    created_at: datetime
    updated_at: datetime
    current_url: str | None = None
    step_count: int = 0
    max_steps: int
    representation_history: list[dict[str, Any]] = Field(default_factory=list)
    action_history: list[dict[str, Any]] = Field(default_factory=list)
    human_pause_enabled: bool = True
    paused_by_human: bool = False
    pause_reason: str | None = None
    timeout_seconds: int = 300  # Session timeout in seconds

    @property
    def expires_at(self) -> datetime:
        """Calculate session expiration time."""
        return self.updated_at + timedelta(seconds=self.timeout_seconds)

    @property
    def is_expired(self) -> bool:
        """Check if session has expired."""
        return datetime.now(timezone.utc) > self.expires_at


class AWIExecutionRequest(BaseModel):
    """Request to execute an AWI action."""

    session_id: str = Field(..., description="AWI session ID")
    action: AWIStandardAction = Field(..., description="Standardized action to execute")
    parameters: dict[str, Any] = Field(
        default_factory=dict, description="Action parameters"
    )
    representation_request: AWIRepresentationType | None = Field(
        None,
        description="Request specific state representation after action",
    )
    dry_run: bool = Field(default=False, description="Simulate without side effects")


class AWIExecutionResponse(BaseModel):
    """Response from AWI action execution."""

    execution_id: str
    session_id: str
    action: AWIStandardAction
    status: str
    parameters: dict[str, Any]
    result: dict[str, Any] | None = None
    effect_status: str | None = Field(
        default=None,
        description="not_dispatched for a rejected action; unknown when browser effects cannot be excluded",
    )
    error: str | None = None
    new_state: dict[str, Any] | None = None
    representation: dict[str, Any] | None = None
    duration_ms: int | None = None
    cost_estimate: float | None = None


class AWIRepresentationRequest(BaseModel):
    """Request a specific representation of the current state."""

    session_id: str
    representation_type: AWIRepresentationType
    options: dict[str, Any] = Field(
        default_factory=dict,
        description="Representation options (e.g., max_length, embedding_model)",
    )


class AWIRepresentationResponse(BaseModel):
    """Response with requested representation."""

    representation_id: str
    session_id: str
    representation_type: AWIRepresentationType
    content: Any
    metadata: dict[str, Any]
    generated_at: datetime


class AWITaskStatus(str, Enum):
    """Status of an AWI task in the queue."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    CANCELLED = "cancelled"


class AWITaskCreate(BaseModel):
    """Create a new AWI task in the queue."""

    task_type: str = Field(..., description="Type of AWI task")
    target_url: str = Field(..., description="Target URL")
    action_sequence: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Sequence of AWI actions to execute",
    )
    priority: int = Field(
        default=5, ge=1, le=10, description="Task priority (1=highest)"
    )
    max_concurrent: int = Field(
        default=1, ge=1, le=10, description="Max concurrent steps"
    )
    callback_url: str | None = Field(
        None, description="Webhook for completion notification"
    )


class AWITask(BaseModel):
    """An AWI task in the queue."""

    task_id: str
    task_type: str
    target_url: str
    status: AWITaskStatus
    priority: int
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    current_action_index: int = 0
    total_actions: int
    action_sequence: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Sequence of AWI actions to execute",
    )
    result: dict[str, Any] | None = None
    error: str | None = None


class AWITaskQueueStatus(BaseModel):
    """Status of the AWI task queue."""

    total_pending: int = 0
    total_running: int = 0
    total_completed: int = 0
    total_failed: int = 0
    current_throughput: float = 0.0
    avg_task_duration_ms: float = 0.0
    queue: list[AWITask] = Field(default_factory=list)


class AWIHumanIntervention(BaseModel):
    """Request human intervention in an AWI session."""

    session_id: str
    action: str = Field(..., description="intervene, resume, or steer")
    reason: str | None = Field(None, description="Reason for intervention")
    steer_instructions: str | None = Field(
        None, description="New instructions if steering"
    )


class AWISessionState(BaseModel):
    """Full state of an AWI session for recovery."""

    session_id: str
    target_url: str
    current_url: str
    cookies: dict[str, str]
    local_storage: dict[str, str]
    session_storage: dict[str, str]
    form_data: dict[str, str]
    step_count: int
    action_history: list[dict[str, Any]]
    representation_history: list[dict[str, Any]]
    created_at: datetime
    updated_at: datetime
