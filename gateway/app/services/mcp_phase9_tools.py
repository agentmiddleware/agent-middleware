"""
PROOF SURFACE — frozen. Do not expand; see docs/PROOF_SURFACES.md.
MCP Phase 9 Tools Registration
==============================

Registers Phase 9 enhanced capabilities (passkey auth, DOM bridge, RAG memory)
as discoverable MCP tools.

All Phase 9 AWI tools are marked ``require_permit=True`` so they always run
the governed MCP path (permit → meter → receipt) even when legacy
unpermitted MCP is enabled.
"""

import logging
from threading import Lock
from typing import Any, Callable, TypedDict

from pydantic import BaseModel

from .service_registry import get_service_registry
from ..schemas.billing import ServiceCategory
from ..schemas.awi_enhanced import (
    PasskeyChallengeRequest,
    PasskeyVerifyRequest,
    DOMBridgeSessionRequest,
    DOMSyncRequest,
    DOMStateRequest,
    DOMActionPreviewRequest,
    MemoryIndexRequest,
    RAGQueryRequest,
    SessionContextRequest,
)

logger = logging.getLogger(__name__)

# All Phase 9 AWI tools must stay on the trust spine (no legacy bypass).
ALWAYS_GOVERNED_AWI_TOOLS = frozenset(
    {
        "awi_passkey_challenge",
        "awi_passkey_verify",
        "awi_dom_bridge_session",
        "awi_dom_sync",
        "awi_dom_state",
        "awi_dom_action_preview",
        "awi_memory_index",
        "awi_rag_query",
        "awi_session_context",
    }
)

# Marketplace-style preview stubs registered only when proof surfaces are on.
DEFAULT_MCP_STUB_SERVICE_IDS = frozenset(
    {
        "data-indexer",
        "content-generator",
        "telemetry-processor",
        "semantic-search",
    }
)

# Discovery must not advertise these when ENABLE_PROOF_SURFACES=false.
PROOF_SURFACE_MCP_STUB_IDS = ALWAYS_GOVERNED_AWI_TOOLS | DEFAULT_MCP_STUB_SERVICE_IDS


async def awi_passkey_challenge(session_id: str, action: str) -> dict[str, Any]:
    """Create a WebAuthn challenge via the real provider (governed MCP path)."""
    from .webauthn_provider import get_webauthn_provider

    provider = get_webauthn_provider()
    challenge = await provider.create_challenge(
        session_id=session_id,
        action=action,
    )
    return {
        "status": "ok",
        "governed": True,
        "tool": "awi_passkey_challenge",
        **challenge,
    }


async def awi_passkey_verify(challenge_id: str, credential: dict) -> dict[str, Any]:
    """Wrapper for passkey verification (endpoint pointer; not always-governed)."""
    return {
        "endpoint": "/v1/awi/passkey/verify",
        "method": "POST",
        "requires": ["challenge_id", "credential"],
    }


async def awi_dom_bridge_session(
    target_url: str, headless: bool = True, **kwargs
) -> dict[str, Any]:
    """Wrapper for DOM bridge session creation."""
    return {
        "endpoint": "/v1/awi/dom/session",
        "method": "POST",
        "requires": ["target_url"],
    }


async def awi_dom_sync(
    session_id: str, action: str, parameters: dict | None = None, **kwargs
) -> dict[str, Any]:
    """Wrapper for DOM sync execution."""
    return {
        "endpoint": "/v1/awi/dom/sync",
        "method": "POST",
        "requires": ["session_id", "action"],
    }


async def awi_dom_state(
    session_id: str, representation_type: str = "summary", **kwargs
) -> dict[str, Any]:
    """Wrapper for DOM state retrieval."""
    return {
        "endpoint": "/v1/awi/dom/state",
        "method": "GET",
        "requires": ["session_id"],
    }


async def awi_dom_action_preview(
    session_id: str, action: str, parameters: dict | None = None, **kwargs
) -> dict[str, Any]:
    """Wrapper for DOM action preview."""
    return {
        "endpoint": "/v1/awi/dom/preview",
        "method": "POST",
        "requires": ["session_id", "action"],
    }


async def awi_memory_index(
    session_id: str, session_type: str, action_history: list | None = None, **kwargs
) -> dict[str, Any]:
    """Wrapper for memory indexing."""
    return {
        "endpoint": "/v1/awi/rag/index",
        "method": "POST",
        "requires": ["session_id", "session_type"],
    }


async def awi_rag_query(query: str, top_k: int = 5, **kwargs) -> dict[str, Any]:
    """Wrapper for RAG memory queries.

    The MCP dispatch hands this tool no caller wallet, so it must not search
    the shared memory store: doing so returned every tenant's memories under a
    signed success receipt. Like its sibling wrappers it points at the HTTP
    route, which scopes results to the caller's wallet.
    """
    return {
        "endpoint": "/v1/awi/rag/query",
        "method": "POST",
        "requires": ["query"],
    }


