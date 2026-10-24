"""CrewAI Tool for Agent Middleware API."""

import asyncio
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

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

    client: B2AClient | None = None
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
    _sync_loop: asyncio.AbstractEventLoop | None = PrivateAttr(default=None)
    _owns_client: bool = PrivateAttr(default=False)

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
        self._sync_loop = None
        self._owns_client = False

    def _get_client(self) -> B2AClient:
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

    def _drive(self, coro: Any) -> Any:
        # CrewAI calls this from a normal thread, where Python 3.12 and later
        # have no current event loop. asyncio.run() would also close the loop
        # and the HTTP client would fail on the next call. Keep one loop.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError(
                "synchronous run was called while an event loop is running; "
                "use the async tool path"
            )
        loop = self._sync_loop
        if loop is None or loop.is_closed():
            loop = asyncio.new_event_loop()
            self._sync_loop = loop
        return loop.run_until_complete(coro)

    def close(self) -> None:
        """Close the sync loop this tool opened. Safe to call more than once."""
        loop = self._sync_loop
        client = self.client if self._owns_client else None
        self._sync_loop = None
        if loop is None or loop.is_closed():
            return
        if client is not None:
            try:
                loop.run_until_complete(client.close())
            except Exception:  # noqa: BLE001, S110
                # Cleanup must not hide the call that already finished.
                pass
            self.client = None
        loop.close()

    async def _execute(
        self, operation: str, client: B2AClient | None = None, **kwargs: Any
    ) -> str:
        try:
            client = client or self._get_client()
            if operation == "discover_tools":
                tools = await client.discover_tools()
                return str(
                    [{"name": t.name, "description": t.description} for t in tools]
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
                if permit_key in self._permit_cache:
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
        if operation == "call_tool":
            try:
                _idempotency_key(idempotency_key, "idempotency_key")
                _idempotency_key(permit_idempotency_key, "permit_idempotency_key")
            except (TypeError, ValueError) as exc:
                return _error_text(exc)

        call_kwargs = dict(
            tool_name=tool_name,
            idempotency_key=idempotency_key,
            permit_idempotency_key=permit_idempotency_key,
            arguments=arguments,
            **kwargs,
        )

        # A supplied client belongs to the caller. Reuse it on one persistent
        # loop when possible. A fresh client is created and closed on the loop
        # that drives each ordinary sync call.
        if self.client is not None and not self._owns_client:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                coro = self._execute(operation, **call_kwargs)
                try:
                    return self._drive(coro)
                except Exception as exc:  # noqa: BLE001
                    coro.close()
                    return _error_text(exc)
            try:
                return run_blocking(lambda: self._execute(operation, **call_kwargs))
            except Exception as exc:  # noqa: BLE001
                return _error_text(exc)

        async def _drive_fresh() -> str:
            client = B2AClient(api_key=self.api_key, base_url=self.base_url)
            try:
                return await self._execute(operation, client=client, **call_kwargs)
            finally:
                await client.close()

        try:
            return run_blocking(_drive_fresh)
        except Exception as exc:  # noqa: BLE001
            return _error_text(exc)

    async def _arun(
        self,
        operation: str,
        tool_name: str = "",
        idempotency_key: str = "",
        permit_idempotency_key: str = "",
        arguments: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        """Asynchronous operation (preferred)."""
        return await self._execute(
            operation,
            tool_name=tool_name,
            idempotency_key=idempotency_key,
            permit_idempotency_key=permit_idempotency_key,
            arguments=arguments,
            **kwargs,
        )
