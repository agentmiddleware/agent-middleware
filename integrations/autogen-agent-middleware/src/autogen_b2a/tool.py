"""AutoGen function tools for Agent Middleware API."""

import copy
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from autogen.agentchat.conversable_agent import ConversableAgent
from b2a_sdk.models import PermitRequest

from .client import B2AClient

# Same limit the SDK and the trust plane enforce. A longer key must fail
# before a permit body is saved under it.
_MAX_IDEMPOTENCY_KEY_LENGTH = 128
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


def _normalize_idempotency_key(value: object, label: str) -> str:
    """Return the key the SDK will send, or reject it before any permit is pinned.

    ``b2a_sdk`` strips surrounding whitespace and then refuses a blank or
    oversized key. The trust plane stores that text as sent and refuses
    control characters. The permit cache has to use the same text: a padded
    key and its stripped form are one server key, and a second permit body
    under that key is a conflict, not a replay.
    """
    if not isinstance(value, str):
        raise TypeError(f"{label} is required and must be a string")
    key = value.strip()
    if not key:
        raise ValueError(f"{label} is required and must not be blank")
    if len(key) > _MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ValueError(
            f"{label} must be at most {_MAX_IDEMPOTENCY_KEY_LENGTH} characters"
        )
    if _CONTROL_CHARACTERS.search(key):
        raise ValueError(f"{label} must not contain control characters")
    return key


def _require_tool_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("tool_name is required and must not be blank")
    return value


def _require_permit_budget(value: object) -> Decimal:
    """Reject a budget the trust plane will not mint, before the key is pinned."""
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, float, str)):
        raise TypeError("permit_budget must be a positive decimal")
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("permit_budget must be a positive decimal") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("permit_budget must be a positive decimal")
    # The trust plane stores Numeric(20, 8) through a float-backed SQLite
    # path. Reject values that would change when stored before pinning a key.
    if amount >= Decimal(1000000000000) or Decimal(f"{float(amount):.8f}") != amount:
        raise ValueError("permit_budget cannot be stored exactly")
    return amount