async def awi_session_context(
    current_session_id: str, current_state: dict | None = None, **kwargs
) -> dict[str, Any]:
    """Wrapper for session context retrieval."""
    return {
        "endpoint": "/v1/awi/rag/context",
        "method": "POST",
        "requires": ["current_session_id"],
    }


class Phase9ToolDef(TypedDict):
    """Registration payload for a Phase 9 MCP tool."""

    service_id: str
    name: str
    description: str
    category: ServiceCategory
    credits_per_unit: float
    unit_name: str
    func: Callable[..., Any]
    input_model: type[BaseModel]
    require_permit: bool


MCP_PHASE9_TOOLS: list[Phase9ToolDef] = [
    {
        "service_id": "awi_passkey_challenge",
        "name": "AWI Passkey Challenge",
        "description": (
            "Generate a passkey challenge for high-risk action verification "
            "using FIDO2/WebAuthn. Always requires a signed permit."
        ),
        "category": ServiceCategory.AGENT_COMMS,
        "credits_per_unit": 1.0,
        "unit_name": "challenge",
        "func": awi_passkey_challenge,
        "input_model": PasskeyChallengeRequest,
        "require_permit": True,
    },
    {
        "service_id": "awi_passkey_verify",
        "name": "AWI Passkey Verify",
        "description": (
            "Verify a passkey assertion response from the client authenticator. "
            "Always requires a signed permit."
        ),
        "category": ServiceCategory.AGENT_COMMS,
        "credits_per_unit": 2.0,
        "unit_name": "verification",
        "func": awi_passkey_verify,
        "input_model": PasskeyVerifyRequest,
        "require_permit": True,
    },
    {
        "service_id": "awi_dom_bridge_session",
        "name": "AWI DOM Bridge Session",
        "description": (
            "Create a new Playwright bridge session for real browser automation. "
            "Always requires a signed permit."
        ),
        "category": ServiceCategory.AGENT_COMMS,
        "credits_per_unit": 5.0,
        "unit_name": "session",
        "func": awi_dom_bridge_session,
        "input_model": DOMBridgeSessionRequest,
        "require_permit": True,
    },
    {
        "service_id": "awi_dom_sync",
        "name": "AWI DOM Sync",
        "description": (
            "Execute an AWI action against real browser DOM and return state "
            "representation. Always requires a signed permit."
        ),
        "category": ServiceCategory.AGENT_COMMS,
        "credits_per_unit": 3.0,
        "unit_name": "execution",
        "func": awi_dom_sync,
        "input_model": DOMSyncRequest,
        "require_permit": True,
    },
    {
        "service_id": "awi_dom_state",
        "name": "AWI DOM State",
        "description": (
            "Get current DOM state as AWI representation (summary, accessibility "
            "tree, etc.). Always requires a signed permit."
        ),
        "category": ServiceCategory.AGENT_COMMS,
        "credits_per_unit": 2.0,
        "unit_name": "query",
        "func": awi_dom_state,
        "input_model": DOMStateRequest,
        "require_permit": True,
    },
    {
        "service_id": "awi_dom_action_preview",
        "name": "AWI DOM Action Preview",
        "description": (
            "Preview what Playwright commands will be generated for an AWI action. "
            "Always requires a signed permit."
        ),
        "category": ServiceCategory.AGENT_COMMS,
        "credits_per_unit": 2.0,
        "unit_name": "preview",
        "func": awi_dom_action_preview,
        "input_model": DOMActionPreviewRequest,
        "require_permit": True,
    },
    {
        "service_id": "awi_memory_index",
        "name": "AWI Memory Index",
        "description": (
            "Index a completed AWI session for semantic search over session history. "
            "Always requires a signed permit."
        ),
        "category": ServiceCategory.AGENT_COMMS,
        "credits_per_unit": 5.0,
        "unit_name": "indexing",
        "func": awi_memory_index,
        "input_model": MemoryIndexRequest,
        "require_permit": True,
    },
    {
        "service_id": "awi_rag_query",
        "name": "AWI RAG Query",
        "description": (
            "Semantic search query over session memories using vector "
            "similarity. Always requires a signed permit."
        ),
        "category": ServiceCategory.AGENT_COMMS,
        "credits_per_unit": 3.0,
        "unit_name": "search",
        "func": awi_rag_query,
        "input_model": RAGQueryRequest,
        "require_permit": True,
    },
    {
        "service_id": "awi_session_context",
        "name": "AWI Session Context",
        "description": (
            "Get relevant context from past sessions for the current session. "
            "Always requires a signed permit."
        ),
        "category": ServiceCategory.AGENT_COMMS,
        "credits_per_unit": 2.0,
        "unit_name": "context",
        "func": awi_session_context,
        "input_model": SessionContextRequest,
        "require_permit": True,
    },
]


def register_phase9_tools() -> None:
    """Register all Phase 9 tools with the service registry."""
    registry = get_service_registry()

    for tool in MCP_PHASE9_TOOLS:
        registry.register_local(
            service_id=tool["service_id"],
            name=tool["name"],
            description=tool["description"],
            category=tool["category"],
            func=tool["func"],
            input_model=tool["input_model"],
            credits_per_unit=tool["credits_per_unit"],
            unit_name=tool["unit_name"],
            require_permit=bool(tool.get("require_permit", False)),
        )
        logger.info(f"Registered Phase 9 MCP tool: {tool['service_id']}")


