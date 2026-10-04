"""Agent-readable documentation routes for the governed MCP gateway.

Endpoints:
- /llms.txt — Canonical public path backed by ``static/llm.txt``
- /llm.txt — Backward-compatible alias
- /docs/index — Structured JSON doc index for agent navigation
- /.well-known/agent.json — Project discovery manifest
"""

from fastapi import APIRouter

from ..core.config import get_settings
from ..core.product_positioning import (
    LEGACY_PRODUCT_WEDGE,
    get_product_positioning,
)
from .well_known import get_agent_first_metadata

router = APIRouter(
    tags=["Documentation & Discovery"],
)

_TRUST_PLANE_SECTIONS = [
    {
        "id": "agent_manifest",
        "title": "Agent bootstrap manifest",
        "path": "/.well-known/agent.json",
        "content_type": "application/json",
        "summary": ("Canonical bootstrap: capabilities, endpoints, and agent_first."),
    },
    {
        "id": "wedge",
        "title": "Transaction-integrity boundary",
        "path": "/WEDGE.md",
        "content_type": "text/markdown",
        "summary": "Transaction integrity for consequential autonomous actions.",
    },
    {
        "id": "security_limitations",
        "title": "Security limitations",
        "path": "/SECURITY_LIMITATIONS.md",
        "content_type": "text/markdown",
        "summary": "Honest non-claims and required production posture.",
    },
    {
        "id": "partner_guide",
        "title": "Design partner guide",
        "path": "/DESIGN_PARTNER_GUIDE.md",
        "content_type": "text/markdown",
        "summary": "One-tool pilot path for a configured upstream MCP tool.",
    },
    {
        "id": "agent_accountability",
        "title": "Agent accountability",
        "path": "/docs/agent-accountability.md",
        "content_type": "text/markdown",
        "summary": (
            "Why an agent runs inside the permit/receipt loop, how to verify "
            "a receipt offline with no credentials, and what receipts do not "
            "prove."
        ),
    },
    {
        "id": "overview",
        "title": "API Overview (llms.txt)",
        "path": "/llms.txt",
        "content_type": "text/plain",
        "summary": "Full API documentation in LLM-optimized plaintext format.",
    },
    {
        "id": "openapi",
        "title": "OpenAPI Specification",
        "path": "/openapi.json",
        "content_type": "application/json",
        "summary": (
            "Machine-readable OpenAPI 3.1 specification with all endpoints, "
            "schemas, and examples."
        ),
    },
    {
        "id": "interactive",
        "title": "Interactive API Explorer",
        "path": "/docs",
        "content_type": "text/html",
        "summary": (
            "Swagger UI for testing endpoints (primarily for human developers)."
        ),
    },
    {
        "id": "redoc",
        "title": "ReDoc Reference",
        "path": "/redoc",
        "content_type": "text/html",
        "summary": "Detailed API reference documentation.",
    },
]

