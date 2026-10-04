"""Protocol-neutral governed-invocation adapter seam.

The trust plane governs *actions*, not a particular wire protocol. An adapter
normalizes a protocol-specific request into a :class:`GovernedRequest`, runs it
through the single governed-invocation pipeline (permit validation, policy
evaluation, idempotency, metering, signed receipt, audit), and normalizes the
:class:`GovernedResult` back into the protocol's response shape.

MCP is the first and currently only adapter. It delegates to the existing
governed path in ``app.routers.mcp`` (``_execute_registered_tool``) so there is
exactly one source of truth for governance — the adapter adds a stable seam,
not a second implementation. Other protocols (AWI, browser, WebMCP) can be
added later by implementing the same interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.core.auth import AuthContext
from app.services.agent_money import AgentMoney
from app.services.idempotency import validate_client_idempotency_key


@dataclass
class GovernedRequest:
    """A protocol-neutral, governable tool invocation."""

    protocol: str
    tool_name: str
    arguments: dict[str, Any]
    wallet_id: str | None
    auth: AuthContext
    money: AgentMoney
    permit_id: str | None = None
    # Signed price commitment to charge against, if the caller holds one.
    quote_id: str | None = None
    idempotency_key: str | None = None
    transport: str = "adapter"
    endpoint: str = "/mcp/messages"
    request_id: str | None = None
    request_payload: dict[str, Any] | None = None


class GovernedRequestInvalid(ValueError):
    """The protocol request cannot be normalized into a governed request.

    Raised before any permit, idempotency, metering, or dispatch step runs.
    Transports map it to their invalid-params shape (JSON-RPC ``-32602``,
    HTTP 400); every message starts with ``Invalid params`` so the shared
    JSON-RPC code mapping recognizes it even when it propagates as a plain
    ``ValueError``.
    """


_CONTEXT_STRING_FIELDS = ("wallet_id", "permit_id", "quote_id", "request_path")


def validate_tools_call_params(raw: Any) -> dict[str, Any]:
    """Return the ``tools/call`` params as a well-shaped mapping, or refuse.

    Accepts either the full JSON-RPC body (``{"params": {...}}``) or the
    params object itself, as ``normalize_request`` always has. Every member
    the pipeline later reads is type-checked here so a malformed envelope is
    a controlled client error instead of an attribute error deep in the
    governed path. JSON ``null`` for ``arguments`` or ``mcpContext`` reads as
    the member being absent; any other non-object value is refused rather than
    coerced into an empty default.
    """
    if not isinstance(raw, dict):
        raise GovernedRequestInvalid(
            "Invalid params: tools/call params must be an object"
        )
    # A JSON-RPC envelope is known by its protocol members. An object without
    # them is unwrapped only when it carries no top-level tool ``name``, so a
    # params object that happens to contain a key called "params" is not
    # mistaken for an envelope, while an envelope that also carries a stray
    # top-level ``name`` member is still unwrapped.
    is_envelope = "params" in raw and (
        "method" in raw or "jsonrpc" in raw or "name" not in raw
    )
    params = raw["params"] if is_envelope else raw
    if not isinstance(params, dict):
        raise GovernedRequestInvalid("Invalid params: params must be an object")

    name = params.get("name")
    if name is None or name == "":
        # Same message the pipeline has always used for an absent tool name.
        raise GovernedRequestInvalid("Missing tool name")
    if not isinstance(name, str) or not name.strip():
        raise GovernedRequestInvalid("Invalid params: name must be a non-blank string")

    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    elif not isinstance(arguments, dict):
        raise GovernedRequestInvalid("Invalid params: arguments must be an object")

    mcp_context = params.get("mcpContext")
    if mcp_context is None:
        mcp_context = {}
    elif not isinstance(mcp_context, dict):
        raise GovernedRequestInvalid("Invalid params: mcpContext must be an object")
    for field_name in _CONTEXT_STRING_FIELDS:
        value = mcp_context.get(field_name)
        if value is not None and not isinstance(value, str):
            raise GovernedRequestInvalid(
                f"Invalid params: mcpContext.{field_name} must be a string"
            )
    key = mcp_context.get("idempotency_key")
    if key is not None:
        # Raises InvalidIdempotencyKeyError, which carries its own
        # machine-actionable detail; callers surface it as invalid params.
        validate_client_idempotency_key(key, source="mcpContext.idempotency_key")

    return {
        **params,
        "name": name,
        "arguments": arguments,
        "mcpContext": mcp_context,
    }


@dataclass
class GovernedResult:
    """The outcome of a governed invocation, protocol-neutral."""

    protocol: str
    raw: dict[str, Any]
    is_error: bool = False
    receipt: dict[str, Any] | None = None
    outcome: str | None = None
    ledger_entry_id: str | None = None
    error: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class GovernedInvocationAdapter(ABC):
    """Base class for protocol adapters into the governed invocation pipeline."""

    protocol: str

    @abstractmethod
    async def normalize_request(self, raw: Any, **context: Any) -> GovernedRequest:
        """Translate a protocol-specific request into a GovernedRequest."""

    @abstractmethod
    async def invoke(self, request: GovernedRequest) -> GovernedResult:
        """Run the request through the governed invocation pipeline."""

    @abstractmethod
    async def normalize_response(self, result: GovernedResult) -> dict[str, Any]:
        """Translate a GovernedResult back into the protocol's response shape."""


