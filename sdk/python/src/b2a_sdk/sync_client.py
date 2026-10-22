"""Synchronous wrapper around the async trust-loop client.

Plain ``asyncio.run(...)`` also works, but first-time pilot developers hit
the async-only surface before anything else, so this module offers the six
governed-loop calls as blocking methods with no event-loop management:

``discover -> permit -> invoke -> receipt -> evidence``

The async client stays authoritative; every method here runs exactly one
event loop per call and delegates to it. For concurrent or long-lived
work, use :class:`b2a_sdk.client.AgentMiddlewareClient` directly.
"""

from __future__ import annotations

import asyncio
from typing import Any, overload

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
        return asyncio.run(self._client.discover_tools())

    def create_permit(
        self,
        request: PermitRequest,
        *,
        idempotency_key: str,
    ) -> Permit:
        """Create a signed permit using a caller-owned idempotency key."""
        return asyncio.run(self._client.create_permit(request, idempotency_key=idempotency_key))

    @overload
    def invoke_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        wallet_id: str,
        permit_id: str,
        idempotency_key: str,
    ) -> InvocationResult: ...

    @overload
    def invoke_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        wallet_id: str,
        permit_id: str,
        idempotency_key: str,
        business_id: str | None = None,
        include: list[str] | tuple[str, ...] | None = None,
        exclude: list[str] | tuple[str, ...] | None = None,
    ) -> InvocationResult: ...

    def invoke_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        wallet_id: str,
        permit_id: str,
        idempotency_key: str,
        business_id: str | None = None,
        include: list[str] | tuple[str, ...] | None = None,
        exclude: list[str] | tuple[str, ...] | None = None,
    ) -> InvocationResult:
        """Invoke one MCP tool through the governed permit and receipt loop."""
        return asyncio.run(
            self._client.invoke_tool(
                name,
                arguments,
                wallet_id=wallet_id,
                permit_id=permit_id,
                idempotency_key=idempotency_key,
                business_id=business_id,
                include=include,
                exclude=exclude,
            )
        )

    def get_receipt(self, receipt_id: str) -> Receipt:
        """Fetch a wallet-authorized signed receipt."""
        return asyncio.run(self._client.get_receipt(receipt_id))

    def verify_receipt(self, receipt_id: str) -> ReceiptVerification:
        """Ask the trust plane to verify a receipt signature and linkage."""
        return asyncio.run(self._client.verify_receipt(receipt_id))

    def get_evidence(self, receipt_id: str) -> EvidenceBundle:
        """Fetch the buyer-facing verification bundle for a receipt."""
        return asyncio.run(self._client.get_evidence(receipt_id))

    def close(self) -> None:
        """Close the underlying HTTP client."""
        asyncio.run(self._client.close())

    def __enter__(self) -> SyncAgentMiddlewareClient:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


__all__ = ["SyncAgentMiddlewareClient"]
