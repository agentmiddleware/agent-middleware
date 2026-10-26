"""
B2A Client — Framework Integration Core
======================================
HTTP client for Agent Middleware API.
"""

# Public client validation uses ValueError for malformed responses and inputs.
# ruff: noqa: TRY004

import math
import re
from dataclasses import dataclass
from typing import Any

import httpx

_TELEMETRY_EVENT_TYPES = {
    "error",
    "warning",
    "session",
    "api_call",
    "llm_trace",
    "performance",
    "custom",
}
_TELEMETRY_SEVERITIES = {"critical", "high", "medium", "low", "info"}
_MESSAGE_PRIORITIES = {"critical", "high", "normal", "low"}
_ILLEGAL_HEADER_CHARS = ("\r", "\n", "\x00")
_WALLET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def _base_url(url: object) -> str:
    if not isinstance(url, str) or not url.strip():
        raise ValueError("api_url must not be blank")
    return url.strip().rstrip("/")


def _header_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if any(char in value for char in _ILLEGAL_HEADER_CHARS):
        raise ValueError(f"{field} contains an illegal header character")
    return value


def _idempotency_header(value: object, *, blank_message: str) -> str:
    if isinstance(value, str) and any(char in value for char in _ILLEGAL_HEADER_CHARS):
        raise ValueError("idempotency_key contains an illegal header character")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(blank_message)
    if len(value) > 128:
        raise ValueError("idempotency_key must be at most 128 characters")
    return value


def _path_wallet_id(value: object) -> str:
    if not isinstance(value, str) or _WALLET_ID.fullmatch(value) is None:
        raise ValueError("wallet_id must be a single URL path segment")
    return value


