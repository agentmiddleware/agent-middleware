"""Internal tool implementations."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from math import isnan
from typing import Any

from b2a_sdk.models import PermitRequest
from langchain_core.tools import StructuredTool

from .client import B2AClient

# Same limit the shared client and the server enforce.
_MAX_IDEMPOTENCY_KEY_LENGTH = 128


def _require_wallet_id(wallet_id: object) -> None:
    if not isinstance(wallet_id, str) or not wallet_id.strip():
        raise ValueError("wallet_id is required and must not be blank")


def _require_permit_budget(permit_budget: object) -> None:
    if isinstance(permit_budget, bool) or not isinstance(permit_budget, (Decimal, int, float)):
        raise ValueError("permit_budget must be a positive amount")  # noqa: TRY004 - caller contract
    if isinstance(permit_budget, Decimal):
        if not permit_budget.is_finite() or permit_budget <= 0:
            raise ValueError("permit_budget must be a positive amount")
        return
    if (isinstance(permit_budget, float) and isnan(permit_budget)) or permit_budget <= 0:
        raise ValueError("permit_budget must be a positive amount")


def _require_permit_ttl(permit_ttl_minutes: object) -> None:
    if isinstance(permit_ttl_minutes, bool) or not isinstance(permit_ttl_minutes, (int, float)):
        raise ValueError(  # noqa: TRY004 - caller contract
            "permit_ttl_minutes must be a positive number of minutes"
        )
    if permit_ttl_minutes <= 0:
        raise ValueError("permit_ttl_minutes must be a positive number of minutes")


def _caller_idempotency_key(value: object, field: str) -> str:
    """Return the key text the shared client will send.

    The client strips surrounding whitespace, then refuses a blank or over-long
    key. The permit cache has to use that same text. Caching the raw caller
    string treats a padded key and its stripped form as two permits, so the
    second call builds a new expiry and the server rejects the key as reused.

    Control characters are refused before a permit is requested. The server
    refuses them, and a newline does not belong in a request header.
    """
    if not isinstance(value, str):
        raise ValueError(f"{field} is required and must not be blank")  # noqa: TRY004 - caller contract
    key = value.strip()
    if not key:
        raise ValueError(f"{field} is required and must not be blank")
    if len(key) > _MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ValueError(f"{field} must be at most {_MAX_IDEMPOTENCY_KEY_LENGTH} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in key):
        raise ValueError(f"{field} must not contain control characters")
    return key


def _require_tool_name(tool_name: object) -> str:
    if not isinstance(tool_name, str) or not tool_name.strip():
        raise ValueError("tool_name is required and must not be blank")
    if any(ord(char) < 32 or ord(char) == 127 for char in tool_name):
        raise ValueError("tool_name must not contain control characters")
    return tool_name


def create_mcp_tool(
    client: B2AClient,
    *,
    wallet_id: str,
    permit_budget: Decimal = Decimal(100),
    permit_ttl_minutes: int = 30,
) -> StructuredTool:
    """Create a LangChain tool that calls MCP endpoints via governed permit→invoke→receipt flow.

    Args:
        client: AgentMiddlewareClient instance
        wallet_id: Wallet ID for billing
        permit_budget: Maximum credits per permit (default 100)
        permit_ttl_minutes: Permit lifetime in minutes (default 30)
    """
    _require_wallet_id(wallet_id)
    _require_permit_budget(permit_budget)
    _require_permit_ttl(permit_ttl_minutes)
    # Cache permits by permit_idempotency_key to avoid 409 on replay
    # Server hashes the FULL permit request body including expires_at.
    # Sending different expires_at with same key → 409 IdempotencyConflictError.
    # Keep the original body before sending and the ID after acknowledgement.
    permit_cache: dict[str, str] = {}  # permit_idempotency_key → permit_id
    permit_requests: dict[str, PermitRequest] = {}

    async def call_mcp(
        tool_name: str,
        idempotency_key: str,
        permit_idempotency_key: str,
        arguments: dict[str, Any] | None = None,
    ) -> str:
        """Call an MCP tool via permit→invoke→receipt flow.

        Args:
            tool_name: Name of the MCP tool to call
            idempotency_key: Caller-supplied idempotency key for invoke_tool (required)
            permit_idempotency_key: Caller-supplied permit idempotency key (required).
                Reused across replays to avoid creating multiple permits.
            arguments: Arguments to pass to the tool

        Idempotency and replay:
            On first call, creates a permit and caches permit_id by permit_idempotency_key.
            On replay with same permit_idempotency_key, reuses the cached permit_id.
            This avoids 409 IdempotencyConflictError from different expires_at timestamps.
        """
        idempotency_key = _caller_idempotency_key(idempotency_key, "idempotency_key")
        permit_idempotency_key = _caller_idempotency_key(
            permit_idempotency_key, "permit_idempotency_key"
        )
        tool_name = _require_tool_name(tool_name)
        if arguments is None:
            arguments = {}
        elif not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")

        # Snapshot before awaiting creation, including a response lost after
        # acceptance. Concurrent coroutines also see this same request body.
        request = permit_requests.get(permit_idempotency_key)
        if request is None:
            request = PermitRequest(
                issuer_wallet_id=wallet_id,
                subject_wallet_id=wallet_id,
                max_credits=permit_budget,
                expires_at=datetime.now(UTC) + timedelta(minutes=permit_ttl_minutes),
                allowed_tools=[tool_name],
                scopes=[f"tool:{tool_name}:invoke", "billing:charge"],
            )
            permit_requests[permit_idempotency_key] = request
        elif request.allowed_tools != [tool_name]:
            raise ValueError("permit_idempotency_key reused with different permit terms")

        if permit_idempotency_key in permit_cache:
            permit_id = permit_cache[permit_idempotency_key]
        else:
            permit = await client.create_permit(request, idempotency_key=permit_idempotency_key)
            permit_id = permit.permit_id
            permit_cache[permit_idempotency_key] = permit_id

        result = await client.invoke_tool(
            tool_name,
            arguments,
            wallet_id=wallet_id,
            permit_id=permit_id,
            idempotency_key=idempotency_key,
        )

        return str(
            {
                "content": result.content,
                "structured_content": result.structured_content,
                "receipt_id": result.receipt.receipt_id,
                "credits_charged": str(result.receipt.credits_charged),
                "signature": result.receipt.signature,
            }
        )

    # call_mcp is async, so it must go in the coroutine slot; passing it as
    # func makes ainvoke return an un-awaited coroutine object instead of a result.
    # The argument schema is inferred from call_mcp's signature: an explicit
    # args_schema dict must be JSON Schema, and a dict of Python types breaks
    # tool.args and LLM tool binding.
    return StructuredTool.from_function(
        coroutine=call_mcp,
        name="mcp_tool_call",
        description="Call a Model Context Protocol (MCP) tool from Agent Middleware API. "
        "Use this to access billable services like data indexing, content generation, etc. "
        "Returns signed receipts for all invocations. "
        "Requires both idempotency_key and permit_idempotency_key for safe replay.",
    )


def create_wallet_tool(client: B2AClient, *, wallet_id: str) -> StructuredTool:
    """Create a LangChain tool for wallet operations."""
    _require_wallet_id(wallet_id)

    async def get_balance() -> str:
        """Get current wallet balance."""
        balance = await client.get_balance(wallet_id)
        return f"Balance: {balance} credits"

    return StructuredTool.from_function(
        coroutine=get_balance,
        name="wallet_balance",
        description="Get the current wallet balance from Agent Middleware API.",
    )


def create_langgraph_tools(
    client: B2AClient,
    *,
    wallet_id: str,
    permit_budget: Decimal = Decimal(100),
    permit_ttl_minutes: int = 30,
) -> list[Callable]:
    """Get tools formatted for LangGraph ReAct agents."""
    return [
        create_mcp_tool(
            client,
            wallet_id=wallet_id,
            permit_budget=permit_budget,
            permit_ttl_minutes=permit_ttl_minutes,
        ),
        create_wallet_tool(client, wallet_id=wallet_id),
    ]
