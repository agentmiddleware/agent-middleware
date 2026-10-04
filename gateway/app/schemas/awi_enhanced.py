"""
Schemas for Phase 9 AWI Enhanced Features
===========================================

Based on arXiv:2506.10953v1 gap analysis:
- Passkey/WebAuthn models for biometric authentication
- DOM Bridge models for bidirectional AWI↔DOM translation
- RAG Memory models for semantic search over sessions
"""

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.core.time import utc_now


class PasskeyStatus(str, Enum):
    """Status of a passkey verification."""

    PENDING = "pending"
    VERIFIED = "verified"
    FAILED = "failed"
    EXPIRED = "expired"


class DOMSessionStatus(str, Enum):
    """Status of a DOM bridge session."""

    ACTIVE = "active"
    CLOSED = "closed"
    ERROR = "error"


class DOMRepresentationType(str, Enum):
    """Types of state representations."""

    SUMMARY = "summary"
    ACCESSIBILITY_TREE = "accessibility_tree"
    JSON_STRUCTURE = "json_structure"
    FULL_DOM = "full_dom"


class TranslationMode(str, Enum):
    """Browser automation mode."""

    PLAYWRIGHT = "playwright"
    CDP_DIRECT = "cdp_direct"
    SELENIUM_COMPAT = "selenium_compat"


class CommandType(str, Enum):
    """Types of Playwright commands."""

    CLICK = "click"
    FILL = "fill"
    SELECT = "select"
    PRESS = "press"
    HOVER = "hover"
    SCROLL = "scroll"
    GOTO = "goto"
    UPLOAD = "upload_file"
    EVALUATE = "evaluate"


# ─────────────────────────────────────────────────────────────────────────────
# Passkey / WebAuthn Models
# ─────────────────────────────────────────────────────────────────────────────


class PasskeyChallengeRequest(BaseModel):
    """Request a WebAuthn challenge for high-risk action verification."""

    session_id: str = Field(..., description="AWI session ID requiring verification")
    action: str = Field(..., description="Action requiring passkey")


class PasskeyChallengeResponse(BaseModel):
    """WebAuthn challenge response with credential options."""

    challenge_id: str = Field(..., description="Unique challenge identifier")
    challenge: str = Field(..., description="Base64URL-encoded challenge bytes")
    rp_id: str = Field(..., description="Relying Party ID (domain)")
    rp_name: str = Field(..., description="Relying Party name")
    timeout: int = Field(..., description="Challenge timeout in milliseconds")
    user_verification: str = Field(
        default="required", description="User verification requirement"
    )
    public_key_cred_params: list[dict[str, Any]] = Field(
        ..., description="Supported public key algorithms"
    )
    exclude_credentials: list[dict[str, Any]] = Field(
        default_factory=list, description="Credentials to exclude"
    )
    authenticator_selection: dict[str, Any] = Field(
        ..., description="Authenticator requirements"
    )


class PasskeyVerifyRequest(BaseModel):
    """Verify WebAuthn credential response from client."""

    challenge_id: str = Field(..., description="Challenge ID from challenge response")
    credential: dict[str, Any] = Field(
        ...,
        description="WebAuthn credential response (from navigator.credentials.get())",
    )


class PasskeyVerifyResponse(BaseModel):
    """Response after successful passkey verification."""

    verified: bool = Field(..., description="Whether verification succeeded")
    challenge_id: str = Field(..., description="Challenge that was verified")
    session_id: str = Field(..., description="AWI session ID")
    action: str = Field(..., description="Action that was verified")
    verified_at: str = Field(..., description="ISO timestamp of verification")
    expires_in_seconds: int = Field(
        default=300, description="Verification validity period"
    )


class PasskeySessionStatus(BaseModel):
    """Check verification status for a session:action pair."""

    session_id: str = Field(..., description="AWI session ID")
    action: str = Field(..., description="Action to check")
    is_verified: bool = Field(..., description="Whether action is verified")
    verified_at: Optional[str] = Field(
        None, description="When verified (if applicable)"
    )
    expires_in_seconds: Optional[int] = Field(
        None, description="Time until expiry (if verified)"
    )


# ─────────────────────────────────────────────────────────────────────────────
# DOM Bridge Models
# ─────────────────────────────────────────────────────────────────────────────


