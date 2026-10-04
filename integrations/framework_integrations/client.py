"""
B2A Client — Framework Integration Core
======================================
HTTP client for Agent Middleware API.
"""

from dataclasses import dataclass
from typing import Any, Optional
import httpx


@dataclass
class B2AConfig:
    """Configuration for B2A client."""

    api_url: str = "http://localhost:8000"
    api_key: str = ""
    wallet_id: str = ""
    timeout: int = 30


class B2AClient:
    """
    Client for Agent Middleware API.

    Provides methods for:
    - Billing operations
    - Telemetry
    - Agent communication
    - AI decision making
    - MCP tool discovery and execution
    - AWI session management
    """

    def __init__(self, config: Optional[B2AConfig] = None, **kwargs):
        if config is None:
            config = B2AConfig(**kwargs)
        self.config = config
        self._client = httpx.AsyncClient(timeout=config.timeout)

    def _headers(self) -> dict[str, str]:
        return {
            "X-API-Key": self.config.api_key,
            "Content-Type": "application/json",
        }

    async def close(self):
        await self._client.aclose()

    async def get_balance(self) -> float:
        """Get current wallet balance."""
        response = await self._client.get(
            f"{self.config.api_url}/v1/billing/wallets/{self.config.wallet_id}",
            headers=self._headers(),
        )
        response.raise_for_status()
        data = response.json()
        return float(data.get("balance", 0))

    async def emit_telemetry(
        self,
        event: str,
        properties: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Emit a telemetry event."""
        payload = {
            "event": event,
            "agent_id": self.config.wallet_id,
            "properties": properties or {},
        }
        response = await self._client.post(
            f"{self.config.api_url}/v1/telemetry/events",
            headers=self._headers(),
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def send_message(
        self,
        to_agent_id: str,
        content: dict[str, Any],
        priority: str = "normal",
    ) -> dict[str, Any]:
        """Send a message to another agent."""
        payload = {
            "from_agent_id": self.config.wallet_id,
            "to_agent_id": to_agent_id,
            "content": content,
            "priority": priority,
        }
        response = await self._client.post(
            f"{self.config.api_url}/v1/comms/messages",
            headers=self._headers(),
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def decide(
        self,
        context: dict[str, Any],
        options: list[str],
    ) -> str:
        """Make an AI-powered decision."""
        payload = {
            "agent_id": self.config.wallet_id,
            "context": context,
            "options": options,
        }
        response = await self._client.post(
            f"{self.config.api_url}/v1/ai/decide",
            headers=self._headers(),
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        return data.get("decision", options[0])

    async def heal(self, issue: str, context: dict[str, Any]) -> dict[str, Any]:
        """AI-powered self-healing diagnostics."""
        payload = {
            "issue": issue,
            "context": context,
        }
        response = await self._client.post(
            f"{self.config.api_url}/v1/ai/heal",
            headers=self._headers(),
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def create_awi_session(
        self,
        target_url: str,
        max_steps: int = 100,
    ) -> dict[str, Any]:
        """Create an AWI session for web automation."""
        payload = {
            "target_url": target_url,
            "max_steps": max_steps,
        }
        response = await self._client.post(
            f"{self.config.api_url}/v1/awi/sessions",
            headers=self._headers(),
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def execute_awi_action(
        self,
        session_id: str,
        action: str,
        parameters: dict[str, Any],
        *,
        permit_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Call the proof-only AWI route with its required governance headers.

        Reuse the caller-owned idempotency key verbatim for one logical action.
        The server remains responsible for authorization and accounting.
        """
        if not isinstance(permit_id, str) or not permit_id.strip():
            raise ValueError("permit_id must not be blank")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key must not be blank")
        if len(idempotency_key) > 128:
            raise ValueError("idempotency_key must be at most 128 characters")
        payload = {
            "session_id": session_id,
            "action": action,
            "parameters": parameters,
        }
        response = await self._client.post(
            f"{self.config.api_url}/v1/awi/execute",
            headers={
                **self._headers(),
                "X-Permit-Id": permit_id,
                "Idempotency-Key": idempotency_key,
            },
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def get_mcp_tools(self) -> list[dict[str, Any]]:
        """Get available MCP tools."""
        response = await self._client.get(
            f"{self.config.api_url}/mcp/tools.json",
            headers=self._headers(),
        )
        response.raise_for_status()
        data = response.json()
        return data.get("tools", [])

    async def discover(self) -> dict[str, Any]:
        """Get the discovery manifest."""
        response = await self._client.get(
            f"{self.config.api_url}/v1/discover",
            headers=self._headers(),
        )
        response.raise_for_status()
        return response.json()

    async def charge(
        self,
        service_category: str,
        units: float = 1.0,
        description: str = "",
        *,
        idempotency_key: str,
        request_path: Optional[str] = None,
    ) -> dict[str, Any]:
        """Charge the configured wallet for metered usage.

        Follows the ``POST /v1/billing/charge`` contract: the wallet, service
        category and units travel as query parameters, and the server prices
        the units (a caller cannot name an amount). ``idempotency_key`` is
        required and caller-owned: retrying with the same key replays the
        original outcome instead of debiting the wallet a second time.
        """
        # The server treats an empty Idempotency-Key as "no key", which would
        # silently drop the double-charge protection, so refuse it here.
        if not idempotency_key or not idempotency_key.strip():
            raise ValueError("idempotency_key is required and must not be blank")
        params: dict[str, str | float] = {
            "wallet_id": self.config.wallet_id,
            "service": service_category,
            "units": units,
        }
        if description:
            params["description"] = description
        if request_path:
            params["request_path"] = request_path
        response = await self._client.post(
            f"{self.config.api_url}/v1/billing/charge",
            headers={**self._headers(), "Idempotency-Key": idempotency_key},
            params=params,
        )
        response.raise_for_status()
        return response.json()