_TRUST_PLANE_SERVICES = [
    {
        "id": "agent-billing",
        "name": "Configured authority consumption",
        "base_path": "/v1/billing",
        "surface": "product",
        "capabilities": [
            "sponsor-wallets",
            "agent-wallets",
            "per-action-micro-metering",
            "transaction-ledger",
            "daily-spend-limits",
            "bounded-credit-and-call-allowance",
            "at-most-one-debit-per-logical-action",
        ],
        "quickstart_endpoint": "POST /v1/billing/wallets/sponsor",
    },
    {
        "id": "mcp-trust-plane",
        "name": "Consequential-action transaction integrity",
        "base_path": "/mcp/messages",
        "transport": "legacy_project_json_rpc",
        "transport_note": (
            "Project-specific JSON-RPC endpoint; it does not implement the "
            "standard MCP initialization lifecycle."
        ),
        "surface": "product",
        "transaction_scope": "configured_upstream_mcp_tool_only",
        "capabilities": [
            "tools-discovery",
            "permit-gated-invoke",
            "idempotent-retries",
            "signed-receipts",
            "logical-action-identity",
            "at-most-one-gateway-dispatch",
            "delivery-uncertain-no-automatic-redispatch",
            "linked-gateway-evidence",
        ],
        "quickstart_endpoint": "POST /mcp/messages",
    },
    {
        "id": "permits",
        "name": "Bounded delegated authority",
        "base_path": "/v1/permits",
        "surface": "product",
        "capabilities": [
            "scoped-permits",
            "revoke",
            "budget-binding",
            "credit-and-call-allowance-binding",
        ],
        "quickstart_endpoint": "POST /v1/permits",
    },
    {
        "id": "receipts",
        "name": "Linked gateway evidence",
        "base_path": "/v1/receipts",
        "surface": "product",
        "capabilities": [
            "receipt-verify",
            "evidence-bundle",
            "gateway-state-not-downstream-effect-proof",
        ],
        "quickstart_endpoint": "GET /v1/receipts",
    },
    {
        "id": "audit",
        "name": "Gateway audit linkage",
        "base_path": "/v1/audit",
        "surface": "product",
        "capabilities": [
            "tamper-evident-chain",
            "wallet-scoped-events",
            "logical-action-correlation",
        ],
        "quickstart_endpoint": "GET /v1/audit/events",
    },
]

_PROOF_SURFACE_SERVICES = [
    {
        "id": "iot-bridge",
        "name": "IoT Protocol Bridge",
        "base_path": "/v1/iot",
        "surface": "proof_surface",
        "capabilities": [
            "device-registration",
            "protocol-translation",
            "topic-acl-enforcement",
        ],
        "quickstart_endpoint": "POST /v1/iot/devices",
    },
    {
        "id": "autonomous-pm",
        "name": "Autonomous Product Manager",
        "base_path": "/v1/telemetry",
        "surface": "proof_surface",
        "capabilities": [
            "telemetry-ingestion",
            "anomaly-detection",
            "auto-pr-generation",
        ],
        "quickstart_endpoint": "POST /v1/telemetry/events",
    },
    {
        "id": "media-engine",
        "name": "Programmatic Media Engine",
        "base_path": "/v1/media",
        "surface": "proof_surface",
        "capabilities": ["video-ingestion", "viral-hook-detection"],
        "quickstart_endpoint": "POST /v1/media/videos",
    },
    {
        "id": "agent-comms",
        "name": "Agent Communications",
        "base_path": "/v1/comms",
        "surface": "proof_surface",
        "capabilities": ["agent-messaging", "capability-discovery"],
        "quickstart_endpoint": "POST /v1/comms/agents",
    },
]


@router.get(
    "/docs/index",
    summary="Structured documentation index",
    description=(
        "Returns a JSON index for the consequential-action transaction-integrity "
        "boundary. Proof-surface service entries appear only when mounted."
    ),
)
async def get_doc_index():
    services = [dict(service) for service in _TRUST_PLANE_SERVICES]
    if get_settings().ENABLE_STANDARD_MCP_ENDPOINT:
        for service in services:
            if service["id"] == "mcp-trust-plane":
                service["standard_streamable_http_endpoint"] = "/mcp"
                break
    if get_settings().ENABLE_PROOF_SURFACES:
        services.extend(_PROOF_SURFACE_SERVICES)

    return {
        "agent_first": get_agent_first_metadata(),
        "version": "0.2.0",
        "format": "agent-native-docs/v1",
        "positioning": get_product_positioning(),
        # Compatibility-only v1 alias; ``positioning`` is canonical.
        "product_wedge": LEGACY_PRODUCT_WEDGE,
        "sections": list(_TRUST_PLANE_SECTIONS),
        "services": services,
        "auth": {
            "method": "api_key",
            "header": "X-API-Key",
            "docs": "/llm.txt#authentication",
        },
        "note": (
            "A durable gateway state machine, not distributed ACID or proof of "
            "the downstream effect. Proof-surface services appear only when "
            "ENABLE_PROOF_SURFACES=true."
        ),
    }