class DOMBridgeSessionRequest(BaseModel):
    """Create a new Playwright bridge session."""

    target_url: str = Field(
        ...,
        description="URL to open in the browser",
        examples=["https://example.com/shop"],
    )
    wallet_id: Optional[str] = Field(None, description="Wallet that owns this session")
    headless: bool = Field(default=True, description="Run browser in headless mode")
    # Bounded: the size feeds a real browser context, and an unbounded one
    # lets a caller exhaust memory on render/screenshot. Keep in step with
    # the bridge-side check in AWIPlaywrightBridge.create_session.
    viewport_width: int = Field(
        default=1280, ge=320, le=3840, description="Viewport width in pixels"
    )
    viewport_height: int = Field(
        default=720, ge=240, le=2160, description="Viewport height in pixels"
    )


class DOMBridgeSessionResponse(BaseModel):
    """Response after creating a DOM bridge session."""

    session_id: str = Field(..., description="Browser session ID")
    current_url: str = Field(..., description="Current page URL")


class DOMElementInfo(BaseModel):
    """Information about a DOM element."""

    tag: str = Field(..., description="HTML tag name")
    text_content: str = Field(default="", description="Text content")
    attributes: dict[str, str] = Field(
        default_factory=dict, description="HTML attributes"
    )
    css_selector: str = Field(..., description="Generated CSS selector")
    xpath: str = Field(..., description="XPath expression")
    is_interactive: bool = Field(default=False, description="Is clickable/form element")
    role: Optional[str] = Field(None, description="ARIA role")
    label: Optional[str] = Field(None, description="Associated label")


class PlaywrightCommandInfo(BaseModel):
    """Information about a Playwright command."""

    command_type: str = Field(..., description="Command type (click, fill, etc.)")
    target: str = Field(..., description="CSS selector or XPath target")
    value: Optional[Any] = Field(
        None, description="Command value (text, selector, etc.)"
    )
    options: dict[str, Any] = Field(default_factory=dict, description="Command options")


class DOMSyncRequest(BaseModel):
    """Execute an AWI action against real browser DOM."""

    session_id: str = Field(..., description="Browser session ID")
    action: str = Field(
        ...,
        description="AWI action name (e.g., 'search_and_sort', 'add_to_cart')",
    )
    parameters: dict[str, Any] = Field(
        default_factory=dict, description="Action parameters"
    )
    return_screenshot: bool = Field(
        default=False, description="Include screenshot in response"
    )


class DOMSyncResponse(BaseModel):
    """Response from DOM sync execution."""

    status: str = "success"
    effect_status: str | None = None
    session_id: str = Field(..., description="Browser session ID")
    execution_id: str = Field(..., description="Unique execution ID")
    action: str = Field(..., description="Action that was executed")
    commands_generated: int = Field(
        ..., description="Number of Playwright commands generated"
    )
    commands_executed: int = Field(
        ..., description="Number of commands successfully executed"
    )
    new_url: Optional[str] = Field(None, description="URL after execution")
    state_representation: dict[str, Any] = Field(
        ..., description="AWI state representation of result"
    )
    error: Optional[str] = Field(None, description="Error message if failed")
    screenshot_url: Optional[str] = Field(
        default=None, description="Screenshot URL if requested"
    )


class DOMStateRequest(BaseModel):
    """Request current DOM state as AWI representation."""

    representation_type: DOMRepresentationType = Field(
        default=DOMRepresentationType.SUMMARY,
        description="Type of representation to generate",
    )
    include_elements: bool = Field(default=True, description="Include element list")


class DOMStateResponse(BaseModel):
    """DOM state as AWI representation."""

    session_id: str
    url: str
    title: str
    representation_type: DOMRepresentationType
    page_type: str = Field(
        ..., description="Classified page type (shopping, login, etc.)"
    )
    main_content: str
    interactive_elements: list[DOMElementInfo] = Field(default_factory=list)
    forms: list[dict[str, Any]] = Field(default_factory=list)
    navigation: list[dict[str, str]] = Field(default_factory=list)
    screenshot_url: Optional[str] = None


class DOMActionPreviewRequest(BaseModel):
    """Preview what commands will be generated for an action."""

    session_id: str
    action: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class DOMActionPreviewResponse(BaseModel):
    """Preview of commands for an action."""

    session_id: str
    action: str
    commands: list[PlaywrightCommandInfo]
    estimated_duration_ms: int
    elements_found: dict[str, int] = Field(
        default_factory=dict, description="Count of elements by semantic type"
    )


