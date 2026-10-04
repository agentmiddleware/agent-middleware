"""Machine-readable discovery endpoints for autonomous agents.

Provides project-defined endpoints clients can use to discover the governed MCP
gateway, its currently available tools, pricing, and integration guidance.
"""

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from typing import Any, Optional

from ..core.auth import reject_anonymous_production_catalog, verify_api_key
from ..core.config import get_settings
from ..core.product_positioning import POSITIONING_DESCRIPTION
from ..core.rate_limiter import rate_limit_discovery
from .well_known import get_agent_first_metadata

router = APIRouter(
    prefix="/v1",
    tags=["Agent Discovery"],
)

settings = get_settings()


def _is_stripe_configured() -> bool:
    """Check if Stripe is configured (same truth as /health/dependencies)."""
    return bool(get_settings().STRIPE_SECRET_KEY)


class ServiceCapability(BaseModel):
    name: str
    version: str
    description: str
    category: str
    requires_auth: bool = True
    surface: str = Field(
        default="product",
        description=(
            "'product' = consequential-action transaction-integrity boundary; "
            "'proof_surface' = labeled demo/workload scaffolding (often simulated)."
        ),
    )
    simulation_default: bool | None = Field(
        default=None,
        description="When true, this surface defaults to simulation mode.",
    )


class MCPToolInfo(BaseModel):
    service_id: str
    name: str
    description: str
    category: str
    credits_per_call: float
    unit_name: str


class AWIEndpoint(BaseModel):
    path: str
    method: str
    description: str
    action_type: Optional[str] = None


class PricingTier(BaseModel):
    tier_name: str
    price_per_credit: float
    minimum_purchase: float
    features: list[str]


class CoreDiscoveryManifest(BaseModel):
    """Wedge-only discovery manifest — what a production deployment serves.

    Response model for /v1/discover when proof surfaces are unmounted, so the
    public OpenAPI contract carries no proof-surface vocabulary. Instances
    that mount proof surfaces serve the DiscoveryManifest subclass instead.
    """

    name: str = Field(description="Service name")
    version: str = Field(description="API version")
    description: str = Field(description="What this service provides")

    capabilities: list[ServiceCapability] = Field(
        default_factory=list, description="List of service capabilities"
    )

    mcp_tools: list[MCPToolInfo] = Field(
        default_factory=list, description="Available MCP tools"
    )

    pricing: list[PricingTier] = Field(
        default_factory=list, description="Pricing tiers"
    )

    authentication: dict = Field(
        default_factory=lambda: {
            "method": "api_key",
            "header": "X-API-Key",
            "format": "string",
        }
    )

    rate_limits: dict = Field(
        default_factory=lambda: _build_rate_limits(),
        description=(
            "Enforced request budget and the bucket it is counted against. "
            "Mirrors the X-RateLimit-* response headers."
        ),
    )

    documentation: dict = Field(
        default_factory=lambda: {
            "openapi": "/openapi.json",
            "interactive_docs": "/docs",
            "llm_readable": "/llm.txt",
            "llms_readable": "/llms.txt",
            "agent_manifest": "/.well-known/agent.json",
            "wedge": "/WEDGE.md",
            "security_limitations": "/SECURITY_LIMITATIONS.md",
            "partner_guide": "/DESIGN_PARTNER_GUIDE.md",
        }
    )

    integration_guides: dict = Field(
        default_factory=dict,
        description="Integration entry points; proof-surface guides only when mounted.",
    )

    agent_first: dict[str, Any] = Field(
        default_factory=get_agent_first_metadata,
        description=(
            "Same metadata as /.well-known/agent.json agent_first: "
            "bootstrap order and where to read simulation vs real state."
        ),
    )


class DiscoveryManifest(CoreDiscoveryManifest):
    """Full discovery manifest for instances that mount proof surfaces."""

    awi_endpoints: list[AWIEndpoint] = Field(
        default_factory=list, description="AWI (Agentic Web Interface) endpoints"
    )