@dataclass
class B2AConfig:
    """Configuration for B2A client."""

    api_url: str = "http://localhost:8000"
    api_key: str = ""
    wallet_id: str = ""
    timeout: int = 30
    agent_id: str = ""


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

    def __init__(self, config: B2AConfig | None = None, **kwargs):
        if config is None:
            config = B2AConfig(**kwargs)
        self.config = config
        self._base_url = _base_url(config.api_url)
        self._client = httpx.AsyncClient(timeout=config.timeout)

    def _headers(self) -> dict[str, str]:
        return {
            "X-API-Key": _header_text(self.config.api_key, "api_key"),
            "Content-Type": "application/json",
        }

    async def close(self):
        await self._client.aclose()

    async def get_balance(self) -> float:
        """Get current wallet balance."""
        wallet_id = _path_wallet_id(self.config.wallet_id)
        response = await self._client.get(
            f"{self._base_url}/v1/billing/wallets/{wallet_id}",
            headers=self._headers(),
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or "balance" not in data:
            raise ValueError("wallet response is missing balance")
        balance = data["balance"]
        # bool is an int subclass, so reject it before the number check.
        if isinstance(balance, bool) or not isinstance(balance, (int, float)):
            raise ValueError("wallet balance must be a finite number")
        try:
            numeric_balance = float(balance)
        except (OverflowError, ValueError) as exc:
            raise ValueError("wallet balance is not a number") from exc
        if not math.isfinite(numeric_balance):
            raise ValueError("wallet balance must be a finite number")
        return numeric_balance

    async def emit_telemetry(
        self,
        event: str,
        properties: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Emit one telemetry event using the gateway single-event contract."""
        if properties is not None and not isinstance(properties, dict):
            raise ValueError("properties must be a dict")
        props = dict(properties or {})
        event_type = event if event in _TELEMETRY_EVENT_TYPES else "custom"
        message = props.pop("message", None)
        if not isinstance(message, str) or not message.strip():
            message = event
        if not isinstance(message, str) or not message.strip():
            raise ValueError("telemetry message must be a non-blank string")
        source = props.pop("source", None)
        if not isinstance(source, str) or not source.strip():
            source = "framework_integrations"
        severity = props.pop("severity", None)
        if not isinstance(severity, str) or severity not in _TELEMETRY_SEVERITIES:
            severity = "info"
        # Keep the caller's name when it is not already a gateway event type.
        if event not in _TELEMETRY_EVENT_TYPES and "event" not in props:
            props["event"] = event
        payload = {
            "event_type": event_type,
            "source": source,
            "message": message,
            "severity": severity,
            "metadata": props,
        }
        response = await self._client.post(
            f"{self._base_url}/v1/telemetry/events/single",
            headers=self._headers(),
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        failed = data.get("failed") if isinstance(data, dict) else None
        if type(failed) is not int or failed < 0:
            raise ValueError("telemetry response did not include a valid failed count")
        if failed:
            raise RuntimeError("telemetry ingestion failed")
        return data

    async def send_message(
        self,
        to_agent_id: str,
        content: dict[str, Any],
        priority: str = "normal",
    ) -> dict[str, Any]:
        """Send a message to another agent."""
        if (
            not isinstance(self.config.agent_id, str)
            or not self.config.agent_id.strip()
        ):
            raise ValueError("agent_id is required to send messages")
        if _WALLET_ID.fullmatch(self.config.agent_id) is None:
            raise ValueError("agent_id must be a single agent ID token")
        if not isinstance(to_agent_id, str) or not to_agent_id.strip():
            raise ValueError("to_agent_id must not be blank")
        if not isinstance(content, dict):
            raise ValueError("content must be a dict")
        if not isinstance(priority, str):
            raise ValueError("priority must be one of: critical, high, normal, low")
        priority_value = priority.strip().lower()
        if priority_value not in _MESSAGE_PRIORITIES:
            raise ValueError("priority must be one of: critical, high, normal, low")
        subject = "message"
        raw_subject = content.get("subject") if isinstance(content, dict) else None
        if isinstance(raw_subject, str) and raw_subject.strip():
            subject = raw_subject
        payload = {
            "to_agent": to_agent_id,
            "message_type": "request",
            "priority": priority_value,
            "subject": subject,
            "body": content,
        }
        response = await self._client.post(
            f"{self._base_url}/v1/comms/messages",
            headers=self._headers(),
            params={"from_agent": self.config.agent_id.strip()},
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
        if (
            not isinstance(options, list)
            or not options
            or not all(isinstance(option, str) and option.strip() for option in options)
        ):
            raise ValueError("options must be a list of non-blank strings")
        payload = {
            "agent_id": self.config.wallet_id,
            "context": context,
            "options": options,
        }
        response = await self._client.post(
            f"{self._base_url}/v1/ai/decide",
            headers=self._headers(),
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        action = data.get("action") if isinstance(data, dict) else None
        if not isinstance(action, str) or not action.strip():
            raise ValueError("decide response is missing a non-blank action")
        return action

    async def heal(self, issue: str, context: dict[str, Any]) -> dict[str, Any]:
        """AI-powered self-healing diagnostics."""
        payload = {
            "issue": issue,
            "context": context,
        }
        response = await self._client.post(
            f"{self._base_url}/v1/ai/heal",
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
        # bool is an int, and the gateway accepts only 1..1000.
        if (
            isinstance(max_steps, bool)
            or not isinstance(max_steps, int)
            or max_steps < 1
            or max_steps > 1000
        ):
            raise ValueError("max_steps must be an integer from 1 to 1000")
        if not isinstance(target_url, str) or not target_url.strip():
            raise ValueError("target_url must not be blank")
        payload = {
            "target_url": target_url,
            "wallet_id": self.config.wallet_id,
            "max_steps": max_steps,
        }
        response = await self._client.post(
            f"{self._base_url}/v1/awi/sessions",
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
        if isinstance(permit_id, str) and any(
            char in permit_id for char in _ILLEGAL_HEADER_CHARS
        ):
            raise ValueError("permit_id contains an illegal header character")
        if not isinstance(permit_id, str) or not permit_id.strip():
            raise ValueError("permit_id must not be blank")
        key = _idempotency_header(
            idempotency_key, blank_message="idempotency_key must not be blank"
        )
        payload = {
            "session_id": session_id,
            "action": action,
            "parameters": parameters,
        }
        response = await self._client.post(
            f"{self._base_url}/v1/awi/execute",
            headers={
                **self._headers(),
                "X-Permit-Id": permit_id,
                "Idempotency-Key": key,
            },
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def get_mcp_tools(self) -> list[dict[str, Any]]:
        """Get available MCP tools."""
        response = await self._client.get(
            f"{self._base_url}/mcp/tools.json",
            headers=self._headers(),
        )
        response.raise_for_status()
        data = response.json()
        tools = data.get("tools") if isinstance(data, dict) else None
        if not isinstance(tools, list):
            raise ValueError("tools manifest did not include a list of tools")
        return tools

    async def discover(self) -> dict[str, Any]:
        """Get the discovery manifest."""
        response = await self._client.get(
            f"{self._base_url}/v1/discover",
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
        request_path: str | None = None,
    ) -> dict[str, Any]:
        """Charge the configured wallet for metered usage.

        Follows the ``POST /v1/billing/charge`` contract: the wallet, service
        category and units travel as query parameters, and the server prices
        the units (a caller cannot name an amount). ``idempotency_key`` is
        required and caller-owned: retrying with the same key replays the
        original outcome instead of debiting the wallet a second time.
        """
        key = _idempotency_header(
            idempotency_key,
            blank_message="idempotency_key is required and must not be blank",
        )
        # The server prices the units; a negative or non-finite quantity must
        # never reach it as a charge.
        if isinstance(units, bool) or not isinstance(units, (int, float)):
            raise ValueError("units must be a finite number and must not be negative")
        if not math.isfinite(units) or units < 0:
            raise ValueError("units must be a finite number and must not be negative")
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
            f"{self._base_url}/v1/billing/charge",
            headers={**self._headers(), "Idempotency-Key": key},
            params=params,
        )
        response.raise_for_status()
        return response.json()
