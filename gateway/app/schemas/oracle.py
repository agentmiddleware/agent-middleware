"""
Schemas for the Agent Oracle Infiltration Service.

The Oracle crawls agent directories, indexes API capabilities,
computes compatibility scores, and registers our API in external
agent networks for inbound discovery traffic.

Think: SEO for the agentic web. If agents can't find you, you don't exist.
"""

from pydantic import BaseModel, Field, field_validator
from enum import Enum
from datetime import datetime
import re


class OracleStatus(str, Enum):
    """Status of an oracle crawl/registration."""

    PENDING = "pending"
    CRAWLING = "crawling"
    INDEXED = "indexed"
    REGISTERED = "registered"
    FAILED = "failed"
    STALE = "stale"


class DirectoryType(str, Enum):
    """Types of agent directories the Oracle can infiltrate."""

    WELL_KNOWN = "well_known"  # /.well-known/agent.json endpoints
    LLM_TXT = "llm_txt"  # /llm.txt documentation
    OPENAPI = "openapi"  # OpenAPI spec crawling
    AGENT_REGISTRY = "agent_registry"  # Centralized agent registries
    PLUGIN_STORE = "plugin_store"  # Plugin/tool marketplaces
    MCP_SERVER = "mcp_server"  # Model Context Protocol servers


class CompatibilityTier(str, Enum):
    """How well an external API fits this control plane."""

    NATIVE = "native"  # Direct API-to-API, zero adaptation needed
    COMPATIBLE = "compatible"  # Minor translation layer required
    BRIDGEABLE = "bridgeable"  # Needs our IoT/Comms bridge
    INCOMPATIBLE = "incompatible"


# --- Crawl Target Schemas ---

SAFE_URL_PATTERN = re.compile(r"^https?://[a-zA-Z0-9]")


class CrawlTargetRequest(BaseModel):
    """Submit a URL for the Oracle to crawl and index."""

    url: str = Field(
        ...,
        description="Base URL of the API or agent directory to crawl.",
        examples=["https://api.openai.com", "https://api.anthropic.com"],
    )
    directory_type: DirectoryType = Field(
        default=DirectoryType.WELL_KNOWN,
        description="Type of directory to look for at this URL.",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Custom tags for categorization.",
        examples=[["ai", "llm", "agent-tools"]],
    )
    priority: int = Field(
        default=5,
        ge=1,
        le=10,
        description="Crawl priority (1=lowest, 10=highest).",
    )

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        if not SAFE_URL_PATTERN.match(v):
            raise ValueError("URL must start with http:// or https://")
        if len(v) > 2048:
            raise ValueError("URL must be under 2048 characters")
        return v.rstrip("/")


class CrawlTargetResponse(BaseModel):
    """Result of submitting a crawl target."""

    target_id: str
    url: str
    directory_type: DirectoryType
    status: OracleStatus
    queued_at: datetime


# --- Indexed API Schemas ---


class IndexedCapability(BaseModel):
    """A single capability discovered by the Oracle crawler."""

    name: str
    description: str
    endpoint: str | None = None
    method: str | None = Field(None, examples=["GET", "POST"])
    auth_required: bool = True


class IndexedAPI(BaseModel):
    """An API that has been crawled and indexed by the Oracle."""

    api_id: str
    url: str
    name: str
    description: str
    directory_type: DirectoryType
    capabilities: list[IndexedCapability] = Field(default_factory=list)
    compatibility_tier: CompatibilityTier
    compatibility_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="How well this API complements this control plane (0-1).",
    )
    tags: list[str] = Field(default_factory=list)
    last_crawled: datetime
    status: OracleStatus


class OracleCrawlTargetRecord(BaseModel):
    """One durable crawl target row as returned by ``GET /v1/oracle/index``."""

    target_id: str
    url: str
    domain: str = Field(
        ...,
        description="Host extracted from url (lowercase), for filtering and display.",
    )
    directory_type: str
    status: str
    api_id: str | None = None
    queued_at: datetime
    crawled_at: datetime | None = None
    raw_payload_hash: str | None = Field(
        None,
        description="SHA-256 of canonical crawl payload when SIMULATION_MODE_ORACLE is false.",
    )


class IndexedAPIListResponse(BaseModel):
    """Paginated list of indexed APIs and/or durable crawl rows."""

    apis: list[IndexedAPI]
    total: int = Field(
        ...,
        description="Total rows matching the active query (indexed APIs or crawl targets).",
    )
    filters_applied: dict = Field(default_factory=dict)
    limit: int = Field(50, ge=1, le=200)
    offset: int = Field(0, ge=0)
    crawl_targets: list[OracleCrawlTargetRecord] = Field(
        default_factory=list,
        description=(
            "When ``domain`` is passed to ``GET /v1/oracle/index``, populated from "
            "``oracle_crawl_targets``; otherwise empty."
        ),
    )


# --- Registration Schemas ---


class RegistrationTarget(BaseModel):
    """An external directory where we want to register our API."""

    directory_url: str = Field(
        ...,
        description="URL of the agent directory/registry to register with.",
    )
    directory_type: DirectoryType
    registration_payload: dict = Field(
        default_factory=dict,
        description="Custom payload for this directory's registration format.",
    )


class RegistrationRequest(BaseModel):
    """Request to register our API in external agent networks."""

    targets: list[RegistrationTarget] = Field(
        ...,
        min_length=1,
        max_length=50,
        description="External directories to register with.",
    )
    profile: dict = Field(
        default_factory=dict,
        description="Override fields in our agent profile for this registration.",
    )


class RegistrationResult(BaseModel):
    """Result of registering with a single directory."""

    directory_url: str
    directory_type: DirectoryType
    status: OracleStatus
    registration_id: str | None = None
    message: str = ""


class RegistrationResponse(BaseModel):
    """Aggregated registration results."""

    results: list[RegistrationResult]
    total_attempted: int
    total_registered: int
    total_failed: int


# --- Ranking & Analytics Schemas ---


class VisibilityScore(BaseModel):
    """Our API's visibility score across agent networks."""

    overall_score: float = Field(
        ...,
        ge=0.0,
        le=100.0,
        description="Composite visibility score (0-100).",
    )
    directories_registered: int
    directories_crawled: int
    inbound_discovery_requests: int = Field(
        default=0,
        description="Number of times external agents have discovered us.",
    )
    top_referrers: list[dict] = Field(
        default_factory=list,
        description="Top directories sending discovery traffic.",
    )
    compatibility_map: dict[str, int] = Field(
        default_factory=dict,
        description="Count of indexed APIs by compatibility tier.",
    )
    recommendations: list[str] = Field(
        default_factory=list,
        description="Actionable recommendations to improve visibility.",
    )


class NetworkGraphNode(BaseModel):
    """A node in the agent network graph."""

    node_id: str
    name: str
    url: str
    node_type: str = Field(
        ...,
        description="'self', 'indexed_api', or 'directory'",
    )
    compatibility_tier: CompatibilityTier | None = None
    connections: list[str] = Field(
        default_factory=list,
        description="IDs of connected nodes.",
    )


class NetworkGraphResponse(BaseModel):
    """The agent network graph centered on our API."""

    nodes: list[NetworkGraphNode]
    edges: list[dict]
    total_nodes: int
    total_edges: int
    center_node: str = Field(
        ...,
        description="Node ID of our API (the center of the graph).",
    )