def _build_capabilities() -> list[ServiceCapability]:
    """Build the list of service capabilities with product vs proof labels."""
    product = [
        ServiceCapability(
            name="billing",
            version="1.0",
            description=(
                "Configured credit/call allowance and at-most-one debit bound to "
                "a logical action"
            ),
            category="financial",
            surface="product",
        ),
        ServiceCapability(
            name="mcp",
            version="1.0",
            description=(
                "Configured-upstream consequential-action transaction integrity: "
                "logical identity, one-shot gateway dispatch, explicit uncertainty, "
                "and linked evidence"
            ),
            category="tooling",
            surface="product",
        ),
        ServiceCapability(
            name="permits",
            version="1.0",
            description=(
                "Bounded delegated authority scoped to tool, wallet, configured "
                "allowance, and expiry"
            ),
            category="authorization",
            surface="product",
        ),
        ServiceCapability(
            name="receipts",
            version="1.0",
            description=(
                "Signed gateway evidence linked to logical-action and dispatch state; "
                "not proof of downstream effect"
            ),
            category="evidence",
            surface="product",
        ),
        ServiceCapability(
            name="audit",
            version="1.0",
            description="Wallet-scoped audit linkage for transaction reconciliation",
            category="governance",
            surface="product",
        ),
        ServiceCapability(
            name="policies",
            version="1.0",
            description="Policy bundles evaluated on governed invocations",
            category="governance",
            surface="product",
        ),
        ServiceCapability(
            name="api_keys",
            version="1.0",
            description="Wallet-scoped API key issuance and rotation",
            category="security",
            surface="product",
        ),
    ]

    # KYC is a dormant trust surface: its router mounts only with
    # ENABLE_PROOF_SURFACES, and it is only *usable* with Stripe configured.
    # Advertise the capability only when both hold, so discovery never points
    # at routes that answer 404.
    if _is_stripe_configured() and get_settings().ENABLE_PROOF_SURFACES:
        product.append(
            ServiceCapability(
                name="kyc",
                version="1.0",
                description="Stripe Identity KYC verification for sponsor wallets",
                category="compliance",
                surface="product",
            )
        )

    proof = [
        ServiceCapability(
            name="telemetry",
            version="1.0",
            description="Emit events, detect anomalies, and trigger autonomous responses",
            category="observability",
            surface="proof_surface",
            simulation_default=True,
        ),
        ServiceCapability(
            name="comms",
            version="1.0",
            description="Agent-to-agent messaging (webhook delivery simulated by default)",
            category="communication",
            surface="proof_surface",
            simulation_default=True,
        ),
        ServiceCapability(
            name="ai",
            version="1.0",
            description="AI-powered decision making, self-healing, and memory demos",
            category="intelligence",
            surface="proof_surface",
            simulation_default=True,
        ),
        ServiceCapability(
            name="awi",
            version="1.0",
            description=(
                "Agentic Web Interface proof surface — HTTP execute and selected "
                "actions require X-Permit-Id and Idempotency-Key; see "
                "/.well-known/awi.json for route-specific requirements. "
                "Not a qualified production transaction-integrity boundary."
            ),
            category="automation",
            surface="proof_surface",
            simulation_default=True,
        ),
        ServiceCapability(
            name="sandbox",
            version="1.0",
            description="Dry-run and behavioral sandbox demos (not production isolation)",
            category="testing",
            surface="proof_surface",
            simulation_default=True,
        ),
        ServiceCapability(
            name="iot",
            version="1.0",
            description="IoT protocol bridge (MQTT/CoAP simulated by default)",
            category="iot",
            surface="proof_surface",
            simulation_default=True,
        ),
        ServiceCapability(
            name="passkey",
            version="1.0",
            description=(
                "FIDO2/WebAuthn for high-risk AWI actions; mock path refused in "
                "production-like environments"
            ),
            category="security",
            surface="proof_surface",
            simulation_default=True,
        ),
        ServiceCapability(
            name="dom_bridge",
            version="1.0",
            description="Playwright DOM↔AWI bridge proof surface",
            category="automation",
            surface="proof_surface",
            simulation_default=True,
        ),
        ServiceCapability(
            name="rag_memory",
            version="1.0",
            description="Semantic memory over AWI sessions (may use mock embeddings)",
            category="intelligence",
            surface="proof_surface",
            simulation_default=True,
        ),
    ]

    if get_settings().ENABLE_PROOF_SURFACES:
        return product + proof
    return product


def _build_mcp_tools() -> list[MCPToolInfo]:
    """Build the list of available MCP tools from the actual registry.

    Returns the same tools as ``/mcp/tools.json`` to ensure discovery consistency.
    When proof surfaces are off, returns only registered tools (dogfood/partner).
    When on, includes proof-surface stubs plus any registered tools.
    """
    from ..routers.mcp import _ensure_local_mcp_tools_registered
    from ..services.service_registry import get_service_registry

    _ensure_local_mcp_tools_registered()
    registry = get_service_registry()
    tools = []

    for service in registry._local_registry.values():
        tools.append(
            MCPToolInfo(
                service_id=service["service_id"],
                name=service["service_id"],
                description=service.get("description", ""),
                category=service.get("category", "unknown"),
                credits_per_call=service.get("credits_per_unit", 1.0),
                unit_name=service.get("unit_name", "call"),
            )
        )

    return tools


