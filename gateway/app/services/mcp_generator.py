"""
MCP Server Generator
====================

Generates MCP-shaped tool metadata from registered services.
Standalone ungoverned code generation is retired. The HTTP tools.json envelope is project-specific.

The supported governed MCP server is mounted on FastAPI.

MCP Protocol Reference: https://modelcontextprotocol.io/
"""

import asyncio
import logging
import math
from datetime import datetime, timezone
from typing import Any

from ..core.product_positioning import (
    LEGACY_MCP_SERVER_NAME,
    POSITIONING_DESCRIPTION,
    get_product_positioning,
)
from ..schemas.billing import ServiceCategory
from ..services.mcp_integration_truth import truth_for_category
from ..services.service_registry import ServiceRegistry, get_service_registry

logger = logging.getLogger(__name__)

MCP_TOOLS_JSON_VERSION = "1.0"
MCP_SERVER_VERSION = "1.0"


def _finite_price(value: Any) -> float | None:
    """Coerce a registry price to a finite float, or ``None`` if unusable.

    ``float()`` raises ``OverflowError`` for an int too large to represent and
    ``TypeError``/``ValueError`` for junk, and it happily returns a not-a-number
    or infinite value for the strings "nan" and "inf" — none of which can be
    published in a JSON manifest.
    """
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