def _require_positive_ttl(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError("permit_ttl_minutes must be greater than zero")


class B2AFunctionTool:
    """AutoGen-compatible function tool for Agent Middleware API via governed permit→invoke→receipt flow.

    Provides MCP tools and wallet operations as callable functions for AutoGen agents.
    All invocations return signed receipts.
    """

    def __init__(
        self,
        api_key: str,
        wallet_id: str,
        base_url: str = "https://api.thisisatest.tech",
        permit_budget: Decimal = Decimal(100),
        permit_ttl_minutes: int = 30,
    ):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key is required and must not be blank")
        if not isinstance(wallet_id, str) or not wallet_id.strip():
            raise ValueError("wallet_id is required and must not be blank")
        self.client = B2AClient(
            api_key=api_key,
            base_url=base_url,
        )
        self.wallet_id = wallet_id
        self.permit_budget = permit_budget
        self.permit_ttl_minutes = permit_ttl_minutes
        # Cache permits to avoid 409 on replay (server hashes full permit body including expires_at).
        # Keys here are the stripped text the SDK sends, not the raw caller string.
        self._permit_cache: dict[str, str] = {}  # permit_idempotency_key → permit_id
        self._permit_requests: dict[str, PermitRequest] = {}

    def _current_wallet_id(self) -> str:
        wallet_id = self.wallet_id
        if not isinstance(wallet_id, str) or not wallet_id.strip():
            raise ValueError("wallet_id is required and must not be blank")
        return wallet_id

    async def discover_tools(self) -> list[dict[str, Any]]:
        """Discover all available MCP tools."""
        tools = await self.client.discover_tools()
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in tools
        ]

    async def call_mcp_tool(
        self,
        tool_name: str,
        idempotency_key: str,
        permit_idempotency_key: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call an MCP tool via governed permit→invoke→receipt flow.

        Args:
            tool_name: Name of the tool to call
            idempotency_key: Caller-supplied idempotency key (required)
            permit_idempotency_key: Caller-supplied permit idempotency key (required)
            arguments: Tool arguments
        """
        if arguments is None:
            arguments = {}
        elif not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
        # Freeze the mapping before the first await. A caller that reuses the
        # dict must not change the invocation that is already in flight.
        arguments = copy.deepcopy(arguments)

        tool_name = _require_tool_name(tool_name)
        invoke_key = _normalize_idempotency_key(idempotency_key, "idempotency_key")
        permit_key = _normalize_idempotency_key(
            permit_idempotency_key, "permit_idempotency_key"
        )
        # Read wallet, budget, and lifetime once. A later change to those
        # attributes belongs to the next call, not to the permit this call mints.
        wallet_id = self._current_wallet_id()
        permit_budget = _require_permit_budget(self.permit_budget)
        permit_ttl_minutes = self.permit_ttl_minutes
        _require_positive_ttl(permit_ttl_minutes)

        # Retain the body before the first await: a lost acknowledgement must
        # replay the original expiry under the same caller-owned key.
        request = self._permit_requests.get(permit_key)
        if request is None:
            request = PermitRequest(
                issuer_wallet_id=wallet_id,
                subject_wallet_id=wallet_id,
                max_credits=permit_budget,
                expires_at=datetime.now(UTC) + timedelta(minutes=permit_ttl_minutes),
                allowed_tools=[tool_name],
                scopes=[f"tool:{tool_name}:invoke", "billing:charge"],
            )
            self._permit_requests[permit_key] = request
        elif (
            request.subject_wallet_id != wallet_id
            or request.max_credits != permit_budget
            or request.allowed_tools != [tool_name]
        ):
            raise ValueError(
                "permit_idempotency_key reused with different permit terms"
            )

        if permit_key in self._permit_cache:
            permit_id = self._permit_cache[permit_key]
        else:
            permit = await self.client.create_permit(
                request, idempotency_key=permit_key
            )
            permit_id = permit.permit_id
            self._permit_cache[permit_key] = permit_id

        result = await self.client.invoke_tool(
            tool_name,
            arguments,
            wallet_id=wallet_id,
            permit_id=permit_id,
            idempotency_key=invoke_key,
        )

        return {
            "content": result.content,
            "structured_content": result.structured_content,
            "receipt_id": result.receipt.receipt_id,
            "credits_charged": str(result.receipt.credits_charged),
            "signature": result.receipt.signature,
        }

    async def get_wallet_balance(self) -> float:
        """Get current wallet balance."""
        return await self.client.get_balance(self._current_wallet_id())

    def get_function_schemas(self) -> list[dict[str, Any]]:
        """Get OpenAI function schemas for all available operations."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "discover_tools",
                    "description": "Discover all available MCP tools from Agent Middleware API",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "call_mcp_tool",
                    "description": "Call a specific MCP tool via governed permit→invoke→receipt flow. Returns signed receipt.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "tool_name": {
                                "type": "string",
                                "description": "Name of the MCP tool to call",
                            },
                            "idempotency_key": {
                                "type": "string",
                                "description": "Caller-supplied idempotency key (required, must be unique per invocation)",
                            },
                            "permit_idempotency_key": {
                                "type": "string",
                                "description": "Caller-supplied permit idempotency key (required, must be stable for replay)",
                            },
                            "arguments": {
                                "type": "object",
                                "description": "Arguments to pass to the tool",
                            },
                        },
                        "required": [
                            "tool_name",
                            "idempotency_key",
                            "permit_idempotency_key",
                        ],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_wallet_balance",
                    "description": "Get the current wallet balance in credits",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]


def register_b2a_tools(agent: ConversableAgent, b2a_tool: B2AFunctionTool) -> None:
    """Register B2A tools with an AutoGen agent.

    Args:
        agent: AutoGen ConversableAgent instance
        b2a_tool: B2AFunctionTool instance configured with API credentials
    """
    function_schemas = b2a_tool.get_function_schemas()
    for schema in function_schemas:
        agent.register_function(
            function_map={
                schema["function"]["name"]: getattr(
                    b2a_tool, schema["function"]["name"]
                ),
            }
        )