def _build_awi_endpoints() -> list[AWIEndpoint]:
    """Build the list of AWI endpoints."""
    return [
        AWIEndpoint(
            path="/v1/awi/sessions",
            method="POST",
            description="Create a stateful AWI session",
            action_type="session_management",
        ),
        AWIEndpoint(
            path="/v1/awi/execute",
            method="POST",
            description="Execute standardized higher-level actions",
            action_type="action_execution",
        ),
        AWIEndpoint(
            path="/v1/awi/represent",
            method="POST",
            description="Get progressive representations (summary, embedding, full)",
            action_type="representation",
        ),
        AWIEndpoint(
            path="/v1/awi/intervene",
            method="POST",
            description="Human pause/steer for agentic task queues",
            action_type="human_oversight",
        ),
        AWIEndpoint(
            path="/v1/awi/queue/status",
            method="GET",
            description="Check task queue status",
            action_type="queue_management",
        ),
        AWIEndpoint(
            path="/v1/awi/vocabulary",
            method="GET",
            description="Get the AWI action vocabulary",
            action_type="discovery",
        ),
        AWIEndpoint(
            path="/v1/awi/passkey/challenge",
            method="POST",
            description="Create WebAuthn challenge for high-risk action verification",
            action_type="security",
        ),
        AWIEndpoint(
            path="/v1/awi/passkey/verify",
            method="POST",
            description="Verify WebAuthn credential response",
            action_type="security",
        ),
        AWIEndpoint(
            path="/v1/awi/passkey/status/{session_id}/{action}",
            method="GET",
            description="Check passkey verification status",
            action_type="security",
        ),
        AWIEndpoint(
            path="/v1/awi/dom/session",
            method="POST",
            description="Create browser session for DOM automation",
            action_type="browser_automation",
        ),
        AWIEndpoint(
            path="/v1/awi/dom/sync",
            method="POST",
            description="Execute AWI action via Playwright",
            action_type="browser_automation",
        ),
        AWIEndpoint(
            path="/v1/awi/dom/state/{session_id}",
            method="GET",
            description="Get DOM state as AWI representation",
            action_type="browser_automation",
        ),
        AWIEndpoint(
            path="/v1/awi/rag/index",
            method="POST",
            description="Index AWI session for semantic retrieval",
            action_type="memory",
        ),
        AWIEndpoint(
            path="/v1/awi/rag/query",
            method="POST",
            description="Semantic search over session memories",
            action_type="memory",
        ),
        AWIEndpoint(
            path="/v1/awi/rag/context/{session_id}",
            method="GET",
            description="Get context from past sessions",
            action_type="memory",
        ),
    ]


def _build_rate_limits() -> dict[str, Any]:
    """Describe the limit ``RateLimitMiddleware`` actually enforces."""
    return rate_limit_discovery()


def _build_pricing() -> list[PricingTier]:
    """Describe the controlled pilot without inventing public commercial tiers."""
    return [
        PricingTier(
            tier_name="self_hosted",
            price_per_credit=0.0,
            minimum_purchase=0.0,
            features=[
                "Logical action + bounded delegated authority",
                "Configured-upstream at-most-one dispatch/debit + delivery uncertainty",
                "Linked gateway receipt/audit evidence for reconciliation",
            ],
        ),
        PricingTier(
            tier_name="design_partner",
            price_per_credit=0.0,
            minimum_purchase=0.0,
            features=[
                "Same transaction-integrity loop as self-hosted",
                "One environment-configured upstream MCP tool",
                "Operator-provisioned wallet, key, credits, and permit",
                "No public SLA, pricing, compliance, or tenant-isolation claim",
            ],
        ),
    ]


def _build_integration_guides() -> dict[str, str]:
    """Trust-plane guides; AWI guide only when proof surfaces are mounted."""
    guides = {
        "mcp_tools": "/mcp/tools.json",
        "mcp_json_rpc": "/mcp/messages",
        "mcp_json_rpc_status": "legacy_project_transport",
        "mcp_json_rpc_note": (
            "Project-specific JSON-RPC endpoint; it does not implement the "
            "standard MCP initialization lifecycle."
        ),
        "llm_txt": "/llm.txt",
        "llms_txt": "/llms.txt",
        "wedge": "/WEDGE.md",
        "partner_guide": "/DESIGN_PARTNER_GUIDE.md",
        "dogfood": "make dogfood-trust-plane (local partner.notes.write)",
    }
    if get_settings().ENABLE_STANDARD_MCP_ENDPOINT:
        guides["standard_mcp"] = "/mcp"
    if get_settings().ENABLE_PROOF_SURFACES:
        guides["awi_adoption"] = "/docs/awi-adoption-guide.md"
    return guides


