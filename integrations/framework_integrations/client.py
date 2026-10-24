"""
B2A Client — Framework Integration Core
======================================
HTTP client for Agent Middleware API.
"""

# Public client validation uses ValueError for malformed responses and inputs.
# ruff: noqa: TRY004

import re
from dataclasses import dataclass
from math import isfinite
from typing import Any

import httpx

_ILLEGAL_HEADER_CHARS = ("\r", "\n", "\x00")
# Wallet ids are a single URL path segment (agt-/spn-/chd- plus the ids the
# legacy tests use). Anything else can change which route the request hits.
_WALLET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_TELEMETRY_EVENT_TYPES = frozenset(
    {"error", "warning", "session", "api_call", "llm_trace", "performance", "custom"}
)
_MESSAGE_PRIORITIES = frozenset({"critical", "high", "normal", "low"})


def _base_url(url: object) -> str:
    if not isinstance(url, str) or not url.strip():
        raise ValueError("api_url must not be blank")
    return url.strip().rstrip("/")


def _header_text(value: object, field: str) -> str:
    """Return a header value, or refuse characters that would split the header."""
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


def _permit_header(value: object) -> str:
    if isinstance(value, str) and any(char in value for char in _ILLEGAL_HEADER_CHARS):
        raise ValueError("permit_id contains an illegal header character")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("permit_id must not be blank")
    return value


def _path_wallet_id(value: object) -> str:
    if not isinstance(value, str) or _WALLET_ID.fullmatch(value) is None:
        raise ValueError(
            "wallet_id must be a single token of letters, digits, "
            "underscores, or hyphens"
        )
    return value


def _balance_value(data: object) -> float:
    if not isinstance(data, dict) or "balance" not in data:
        raise ValueError("wallet response did not include balance")
    raw = data["balance"]
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ValueError("wallet balance is not a number")
    try:
        value = float(raw)
    except (ValueError, OverflowError) as exc:
        raise ValueError("wallet balance is not a number") from exc
    if not isfinite(value):
        raise ValueError("wallet balance is not a finite number")
    return value


@dataclass
class B2AConfig:
    """Configuration for B2A client."""

    api_url: str = "http://localhost:8000"
    api_key: str = ""
    wallet_id: str = ""
    timeout: int = 30
    sender_agent_id: str | None = None


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
        return _balance_value(response.json())

    async def emit_telemetry(
        self,
        event: str,
        properties: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Emit one telemetry event as a batch the ingest route accepts.

        ``POST /v1/telemetry/events`` reads ``events[]`` with ``event_type``,
        ``source``, and ``message``. A free-form name such as
        ``task_completed`` is stored as event type ``custom``. Names that
        already match the server's event types are sent as that type.
        """
        if not isinstance(event, str) or not event.strip():
            raise ValueError("event must not be blank")
        if properties is None:
            properties = {}
        if not isinstance(properties, dict):
            raise ValueError("properties must be a dict")
        event_name = event.strip()
        event_type = event_name if event_name in _TELEMETRY_EVENT_TYPES else "custom"
        metadata = dict(properties)
        metadata.setdefault("event", event_name)
        payload = {
            "events": [
                {
                    "event_type": event_type,
                    "source": "framework_integrations",
                    "message": event_name,
                    "metadata": metadata,
                }
            ]
        }
        response = await self._client.post(
            f"{self._base_url}/v1/telemetry/events",
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
        *,
        subject: str = "agent message",
    ) -> dict[str, Any]:
        """Send a message to another agent.

        ``POST /v1/comms/messages`` takes the sender as the ``from_agent``
        query parameter and requires ``to_agent``, ``subject``, and ``body``.
        ``sender_agent_id`` identifies the registered sender. If omitted,
        the wallet id is used for legacy configurations where the IDs match.
        """
        sender = self.config.sender_agent_id
        if sender is None:
            sender = _path_wallet_id(self.config.wallet_id)
        elif not isinstance(sender, str) or _WALLET_ID.fullmatch(sender) is None:
            raise ValueError("sender_agent_id must be a single agent ID token")
        if not isinstance(to_agent_id, str) or not to_agent_id.strip():
            raise ValueError("to_agent_id must not be blank")
        if not isinstance(content, dict):
            raise ValueError("content must be a dict")
        if not isinstance(subject, str) or not subject.strip():
            raise ValueError("subject must not be blank")
        if not isinstance(priority, str):
            raise ValueError("priority must be one of: critical, high, normal, low")
        priority_value = priority.strip().lower()
        if priority_value not in _MESSAGE_PRIORITIES:
            raise ValueError("priority must be one of: critical, high, normal, low")
        payload = {
            "to_agent": to_agent_id,
            "message_type": "request",
            "priority": priority_value,
            "subject": subject.strip(),
            "body": content,
        }
        response = await self._client.post(
            f"{self._base_url}/v1/comms/messages",
            headers=self._headers(),
            params={"from_agent": sender},
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def decide(
        self,
        context: dict[str, Any],
        options: list[str],
    ) -> str:
        """Make an AI-powered decision.

        The route returns the chosen action in ``action``. A missing action
        is an error: the client does not substitute ``options[0]``.
        """
        if not isinstance(context, dict):
            raise ValueError("context must be a dict")
        if not isinstance(options, list) or not all(
            isinstance(option, str) and option.strip() for option in options
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
            raise ValueError("decision response did not include an action")
        return action

    async def heal(self, issue: str, context: dict[str, Any]) -> dict[str, Any]:
        """AI-powered self-healing diagnostics."""
        if not isinstance(context, dict):
            raise ValueError("context must be a dict")
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
        """Create an AWI session for web automation.

        A configured wallet id is sent on the request. Without it, the route
        allows only a bootstrap admin key.
        """
        if not isinstance(target_url, str) or not target_url.strip():
            raise ValueError("target_url must not be blank")
        if (
            isinstance(max_steps, bool)
            or not isinstance(max_steps, int)
            or not 1 <= max_steps <= 1000
        ):
            raise ValueError("max_steps must be an integer from 1 to 1000")
        payload: dict[str, Any] = {
            "target_url": target_url,
            "max_steps": max_steps,
        }
        if self.config.wallet_id:
            payload["wallet_id"] = self.config.wallet_id
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
        permit = _permit_header(permit_id)
        key = _idempotency_header(
            idempotency_key,
            blank_message="idempotency_key must not be blank",
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
                "X-Permit-Id": permit,
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
