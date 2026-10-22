"""Synchronous wrapper around the async trust-loop client.

Plain ``asyncio.run(...)`` also works, but first-time pilot developers hit
the async-only surface before anything else, so this module offers the six
governed-loop calls as blocking methods with no event-loop management:

``discover -> permit -> invoke -> receipt -> evidence``

The async client stays authoritative; every method here delegates to it
on one event loop the wrapper owns for its lifetime. A fresh loop per call
(``asyncio.run``) would break the second call: the HTTP connection pool
keeps keep-alive connections bound to the first, already-closed loop. For
concurrent or long-lived work, use
:class:`b2a_sdk.client.AgentMiddlewareClient` directly.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .models import (
    EvidenceBundle,
    InvocationResult,
    Permit,
    PermitRequest,
    Receipt,
    ReceiptVerification,
    ToolDefinition,
)


class SyncAgentMiddlewareClient:
    """Blocking client for the governed MCP trust loop.

    Usage:
        with SyncAgentMiddlewareClient(
            api_key="agt-your-api-key",
            base_url="https://your-gateway.example.com",
        ) as client:
            tools = client.discover_tools()
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "http://localhost:8000",
        timeout: float = 10.0,
        *,
        transport: Any | None = None,
        bearer_token: str | None = None,
    ) -> None:
        """Create the wrapper and its underlying async client.

        Args:
            api_key: API key for authentication (X-API-Key header)
            base_url: Base URL of the middleware API. Defaults to the local
                gateway; pass the deployment URL explicitly for anything
                else, since the default never points at a hosted server.
            timeout: Request timeout in seconds
            transport: Optional HTTPX transport, primarily for in-process tests
            bearer_token: Optional Bearer [REDACTED] for IGA-governed flows
        """
        from .client import AgentMiddlewareClient

        self._client = AgentMiddlewareClient(
            api_key,
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            bearer_token=bearer_token,
        )
        self._loop: asyncio.AbstractEventLoop | None = asyncio.new_event_loop()

    def _run(self, coro: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            coro.close()
            raise RuntimeError(
                "SyncAgentMiddlewareClient cannot run inside an event loop; "
                "use AgentMiddlewareClient"
            )
        if self._loop is None or self._loop.is_closed():
            coro.close()
            raise RuntimeError("SyncAgentMiddlewareClient is closed")
        return self._loop.run_until_complete(coro)

    @property
    def base_url(self) -> str:
        """The base URL the underlying client sends requests to."""
        return self._client.base_url

    @property
    def api_key(self) -> str:
        """The API key this client sends as ``X-API-Key`` (read-only)."""
        return self._client.api_key

    def discover_tools(self) -> list[ToolDefinition]:
        """Return the executable tools advertised by the MCP gateway."""
        return self._run(self._client.discover_tools())

    def create_permit(
        self,
        request: PermitRequest,
        *,
        idempotency_key: str,
    ) -> Permit:
        """Create a signed permit using a caller-owned idempotency key."""
        return self._run(self._client.create_permit(request, idempotency_key=idempotency_key))

    def invoke_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        wallet_id: str,
        permit_id: str,
        idempotency_key: str,
    ) -> InvocationResult:
        """Invoke one MCP tool through the governed permit and receipt loop."""
        return self._run(
            self._client.invoke_tool(
                name,
                arguments,
                wallet_id=wallet_id,
                permit_id=permit_id,
                idempotency_key=idempotency_key,
            )
        )

    def get_receipt(self, receipt_id: str) -> Receipt:
        """Fetch a wallet-authorized signed receipt."""
        return self._run(self._client.get_receipt(receipt_id))

    def verify_receipt(self, receipt_id: str) -> ReceiptVerification:
        """Ask the trust plane to verify a receipt signature and linkage."""
        return self._run(self._client.verify_receipt(receipt_id))

    def get_evidence(self, receipt_id: str) -> EvidenceBundle:
        """Fetch the buyer-facing verification bundle for a receipt."""
        return self._run(self._client.get_evidence(receipt_id))

    def close(self) -> None:
        """Close the underlying HTTP client and the owned loop. Idempotent."""
        loop, self._loop = self._loop, None
        if loop is None or loop.is_closed():
            return
        try:
            loop.run_until_complete(self._client.close())
        finally:
            loop.close()

    def __enter__(self) -> SyncAgentMiddlewareClient:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


__all__ = ["SyncAgentMiddlewareClient"]
