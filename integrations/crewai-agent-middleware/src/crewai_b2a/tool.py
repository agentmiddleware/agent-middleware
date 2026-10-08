"""CrewAI Tool for Agent Middleware API."""

import asyncio
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from b2a_sdk.models import PermitRequest
from crewai.tools import BaseTool
from pydantic import BaseModel

from .client import B2AClient


def run_blocking(coro_factory: Callable[[], Awaitable[Any]]) -> Any:
    """Drive an async callable to completion from synchronous CrewAI code.

    Uses ``asyncio.run`` on a fresh event loop when the caller has none, and
    otherwise runs it on a fresh loop in a short-lived helper thread. This
    keeps CrewAI's synchronous ``_run`` path working for repeated calls and
    from inside an already running event loop, where ``loop.run_until_complete``
    would fail with "This event loop is already running". The factory is
    invoked inside the thread that will run the loop, so loop-bound resources
    (HTTP connection pools) are created and used on the same loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())

    outcome: dict[str, Any] = {}

    def _target() -> None:
        try:
            outcome["result"] = asyncio.run(coro_factory())
        except BaseException as exc:  # noqa: BLE001  # re-raised in caller thread
            outcome["error"] = exc

    worker = threading.Thread(target=_target, daemon=True)
    worker.start()
    worker.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("result")


class MCPToolSchema(BaseModel):
    """Schema for MCP tool input."""

    tool_name: str
    idempotency_key: str
    permit_idempotency_key: str
    arguments: dict[str, Any] = {}


class WalletBalanceSchema(BaseModel):
    """Schema for wallet balance check."""


class CrewAIB2ATool(BaseTool):
    """CrewAI tool for Agent Middleware API operations via governed permit→invoke→receipt flow."""

    name: str = "Agent_Middleware_API"
    description: str = (
        "Access Agent Middleware API for MCP tools and wallet operations. "
        "Use this to call billable services and manage agent billing. "
        "All invocations return signed receipts."
    )

    client: B2AClient | None = None
    base_url: str = "https://api.thisisatest.tech"
    api_key: str
    wallet_id: str
    permit_budget: Decimal = Decimal(100)
    permit_ttl_minutes: int = 30
    # Cache permits to avoid 409 on replay (server hashes full permit body including expires_at)
    # permit_idempotency_key -> permit_id
    _permit_cache: dict[str, str] = {}  # noqa: RUF012
    _permit_requests: dict[str, PermitRequest] = {}  # noqa: RUF012

    def __init__(
        self,
        api_key: str,
        wallet_id: str,
        base_url: str = "https://api.thisisatest.tech",
        permit_budget: Decimal = Decimal(100),
        permit_ttl_minutes: int = 30,
        **kwargs,
    ):
        # BaseTool is a pydantic model: required fields must reach its
        # validator, so pass them through instead of assigning afterwards.
        super().__init__(
            api_key=api_key,
            wallet_id=wallet_id,
            base_url=base_url,
            permit_budget=permit_budget,
            permit_ttl_minutes=permit_ttl_minutes,
            **kwargs,
        )
        self._permit_cache = {}  # Instance-specific cache
        self._permit_requests = {}

    def _get_client(self) -> B2AClient:
        if self.client is None:
            self.client = B2AClient(
                api_key=self.api_key,
                base_url=self.base_url,
            )
        return self.client

    def _permit_request(self, tool_name: str, permit_key: str) -> PermitRequest:
        # Both sync and async entry points retain the body before sending it.
        # A lost response therefore cannot change the expiry on retry.
        request = self._permit_requests.get(permit_key)
        if request is None:
            request = PermitRequest(
                issuer_wallet_id=self.wallet_id,
                subject_wallet_id=self.wallet_id,
                max_credits=self.permit_budget,
                expires_at=datetime.now(UTC)
                + timedelta(minutes=self.permit_ttl_minutes),
                allowed_tools=[tool_name],
                scopes=[f"tool:{tool_name}:invoke", "billing:charge"],
            )
            self._permit_requests[permit_key] = request
        elif (
            request.subject_wallet_id != self.wallet_id
            or request.max_credits != self.permit_budget
            or request.allowed_tools != [tool_name]
        ):
            raise ValueError(
                "permit_idempotency_key reused with different permit terms"
            )
        return request

    def _new_sync_client(self) -> B2AClient:
        # One client per synchronous drive, created inside the loop that will
        # use it. The shared async client cannot be reused here: its HTTP
        # connection pool is bound to the event loop that first used it, so a
        # later sync call on a fresh loop would fail with "Event loop is
        # closed". Permit state stays on the tool (permit ids are plain
        # strings), so replays still reuse permits across drives.
        return B2AClient(api_key=self.api_key, base_url=self.base_url)

    async def _execute(
        self,
        operation: str,
        client: B2AClient,
        kwargs: dict[str, Any],
    ) -> str:
        """Shared governed flow for both the sync and async entry points."""
        if operation == "discover_tools":
            tools = await client.discover_tools()
            return str([{"name": t.name, "description": t.description} for t in tools])

        elif operation == "call_tool":
            tool_name = kwargs.get("tool_name")
            idempotency_key = kwargs.get("idempotency_key")
            permit_idempotency_key = kwargs.get("permit_idempotency_key")
            arguments = kwargs.get("arguments", {})

            if not idempotency_key or not idempotency_key.strip():
                return "Error: idempotency_key is required and must not be blank"

            if not permit_idempotency_key or not permit_idempotency_key.strip():
                return "Error: permit_idempotency_key is required and must not be blank"

            request = self._permit_request(tool_name, permit_idempotency_key)
            if permit_idempotency_key in self._permit_cache:
                permit_id = self._permit_cache[permit_idempotency_key]
            else:
                permit = await client.create_permit(
                    request, idempotency_key=permit_idempotency_key
                )
                permit_id = permit.permit_id
                self._permit_cache[permit_idempotency_key] = permit_id

            result = await client.invoke_tool(
                tool_name,
                arguments,
                wallet_id=self.wallet_id,
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

        elif operation == "balance":
            balance = await client.get_balance(self.wallet_id)
            return f"Balance: {balance} credits"

        else:
            return f"Unknown operation: {operation}"

    def _run(
        self,
        operation: str,
        **kwargs,
    ) -> str:
        """Synchronous operation (for CrewAI compatibility).

        Args:
            operation: One of 'discover_tools', 'call_tool', 'balance'
            **kwargs: Operation-specific arguments
        """
        if operation == "call_tool":
            # Fail fast before opening any client or loop, as _arun does.
            idempotency_key = kwargs.get("idempotency_key")
            permit_idempotency_key = kwargs.get("permit_idempotency_key")
            if not idempotency_key or not idempotency_key.strip():
                return "Error: idempotency_key is required and must not be blank"
            if not permit_idempotency_key or not permit_idempotency_key.strip():
                return "Error: permit_idempotency_key is required and must not be blank"

        async def _drive() -> str:
            client = self._new_sync_client()
            try:
                return await self._execute(operation, client, kwargs)
            finally:
                await client.close()

        try:
            return run_blocking(_drive)
        except Exception as e:  # noqa: BLE001
            return f"Error: {e!s}"

    async def _arun(
        self,
        operation: str,
        **kwargs,
    ) -> str:
        """Asynchronous operation (preferred)."""
        client = self._get_client()

        try:
            return await self._execute(operation, client, kwargs)
        except Exception as e:  # noqa: BLE001
            return f"Error: {e!s}"