class McpGovernedAdapter(GovernedInvocationAdapter):
    """First adapter: Model Context Protocol (JSON-RPC ``tools/call``)."""

    protocol = "mcp"

    async def normalize_request(self, raw: Any, **context: Any) -> GovernedRequest:
        """Translate an MCP ``tools/call`` body into a GovernedRequest.

        Required context: ``auth`` (AuthContext) and ``money`` (AgentMoney).
        Optional: ``transport``, ``endpoint``, ``request_id``,
        ``idempotency_key``, ``request_payload``.
        """
        auth: AuthContext = context["auth"]
        money: AgentMoney = context["money"]
        idempotency_key: str | None = context.get("idempotency_key")

        params = validate_tools_call_params(raw)
        mcp_context = params["mcpContext"]
        if idempotency_key is None:
            # The transport resolved no key of its own; the body's key, if
            # any, was validated above and is used exactly as sent.
            idempotency_key = mcp_context.get("idempotency_key")
        return GovernedRequest(
            protocol=self.protocol,
            tool_name=params["name"],
            arguments=params["arguments"],
            wallet_id=mcp_context.get("wallet_id"),
            auth=auth,
            money=money,
            permit_id=mcp_context.get("permit_id"),
            quote_id=mcp_context.get("quote_id"),
            idempotency_key=idempotency_key,
            transport=context.get("transport", "adapter"),
            endpoint=context.get("endpoint", "/mcp/messages"),
            request_id=context.get("request_id"),
            request_payload=context.get("request_payload"),
        )

    async def invoke(self, request: GovernedRequest) -> GovernedResult:
        """Run the governed pipeline.

        Governance failures (denials, insufficient funds, tool errors) are
        raised as the pipeline's typed exceptions and propagate to the caller,
        which owns the protocol-specific error envelope. This keeps a single
        source of truth for both the happy path and the error semantics.
        """
        # Lazy import: the MCP router pulls in many services at import time, and
        # this keeps the trust package import-light and free of cycles.
        from app.routers.mcp import _execute_registered_tool

        raw = await _execute_registered_tool(
            tool_name=request.tool_name,
            arguments=request.arguments,
            wallet_id=request.wallet_id,
            auth=request.auth,
            money=request.money,
            transport=request.transport,
            endpoint=request.endpoint,
            request_id=request.request_id,
            permit_id=request.permit_id,
            quote_id=request.quote_id,
            idempotency_key=request.idempotency_key,
            request_payload=request.request_payload,
        )

        receipt = raw.get("receipt") if isinstance(raw, dict) else None
        return GovernedResult(
            protocol=self.protocol,
            raw=raw,
            is_error=bool(raw.get("isError")) if isinstance(raw, dict) else False,
            receipt=receipt,
            outcome=(receipt or {}).get("outcome"),
            ledger_entry_id=(receipt or {}).get("ledger_entry_id"),
        )

    async def normalize_response(self, result: GovernedResult) -> dict[str, Any]:
        return result.raw


__all__ = [
    "GovernedRequest",
    "GovernedRequestInvalid",
    "GovernedResult",
    "GovernedInvocationAdapter",
    "McpGovernedAdapter",
    "validate_tools_call_params",
]