@router.get(
    "/discover",
    # Boot-time posture decides the public contract: with proof surfaces
    # unmounted the response model is the wedge-only manifest, so the OpenAPI
    # schema (and the serialized response) carry no AWI vocabulary at all.
    response_model=(
        DiscoveryManifest
        if get_settings().ENABLE_PROOF_SURFACES
        else CoreDiscoveryManifest
    ),
    summary="Agent Discovery Manifest",
    # Boot-time posture again: an instance that mounts no proof surfaces does
    # not describe them either.
    description=(
        (
            "Aggregated capability index: services, MCP tools, and pricing — "
            "plus agentic-web endpoints on instances that mount proof "
            "surfaces. "
            if get_settings().ENABLE_PROOF_SURFACES
            else "Aggregated capability index: services, MCP tools, and pricing. "
        )
        + "Bootstrap order is defined in `/.well-known/agent.json` under "
        "`agent_first.bootstrap_sequence` (this endpoint is optional after "
        "those hints)."
    ),
)
async def get_discovery_manifest(request: Request):
    """
    Aggregated discovery manifest for autonomous agents.

    Prefer `GET /.well-known/agent.json` first for `agent_first` metadata, then
    this payload for a fuller catalog when needed. Production-like boots
    require the same credentials as invoke; the catalog names the configured
    tool.
    """
    await reject_anonymous_production_catalog(request)
    # Ensure local tools are registered (lazy registration, respects flags)
    from ..routers.mcp import _ensure_local_mcp_tools_registered

    _ensure_local_mcp_tools_registered()

    return DiscoveryManifest(
        name=settings.APP_NAME,
        version=settings.APP_VERSION,
        description=(
            POSITIONING_DESCRIPTION
            + (
                " Capabilities with surface=proof_surface are labeled scaffolding."
                if get_settings().ENABLE_PROOF_SURFACES
                else ""
            )
        ),
        capabilities=_build_capabilities(),
        mcp_tools=_build_mcp_tools(),
        awi_endpoints=_build_awi_endpoints()
        if get_settings().ENABLE_PROOF_SURFACES
        else [],
        pricing=_build_pricing(),
        rate_limits=_build_rate_limits(),
        integration_guides=_build_integration_guides(),
    )


@router.get(
    "/discover/tools",
    summary="List Available MCP Tools",
    description="Returns all available MCP tools with their schemas and pricing.",
)
async def list_mcp_tools(api_key: str = Depends(verify_api_key)):
    """
    List all MCP tools available for this API key.

    Tools are returned with full schema definitions suitable for
    direct use with MCP clients.
    """
    # Ensure local tools are registered (lazy registration, respects flags)
    from ..routers.mcp import _ensure_local_mcp_tools_registered

    _ensure_local_mcp_tools_registered()

    tools = _build_mcp_tools()
    result = {
        "tools": tools,
        "total": len(tools),
        "mcp_tools_json": "/mcp/tools.json",
        "mcp_json_rpc_endpoint": "/mcp/messages",
    }
    if get_settings().ENABLE_STANDARD_MCP_ENDPOINT:
        result["mcp_endpoint"] = "/mcp"
    return result


@router.get(
    "/discover/awi",
    summary="List AWI Endpoints",
    description="Returns all Agentic Web Interface endpoints and the action vocabulary.",
    # AWI is a gated proof surface. The route keeps answering its honest
    # "not mounted" payload for direct callers, but it is only advertised in
    # the OpenAPI contract when proof surfaces are actually mounted.
    include_in_schema=get_settings().ENABLE_PROOF_SURFACES,
)
async def list_awi_endpoints(api_key: str = Depends(verify_api_key)):
    """
    List all AWI endpoints and the action vocabulary.

    This enables agents to understand:
    - How to create AWI sessions
    - What actions are available
    - How to request different representations
    - How to implement human pause/steer
    """
    if not get_settings().ENABLE_PROOF_SURFACES:
        return {
            "endpoints": [],
            "mounted": False,
            "note": "AWI is a proof surface and is not mounted (ENABLE_PROOF_SURFACES=false).",
            "mcp_tools_json": "/mcp/tools.json",
        }
    return {
        "endpoints": _build_awi_endpoints(),
        "vocabulary_endpoint": "/v1/awi/vocabulary",
        "reference": "/docs/awi-adoption-guide.md",
        "note": (
            "AWI is a labeled proof surface outside the transaction-integrity boundary."
        ),
    }
