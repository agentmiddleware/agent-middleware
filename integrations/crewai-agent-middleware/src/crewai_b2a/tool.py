"""CrewAI Tool for Agent Middleware API."""

import asyncio
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Self

from b2a_sdk.models import PermitRequest
from crewai.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr

from .client import B2AClient

_MAX_KEY_LENGTH = 128


def run_blocking(coro_factory: Callable[[], Awaitable[Any]]) -> Any:
    """Run a fresh async operation from sync code, including inside a running loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())

    outcome: dict[str, Any] = {}

    def _target() -> None:
        try:
            outcome["result"] = asyncio.run(coro_factory())
        except BaseException as exc:  # noqa: BLE001
            outcome["error"] = exc

    worker = threading.Thread(target=_target, daemon=True)
    worker.start()
    worker.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("result")


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required and must not be blank")
    return value.strip()


def _idempotency_key(value: object, name: str) -> str:
    key = _required_text(value, name)
    if len(key) > _MAX_KEY_LENGTH:
        raise ValueError(f"{name} must be at most 128 characters")
    return key


def _tool_arguments(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError("arguments must be an object")
    return value


def _receipt_id_from_payload(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    containers = [payload.get("result")]
    error = payload.get("error")
    if isinstance(error, dict):
        containers.append(error.get("data"))
    for container in containers:
        if not isinstance(container, dict):
            continue
        receipt = container.get("receipt")
        if not isinstance(receipt, dict):
            continue
        receipt_id = receipt.get("receipt_id")
        if isinstance(receipt_id, str) and receipt_id:
            return receipt_id
    return None


def _error_text(exc: Exception) -> str:
    receipt_id = getattr(exc, "receipt_id", None)
    if not isinstance(receipt_id, str) or not receipt_id:
        receipt_id = _receipt_id_from_payload(getattr(exc, "payload", None))
    if receipt_id:
        return f"Error: {exc} receipt_id={receipt_id}"
    return f"Error: {exc}"


class CrewAIB2AToolInput(BaseModel):
    """Arguments a CrewAI agent can pass into this tool."""

    operation: str = Field(description="One of discover_tools, call_tool, or balance.")
    tool_name: str = Field(
        default="",
        description="MCP tool name. Required for call_tool.",
    )
    idempotency_key: str = Field(
        default="",
        description=(
            "Caller-owned invoke key. Required for call_tool. "
            "Reuse it only for the same invocation."
        ),
    )
    permit_idempotency_key: str = Field(
        default="",
        description=(
            "Caller-owned permit key. Required for call_tool. "
            "Reuse it to replay the same permit."
        ),
    )
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="Arguments passed to the MCP tool.",
    )


class CrewAIB2ATool(BaseTool):
    """CrewAI tool for Agent Middleware API operations via governed permit→invoke→receipt flow."""

    name: str = "Agent_Middleware_API"
    description: str = (
        "Access Agent Middleware API for MCP tools and wallet operations. "
        "Use this to call billable services and manage agent billing. "
        "All invocations return signed receipts."
    )
    args_schema: type[BaseModel] = CrewAIB2AToolInput

    # Async calls use a borrowed client directly. Sync calls use a separate
    # runner-loop client; configure it with sync_client_factory when borrowing.
    client: B2AClient | None = None
    sync_client_factory: Callable[[], B2AClient] | None = Field(
        default=None, exclude=True, repr=False
    )
    base_url: str = "https://api.thisisatest.tech"
    api_key: str = Field(repr=False, exclude=True)
    wallet_id: str
    permit_budget: Decimal = Decimal(100)
    permit_ttl_minutes: int = 30

    # One dict per instance. A shared class dict would let one wallet reuse
    # another wallet's permit when both agents pick the same permit key.
    _permit_cache: dict[str, str] = PrivateAttr(default_factory=dict)
    _permit_requests: dict[str, PermitRequest] = PrivateAttr(default_factory=dict)
    _permit_ttls: dict[str, int] = PrivateAttr(default_factory=dict)
    _active_permit_calls: dict[str, int] = PrivateAttr(default_factory=dict)
    _permit_lock: Any = PrivateAttr(default=None)
    _sync_runner_lock: Any = PrivateAttr(default=None)
    _sync_runner_loop: asyncio.AbstractEventLoop | None = PrivateAttr(default=None)
    _sync_runner_thread: threading.Thread | None = PrivateAttr(default=None)
    _sync_client: B2AClient | None = PrivateAttr(default=None)
    _close_lock: Any = PrivateAttr(default=None)
    _owns_client: bool = PrivateAttr(default=False)
    _closing: bool = PrivateAttr(default=False)
    _runner_shutdown: bool = PrivateAttr(default=False)

    def __init__(
        self,
        api_key: str,
        wallet_id: str,
        base_url: str = "https://api.thisisatest.tech",
        permit_budget: Decimal = Decimal(100),
        permit_ttl_minutes: int = 30,
        **kwargs: Any,
    ):
        # BaseTool is a pydantic model: required fields must reach its
        # validator, so pass them through instead of assigning afterwards.
        super().__init__(
            api_key=_required_text(api_key, "api_key"),
            wallet_id=_required_text(wallet_id, "wallet_id"),
            base_url=_required_text(base_url, "base_url"),
            permit_budget=permit_budget,
            permit_ttl_minutes=permit_ttl_minutes,
            **kwargs,
        )
        self._permit_cache = {}
        self._permit_requests = {}
        self._permit_ttls = {}
        self._active_permit_calls = {}
        self._permit_lock = threading.Lock()
        self._sync_runner_lock = threading.RLock()
        self._sync_runner_loop = None
        self._sync_runner_thread = None
        self._sync_client = None
        self._close_lock = threading.Lock()
        self._owns_client = False
        self._closing = False
        self._runner_shutdown = False

    def _get_client(self) -> B2AClient:
        with self._sync_runner_lock:
            if self._closing:
                raise RuntimeError("CrewAI tool is closed")
            if (
                self.client is not None
                and not self._owns_client
                and asyncio.get_running_loop() is self._sync_runner_loop
            ):
                if self._sync_client is None:
                    if self.sync_client_factory is None:
                        raise ValueError(
                            "sync_client_factory is required for sync calls "
                            "with a borrowed client"
                        )
                    self._sync_client = self.sync_client_factory()
                return self._sync_client
            if self.client is None:
                self.client = B2AClient(
                    api_key=self.api_key,
                    base_url=self.base_url,
                )
                self._owns_client = True
            return self.client

    def _permit_request(self, tool_name: str, permit_key: str) -> PermitRequest:
        # Both sync and async entry points retain the body before sending it.
        # A lost response therefore cannot change the expiry on retry.
        expected_scopes = [f"tool:{tool_name}:invoke", "billing:charge"]
        with self._permit_lock:
            request = self._permit_requests.get(permit_key)
            if request is None:
                request = PermitRequest(
                    issuer_wallet_id=self.wallet_id,
                    subject_wallet_id=self.wallet_id,
                    max_credits=self.permit_budget,
                    expires_at=datetime.now(UTC)
                    + timedelta(minutes=self.permit_ttl_minutes),
                    allowed_tools=[tool_name],
                    scopes=expected_scopes,
                )
                self._permit_requests[permit_key] = request
                self._permit_ttls[permit_key] = self.permit_ttl_minutes
                return request
            if (
                request.issuer_wallet_id != self.wallet_id
                or request.subject_wallet_id != self.wallet_id
                or request.max_credits != self.permit_budget
                or request.allowed_tools != [tool_name]
                or list(request.scopes) != expected_scopes
                or self._permit_ttls.get(permit_key) != self.permit_ttl_minutes
            ):
                raise ValueError(
                    "permit_idempotency_key reused with different permit terms"
                )
            return request

    def _begin_permit_call(self, operation: str, permit_key: str) -> bool:
        if operation != "call_tool" or not isinstance(permit_key, str):
            return False
        permit_key = permit_key.strip()
        with self._permit_lock:
            active = self._active_permit_calls.get(permit_key, 0)
            self._active_permit_calls[permit_key] = active + 1
            return active > 0

    def _finish_permit_call(self, operation: str, permit_key: str) -> None:
        if operation != "call_tool" or not isinstance(permit_key, str):
            return
        permit_key = permit_key.strip()
        with self._permit_lock:
            active = self._active_permit_calls[permit_key]
            if active == 1:
                del self._active_permit_calls[permit_key]
            else:
                self._active_permit_calls[permit_key] = active - 1

    def _ensure_sync_runner(self) -> asyncio.AbstractEventLoop:
        """Keep sync calls on one private loop, including calls from worker threads."""
        with self._sync_runner_lock:
            if self._closing:
                raise RuntimeError("CrewAI tool is closed")
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
                loop.call_soon(ready.set)
                loop.run_forever()

            thread = threading.Thread(
                target=_run_loop,
                name="crewai-b2a-loop",
                daemon=True,
            )
            thread.start()
            self._sync_runner_loop = loop
            self._sync_runner_thread = thread
            if not ready.wait(timeout=5):
                raise RuntimeError("sync runner loop did not start")
            return loop

    def _submit(self, coro: Any) -> Any:
        loop = self._ensure_sync_runner()
        with self._sync_runner_lock:
            if (
                self._closing
                or loop is not self._sync_runner_loop
                or not loop.is_running()
            ):
                raise RuntimeError("CrewAI tool is closed")
            return asyncio.run_coroutine_threadsafe(coro, loop)

    def _sync_await(self, coro: Any) -> Any:
        """Run a coroutine on the private loop and wait for its result."""
        return self._submit(coro).result()

    def _stop_sync_runner(self) -> None:
        """Stop the private loop after its HTTP client has been closed."""
        with self._sync_runner_lock:
            loop = self._sync_runner_loop
            thread = self._sync_runner_thread
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(loop.stop)
            if thread is not None:
                thread.join(timeout=1)
                if thread.is_alive():
                    raise RuntimeError("sync runner loop did not stop")
            if loop is not None:
                if asyncio.all_tasks(loop):
                    raise RuntimeError("sync runner still has pending tasks")
                loop.close()
            self._sync_runner_loop = None
            self._sync_runner_thread = None

    def close(self) -> None:
        """Close the owned client and sync loop. Safe to call more than once."""
        with self._close_lock:
            with self._sync_runner_lock:
                self._closing = True
                clients = [
                    client
                    for client in (
                        self._sync_client,
                        self.client if self._owns_client else None,
                    )
                    if client is not None
                ]
                loop = self._sync_runner_loop

            async def shutdown() -> None:
                current = asyncio.current_task()
                pending = [task for task in asyncio.all_tasks() if task is not current]
                for task in pending:
                    task.cancel()
                if pending:
                    _, still_pending = await asyncio.wait(pending, timeout=1)
                    if still_pending:
                        raise RuntimeError("sync runner tasks did not cancel")
                for client in clients:
                    await asyncio.wait_for(client.close(), timeout=1)

            if not self._runner_shutdown and loop is not None and loop.is_running():
                asyncio.run_coroutine_threadsafe(shutdown(), loop).result(
                    timeout=2 + len(clients)
                )
                self._runner_shutdown = True
            elif not self._runner_shutdown and clients:
                raise RuntimeError("sync runner loop is not running")
            self._stop_sync_runner()
            self._sync_client = None
            if self._owns_client:
                self.client = None
            self._runner_shutdown = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    async def _execute(
        self, operation: str, *, replay_permit: bool = False, **kwargs: Any
    ) -> str:
        try:
            client = self._get_client()
            if operation == "discover_tools":
                tools = await client.discover_tools()
                return str(
                    [
                        {
                            "name": t.name,
                            "description": t.description,
                            "input_schema": t.input_schema,
                        }
                        for t in tools
                    ]
                )

            if operation == "call_tool":
                tool_name = _required_text(kwargs.get("tool_name"), "tool_name")
                idempotency_key = _idempotency_key(
                    kwargs.get("idempotency_key"), "idempotency_key"
                )
                permit_key = _idempotency_key(
                    kwargs.get("permit_idempotency_key"),
                    "permit_idempotency_key",
                )
                arguments = _tool_arguments(kwargs.get("arguments", {}))
                request = self._permit_request(tool_name, permit_key)
                if permit_key in self._permit_cache and not replay_permit:
                    permit_id = self._permit_cache[permit_key]
                else:
                    permit = await client.create_permit(
                        request, idempotency_key=permit_key
                    )
                    permit_id = permit.permit_id
                    self._permit_cache[permit_key] = permit_id

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

            if operation == "balance":
                balance = await client.get_balance(self.wallet_id)
                return f"Balance: {balance} credits"

            return f"Unknown operation: {operation}"
        except Exception as exc:  # noqa: BLE001
            # CrewAI agents read a string. Any failure here becomes that string.
            return _error_text(exc)

    def _run(
        self,
        operation: str,
        tool_name: str = "",
        idempotency_key: str = "",
        permit_idempotency_key: str = "",
        arguments: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        """Synchronous operation (for CrewAI compatibility)."""
        if (
            self.client is not None
            and not self._owns_client
            and self.sync_client_factory is None
        ):
            try:
                asyncio.get_running_loop()
                return "Error: sync_client_factory is required for sync calls with a borrowed client"
            except RuntimeError:
                pass
        replay_permit = self._begin_permit_call(operation, permit_idempotency_key)
        coro = self._execute(
            operation,
            replay_permit=replay_permit,
            tool_name=tool_name,
            idempotency_key=idempotency_key,
            permit_idempotency_key=permit_idempotency_key,
            arguments=arguments,
            **kwargs,
        )
        try:
            return self._sync_await(coro)
        except Exception as exc:  # noqa: BLE001
            # The coroutine was not scheduled, or it already finished.
            try:
                coro.close()
            except RuntimeError:
                pass
            return _error_text(exc)
        finally:
            self._finish_permit_call(operation, permit_idempotency_key)

    async def _arun(
        self,
        operation: str,
        tool_name: str = "",
        idempotency_key: str = "",
        permit_idempotency_key: str = "",
        arguments: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        """Asynchronous operation on the client's owning loop."""
        try:
            replay_permit = self._begin_permit_call(operation, permit_idempotency_key)
            try:
                coro = self._execute(
                    operation,
                    replay_permit=replay_permit,
                    tool_name=tool_name,
                    idempotency_key=idempotency_key,
                    permit_idempotency_key=permit_idempotency_key,
                    arguments=arguments,
                    **kwargs,
                )
                if self.client is not None and not self._owns_client:
                    return await coro
                try:
                    future = self._submit(coro)
                except Exception:
                    coro.close()
                    raise
                return await asyncio.wrap_future(future)
            finally:
                self._finish_permit_call(operation, permit_idempotency_key)
        except Exception as exc:  # noqa: BLE001
            return _error_text(exc)