class McpGenerator:
    """
    Generates project tool manifests for the governed MCP server.

    MCP Manifest Structure (tools.json):
    {
        "version": "1.0",
        "name": "Agent Middleware MCP Trust Plane",
        "description": "...",
        "tools": [
            {
                "name": "service_name",
                "description": "...",
                "inputSchema": { ... },
                "annotations": {
                    "creditsPerCall": 10.0,
                    "unitName": "call",
                    "category": "content_factory"
                }
            }
        ]
    }
    """

    # Stable protocol identity retained for existing MCP clients. Canonical
    # category metadata lives in the versioned ``positioning`` object.
    MANIFEST_NAME = LEGACY_MCP_SERVER_NAME
    MANIFEST_DESCRIPTION = (
        f"{POSITIONING_DESCRIPTION} Tools listed here are "
        "executable local, configured upstream, or dogfood endpoints — not a "
        "metadata-only service catalog."
    )

    def __init__(self, registry: ServiceRegistry | None = None):
        self.registry = registry or get_service_registry()

    def _tools_manifest_envelope(self, tools: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "version": MCP_TOOLS_JSON_VERSION,
            "name": self.MANIFEST_NAME,
            "description": self.MANIFEST_DESCRIPTION,
            "positioning": get_product_positioning(),
            "tools": tools,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    def generate_tools_json(
        self,
        category: ServiceCategory | None = None,
        include_local: bool = True,
        include_persistent: bool = False,
    ) -> dict[str, Any]:
        """
        Generate a project tools.json manifest for executable MCP discovery.

        Clients can request the same list with ``tools/list`` on this project's
        governed JSON-RPC endpoint. They may fetch this HTTP mirror before
        authenticating there. Metadata-only database registrations are excluded
        by default because the gateway has no executable transport for them;
        callers that need an administrative catalog may opt in explicitly.
        """
        services = []

        if include_local:
            local_services = self.registry._local_registry
            for service in local_services.values():
                if category and service.get("category") != category.value:
                    continue
                services.append(self._service_to_mcp_tool(service))

        if include_persistent:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                persistent_services = asyncio.run(
                    self.registry.list_persistent(category=category)
                )
            else:
                raise RuntimeError(
                    "generate_tools_json() cannot load persistent services from an "
                    "active event loop. Use generate_tools_json_async() instead."
                )
            for service in persistent_services:
                services.append(self._service_to_mcp_tool(service))

        return self._tools_manifest_envelope(services)

    async def generate_tools_json_async(
        self,
        category: ServiceCategory | None = None,
        include_local: bool = True,
        include_persistent: bool = False,
    ) -> dict[str, Any]:
        """Async version of generate_tools_json."""
        services = []

        if include_local:
            local_services = self.registry._local_registry
            for service in local_services.values():
                if category and service.get("category") != category.value:
                    continue
                services.append(self._service_to_mcp_tool(service))

        if include_persistent:
            try:
                persistent_services = await self.registry.list_persistent(
                    category=category
                )
                for service in persistent_services:
                    services.append(self._service_to_mcp_tool(service))
            except RuntimeError as e:
                if "DATABASE_URL" in str(e):
                    logger.debug("No database configured, skipping persistent services")
                else:
                    raise

        return self._tools_manifest_envelope(services)

    def _service_to_mcp_tool(self, service: dict) -> dict[str, Any]:
        """Convert a service record to MCP tool format."""
        input_schema = service.get("input_schema")
        if input_schema is None:
            input_schema = {"type": "object", "properties": {}}
        tool = {
            "name": service["service_id"],
            "description": service.get("description", ""),
            "inputSchema": input_schema,
        }

        cat = service.get("category", "unknown") or "unknown"
        truth = truth_for_category(cat)

        # A malformed price must neither crash generation nor poison the
        # manifest: NaN/Infinity serialize as bare `NaN`/`Infinity` literals,
        # which are not valid JSON (RFC 8259), so one bad row would make the
        # WHOLE manifest unparseable for a strict client. Prices are therefore
        # normalized to finite values here, and the conservative 1.0 fallback
        # ensures an unreadable price never advertises a free tool.
        raw_exact = service.get("credits_per_unit_exact")
        exact_price = _finite_price(raw_exact)
        declared_price = _finite_price(service.get("credits_per_unit"))
        if raw_exact is not None and exact_price is None:
            # Present but unreadable: the authoritative price is corrupt, so
            # this tool's real cost is unknown. Do not let a possibly-stale
            # declared value (0.0 included) advertise it as free.
            per_call_cost = 1.0
        elif exact_price is not None:
            per_call_cost = exact_price
        elif declared_price is not None:
            per_call_cost = declared_price
        else:
            per_call_cost = 1.0

        # The governance contract behind tools/call: authorized by a
        # wallet-bounded permit, metered, and answered with a signed receipt,
        # with approvalMayBeRequired signaling that wallet policy or the
        # backing permit can pause any call on a human decision (a retryable
        # pending_human_approval, not a failure). Advertised only when the
        # invocation path actually enforces it on every call: a permissive
        # deployment (ALLOW_LEGACY_UNPERMITTED_MCP, local/demo only — refused
        # at boot in production-like environments) accepts ungoverned
        # permit-less calls, and this manifest must not promise guarantees
        # that path does not provide. A require_permit tool is the exception:
        # the router forces the governed path for it even in permissive mode.
        from ..core.config import get_settings

        settings = get_settings()
        governed = bool(
            (settings.TRUST_MODE_ENABLED and not settings.ALLOW_LEGACY_UNPERMITTED_MCP)
            or service.get("require_permit")
        )

        annotations = {
            # The normalized price, so the advertised number is always finite
            # and always agrees with economicAction below.
            "creditsPerCall": per_call_cost,
            "unitName": service.get("unit_name", "call"),
            "category": cat,
            "simulation": truth["simulation"],
            "integrationStatus": truth["integration_status"],
            "governed": governed,
            "receiptProvided": governed,
            "supportsIdempotency": governed,
            "economicAction": per_call_cost > 0,
            "approvalMayBeRequired": governed,
        }
        # Advertised only when it is a usable finite price: an "exact" price a
        # client cannot parse is worse than an absent one, and the raw value is
        # kept verbatim so a valid decimal keeps its precision and formatting.
        if exact_price is not None:
            annotations["creditsPerCallExact"] = service["credits_per_unit_exact"]

        if truth.get("runtime_service"):
            annotations["runtimeService"] = truth["runtime_service"]

        if self.registry.get_action_binding(service) is not None:
            annotations["actionPermitRequired"] = True

        if service.get("require_permit"):
            annotations["requirePermit"] = True

        if service.get("owner_wallet_id"):
            annotations["providerWallet"] = service["owner_wallet_id"]

        # The local runtime currently serializes return values into text content
        # rather than enforcing MCP structuredContent, so advertising the schema
        # as MCP outputSchema would promise a contract the call path cannot honor.
        if service.get("output_schema"):
            annotations["hasOutputSchema"] = True

        tool["annotations"] = annotations

        return tool

    def generate_standalone_server(
        self,
        output_path: str,
        title: str = "B2A Custom MCP Server",
        description: str = "Custom MCP server generated from B2A marketplace",
        transport: str = "stdio",
    ) -> str:
        """Refuse the retired ungoverned standalone generator before any effects.

        Keep the callable signature for an explicit error on existing callers.
        The active gateway and tool-manifest generation remain supported.
        """
        raise RuntimeError(
            "Standalone MCP generation is retired; use the governed "
            "POST /mcp/messages flow in docs/quickstart.md instead."
        )

    def generate_tools_list_response(
        self,
        category: ServiceCategory | None = None,
    ) -> dict[str, Any]:
        """
        Generate a tools/list MCP protocol response.

        This is the JSON-RPC response format for the MCP protocol's
        tools/list method.
        """
        manifest = self.generate_tools_json(category=category)
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "tools": manifest["tools"],
            },
        }


_mcp_generator: McpGenerator | None = None


def get_mcp_generator() -> McpGenerator:
    """Get or create the global McpGenerator singleton."""
    global _mcp_generator
    if _mcp_generator is None:
        _mcp_generator = McpGenerator()
    return _mcp_generator