_registered = False
_registration_lock = Lock()


def ensure_phase9_registered():
    """Ensure Phase 9 tools are registered exactly once when proof surfaces are on."""
    from ..core.config import get_settings

    if not get_settings().ENABLE_PROOF_SURFACES:
        return

    global _registered
    if _registered:
        return

    with _registration_lock:
        if not _registered:
            register_phase9_tools()
            _registered = True


_default_services_registered = False


def unregister_proof_surface_mcp_stubs() -> None:
    """Remove Phase9 AWI / marketplace stub tools from the local registry.

    Used when ``ENABLE_PROOF_SURFACES=false`` so discovery stays honest even if
    an earlier process path registered the stubs (e.g. tests flipping the flag).
    """
    global _registered, _default_services_registered
    registry = get_service_registry()
    with _registration_lock:
        for service_id in PROOF_SURFACE_MCP_STUB_IDS:
            registry.unregister_local(service_id)
        _registered = False
        _default_services_registered = False


def sync_proof_surface_mcp_registration() -> None:
    """Register or unregister proof-surface MCP stubs to match the flag."""
    from ..core.config import get_settings

    if get_settings().ENABLE_PROOF_SURFACES:
        ensure_phase9_registered()
        register_default_mcp_services()
    else:
        unregister_proof_surface_mcp_stubs()


class DefaultServiceDef(TypedDict):
    """Registration payload for a default control-plane MCP service."""

    service_id: str
    name: str
    description: str
    category: ServiceCategory
    credits_per_unit: float
    unit_name: str
    input_model: type[BaseModel] | None
    output_model: type[BaseModel] | None


def _make_default_service_handler(service_def: DefaultServiceDef):
    """Create a deterministic preview handler for a default control-plane tool."""
    category = service_def["category"].value
    service_id = service_def["service_id"]
    service_name = service_def["name"]
    unit_name = service_def["unit_name"]

    async def default_service_handler(**kwargs) -> dict[str, Any]:
        return {
            "status": "preview_available",
            "service_id": service_id,
            "service_name": service_name,
            "category": category,
            "unit_name": unit_name,
            "mode": "agent_operations_control_plane_preview",
            "integration_status": "contract_only",
            "message": (
                f"{service_name} exposes the deterministic control-plane contract "
                "agents can use for discovery, planning, and policy evaluation. "
                "Runtime side effects are not wired in this preview handler."
            ),
            "input_keys": sorted(kwargs),
        }

    return default_service_handler


def register_default_mcp_services():
    """Register marketplace preview stubs when proof surfaces are enabled."""
    from ..core.config import get_settings

    if not get_settings().ENABLE_PROOF_SURFACES:
        return

    global _default_services_registered
    if _default_services_registered:
        return

    with _registration_lock:
        if _default_services_registered:
            return

        registry = get_service_registry()

        default_services: list[DefaultServiceDef] = [
            {
                "service_id": "data-indexer",
                "name": "Data Indexer",
                "description": "Fast vector indexing for documents and content. Enables semantic search capabilities for AI agents.",
                "category": ServiceCategory.PROTOCOL_GEN,
                "credits_per_unit": 10.0,
                "unit_name": "document",
                "input_model": None,
                "output_model": None,
            },
            {
                "service_id": "content-generator",
                "name": "Content Generator",
                "description": "Generate marketing copy, product descriptions, and social media content using AI.",
                "category": ServiceCategory.CONTENT_FACTORY,
                "credits_per_unit": 25.0,
                "unit_name": "piece",
                "input_model": None,
                "output_model": None,
            },
            {
                "service_id": "telemetry-processor",
                "name": "Telemetry Processor",
                "description": "Process and analyze agent telemetry data for anomaly detection and monitoring.",
                "category": ServiceCategory.TELEMETRY_PM,
                "credits_per_unit": 5.0,
                "unit_name": "event",
                "input_model": None,
                "output_model": None,
            },
            {
                "service_id": "semantic-search",
                "name": "Semantic Search",
                "description": "Natural language search across indexed content using embeddings.",
                "category": ServiceCategory.PROTOCOL_GEN,
                "credits_per_unit": 15.0,
                "unit_name": "query",
                "input_model": None,
                "output_model": None,
            },
        ]

        for service_def in default_services:
            try:
                registry.register_local(
                    service_id=service_def["service_id"],
                    name=service_def["name"],
                    description=service_def["description"],
                    category=service_def["category"],
                    func=_make_default_service_handler(service_def),
                    input_model=service_def["input_model"],
                    output_model=service_def["output_model"],
                    credits_per_unit=service_def["credits_per_unit"],
                    unit_name=service_def["unit_name"],
                )
                logger.info(
                    f"Registered default MCP service: {service_def['service_id']}"
                )
            except Exception as e:
                logger.warning(
                    f"Failed to register default service {service_def['service_id']}: {e}"
                )

        _default_services_registered = True
