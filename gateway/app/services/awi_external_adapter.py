"""
AWI External Adapter — Phase 8
===============================
Bridges external AWI calls from website owners into our internal AWI infrastructure.

Website owners mount this adapter to expose their existing APIs as AWI-compliant
endpoints without changing their human-facing UI.
"""

import logging
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..schemas.awi import (
    AWIStandardAction,
)

logger = logging.getLogger(__name__)


class AWIExternalAdapter:
    """
    Adapter that bridges external AWI calls into our internal AWI infrastructure.

    Website owners mount this to expose AWI-compliant endpoints that:
    1. Authenticate the agent (verify wallet + KYC)
    2. Translate AWI actions to their internal API calls
    3. Proxy results back through our AWI infrastructure
    """

    def __init__(self, middleware_url: str, api_key: str):
        self.middleware_url = middleware_url.rstrip("/")
        self.api_key = api_key
        self._client = httpx.AsyncClient(
            base_url=self.middleware_url,
            headers={"X-API-Key": api_key},
            timeout=30.0,
            follow_redirects=False,
        )
        # Mapped website routes go through a client that never carries the
        # middleware credential, so a route_mapping entry cannot receive it.
        self._internal_client = httpx.AsyncClient(
            base_url=self.middleware_url,
            timeout=30.0,
            follow_redirects=False,
        )

    async def close(self):
        """Close the HTTP clients."""
        await self._client.aclose()
        await self._internal_client.aclose()

    async def discover_manifest(self) -> dict[str, Any]:
        """Fetch the AWI manifest from the configured control plane."""
        response = await self._client.get("/v1/awi/vocabulary")
        response.raise_for_status()
        return response.json()

    async def create_external_session(
        self,
        target_url: str,
        agent_wallet_id: str,
        max_steps: int = 100,
    ) -> dict[str, Any]:
        """
        Create a session for an external agent.

        The adapter:
        1. Verifies the agent's wallet + KYC status
        2. Creates an internal AWI session
        3. Returns session details to the external agent
        """
        response = await self._client.post(
            "/v1/awi/sessions",
            json={
                "target_url": target_url,
                "wallet_id": agent_wallet_id,
                "max_steps": max_steps,
            },
        )
        response.raise_for_status()
        return response.json()

    async def execute_action_for_external(
        self,
        session_id: str,
        action: AWIStandardAction,
        parameters: dict[str, Any],
        route_mapping: dict[str, str],
        *,
        permit_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """
        Execute an AWI action, translating it to internal API calls.

        The adapter maps the standardized AWI action to the website's
        internal routes and executes the call. The governed
        ``POST /v1/awi/execute`` call (authorize, meter, receipt) must succeed
        first, with a 2xx *and* ``status == "success"``; the side-effecting
        internal call runs only after it does.
        ``permit_id`` and ``idempotency_key`` are forwarded to that governed
        call as ``X-Permit-Id`` and ``Idempotency-Key``, which it requires.
        """
        action_str = action.value if isinstance(action, AWIStandardAction) else action

        mapped_route = route_mapping.get(action_str)
        if not mapped_route:
            return {
                "success": False,
                "error": f"No route mapping for action: {action_str}",
            }
        if not _is_relative_route(mapped_route):
            return {
                "success": False,
                "error": (
                    f"Route mapping for action {action_str} must be a relative "
                    "path, not an absolute URL"
                ),
            }

        governance_headers: dict[str, str] = {}
        if permit_id is not None:
            governance_headers["X-Permit-Id"] = permit_id
        if idempotency_key is not None:
            governance_headers["Idempotency-Key"] = idempotency_key

        response = await self._client.post(
            "/v1/awi/execute",
            json={
                "session_id": session_id,
                "action": action_str,
                "parameters": parameters,
                "dry_run": False,
            },
            headers=governance_headers,
        )
        response.raise_for_status()
        awi_response = response.json()

        # A 2xx is not a go-ahead on its own: the session layer answers 200
        # with status "paused" (human intervention), "passkey_required",
        # "max_steps_reached" or "error". Only an executed action may reach
        # the website's side-effecting route.
        awi_status = (
            awi_response.get("status") if isinstance(awi_response, dict) else None
        )
        if awi_status != "success":
            return {
                "success": False,
                "error": (
                    f"Governed AWI execute did not succeed (status: {awi_status}); "
                    "internal route not called"
                ),
                "awi_response": awi_response,
                "internal_result": None,
            }

        result = await self._execute_internal_call(mapped_route, parameters)

        return {
            "awi_response": awi_response,
            "internal_result": result,
        }

    async def _execute_internal_call(
        self, route: str, parameters: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute an internal API call (relative routes only, no credential)."""
        if not _is_relative_route(route):
            return {"error": "internal route must be a relative path"}
        try:
            response = await self._internal_client.post(route, json=parameters)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            logger.error(f"Internal call failed: {route} - {e}")
            return {"error": str(e)}

    async def handle_intervention(
        self, session_id: str, intervention_type: str, reason: str | None = None
    ) -> dict[str, Any]:
        """Handle human intervention (pause/resume/steer)."""
        response = await self._client.post(
            "/v1/awi/intervene",
            json={
                "session_id": session_id,
                "action": intervention_type,
                "reason": reason,
            },
        )
        response.raise_for_status()
        return response.json()

    async def generate_external_manifest(
        self,
        name: str,
        version: str,
        actions: list[dict[str, Any]],
        representations: list[str],
    ) -> dict[str, Any]:
        """
        Generate an AWI manifest for external consumption.

        This manifest is served at /.well-known/awi.json for agents
        to discover available actions.
        """
        return {
            "name": name,
            "version": version,
            "awi_version": "1.0.0",
            "framework": "external",
            "actions": actions,
            "representations": [
                {"type": rep, "available": True} for rep in representations
            ],
            "endpoints": {
                "sessions": "/awi/sessions",
                "execute": "/awi/execute",
                "represent": "/awi/represent",
                "intervene": "/awi/intervene",
            },
            "security": {
                "auth_required": True,
                "wallet_required": True,
                "kyc_required": True,
            },
        }


class AWIFallbackAdapter:
    """
    Fallback adapter when a site doesn't implement AWI natively.

    Uses our existing MCP proxy as a fallback, providing a degraded
    but functional experience for agents.
    """

    def __init__(self, middleware_url: str, api_key: str):
        self.awi = AWIExternalAdapter(middleware_url, api_key)

    async def discover(self) -> dict[str, Any]:
        """Discover via MCP fallback."""
        try:
            return await self.awi.discover_manifest()
        except httpx.HTTPError as exc:
            logger.warning(
                "awi_fallback_discover_failed: %s", type(exc).__name__, exc_info=True
            )
            return {
                "name": "MCP Fallback",
                "actions": [],
                "note": "AWI not available, using MCP proxy fallback",
            }

    async def create_session(self, target_url: str, wallet_id: str) -> dict[str, Any]:
        """Create session with fallback.

        No session exists when the control plane is unreachable, so none is
        reported: ``session_id`` is ``None`` and ``status`` is ``unavailable``.
        """
        try:
            return await self.awi.create_external_session(target_url, wallet_id)
        except httpx.HTTPError as exc:
            logger.warning(
                "awi_fallback_create_session_failed: %s",
                type(exc).__name__,
                exc_info=True,
            )
            return {
                "session_id": None,
                "target_url": target_url,
                "status": "unavailable",
                "error": type(exc).__name__,
            }


def _is_relative_route(route: str) -> bool:
    """True when ``route`` names a path, not a URL with a scheme or host."""
    parts = urlsplit(route)
    return not parts.scheme and not parts.netloc


_awi_external_adapter: AWIExternalAdapter | None = None


def get_awi_external_adapter() -> AWIExternalAdapter | None:
    """Get singleton adapter instance."""
    return _awi_external_adapter
