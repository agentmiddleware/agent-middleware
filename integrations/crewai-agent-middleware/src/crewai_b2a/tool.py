"""CrewAI Tool for Agent Middleware API."""

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from b2a_sdk.models import PermitRequest
from crewai.tools import BaseTool
from pydantic import BaseModel

from .client import B2AClient


def _object_arguments(arguments: Any) -> dict[str, Any]:
    """Return tool arguments only when they are an object."""
    if arguments is None:
        return {}
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")  # noqa: TRY004
    return arguments


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
    # threading.Lock() is a factory, not a type, so these stay untyped.
    _permit_lock: Any = None
    _sync_runner_lock: Any = None
    _sync_runner_loop: Any = None
    _sync_runner_thread: Any = None

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
        # Locks are per instance. A class-level Lock would be shared by every tool.
        self._permit_lock = threading.Lock()
        self._sync_runner_lock = threading.Lock()
        self._sync_runner_loop = None
        self._sync_runner_thread = None

    def _get_client(self) -> B2AClient:
        if self.client is None:
            self.client = B2AClient(
                api_key=self.api_key,
                base_url=self.base_url,
            )
        return self.client

    def _ensure_sync_runner(self) -> asyncio.AbstractEventLoop:
        """Return the private loop used by sync CrewAI calls.

        CrewAI calls ``_run`` from worker threads, and from Python 3.12 those
        threads have no current event loop. ``asyncio.run`` would also close
        the loop, which breaks the next call on the same HTTP client.
        """
        lock = self._sync_runner_lock
        if lock is None:
            raise RuntimeError("sync runner lock was not initialized")
        with lock:
            loop = self._sync_runner_loop
            thread = self._sync_runner_thread
            if (
                loop is not None
                and thread is not None
                and thread.is_alive()
                and not loop.is_closed()
            ):
                return loop
            loop = asyncio.new_event_loop()
            ready = threading.Event()

            def _run_loop() -> None:
                asyncio.set_event_loop(loop)
                ready.set()
                loop.run_forever()

            thread = threading.Thread(
                target=_run_loop,
                name="crewai-b2a-loop",
                daemon=True,
            )
            thread.start()
            self._sync_runner_loop = loop
            self._sync_runner_thread = thread
            # The loop thread does not take this lock, so waiting here cannot deadlock.
            if not ready.wait(timeout=5):
                raise RuntimeError("sync runner loop did not start")
            return loop

    def _sync_await(self, coro: Any) -> Any:
        """Run one coroutine on the private loop and wait for it."""
        loop = self._ensure_sync_runner()
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

    def _stop_sync_runner(self) -> None:
        """Stop the private loop. Tests use this so the thread does not linger."""
        lock = self._sync_runner_lock
        if lock is None:
            return
        with lock:
            loop = self._sync_runner_loop
            thread = self._sync_runner_thread
            self._sync_runner_loop = None
            self._sync_runner_thread = None
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=2)

    def _permit_request(self, tool_name: str, permit_key: str) -> PermitRequest:
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise ValueError("tool_name is required and must not be blank")
        lock = self._permit_lock
        if lock is None:
            raise RuntimeError("permit cache lock was not initialized")
        # Hold the lock for the whole get-or-create so two threads share one
        # expires_at under one permit key.
        with lock:
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
        client = self._get_client()

        try:
            if operation == "discover_tools":
                tools = self._sync_await(client.discover_tools())
                return str(
                    [{"name": t.name, "description": t.description} for t in tools]
                )

            elif operation == "call_tool":
                tool_name = kwargs.get("tool_name")
                idempotency_key = kwargs.get("idempotency_key")
                permit_idempotency_key = kwargs.get("permit_idempotency_key")
                arguments = kwargs.get("arguments", {})

                if not idempotency_key or not idempotency_key.strip():
                    return "Error: idempotency_key is required and must not be blank"

                if not permit_idempotency_key or not permit_idempotency_key.strip():
                    return "Error: permit_idempotency_key is required and must not be blank"

                arguments = _object_arguments(arguments)
                request = self._permit_request(tool_name, permit_idempotency_key)
                if permit_idempotency_key in self._permit_cache:
                    permit_id = self._permit_cache[permit_idempotency_key]
                else:
                    permit = self._sync_await(
                        client.create_permit(
                            request, idempotency_key=permit_idempotency_key
                        )
                    )
                    permit_id = permit.permit_id
                    self._permit_cache[permit_idempotency_key] = permit_id

                result = self._sync_await(
                    client.invoke_tool(
                        tool_name,
                        arguments,
                        wallet_id=self.wallet_id,
                        permit_id=permit_id,
                        idempotency_key=idempotency_key,
                    )
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
                balance = self._sync_await(client.get_balance(self.wallet_id))
                return f"Balance: {balance} credits"

            else:
                return f"Unknown operation: {operation}"

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
            if operation == "discover_tools":
                tools = await client.discover_tools()
                return str(
                    [{"name": t.name, "description": t.description} for t in tools]
                )

            elif operation == "call_tool":
                tool_name = kwargs.get("tool_name")
                idempotency_key = kwargs.get("idempotency_key")
                permit_idempotency_key = kwargs.get("permit_idempotency_key")
                arguments = kwargs.get("arguments", {})

                if not idempotency_key or not idempotency_key.strip():
                    return "Error: idempotency_key is required and must not be blank"

                if not permit_idempotency_key or not permit_idempotency_key.strip():
                    return "Error: permit_idempotency_key is required and must not be blank"

                arguments = _object_arguments(arguments)
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

        except Exception as e:  # noqa: BLE001
            return f"Error: {e!s}"