# ─────────────────────────────────────────────────────────────────────────────
# RAG Memory Models
# ─────────────────────────────────────────────────────────────────────────────


class MemoryIndexRequest(BaseModel):
    """Index a completed AWI session for semantic search."""

    session_id: str = Field(..., description="AWI session ID")
    session_type: str = Field(
        ...,
        description="Session type (shopping, form_filling, authentication, etc.)",
    )
    action_history: list[dict[str, Any]] = Field(
        default_factory=list,
        description="List of actions taken in the session",
    )
    state_snapshots: list[dict[str, Any]] = Field(
        default_factory=list,
        description="State representations captured during session",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Additional metadata to store"
    )


class MemoryIndexResponse(BaseModel):
    """Response after indexing a session."""

    memory_id: str = Field(..., description="Unique memory identifier")
    session_id: str = Field(..., description="AWI session ID")
    indexed_at: datetime = Field(default_factory=utc_now)
    entities_extracted: int = Field(..., description="Number of entities extracted")
    intent_inferred: str = Field(..., description="Inferred user intent")


class RAGQueryRequest(BaseModel):
    """Semantic search query over session memories."""

    query: str = Field(
        ...,
        min_length=1,
        pattern=r"\S",
        description="Natural language query containing at least one non-whitespace character",
        examples=[
            "shopping for laptops last week",
            "form submissions involving addresses",
        ],
    )
    session_type: Optional[str] = Field(
        None,
        description="Filter by session type",
    )
    top_k: int = Field(
        default=5, ge=1, le=20, description="Number of results to return"
    )
    similarity_threshold: float = Field(
        default=0.7, ge=0.0, le=1.0, description="Minimum similarity score"
    )
    include_raw_state: bool = Field(
        default=False, description="Include full state data in results"
    )


class MemorySearchResult(BaseModel):
    """A single memory search result."""

    memory_id: str
    session_id: str
    session_type: str
    user_intent: str
    action_sequence: list[str] = Field(default_factory=list)
    key_entities: list[str] = Field(default_factory=list)
    similarity_score: float
    created_at: datetime
    accessed_at: datetime
    access_count: int = 0
    raw_state: Optional[dict[str, Any]] = None


class RAGQueryResponse(BaseModel):
    """Response from RAG query."""

    query: str
    results: list[MemorySearchResult]
    total_found: int
    search_time_ms: Optional[int] = None


class SessionContextRequest(BaseModel):
    """Get context from past sessions for current session."""

    current_session_id: str = Field(..., description="Session requesting context")
    current_state: dict[str, Any] = Field(
        default_factory=dict, description="Current session state"
    )
    session_type: Optional[str] = Field(None, description="Infer session type")
    top_k: int = Field(default=3, ge=1, le=10, description="Number of similar sessions")


class SessionContextResponse(BaseModel):
    """Context from past sessions."""

    current_session_id: str
    current_session_type: str
    relevant_past_sessions: list[dict[str, Any]]
    suggested_next_actions: list[str]
    common_patterns: list[str]


class MemoryDeleteRequest(BaseModel):
    """Delete a memory from the index."""

    memory_id: str = Field(..., description="Memory ID to delete")
    cascade: bool = Field(
        default=False, description="Delete all memories for the session"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Integration Models
# ─────────────────────────────────────────────────────────────────────────────


class AWIEnhancedSessionCreate(BaseModel):
    """Create AWI session with Phase 9 features enabled."""

    target_url: str
    wallet_id: Optional[str] = None
    max_steps: int = Field(default=100, ge=1, le=1000)
    timeout_seconds: int = Field(default=300, ge=10, le=3600)
    allow_human_pause: bool = Field(default=True)
    enable_passkey_gate: bool = Field(
        default=True,
        description="Require passkey for high-risk actions",
    )
    enable_dom_bridge: bool = Field(
        default=False,
        description="Enable real browser automation (requires Playwright)",
    )
    enable_rag_indexing: bool = Field(
        default=True,
        description="Index session for semantic search",
    )


class AWIEnhancedExecutionRequest(BaseModel):
    """Execute action with Phase 9 features."""

    session_id: str
    action: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    passkey_verified: bool = Field(
        default=False,
        description="Skip passkey check (already verified by client)",
    )
    bypass_dom_bridge: bool = Field(
        default=False,
        description="Use mock DOM instead of real browser",
    )
