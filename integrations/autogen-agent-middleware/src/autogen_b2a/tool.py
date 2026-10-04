"""AutoGen function tools for Agent Middleware API."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from autogen.agentchat.conversable_agent import ConversableAgent

from b2a_sdk.models import PermitRequest

from .client import B2AClient


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
        permit_budget: Decimal = Decimal("100"),
        permit_ttl_minutes: int = 30,
    ):
        self.client = B2AClient(
            api_key=api_key,
            base_url=base_url,
        )
        self.wallet_id = wallet_id
        self.permit_budget = permit_budget
        self.permit_ttl_minutes = permit_ttl_minutes
        # Cache permits to avoid 409 on replay (server hashes full permit body including expires_at)
        self._permit_cache: dict[str, str] = {}  # permit_idempotency_key → permit_id
        self._permit_requests: dict[str, PermitRequest] = {}

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
        if not idempotency_key or not idempotency_key.strip():
            raise ValueError("idempotency_key is required and must not be blank")
        if not permit_idempotency_key or not permit_idempotency_key.strip():
            raise ValueError("permit_idempotency_key is required and must not be blank")

        # Retain the body before the first await: a lost acknowledgement must
        # replay the original expiry under the same caller-owned key.
        request = self._permit_requests.get(permit_idempotency_key)
        if request is None:
            request = PermitRequest(
                issuer_wallet_id=self.wallet_id,
                subject_wallet_id=self.wallet_id,
                max_credits=self.permit_budget,
                expires_at=datetime.now(timezone.utc)
                + timedelta(minutes=self.permit_ttl_minutes),
                allowed_tools=[tool_name],
                scopes=[f"tool:{tool_name}:invoke", "billing:charge"],
            )
            self._permit_requests[permit_idempotency_key] = request
        elif (
            request.subject_wallet_id != self.wallet_id
            or request.max_credits != self.permit_budget
            or request.allowed_tools != [tool_name]
        ):
            raise ValueError(
                "permit_idempotency_key reused with different permit terms"
            )

        if permit_idempotency_key in self._permit_cache:
            permit_id = self._permit_cache[permit_idempotency_key]
        else:
            permit = await self.client.create_permit(
                request, idempotency_key=permit_idempotency_key
            )
            permit_id = permit.permit_id
            self._permit_cache[permit_idempotency_key] = permit_id

        result = await self.client.invoke_tool(
            tool_name,
            arguments,
            wallet_id=self.wallet_id,
            permit_id=permit_id,
            idempotency_key=idempotency_key,
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
        return await self.client.get_balance(self.wallet_id)

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
