"""
MCP Router
==========

Governed MCP proxy for the agent trust plane (permits, metering, receipts).

Provides:
- /mcp/tools.json - MCP manifest discovery
- /mcp/messages - JSON-RPC message handling

This enables agents to:
1. Discover ops-registered tools via tools.json
2. Invoke tools under permit + wallet metering + signed receipts

There is no SSE transport. The governed surface is the HTTP/JSON-RPC tools
subset only.
"""

import asyncio
import inspect
import json
import logging
import math
import uuid
from dataclasses import asdict
from decimal import Decimal
from typing import Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from ..audit.lightweight import record_audit
from ..core.config import DuplicateGuardMode, get_settings
from ..policy.jev_guard import evaluate_jev_guard
from ..services.jev_guard_metadata import jev_audit_id, load_jev_guard_metadata
from ..core.auth import (
    AuthContext,
    get_auth_context,
    reject_anonymous_production_catalog,
)
from ..core.oidc_iga import (
    EnterprisePrincipal,
    IGAUseReservation,
    IGAError,
    enforce_tool_call,
    is_iga_issuer_token,
    parse_enterprise_token,
    release_tool_use,
)
from ..trust import (
    AuditChainContendedError,
    AuditEventConflictError,
    RefundReconciliationContendedError,
)
from ..services.billing_engine import (
    LedgerOperationConflictError,
    LedgerWriteContendedError,
)
from ..services.service_registry import get_service_registry
from ..services.action_permits import (
    action_execution_identity,
    canonical_action_payload,
    validate_action_request,
)
from ..db.database import get_session_factory
from ..db.models import PermitModel
from ..services.mcp_generator import get_mcp_generator
from ..services.dogfood_tool import sync_dogfood_tool_registration
from ..services.mcp_phase9_tools import sync_proof_surface_mcp_registration
from ..services.mcp_dispatch_attempts import (
    DispatchAttemptConflictError,
    DispatchClaimUnavailableError,
    DispatchPrepareCommitUncertainError,
    DispatchPrepareRolledBackError,
    DispatchResultRejectedError,
    DispatchResultTooLargeError,
    McpDispatchAttemptService,
    get_mcp_dispatch_attempt_service,
)
from ..services.mcp_dispatch_reconciliation import (
    get_mcp_dispatch_reconciliation_service,
)
from ..services.upstream_mcp import (
    UpstreamMcpDeliveryUncertainError,
    UpstreamMcpDispatchClaimUnavailableError,
    UpstreamMcpPreDispatchError,
    UpstreamMcpResponseRejectedError,
    UpstreamMcpReturnedError,
)
from ..schemas.billing import InsufficientFundsResponse, LedgerEntry, ServiceCategory

# Spine primitives are consumed through the trust-plane facade so the governed
# invocation path depends on the product core by its public boundary.
from ..trust import (
    APPROVAL_STATUS_APPROVED,
    APPROVAL_STATUS_PENDING,
    QUOTE_REASON_CONSUMED,
    GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
    AgentMoney,
    GovernedRequestInvalid,
    HumanApprovalError,
    HumanApprovalUnavailableError,
    IdempotencyBegin,
    IdempotencyConflictError,
    IdempotencyInProgressError,
    InvalidIdempotencyKeyError,
    decode_idempotency_key_header,
    McpGovernedAdapter,
    PermitWriteContendedError,
    PolicyDecision,
    ReceiptWriteContendedError,
    evaluate_tool_invocation,
    evaluate_wallet_policy,
    get_agent_money,
    get_human_approval_service,
    get_idempotency_service,
    charge_units_for,
    extract_recipient_identity,
    get_permit_service,
    get_quote_service,
    get_receipt_service,
    permit_constraints_snapshot,
    recipient_binding_matches,
    record_audit_event,
    get_refund_reconciliation_service,
    resolve_client_idempotency_key,
    sha256_hex,
    tool_price,
    validate_tools_call_params,
)

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/mcp", tags=["MCP"])

# The MCP transport drives the governed pipeline through the protocol-neutral
# adapter seam. The adapter delegates to _execute_registered_tool below, so
# there is exactly one governance implementation.
_mcp_adapter = McpGovernedAdapter()


def _ensure_local_mcp_tools_registered() -> None:
    """Align local MCP tools with proof-surface + dogfood flags (lazy + flag flips)."""
    sync_proof_surface_mcp_registration()
    sync_dogfood_tool_registration()


async def build_mcp_tools_manifest(
    category: ServiceCategory | None = None,
) -> dict[str, Any]:
    """Build the canonical MCP tools manifest for all public discovery routes."""
    _ensure_local_mcp_tools_registered()
    generator = get_mcp_generator()
    return await generator.generate_tools_json_async(category=category)


class ToolExecutionError(RuntimeError):
    """Raised after a dispatched tool fails and compensation is complete."""


class GovernedToolError(RuntimeError):
    """Terminal governed-call error that can carry a signed receipt."""

    def __init__(
        self,
        reason: str,
        *,
        receipt: dict[str, Any] | None = None,
        extra_data: dict[str, Any] | None = None,
        status_code: int = 500,
        jsonrpc_code: int = -32603,
    ) -> None:
        super().__init__(reason)
        self.receipt = receipt
        self.extra_data = extra_data or {}
        self.status_code = status_code
        self.jsonrpc_code = jsonrpc_code


class ToolPermissionDenied(PermissionError):
    """Permission denial that may carry a signed receipt for governed calls.

    ``details`` carries the evaluated constraint behind the denial — the
    budget that was short, the limit that was hit — so the caller can act on
    the refusal instead of only retrying it.
    """

    def __init__(
        self,
        reason: str,
        receipt: dict[str, Any] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(reason)
        self.receipt = receipt
        self.details = details or {}
        self.status_code = 403
        self.jsonrpc_code = -32003


class HumanApprovalPendingSignal(RuntimeError):
    """Governed invoke paused on a retryable human-approval condition.

    Not a terminal outcome: no receipt exists, nothing was charged, and the
    caller's idempotency key was released — the same invoke (same body, same
    key) should be retried once the approval is decided (or, for
    ``human_approval_unavailable``, once Sentinel is reachable again).
    """

    jsonrpc_code = -32005

    def __init__(
        self,
        reason: str,
        *,
        data: dict[str, Any] | None = None,
        status_code: int = 202,
    ) -> None:
        super().__init__(reason)
        self.data = data or {}
        self.status_code = status_code


AUDIT_CHAIN_CONTENDED_AFTER_EFFECTS = "audit_chain_contended_after_effects"
RECEIPT_WRITE_CONTENDED_AFTER_EFFECTS = "receipt_write_contended_after_effects"


class TerminalRecordContendedError(RuntimeError):
    """A terminal record was lost to contention after the call had effects.

    The audit event or the receipt could not be written for a call that had
    already run or already moved the wallet. Both losses are deliberately
    non-retryable, and this type is what carries that: it is not a subclass of
    ``AuditChainContendedError`` or ``ReceiptWriteContendedError``, so neither
    the routers' ``-32005`` handlers nor the idempotency unwind in
    ``_execute_registered_tool`` can catch it and hand a charged caller a
    "retry".

    It exists because ``-32603``/``internal_error`` was the wrong answer, not
    because the outcome is knowable. ``-32603`` is this pipeline's *unclassified*
    channel -- `docs/failure-semantics.md` defines it as "a failure the pipeline
    did not classify" -- and these two sites are the opposite of unclassified:
    each caught a typed contention error, asked whether effects were committed,
    and chose non-retryability on purpose. Reporting a deliberated state through
    the unclassified channel left operators unable to separate it from a genuine
    bug without grepping correlation ids, and left a client free to read
    "internal error" as "try again", which with a *fresh* idempotency key runs
    and charges the call a second time.

    What the name claims is only the situation, never the outcome: effects are
    committed, no receipt exists, the record is counted for manual review, and
    the caller must not retry. Whether the tool's effect landed downstream stays
    exactly as unknowable as it was. This is therefore not wedge #2 -- that wedge
    turns an ambiguous outcome into a distinct *receipted* state, and the
    defining feature here is that no receipt could be written at all. It is the
    honest name for falling out of the receipted state machine.
    """

    jsonrpc_code = -32007
    status_code = 500

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _terminal_record_contended_data(reason: str) -> dict[str, Any]:
    """The machine-actionable body for a ``-32007``, shared by every surface.

    Shaped like the other remediation-carrying errors on this path so a client
    reads one convention: what went wrong, the specific reason code, and the
    one action that is correct.
    """
    return {
        "error": "manual_review_required",
        "reason_code": reason,
        "remediation": {
            "type": "reconcile_out_of_band",
            "detail": (
                "The call's effects are committed and no receipt was written. "
                "Do not retry with a new idempotency key: that would run and "
                "charge the call a second time. A governed record, where one "
                "was opened, stays held by the invocation that ran and answers "
                "idempotency_in_progress until an operator resolves it. This "
                "record is counted for manual review; reconcile from the "
                "ledger entry and the audit chain."
            ),
        },
    }


def _header_idempotency_key_sources(request: Request) -> list[tuple[str, object]]:
    """Every ``Idempotency-Key`` header the caller sent, in wire order.

    Each line is read as the UTF-8 the client wrote, and refused when it is
    not UTF-8, so a non-ASCII key equals the same key sent in the body and no
    two wire values alias to one key. A repeated header is two explicit
    sources: identical copies collapse to one key, differing copies are
    refused as a conflict rather than letting the first one silently win.
    """
    return [
        ("Idempotency-Key header", decode_idempotency_key_header(value))
        for value in request.headers.getlist("idempotency-key")
    ]


def _context_idempotency_key_sources(
    mcp_context: dict[str, Any],
) -> list[tuple[str, object]]:
    """The ``mcpContext.idempotency_key`` source when the body carries one.

    JSON ``null`` reads as absent, matching the ``str | None`` shape the
    typed ``McpContext`` schema has always declared for this member.
    """
    value = mcp_context.get("idempotency_key")
    if value is None:
        return []
    return [("mcpContext.idempotency_key", value)]


# Far above any legitimate MCP message, far below where Python 3.11's JSON
# parser starts raising RecursionError instead of returning a parse result.
# Shared with the standard endpoint so both transports refuse the same depth.
_MAX_JSON_NESTING_DEPTH = 100


def _json_nesting_depth_exceeds(body: bytes, limit: int) -> bool:
    """True when raw JSON bytes nest deeper than ``limit`` outside strings."""
    depth = 0
    in_string = False
    escaped = False
    for byte in body:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:  # double quote
                in_string = False
        elif byte == 0x22:
            in_string = True
        elif byte in (0x7B, 0x5B):  # { or [
            depth += 1
            if depth > limit:
                return True
        elif byte in (0x7D, 0x5D):  # } or ]
            depth = max(0, depth - 1)
    return False


def _reject_non_finite_constant(token: str) -> Any:
    """``json.loads`` hook for the NaN and Infinity literals: refuse them."""
    raise ValueError(f"non-finite JSON number: {token}")


def _finite_float(token: str) -> float:
    """``json.loads`` hook for floats: refuse a literal that overflows to infinity."""
    value = float(token)
    if not math.isfinite(value):
        raise ValueError(f"non-finite JSON number: {token}")
    return value


def _loads_strict_json(raw: bytes) -> Any:
    """Parse a request body as RFC 8259 JSON, refusing what a reply could not carry.

    Python's parser accepts the NaN and Infinity literals and overflows 1e400
    to an infinite float; a JSON "\\ud800" escape decodes to a lone surrogate.
    Every JSON-RPC response echoes the request id and a result may echo
    arguments, and the response serializer refuses non-finite floats and
    cannot utf-8 encode a lone surrogate — after the tool has run and the
    wallet has been charged. Refusing them at parse time keeps that failure
    ahead of every side effect. Raises ValueError, which also covers a JSON
    syntax error and the decode error of a non-UTF-8 body.
    """
    body = json.loads(
        raw, parse_constant=_reject_non_finite_constant, parse_float=_finite_float
    )
    # Rehearse the encode the response will perform (ensure_ascii=False, then
    # utf-8) so an unpaired surrogate anywhere — id, arguments, keys — is a
    # refusal here rather than a 500 after the charge.
    try:
        json.dumps(body, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("request JSON contains an unpaired surrogate") from exc
    return body


def _safe_jsonrpc_id(value: Any) -> str | int | float | None:
    """Echo a JSON-RPC ``id`` only when it is a shape the spec allows.

    Strings, numbers, and ``null`` come back as sent; anything else (an
    object, an array, a boolean) is replaced with ``null`` so a malformed id
    is neither echoed into the response nor stringified into audit records.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (str, int, float)):
        return value
    return None


def _jsonrpc_error_response(
    request_id: str | int | float | None,
    code: int,
    message: str,
    data: dict[str, Any] | None = None,
) -> JSONResponse:
    error_payload: dict[str, Any] = {"code": code, "message": message}
    if data:
        error_payload["data"] = data
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": error_payload})


class McpContext(BaseModel):
    """MCP execution context passed in tool calls."""

    wallet_id: str = Field(..., description="Wallet to charge for this call")
    request_path: str | None = Field(
        None, description="Optional request path for tracking"
    )
    permit_id: str | None = Field(None, description="Signed permit for governed calls")
    quote_id: str | None = Field(
        None, description="Signed price quote to charge against (locks the price)"
    )
    idempotency_key: str | None = Field(
        None, description="Replay key for governed calls"
    )


class ToolCallRequest(BaseModel):
    """MCP tool call request (tools/call method)."""

    name: str = Field(..., description="Tool name (service_id)")
    arguments: dict[str, Any] = Field(
        default_factory=dict, description="Tool arguments"
    )
    mcp_context: McpContext | None = Field(None, description="Billing context")


class ToolCallResponse(BaseModel):
    """MCP tool call response."""

    content: list[dict[str, Any]]
    isError: bool = False
    structuredContent: dict[str, Any] | None = None
    receipt: dict[str, Any] | None = None


@router.get(
    "/tools.json",
    name="MCP Tools Manifest",
    responses={401: {"description": "Missing credentials on production-like boots"}},
)
async def get_tools_json(
    request: Request,
    category: ServiceCategory | None = None,
) -> JSONResponse:
    """
    Return the MCP tools.json manifest.

    This is an HTTP mirror of tool metadata exposed by this project's
    governed JSON-RPC subset. Clients can obtain the same list by
    sending ``tools/list`` to ``/mcp/messages``; that endpoint does not
    implement the complete MCP initialization lifecycle.

    On production-like boots the catalog requires the same credentials as
    invoke. Local-compatible environments keep it anonymous for quickstart.

    Query Parameters:
        category: Optional service category filter

    Returns:
        MCP tools.json manifest with tool definitions
    """
    await reject_anonymous_production_catalog(request)
    manifest = await build_mcp_tools_manifest(category=category)
    return JSONResponse(content=manifest)


@router.post(
    "/messages",
    name="MCP JSON-RPC Messages",
    deprecated=True,
    description=(
        "Legacy project JSON-RPC endpoint, kept for existing clients and the "
        "local proof scripts. It does not implement the standard MCP "
        "initialization lifecycle. Check `/.well-known/agent.json` before "
        "assuming the standard MCP Streamable HTTP endpoint at POST /mcp is "
        "available; both transports run the same governed permit→meter→receipt "
        "path when enabled."
    ),
)
async def handle_messages(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
    money: AgentMoney = Depends(get_agent_money),
) -> JSONResponse:
    """
    Handle MCP JSON-RPC messages.

    Supports:
    - tools/list: List available tools
    - tools/call: Execute a tool

    The request body is a JSON-RPC 2.0 request.
    """
    raw_body = await request.body()
    if _json_nesting_depth_exceeds(raw_body, _MAX_JSON_NESTING_DEPTH):
        # Python 3.11's parser raises RecursionError, not JSONDecodeError, on
        # deep nesting; the standard endpoint already pre-screens raw bytes.
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON: nesting depth exceeds the supported limit",
        )
    try:
        body = _loads_strict_json(raw_body)
    except ValueError:
        # Malformed JSON, a non-UTF-8 body, a non-finite number, or an
        # unpaired surrogate: all are ValueErrors, and none may reach the
        # pipeline — the last two would fail the reply after the charge.
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # The envelope is validated before any member is read. A body that parsed
    # but is not an object (``[]``, ``null``, a string, a number) used to reach
    # ``.get`` and surface as HTTP 500; it is a client error, answered with the
    # JSON-RPC invalid-request shape and a null id because no id can be
    # trusted from a non-object body.
    if not isinstance(body, dict):
        return _jsonrpc_error_response(
            None, -32600, "Invalid Request: JSON-RPC envelope must be an object"
        )
    request_id = _safe_jsonrpc_id(body.get("id"))
    # A version member that is present must be exactly "2.0": a "1.0" envelope
    # used to reach dispatch and execute a governed action. The member is not
    # required here — this route has always accepted version-less envelopes
    # and the standard endpoint is the strict transport — so only a stated,
    # wrong version is refused.
    if "jsonrpc" in body and body["jsonrpc"] != "2.0":
        return _jsonrpc_error_response(
            request_id, -32600, 'Invalid Request: jsonrpc must be "2.0"'
        )
    method = body.get("method")
    if not isinstance(method, str) or not method:
        return _jsonrpc_error_response(
            request_id, -32600, "Invalid Request: method must be a non-empty string"
        )
    params = body.get("params")
    if params is None:
        params = {}
    elif not isinstance(params, dict):
        # JSON-RPC allows positional params in general; this server's methods
        # take named params only, and ``[]`` must not turn into ``{}``.
        return _jsonrpc_error_response(
            request_id, -32602, "Invalid params: params must be an object"
        )

    if method == "tools/list":
        result = await _handle_tools_list(params)
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": result,
            }
        )

    elif method == "tools/call":
        # Shape and replay-key validation run before the governed pipeline is
        # entered, so a refused request has minted, reserved, charged, and
        # dispatched nothing. The header and the body may both carry a key;
        # they must then agree, and a present-but-unusable value in either is
        # refused instead of the other source (or a generated key) winning.
        try:
            call_params = validate_tools_call_params(params)
            client_idempotency_key = resolve_client_idempotency_key(
                [
                    *_header_idempotency_key_sources(request),
                    *_context_idempotency_key_sources(call_params["mcpContext"]),
                ]
            )
        except InvalidIdempotencyKeyError as e:
            return _jsonrpc_error_response(
                request_id, -32602, str(e), e.as_error_data()
            )
        except GovernedRequestInvalid as e:
            return _jsonrpc_error_response(request_id, -32602, str(e))
        try:
            result = await _handle_tools_call(
                call_params,
                auth=auth,
                money=money,
                transport="jsonrpc",
                endpoint="/mcp/messages",
                request_id=str(request_id) if request_id is not None else None,
                idempotency_key=client_idempotency_key,
                request_payload=body,
            )
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": result,
                }
            )
        except HumanApprovalPendingSignal as e:
            error_payload: dict[str, Any] = {
                "code": e.jsonrpc_code,
                "message": str(e),
            }
            if e.data:
                error_payload["data"] = e.data
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": error_payload,
                }
            )
        except (
            IdempotencyInProgressError,
            LedgerWriteContendedError,
            AuditChainContendedError,
            ReceiptWriteContendedError,
            PermitWriteContendedError,
        ) as e:
            # All five mean the same thing to a caller: nothing terminal was
            # recorded, retry the same idempotency key. -32005 is this
            # surface's retryable code and str(e) carries which one it was, so
            # a client can distinguish "a winner is mid-flight" from "the write
            # lost its snapshot", "the audit chain stayed busy", "the receipt
            # insert did" or "the permit write did" without any of them landing
            # as internal_error.
            #
            # Only a refusal that ran nothing reaches here as
            # AuditChainContendedError, ReceiptWriteContendedError or
            # PermitWriteContendedError: the sites that audit, receipt or
            # release budget AFTER the tool ran and was charged deliberately
            # re-raise their loss as a non-retryable type so a charged call is
            # never told to try again.
            #
            # PermitWriteContendedError is matched, not its PermitError base:
            # the base also carries permit_not_found,
            # dispatch_attempt_not_found and the budget denials, none of which
            # are retryable contention.
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32005,
                        "message": str(e),
                    },
                }
            )
        except TerminalRecordContendedError as e:
            # The same contention, on the other side of the charge. Its own
            # code so a client cannot read it as either neighbour: -32005 would
            # invite the retry that runs a paid call again, and -32603 would
            # bury a deliberated state in the unclassified channel.
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": e.jsonrpc_code,
                        "message": e.reason,
                        "data": _terminal_record_contended_data(e.reason),
                    },
                }
            )
        except ToolPermissionDenied as e:
            error_payload = {
                "code": -32003,
                "message": str(e),
            }
            data: dict[str, Any] = {}
            if e.receipt:
                data["receipt"] = e.receipt
            if e.details:
                data["details"] = e.details
            if data:
                error_payload["data"] = data
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": error_payload,
                }
            )
        except GovernedToolError as e:
            error_payload = {
                "code": e.jsonrpc_code,
                "message": str(e),
            }
            error_data = dict(e.extra_data)
            if e.receipt:
                error_data["receipt"] = e.receipt
            if error_data:
                error_payload["data"] = error_data
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": error_payload,
                }
            )
        except PermissionError as e:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32003,
                        "message": str(e),
                    },
                }
            )
        except ToolExecutionError as e:
            # Legacy (unpermitted) tool failure: compensation is complete and,
            # as on the governed path, the message is the tool's own error.
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32603,
                        "message": str(e),
                    },
                }
            )
        except ValueError as e:
            code = _value_error_jsonrpc_code(str(e))
            if code is None:
                error_payload = _internal_error(
                    e, surface="/mcp/messages", request_id=request_id
                )
            else:
                error_payload = {"code": code, "message": str(e)}
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": error_payload,
                }
            )
        except Exception as e:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": _internal_error(
                        e, surface="/mcp/messages", request_id=request_id
                    ),
                }
            )

    else:
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": f"Method not found: {method}",
                },
            }
        )


async def _handle_tools_list(params: dict) -> dict:
    """Handle MCP tools/list request."""
    category = params.get("category")
    if isinstance(category, str) and category:
        try:
            category = ServiceCategory(category)
        except ValueError:
            category = None
    else:
        # A non-string filter is not a category; list everything rather than
        # let an unexpected shape reach the enum constructor.
        category = None

    manifest = await build_mcp_tools_manifest(category=category)
    return {"tools": manifest["tools"]}


def _legacy_mcp_idempotency_endpoints(
    *,
    endpoint: str,
    tool_name: str,
) -> tuple[str, ...]:
    """Return the pre-canonical MCP replay scopes, current transport first."""
    candidates = (
        endpoint,
        "/mcp/messages",
        f"/mcp/tools/{tool_name}/invoke",
    )
    return tuple(
        candidate
        for candidate in dict.fromkeys(candidates)
        if candidate != GOVERNED_MCP_IDEMPOTENCY_ENDPOINT
    )


async def _begin_governed_mcp_idempotency(
    *,
    idem: Any,
    wallet_id: str,
    idempotency_key: str,
    tool_name: str,
    endpoint: str,
    logical_request_payload: dict[str, Any],
    legacy_request_payload: dict[str, Any] | None,
    operation_kind: str,
    wait_timeout_seconds: float = 0.0,
) -> IdempotencyBegin:
    """Adopt a completed pre-canonical replay without creating a new identity.

    Before migration 026, governed MCP calls were keyed by their physical REST
    or JSON-RPC endpoint and hashed the raw transport payload. A canonical row
    must not be created while either legacy scope already owns the wallet/key:
    doing so could debit and dispatch the same historical action again.

    Only the current transport payload can be compared exactly. An existing row
    under the other transport is therefore a fail-closed conflict (or remains in
    progress) rather than an unsafe cross-transport guess. Migration 027 adds a
    database identity shared by old and current workers; the second lookup below
    adopts whichever row won a rolling-deployment insertion race.
    """
    historical_payload = legacy_request_payload or logical_request_payload

    async def resolve_existing() -> IdempotencyBegin | None:
        canonical = await idem.get_record(
            wallet_id=wallet_id,
            endpoint=GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
            idempotency_key=idempotency_key,
        )
        if canonical is not None:
            return await idem.begin_with_record(
                wallet_id=wallet_id,
                endpoint=GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
                idempotency_key=idempotency_key,
                request_payload=logical_request_payload,
                operation_kind=operation_kind,
                wait_timeout_seconds=wait_timeout_seconds,
            )

        for candidate_endpoint in _legacy_mcp_idempotency_endpoints(
            endpoint=endpoint,
            tool_name=tool_name,
        ):
            existing = await idem.get_record(
                wallet_id=wallet_id,
                endpoint=candidate_endpoint,
                idempotency_key=idempotency_key,
            )
            if existing is None:
                continue
            if candidate_endpoint != endpoint:
                if existing.response_json:
                    raise IdempotencyConflictError("idempotency_key_reused")
                raise IdempotencyInProgressError("idempotency_in_progress")
            return await idem.begin_with_record(
                wallet_id=wallet_id,
                endpoint=candidate_endpoint,
                idempotency_key=idempotency_key,
                request_payload=historical_payload,
                operation_kind=operation_kind,
                wait_timeout_seconds=wait_timeout_seconds,
            )
        return None

    existing = await resolve_existing()
    if existing is not None:
        return existing

    try:
        return await idem.begin_with_record(
            wallet_id=wallet_id,
            endpoint=GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
            idempotency_key=idempotency_key,
            request_payload=logical_request_payload,
            operation_kind=operation_kind,
            wait_timeout_seconds=wait_timeout_seconds,
        )
    except IntegrityError:
        # The normalized unique index can reject this insert because a legacy
        # worker committed its physical-endpoint row after our initial probes.
        # Re-resolve that winner; unrelated integrity failures still propagate.
        raced = await resolve_existing()
        if raced is not None:
            return raced
        winner = await idem.get_governed_mcp_record(
            wallet_id=wallet_id,
            idempotency_key=idempotency_key,
        )
        if winner is not None:
            # A legacy REST worker for a different tool may own the normalized
            # wallet/key. Its transport hash cannot represent this request, so
            # surface a stable conflict instead of leaking a raw integrity error.
            if winner.response_json:
                raise IdempotencyConflictError("idempotency_key_reused")
            raise IdempotencyInProgressError("idempotency_in_progress")
        raise


def _verified_enterprise_principal(
    enterprise_bearer_token: str | None,
) -> EnterprisePrincipal | None:
    """Return the verified enterprise (human) principal behind this call, if any.

    ``enterprise_bearer_token`` is the Authorization bearer that
    ``get_auth_context`` carried alongside the caller's X-API-Key — set only
    when the token's unverified issuer names a pinned IGA issuer. Absent
    bearers, internal JWTs, and a disabled IGA layer all yield None here and
    the caller's behavior is unchanged. A bearer FROM a pinned enterprise
    issuer is fully verified by ``parse_enterprise_token`` (pinned key,
    algorithm allowlist, audience, issuer, expiry); verification failures
    raise :class:`IGAError` and the governed pipeline denies with that reason
    — a bad enterprise token never falls through to ungoverned execution.
    """
    if not enterprise_bearer_token:
        return None
    if not is_iga_issuer_token(enterprise_bearer_token):
        return None
    return parse_enterprise_token(enterprise_bearer_token)


async def _execute_registered_tool(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    wallet_id: str | None,
    auth: AuthContext,
    money: AgentMoney,
    transport: str,
    endpoint: str,
    request_id: str | None,
    permit_id: str | None = None,
    quote_id: str | None = None,
    idempotency_key: str | None = None,
    request_payload: dict[str, Any] | None = None,
) -> dict:
    """Run the governed tool call, freeing the key when the answer is "retry".

    Only the unwind lives here; ``_execute_registered_tool_inner`` orchestrates.
    An ``AuditChainContendedError``, ``ReceiptWriteContendedError`` or
    ``PermitWriteContendedError`` that escapes it is provably pre-effect --
    every audit and receipt site past the charge declares
    ``effects_committed=True`` and converts its own loss to a non-retryable
    type, and both budget releases that run past the charge absorb their own
    contention rather than raising it -- so the in-progress idempotency
    record, if one was begun, holds nothing terminal.

    The permit type has to be here, not only in the routers' ladders. The
    governed record is begun before the reserve is attempted, so a contended
    reserve is exactly the case that leaves a record this invocation owns and
    no longer needs; classifying it retryable at the edge while leaving the
    record held would name a retry that meets ``idempotency_in_progress``
    forever.

    Releasing it is what makes the retryable answer true rather than merely
    polite. Reconciliation deliberately does not delete uncharged local
    records, so a record left in progress would meet every retry of that key
    with ``idempotency_in_progress`` forever: the -32005 would name a retry the
    caller could never actually make.

    Only a record this invocation was granted is released, identified by the id
    the begin returned. A call refused with ``idempotency_in_progress`` never
    held one and audits that refusal like any other, so releasing by (wallet,
    endpoint, key) alone would delete the winner's live, uncharged row while
    the winner ran on -- freeing the key to execute and debit a second time.
    Those coordinates name whichever row holds them now; only the granted id
    names ours.
    """
    owned_record: dict[str, Any] = {}
    try:
        return await _execute_registered_tool_inner(
            tool_name=tool_name,
            arguments=arguments,
            wallet_id=wallet_id,
            auth=auth,
            money=money,
            transport=transport,
            endpoint=endpoint,
            request_id=request_id,
            permit_id=permit_id,
            quote_id=quote_id,
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            owned_record=owned_record,
        )
    except ToolPermissionDenied as exc:
        if exc.receipt is None and permit_id and wallet_id:
            exc.receipt = await _action_request_denial_receipt(
                permit_id=permit_id,
                wallet_id=wallet_id,
                auth=auth,
                tool_name=tool_name,
                arguments=arguments,
                reason=str(exc),
                endpoint=endpoint,
                transport=transport,
                request_id=request_id,
                transport_key=idempotency_key,
            )
        raise
    except (
        AuditChainContendedError,
        ReceiptWriteContendedError,
        PermitWriteContendedError,
    ):
        record_id = owned_record.get("record_id")
        record_key = owned_record.get("key", idempotency_key)
        if record_id and wallet_id and record_key:
            idem = get_idempotency_service()
            # The two endpoints a governed record can live under: the canonical
            # one, and the request's own when a pre-canonical legacy row was
            # adopted. The record id is what makes trying both safe -- it pins
            # the release to the row this invocation was granted, so a lookup
            # that lands on another row is a no-op rather than a delete.
            for record_endpoint in (
                {owned_record["endpoint"]}
                if "endpoint" in owned_record
                else {GOVERNED_MCP_IDEMPOTENCY_ENDPOINT, endpoint}
            ):
                try:
                    abandoned = await idem.abandon(
                        wallet_id=wallet_id,
                        endpoint=record_endpoint,
                        idempotency_key=record_key,
                        expected_record_id=record_id,
                    )
                    if abandoned and record_endpoint == "/mcp/action/v1":
                        # Only confirmed deletion proves this invocation's use
                        # unaccepted. Inner cleanup may already have released
                        # it; in that case abandon returns False, not a refund.
                        await _release_iga_use(
                            owned_record.get("iga_granted_use"),
                            tool_name,
                            reason="preacceptance_write_contended",
                        )
                except Exception:
                    logger.exception(
                        "mcp_audit_contended_idempotency_abandon_failed",
                        extra={"wallet_id": wallet_id},
                    )
        raise


async def _action_request_denial_receipt(
    *,
    permit_id: str,
    wallet_id: str,
    auth: AuthContext,
    tool_name: str,
    arguments: dict[str, Any],
    reason: str,
    endpoint: str,
    transport: str,
    request_id: str | None,
    transport_key: str | None,
) -> dict[str, Any] | None:
    """Receipt a verified subject's refused request, never its execution owner.

    Expiry/revocation removes execution authority, not eligibility for denial
    evidence. Foreign or unsigned authority does not establish that eligibility.
    """
    if not auth.is_bootstrap_admin and auth.wallet_id != wallet_id:
        return None
    async with get_session_factory()() as session:
        model = await session.get(PermitModel, permit_id)
    if (
        model is None
        or model.action_contract_version != 1
        or model.subject_wallet_id != wallet_id
        or (model.subject_key_id and model.subject_key_id != auth.key_id)
    ):
        return None
    try:
        if not await get_permit_service().verify_signature(model):
            return None
    except (TypeError, ValueError):
        return None
    # This hashes the refused request. It is intentionally not the authorized
    # action digest and carries no idempotency/dispatch/ledger owner linkage.
    denied_request = {
        "kind": "action_request_denial",
        "permit_id": permit_id,
        "wallet_id": wallet_id,
        "tool_name": tool_name,
        "arguments": arguments,
    }
    decision = evaluate_tool_invocation(
        auth=auth,
        wallet_id=wallet_id,
        tool_name=tool_name,
        estimated_cost=None,
        request_id=request_id,
    )
    audit = await _audit_mcp_invocation(
        decision=decision,
        endpoint=endpoint,
        transport=transport,
        ok=False,
        error=reason,
        effects_committed=False,
        extra_metadata={
            "evidence_scope": "request_denial",
            "permit_id": permit_id,
            "transport_idempotency_key": transport_key,
            "request_hash": sha256_hex(denied_request),
        },
    )
    receipt = await get_receipt_service().create_receipt(
        permit_id=permit_id,
        wallet_id=wallet_id,
        key_id=auth.key_id,
        tool=tool_name,
        request_payload=denied_request,
        response_payload={"error": reason},
        ledger_entry_id=None,
        credits_authorized=Decimal("0"),
        credits_charged=Decimal("0"),
        outcome="denied",
        reason_code=_stable_receipt_reason(reason),
        audit_event_id=audit.event_id,
        constraints_evaluated={"evidence_scope": "request_denial"},
    )
    return _receipt_response_payload(receipt)


async def _assert_action_current_access(
    *,
    permit_id: str,
    wallet_id: str,
    auth: AuthContext,
    tool_name: str,
    arguments: dict[str, Any],
    money: AgentMoney,
    estimated_cost: Decimal,
    phase: str,
) -> None:
    registry = get_service_registry()
    service = await registry.get(tool_name)
    binding = registry.get_action_binding(service) if service else None
    if binding is None or service is None:
        raise ToolPermissionDenied("action_tool_binding_required")
    async with get_session_factory()() as session:
        model = await session.get(PermitModel, permit_id)
    if model is None:
        raise ToolPermissionDenied("permit_not_found")
    validation = await validate_action_request(
        model,
        binding,
        wallet_id,
        auth.key_id,
        arguments,
        "replay" if phase == "replay" else "admission",
    )
    if not validation.allowed:
        raise ToolPermissionDenied(validation.reason or "action_permit_denied")
    try:
        principal = _verified_enterprise_principal(auth.enterprise_bearer_token)
        if principal is not None:
            iga = await enforce_tool_call(principal, tool_name, consume=False)
            if not iga.allowed:
                raise ToolPermissionDenied(iga.reason)
    except IGAError as exc:
        raise ToolPermissionDenied(exc.reason) from exc
    from ..core.runtime_mode import SERVICE_NAMES, is_simulation

    category = service.get("category", ServiceCategory.PLATFORM_FEE.value)
    policy = await evaluate_wallet_policy(
        wallet_id=wallet_id,
        tool_name=tool_name,
        service_category=category,
        estimated_cost=estimated_cost,
        daily_spend_used=await money.get_daily_spend(wallet_id),
        simulation=is_simulation(category) if category in SERVICE_NAMES else False,
        approval_gate_active=False,
    )
    if not policy.allowed:
        raise ToolPermissionDenied(policy.reason or "policy_denied")


async def _execute_registered_tool_inner(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    wallet_id: str | None,
    auth: AuthContext,
    money: AgentMoney,
    transport: str,
    endpoint: str,
    request_id: str | None,
    permit_id: str | None = None,
    quote_id: str | None = None,
    idempotency_key: str | None = None,
    request_payload: dict[str, Any] | None = None,
    owned_record: dict[str, Any] | None = None,
) -> dict:
    if not tool_name:
        raise ValueError("Missing tool name")
    if not wallet_id:
        raise ValueError("Missing wallet_id in mcpContext")

    # Authorize wallet ownership BEFORE the idempotency store is touched. The
    # idempotency lookup key is (wallet_id, endpoint, key) with wallet_id taken
    # from the request body, so running idem.begin() first let an unauthorized
    # caller (a) be served another wallet's stored signed receipt via the replay
    # short-circuit and (b) plant an orphaned in-progress record in the victim's
    # namespace on denial, permanently poisoning that (wallet, key). Gate here so
    # a caller who does not own wallet_id never reaches the store. The priced
    # decision below re-runs this check with the real cost for the audit record.
    tenant_decision = evaluate_tool_invocation(
        auth=auth,
        wallet_id=wallet_id,
        tool_name=tool_name,
        estimated_cost=None,
        request_id=request_id,
    )
    if not tenant_decision.allowed:
        await _audit_mcp_invocation(
            effects_committed=False,
            decision=tenant_decision,
            endpoint=endpoint,
            transport=transport,
            ok=False,
            error=tenant_decision.reason,
        )
        raise ToolPermissionDenied(tenant_decision.reason)

    governed_call = bool(permit_id) or (
        settings.TRUST_MODE_ENABLED and not settings.ALLOW_LEGACY_UNPERMITTED_MCP
    )
    idem = get_idempotency_service()
    idempotency_wait_seconds = max(
        1.0,
        settings.MCP_UPSTREAM_CONNECT_TIMEOUT_SECONDS
        + settings.MCP_UPSTREAM_CALL_TIMEOUT_SECONDS
        + 5.0,
    )
    # Idempotency describes the logical invocation, not transport framing.
    # In particular, JSON-RPC correlation IDs and REST-vs-JSON-RPC routing
    # must not create a second gateway dispatch for the same caller key.
    effective_request_payload = {
        "tool_name": tool_name,
        "arguments": arguments,
        "wallet_id": wallet_id,
        "permit_id": permit_id,
    }
    idempotency_endpoint = (
        GOVERNED_MCP_IDEMPOTENCY_ENDPOINT if governed_call else endpoint
    )
    effective_request_hash = sha256_hex(effective_request_payload)
    replay = None
    idem_started = False
    idem_begin: IdempotencyBegin | None = None

    _ensure_local_mcp_tools_registered()
    registry = get_service_registry()
    service = registry.get_local(tool_name)
    if not service:
        service = await registry.get_persistent(tool_name)
    action_identity = None
    action_binding = registry.get_action_binding(service) if service else None
    action_model = None
    if permit_id:
        async with get_session_factory()() as session:
            action_model = await session.get(PermitModel, permit_id)
    is_action = action_model is not None and any(
        getattr(action_model, field) is not None
        for field in (
            "action_contract_version",
            "action_payload_hash",
            "action_schema_id",
            "action_schema_version",
            "action_public_tool_id",
            "action_upstream_binding_hash",
        )
    )
    if action_binding is not None or is_action:
        if action_binding is None:
            raise ToolPermissionDenied("action_tool_binding_required")
        if not is_action or action_model is None:
            raise ToolPermissionDenied("action_permit_required")
        if quote_id:
            raise ToolPermissionDenied("action_quote_unsupported")
        validation = await validate_action_request(
            action_model, action_binding, wallet_id, auth.key_id, arguments, "replay"
        )
        if not validation.allowed:
            raise ToolPermissionDenied(validation.reason or "action_permit_denied")
        action_identity = action_execution_identity(action_model, action_binding)
        # Forward the same materialized defaults that were signed by the issuer.
        arguments = json.loads(
            canonical_action_payload(action_binding, wallet_id, arguments)
        )["arguments"]
        governed_call = True
        idempotency_endpoint = action_identity.endpoint
        idempotency_key = action_identity.idempotency_key
        effective_request_payload = action_identity.request_payload
        effective_request_hash = sha256_hex(effective_request_payload)

    if not service:
        # Durable replay is authoritative even if an operator has since
        # removed the executable registration. This is essential for terminal
        # evidence recovery and avoids turning a completed invocation into a
        # fresh "not found" response after restart or reconfiguration.
        if governed_call and idempotency_key:
            try:
                idem_begin = await _begin_governed_mcp_idempotency(
                    idem=idem,
                    wallet_id=wallet_id,
                    idempotency_key=idempotency_key,
                    tool_name=tool_name,
                    endpoint=endpoint,
                    logical_request_payload=effective_request_payload,
                    legacy_request_payload=request_payload,
                    operation_kind="unresolved",
                )
                replay = idem_begin.replay
                idem_started = True
                if owned_record is not None:
                    owned_record["record_id"] = idem_begin.record_id
            except IdempotencyInProgressError:
                raise
            except IdempotencyConflictError as exc:
                raise ValueError(str(exc)) from exc
            if replay and replay.response_json:
                if permit_id:
                    await _assert_governed_replay_access(
                        permit_id=permit_id,
                        wallet_id=wallet_id,
                        tool_name=tool_name,
                        key_id=auth.key_id,
                    )
                await _raise_replayed_error(replay)
                return replay.response_json
        reason = f"Tool not found: {tool_name}"
        await _complete_governed_denial_idempotency(
            idem=idem,
            idem_started=idem_started,
            wallet_id=wallet_id,
            endpoint=idempotency_endpoint,
            idempotency_key=idempotency_key,
            reason=reason,
            status_code=400,
        )
        raise ValueError(reason)

    execution_backend = str(service.get("execution_backend") or "metadata_only")
    func = registry.get_local_func(tool_name)
    upstream_executor = registry.get_executor(tool_name)
    if execution_backend == "local" and func is None:
        reason = f"Tool not executable: {tool_name}"
        await _complete_governed_denial_idempotency(
            idem=idem,
            idem_started=idem_started,
            wallet_id=wallet_id,
            endpoint=idempotency_endpoint,
            idempotency_key=idempotency_key,
            reason=reason,
            status_code=400,
        )
        raise ValueError(reason)
    if execution_backend == "upstream_mcp" and upstream_executor is None:
        reason = f"Tool not executable: {tool_name}"
        await _complete_governed_denial_idempotency(
            idem=idem,
            idem_started=idem_started,
            wallet_id=wallet_id,
            endpoint=idempotency_endpoint,
            idempotency_key=idempotency_key,
            reason=reason,
            status_code=400,
        )
        raise ValueError(reason)
    if execution_backend not in {"local", "upstream_mcp"}:
        reason = f"Tool not executable: {tool_name}"
        await _complete_governed_denial_idempotency(
            idem=idem,
            idem_started=idem_started,
            wallet_id=wallet_id,
            endpoint=idempotency_endpoint,
            idempotency_key=idempotency_key,
            reason=reason,
            status_code=400,
        )
        raise ValueError(reason)

    category = ServiceCategory(
        service.get("category", ServiceCategory.PLATFORM_FEE.value)
    )
    # High-value tools can force the permit path even when legacy
    # unpermitted MCP is otherwise allowed.
    if service.get("require_permit"):
        governed_call = True
    if governed_call and action_identity is None:
        idempotency_endpoint = GOVERNED_MCP_IDEMPOTENCY_ENDPOINT

    registered_cost = _registered_tool_cost(service, category)

    # A quote is a signed commitment to a price. Resolve it before anything
    # downstream reads the cost, so the policy decision, the permit budget
    # check, and the charge all see the number the caller was promised — not
    # the live price it may have drifted from. Nothing is spent here; the
    # single-use consume happens immediately before the charge.
    quoted = None
    if quote_id:
        quoted = await get_quote_service().validate_for_action(
            quote_id=quote_id,
            wallet_id=wallet_id,
            tool_name=tool_name,
        )
        if not quoted.allowed or quoted.quote is None:
            reason = quoted.reason or "quote_invalid"
            await _audit_mcp_invocation(
                effects_committed=False,
                decision=tenant_decision,
                endpoint=endpoint,
                transport=transport,
                ok=False,
                error=reason,
                extra_metadata={
                    "quote_id": quote_id,
                    "permit_id": permit_id,
                    "idempotency_key": idempotency_key,
                    "request_hash": effective_request_hash,
                },
            )
            await _complete_governed_denial_idempotency(
                idem=idem,
                idem_started=idem_started,
                wallet_id=wallet_id,
                endpoint=idempotency_endpoint,
                idempotency_key=idempotency_key,
                reason=reason,
            )
            # Denying beats quietly charging a different number: a price lock
            # that silently reprices is worse than no lock at all.
            raise PermissionError(reason)
        registered_cost = quoted.quote.quoted_credits

    charge_units = _charge_units_for_registered_cost(registered_cost, category)
    estimated_cost = float(registered_cost)

    decision = evaluate_tool_invocation(
        auth=auth,
        wallet_id=wallet_id,
        tool_name=tool_name,
        estimated_cost=estimated_cost,
        request_id=request_id,
    )
    if not decision.allowed:
        await _audit_mcp_invocation(
            effects_committed=False,
            decision=decision,
            endpoint=endpoint,
            transport=transport,
            ok=False,
            error=decision.reason,
        )
        raise PermissionError(decision.reason)

    if governed_call and not permit_id:
        await _audit_mcp_invocation(
            effects_committed=False,
            decision=decision,
            endpoint=endpoint,
            transport=transport,
            ok=False,
            error="permit_required",
            extra_metadata={
                "idempotency_key": idempotency_key,
                "request_hash": effective_request_hash,
            },
        )
        await _complete_governed_denial_idempotency(
            idem=idem,
            idem_started=idem_started,
            wallet_id=wallet_id,
            endpoint=idempotency_endpoint,
            idempotency_key=idempotency_key,
            reason="permit_required",
        )
        raise PermissionError("permit_required")
    if governed_call and not idempotency_key:
        await _audit_mcp_invocation(
            effects_committed=False,
            decision=decision,
            endpoint=endpoint,
            transport=transport,
            ok=False,
            error="idempotency_key_required",
            extra_metadata={
                "permit_id": permit_id,
                "request_hash": effective_request_hash,
            },
        )
        raise ValueError("idempotency_key_required")

    if action_identity is not None:
        existing = await idem.get_action_record(
            wallet_id=wallet_id, internal_key=action_identity.idempotency_key
        )
        await _assert_action_current_access(
            permit_id=permit_id or "",
            wallet_id=wallet_id,
            auth=auth,
            tool_name=tool_name,
            arguments=arguments,
            money=money,
            estimated_cost=Decimal("0") if existing else registered_cost,
            phase="replay" if existing else "admission",
        )
        if existing is None:
            admission = await get_permit_service().validate_for_action(
                permit_id=permit_id or "",
                wallet_id=wallet_id,
                tool_name=tool_name,
                estimated_credits=registered_cost,
                key_id=auth.key_id,
                arguments=arguments,
            )
            if not admission.allowed:
                # Another valid caller can finish between lookup and admission.
                # Its used budget/call slot must not prevent adoption of its owner.
                raced_owner = await idem.get_action_record(
                    wallet_id=wallet_id, internal_key=action_identity.idempotency_key
                )
                if raced_owner is None or admission.reason not in {
                    "permit_budget_exceeded",
                    "permit_max_calls_exceeded",
                    "permit_aggregate_value_cap_exceeded",
                }:
                    raise ToolPermissionDenied(
                        admission.reason or "action_permit_denied"
                    )

    if governed_call and idempotency_key and not idem_started:
        try:
            if action_identity is not None:
                idem_begin = await idem.begin_action_with_record(
                    wallet_id=wallet_id,
                    identity=action_identity,
                    wait_timeout_seconds=idempotency_wait_seconds,
                )
            else:
                idem_begin = await _begin_governed_mcp_idempotency(
                    idem=idem,
                    wallet_id=wallet_id,
                    idempotency_key=idempotency_key,
                    tool_name=tool_name,
                    endpoint=endpoint,
                    logical_request_payload=effective_request_payload,
                    legacy_request_payload=request_payload,
                    operation_kind=execution_backend,
                    wait_timeout_seconds=(
                        idempotency_wait_seconds
                        if execution_backend == "upstream_mcp"
                        else 0.0
                    ),
                )
            replay = idem_begin.replay
            idem_started = True
            if owned_record is not None:
                owned_record["record_id"] = idem_begin.record_id
                if action_identity is not None:
                    owned_record["endpoint"] = action_identity.endpoint
                    owned_record["key"] = action_identity.idempotency_key
        except (IdempotencyConflictError, IdempotencyInProgressError) as exc:
            await _audit_mcp_invocation(
                effects_committed=False,
                decision=decision,
                endpoint=endpoint,
                transport=transport,
                ok=False,
                error=str(exc),
                extra_metadata={
                    "permit_id": permit_id,
                    "idempotency_key": idempotency_key,
                    "request_hash": effective_request_hash,
                },
            )
            if isinstance(exc, IdempotencyInProgressError):
                raise
            raise ValueError(str(exc))
        if replay and replay.response_json:
            if action_identity is not None:
                # A waiter may observe completion after authority was revoked.
                # This denial must never finalize or overwrite the accepted owner.
                await _assert_action_current_access(
                    permit_id=permit_id or "",
                    wallet_id=wallet_id,
                    auth=auth,
                    tool_name=tool_name,
                    arguments=arguments,
                    money=money,
                    estimated_cost=Decimal("0"),
                    phase="replay",
                )
            await _assert_governed_replay_access(
                permit_id=permit_id,
                wallet_id=wallet_id,
                tool_name=tool_name,
                key_id=auth.key_id,
            )
            await _raise_replayed_error(replay)
            return replay.response_json

    permit_model = None
    if governed_call:
        permit_validation = await get_permit_service().validate_for_action(
            permit_id=permit_id or "",
            wallet_id=wallet_id,
            tool_name=tool_name,
            estimated_credits=registered_cost,
            key_id=auth.key_id,
            arguments=arguments,
        )
        permit_model = permit_validation.permit
        if not permit_validation.allowed:
            audit_event = await _audit_mcp_invocation(
                effects_committed=False,
                decision=decision,
                endpoint=endpoint,
                transport=transport,
                ok=False,
                error=permit_validation.reason,
                extra_metadata={
                    "permit_id": permit_id,
                    "idempotency_key": idempotency_key,
                    "request_hash": effective_request_hash,
                },
            )
            receipt_payload = None
            reason = permit_validation.reason or "permit_denied"
            if permit_model:
                receipt_payload = await _finalize_governed_denial(
                    unaccepted_action_owner_id=idem_begin.record_id
                    if idem_begin
                    else None,
                    idem=idem,
                    effects_committed=False,
                    approval_consumed=False,
                    permit_model=permit_model,
                    wallet_id=wallet_id,
                    key_id=auth.key_id,
                    endpoint=idempotency_endpoint,
                    idempotency_key=idempotency_key,
                    tool_name=tool_name,
                    request_payload=effective_request_payload,
                    arguments=arguments,
                    registered_cost=registered_cost,
                    audit_event_id=audit_event.event_id,
                    reason=reason,
                    reason_code=reason,
                    outcome="denied",
                    status_code=403,
                )
            elif permit_validation.reason:
                await _complete_governed_denial_idempotency(
                    unaccepted_action_owner_id=idem_begin.record_id
                    if idem_begin
                    else None,
                    idem=idem,
                    idem_started=idem_started,
                    wallet_id=wallet_id,
                    endpoint=idempotency_endpoint,
                    idempotency_key=idempotency_key,
                    reason=permit_validation.reason,
                )
            raise ToolPermissionDenied(
                reason,
                receipt=receipt_payload,
                details=permit_validation.details,
            )

    # --- Enterprise IGA gate (app/core/oidc_iga) ---------------------------
    # When the call rode in with an Authorization bearer whose issuer is a
    # pinned enterprise IdP (Okta / Entra ID), the HUMAN principal behind the
    # agent must hold a group/role that IGA_GROUP_POLICY_MAP grants for this
    # tool. This is the single choke point every governed execution funnels
    # through (JSON-RPC /mcp/messages, REST /mcp/tools/{id}/invoke, and the
    # standard /mcp endpoint all reach _execute_registered_tool via the
    # adapter seam), placed with the other pre-dispatch authorization gates:
    # after permit validation so a denial carries the same signed denied
    # receipt permit/policy denials produce, and before anything is charged
    # or dispatched. With IGA unconfigured, no bearer present, or an internal
    # JWT, auth.enterprise_bearer_token is None and this gate is inert.
    iga_denial_reason: str | None = None
    iga_denial_details: dict[str, Any] | None = None
    # The exact grant an ALLOW consumed a use under, kept so a later
    # pre-dispatch refusal that charges nothing can hand the use back.
    iga_granted_use: (
        tuple[EnterprisePrincipal, str, str, IGAUseReservation | None] | None
    ) = None
    try:
        enterprise_principal = _verified_enterprise_principal(
            auth.enterprise_bearer_token
        )
        if enterprise_principal is not None:
            iga_decision = await enforce_tool_call(enterprise_principal, tool_name)
            if not iga_decision.allowed:
                iga_denial_reason = iga_decision.reason
                iga_denial_details = {
                    "reason_code": iga_decision.reason,
                    **iga_decision.details,
                }
            elif iga_decision.group is not None and iga_decision.policy_id is not None:
                iga_granted_use = (
                    enterprise_principal,
                    iga_decision.group,
                    iga_decision.policy_id,
                    iga_decision.reservation,
                )
                if action_identity is not None and owned_record is not None:
                    owned_record["iga_granted_use"] = iga_granted_use
    except IGAError as exc:
        # Catches verification failures for bearers routed to the IGA layer
        # (bad signature / audience / expiry / kid from a pinned enterprise
        # issuer) and a malformed IGA_GROUP_POLICY_MAP surfaced by
        # enforce_tool_call: fail closed with the reason, never fall through
        # to ungoverned execution. A malformed IGA_TRUSTED_ISSUERS never
        # reaches this handler — is_iga_issuer_token fails closed to the
        # internal-JWT verifier (401 at auth time) and logs the config fault
        # at error level.
        iga_denial_reason = exc.reason
    if iga_denial_reason is not None:
        audit_event = await _audit_mcp_invocation(
            effects_committed=False,
            decision=decision,
            endpoint=endpoint,
            transport=transport,
            ok=False,
            error=iga_denial_reason,
            extra_metadata={
                "permit_id": permit_id,
                "idempotency_key": idempotency_key,
                "request_hash": effective_request_hash,
            },
        )
        receipt_payload = None
        if governed_call and permit_model:
            receipt_payload = await _finalize_governed_denial(
                unaccepted_action_owner_id=idem_begin.record_id if idem_begin else None,
                idem=idem,
                effects_committed=False,
                approval_consumed=False,
                permit_model=permit_model,
                wallet_id=wallet_id,
                key_id=auth.key_id,
                endpoint=idempotency_endpoint,
                idempotency_key=idempotency_key,
                tool_name=tool_name,
                request_payload=effective_request_payload,
                arguments=arguments,
                registered_cost=registered_cost,
                audit_event_id=audit_event.event_id,
                reason=iga_denial_reason,
                reason_code=iga_denial_reason,
                outcome="denied",
                status_code=403,
            )
        else:
            await _complete_governed_denial_idempotency(
                unaccepted_action_owner_id=idem_begin.record_id if idem_begin else None,
                idem=idem,
                idem_started=idem_started,
                wallet_id=wallet_id,
                endpoint=idempotency_endpoint,
                idempotency_key=idempotency_key,
                reason=iga_denial_reason,
            )
        raise ToolPermissionDenied(
            iga_denial_reason,
            receipt=receipt_payload,
            details=iga_denial_details,
        )

    simulation = False
    try:
        from ..core.runtime_mode import SERVICE_NAMES, is_simulation

        # A category outside SERVICE_NAMES has no SIMULATION_MODE_* setting
        # to read, so there is nothing to look up and nothing to warn about.
        # truth_for_category() applies the same rule when it labels such a
        # category "platform" for /mcp/tools.json. These keep the fail-open
        # default set above; a genuine lookup failure for a flagged category
        # still falls through to the warning.
        if category.value in SERVICE_NAMES:
            simulation = is_simulation(category.value)
    except Exception as exc:
        # Default to real-effects (simulation=False) but surface the
        # misconfiguration instead of swallowing it silently.
        logger.warning(
            "runtime_mode_check_failed",
            extra={"category": category.value, "error": str(exc)},
        )
        simulation = False
    policy = await evaluate_wallet_policy(
        wallet_id=wallet_id,
        tool_name=tool_name,
        service_category=category.value,
        estimated_cost=registered_cost,
        daily_spend_used=await money.get_daily_spend(wallet_id),
        simulation=simulation,
        # A permit that routes every invoke through the human-approval gate
        # below satisfies a policy's human_approval_required demand; the
        # policy's other constraints are still enforced.
        approval_gate_active=bool(
            governed_call
            and permit_model is not None
            and permit_model.requires_human_approval
        ),
    )
    policy_metadata = {
        "policy_id": policy.policy_id,
        "evaluated_constraints": policy.evaluated_constraints,
    }
    if not policy.allowed:
        trust_metadata = _trust_metadata(
            permit_id=permit_id,
            idempotency_key=idempotency_key,
            request_payload=effective_request_payload,
            arguments=arguments,
        )
        audit_event = await _audit_mcp_invocation(
            effects_committed=False,
            decision=decision,
            endpoint=endpoint,
            transport=transport,
            ok=False,
            error=policy.reason,
            extra_metadata={**policy_metadata, **trust_metadata},
        )
        if governed_call and permit_model:
            reason = policy.reason or "policy_denied"
            receipt_payload = await _finalize_governed_denial(
                unaccepted_iga_use=iga_granted_use,
                unaccepted_action_owner_id=idem_begin.record_id if idem_begin else None,
                idem=idem,
                effects_committed=False,
                approval_consumed=False,
                permit_model=permit_model,
                wallet_id=wallet_id,
                key_id=auth.key_id,
                endpoint=idempotency_endpoint,
                idempotency_key=idempotency_key,
                tool_name=tool_name,
                request_payload=effective_request_payload,
                arguments=arguments,
                registered_cost=registered_cost,
                audit_event_id=audit_event.event_id,
                reason=reason,
                reason_code=reason,
                outcome="denied",
                status_code=403,
            )
            # A policy denial's actionable constraint is the policy, not the
            # (valid) permit — permit_validation.details is None here. The
            # evaluated constraints describe the caller's own wallet policy,
            # which the wallet holder can already read.
            raise ToolPermissionDenied(
                reason,
                receipt=receipt_payload,
                details=(
                    {
                        "policy_id": policy.policy_id,
                        "reason_code": reason,
                        "evaluated_constraints": policy.evaluated_constraints,
                    }
                    if policy.policy_id
                    else None
                ),
            )
        raise PermissionError(policy.reason)

    jev_escalated = False
    jev_metadata = {}
    jev_settings = get_settings()
    if jev_settings.JEV_RISK_GUARD != DuplicateGuardMode.OFF:
        # Governed terminal replays return above, before policy evaluation;
        # they reuse stored evidence and never ask Jev again.
        jev_event_id = (
            jev_audit_id(wallet_id, idempotency_endpoint, idempotency_key or "")
            if governed_call and idem_begin is not None
            else None
        )
        stored_jev = (
            await load_jev_guard_metadata(jev_event_id, wallet_id)
            if jev_event_id is not None
            else None
        )
        if stored_jev is None:
            evaluated = policy.evaluated_constraints.get("evaluated", [])
            tiers = [constraint.get("risk_tier") for constraint in evaluated]
            tier_order = {"low": 0, "medium": 1, "high": 2}
            policy_risk_tier = (
                max(tiers, key=lambda tier: tier_order[tier])
                if tiers and all(tier in tier_order for tier in tiers)
                else None
            )
            scope = (
                {
                    "scopes": json.loads(permit_model.scopes_json),
                    "allowed_tools": json.loads(permit_model.allowed_tools_json),
                    **_permit_constraints_snapshot(permit_model),
                }
                if permit_model
                else None
            )
            jev = await evaluate_jev_guard(
                tool_name=tool_name,
                tool_description=str(service.get("description") or ""),
                service_category=category.value,
                risk_tier=policy_risk_tier,
                permit_scope=scope,
                requires_human_approval=bool(
                    permit_model and permit_model.requires_human_approval
                ),
                arguments=arguments,
            )
            stored_jev = {
                **asdict(jev),
                "mode": jev_settings.JEV_RISK_GUARD.value,
                "advisory": True,
            }
            if jev_event_id is not None:
                # This checkpoint survives pending approval abandoning its
                # idempotency record, and is recoverable after a worker crash.
                try:
                    await record_audit_event(
                        event="jev.risk_guard",
                        event_id=jev_event_id,
                        wallet_id=wallet_id,
                        tool=tool_name,
                        endpoint=endpoint,
                        auth_source=decision.auth_source,
                        key_id=auth.key_id,
                        policy_decision_id=decision.decision_id,
                        request_id=request_id,
                        ok=True,
                        error=None,
                        metadata={"jev_risk_guard": stored_jev},
                    )
                except (IntegrityError, AuditEventConflictError):
                    winner = await load_jev_guard_metadata(jev_event_id, wallet_id)
                    if winner is None:
                        raise
                    stored_jev = winner
        jev_metadata = {"jev_risk_guard": stored_jev}
        policy_metadata.update(jev_metadata)
        jev_escalated = (
            jev_settings.JEV_RISK_GUARD == DuplicateGuardMode.ENFORCE
            and stored_jev["verdict"] == "escalate"
        )
        if jev_escalated and not (governed_call and permit_model):
            reason = "jev_risk_review_required"
            await _audit_mcp_invocation(
                effects_committed=False,
                decision=decision,
                endpoint=endpoint,
                transport=transport,
                ok=False,
                error=reason,
                extra_metadata=policy_metadata,
            )
            raise PermissionError(reason)

    approval_check = None
    if (
        governed_call
        and permit_model
        and (permit_model.requires_human_approval or jev_escalated)
    ):
        approval_check = await _require_human_approval(
            decision=decision,
            permit_model=permit_model,
            wallet_id=wallet_id,
            key_id=auth.key_id,
            endpoint=endpoint,
            idempotency_endpoint=idempotency_endpoint,
            transport=transport,
            idem=idem,
            idempotency_key=idempotency_key,
            tool_name=tool_name,
            request_payload=effective_request_payload,
            arguments=arguments,
            registered_cost=registered_cost,
            advisory_metadata=jev_metadata,
        )

    # Permit schema v2: recipient_domain constraint for upstream calls
    if (
        governed_call
        and permit_model
        and permit_model.recipient_domain
        and execution_backend == "upstream_mcp"
    ):
        upstream_origin = str(service.get("upstream_origin", ""))
        origin_domain = extract_recipient_identity(upstream_origin)
        if not recipient_binding_matches(
            permit_model.recipient_domain, upstream_origin
        ):
            audit_event = await _audit_mcp_invocation(
                effects_committed=False,
                decision=decision,
                endpoint=endpoint,
                transport=transport,
                ok=False,
                error="permit_recipient_domain_mismatch",
                extra_metadata={
                    **jev_metadata,
                    "permit_id": permit_id,
                    "expected_domain": permit_model.recipient_domain,
                    "actual_domain": origin_domain,
                    "idempotency_key": idempotency_key,
                },
            )
            receipt_payload = await _finalize_governed_denial(
                unaccepted_iga_use=iga_granted_use,
                unaccepted_action_owner_id=idem_begin.record_id if idem_begin else None,
                idem=idem,
                effects_committed=False,
                approval_consumed=approval_check is not None,
                permit_model=permit_model,
                wallet_id=wallet_id,
                key_id=auth.key_id,
                endpoint=idempotency_endpoint,
                idempotency_key=idempotency_key,
                tool_name=tool_name,
                request_payload=effective_request_payload,
                arguments=arguments,
                registered_cost=registered_cost,
                audit_event_id=audit_event.event_id,
                reason="permit_recipient_domain_mismatch",
                reason_code="permit_recipient_domain_mismatch",
                outcome="denied",
                status_code=403,
                approval_id=(approval_check.approval_id if approval_check else None),
            )
            raise ToolPermissionDenied(
                "permit_recipient_domain_mismatch",
                receipt=receipt_payload,
            )

    dispatch_service: McpDispatchAttemptService | None = (
        get_mcp_dispatch_attempt_service()
        if execution_backend == "upstream_mcp"
        else None
    )
    dispatch_attempt = None

    # Linearize authorization as late as possible. For a remote call, reserve
    # permit budget and create the recoverable prepared checkpoint in the same
    # transaction; a durable reservation can therefore never exist without an
    # attempt the reconciler knows how to compensate.
    if governed_call:
        if dispatch_service is not None:
            if idem_begin is None or permit_model is None:
                raise RuntimeError("upstream_governance_context_missing")
            try:
                (
                    permit_validation,
                    dispatch_attempt,
                ) = await dispatch_service.authorize_reserve_and_prepare(
                    idempotency_record_id=idem_begin.record_id,
                    wallet_id=wallet_id,
                    permit_id=permit_model.permit_id,
                    approval_id=(
                        approval_check.approval_id if approval_check else None
                    ),
                    key_id=auth.key_id,
                    public_tool_id=tool_name,
                    upstream_tool_name=str(service["upstream_tool_name"]),
                    upstream_origin=str(service["upstream_origin"]),
                    request_hash=idem_begin.request_hash,
                    credits_authorized=registered_cost,
                    arguments=arguments,
                )
            except DispatchPrepareCommitUncertainError:
                # A durable attempt may already exist or have advanced. Only
                # its owner/reconciler may classify it; this activation must
                # not write a competing receipt or complete idempotency.
                raise IdempotencyInProgressError("idempotency_in_progress") from None
            except PermitWriteContendedError:
                # The reservation's counter predicate lost to a concurrent
                # move and its transaction rolled back: no attempt, no
                # reservation, nothing to compensate. This is not a prepare
                # failure to complete against the key (that would replay a
                # transient loss forever for a call that is in budget and
                # under its cap). It reaches the governed wrapper, which
                # releases the in-progress record and answers -32005 so the
                # caller retries the same key.
                raise
            except Exception as exc:
                if action_identity is not None and not isinstance(
                    exc, DispatchPrepareRolledBackError
                ):
                    # Unknown preparation failures cannot prove non-acceptance.
                    raise IdempotencyInProgressError("idempotency_in_progress") from exc
                reason = "upstream_prepare_failed"
                audit_event = await _audit_mcp_invocation(
                    effects_committed=False,
                    decision=decision,
                    endpoint=endpoint,
                    transport=transport,
                    ok=False,
                    error=reason,
                    extra_metadata={
                        **policy_metadata,
                        **_trust_metadata(
                            permit_id=permit_id,
                            idempotency_key=idempotency_key,
                            request_payload=effective_request_payload,
                            arguments=arguments,
                        ),
                        **_approval_metadata(approval_check),
                    },
                )
                receipt_payload = await _finalize_governed_denial(
                    unaccepted_iga_use=iga_granted_use,
                    unaccepted_action_owner_id=idem_begin.record_id,
                    idem=idem,
                    effects_committed=False,
                    approval_consumed=approval_check is not None,
                    permit_model=permit_model,
                    wallet_id=wallet_id,
                    key_id=auth.key_id,
                    endpoint=idempotency_endpoint,
                    idempotency_key=idempotency_key,
                    tool_name=tool_name,
                    request_payload=effective_request_payload,
                    arguments=arguments,
                    registered_cost=registered_cost,
                    audit_event_id=audit_event.event_id,
                    reason=reason,
                    reason_code=reason,
                    outcome="failed_refunded",
                    status_code=502,
                    idempotency_record_id=idem_begin.record_id,
                    approval_id=(
                        approval_check.approval_id if approval_check else None
                    ),
                )
                raise GovernedToolError(
                    reason,
                    receipt=receipt_payload,
                    status_code=502,
                    jsonrpc_code=-32006,
                ) from exc
        else:
            permit_validation = await get_permit_service().authorize_and_reserve(
                permit_id=permit_id or "",
                wallet_id=wallet_id,
                tool_name=tool_name,
                estimated_credits=registered_cost,
                key_id=auth.key_id,
                arguments=arguments,
            )
        permit_model = permit_validation.permit
        if not permit_validation.allowed:
            audit_event = await _audit_mcp_invocation(
                effects_committed=False,
                decision=decision,
                endpoint=endpoint,
                transport=transport,
                ok=False,
                error=permit_validation.reason,
                extra_metadata={
                    "permit_id": permit_id,
                    "idempotency_key": idempotency_key,
                    "request_hash": effective_request_hash,
                    **(
                        {"denial_details": permit_validation.details}
                        if permit_validation.details
                        else {}
                    ),
                    **_approval_metadata(approval_check),
                    **jev_metadata,
                },
            )
            receipt_payload = None
            reason = permit_validation.reason or "permit_denied"
            if permit_model:
                receipt_payload = await _finalize_governed_denial(
                    unaccepted_iga_use=iga_granted_use,
                    unaccepted_action_owner_id=idem_begin.record_id
                    if idem_begin
                    else None,
                    idem=idem,
                    effects_committed=False,
                    approval_consumed=approval_check is not None,
                    permit_model=permit_model,
                    wallet_id=wallet_id,
                    key_id=auth.key_id,
                    endpoint=idempotency_endpoint,
                    idempotency_key=idempotency_key,
                    tool_name=tool_name,
                    request_payload=effective_request_payload,
                    arguments=arguments,
                    registered_cost=registered_cost,
                    audit_event_id=audit_event.event_id,
                    reason=reason,
                    reason_code=reason,
                    outcome="denied",
                    status_code=403,
                    approval_id=(
                        approval_check.approval_id if approval_check else None
                    ),
                )
            elif permit_validation.reason:
                await _complete_governed_denial_idempotency(
                    unaccepted_iga_use=iga_granted_use,
                    tool_name=tool_name,
                    unaccepted_action_owner_id=idem_begin.record_id
                    if idem_begin
                    else None,
                    idem=idem,
                    idem_started=idem_started,
                    wallet_id=wallet_id,
                    endpoint=idempotency_endpoint,
                    idempotency_key=idempotency_key,
                    reason=permit_validation.reason,
                )
            raise ToolPermissionDenied(
                reason,
                receipt=receipt_payload,
                details=permit_validation.details,
            )

    description = f"MCP {transport} invoke {tool_name}"
    if quoted is not None and quote_id:
        # Single use, checked atomically against the window. Losing here means
        # a concurrent invoke spent the quote, or it expired between the read
        # above and now — either way this call has no locked price to stand on.
        if not await get_quote_service().consume(
            quote_id, idempotency_key=idempotency_key
        ):
            reason = QUOTE_REASON_CONSUMED
            if dispatch_service is not None and dispatch_attempt is not None:
                if action_identity is not None:
                    raise IdempotencyInProgressError("idempotency_in_progress")
                try:
                    await dispatch_service.abandon_effect_free_prepared_attempt(
                        attempt_id=dispatch_attempt.attempt_id,
                        expected_updated_at=dispatch_attempt.updated_at,
                    )
                except DispatchAttemptConflictError:
                    # Cleanup cannot prove the attempt remained effect-free. Any
                    # dispatch conflict (claim unavailable, terminal conflict,
                    # commit-uncertain, ...) means a durable owner may have
                    # advanced it, so do not publish the legacy denial over it;
                    # reconciliation now owns classification.
                    raise IdempotencyInProgressError(
                        "idempotency_in_progress"
                    ) from None
                dispatch_attempt = None
            elif governed_call and permit_model:
                # The quote was lost before anything ran: the reservation taken
                # just above is entirely unspent, so both halves of it go back.
                await _release_local_permit_reservation(
                    permit_model,
                    registered_cost,
                    tool_name,
                    reason=reason,
                )
            # Reached only once the refusal is proven to have dispatched
            # nothing -- remotely because the abandon above succeeded, locally
            # because no durable owner exists at all. Releasing the enterprise
            # use any earlier would hand its max_uses and velocity budget back
            # for an invocation another owner may still go on to send, letting
            # that invocation escape the cap entirely. The conflict path above
            # keeps the use on purpose: cleanup could not prove the attempt
            # effect-free, so leaving the budget spent is the safe answer.
            await _release_iga_use(iga_granted_use, tool_name, reason=reason)
            await _audit_mcp_invocation(
                effects_committed=False,
                decision=decision,
                endpoint=endpoint,
                transport=transport,
                ok=False,
                error=reason,
                extra_metadata={
                    **jev_metadata,
                    "quote_id": quote_id,
                    "permit_id": permit_id,
                    "idempotency_key": idempotency_key,
                    "request_hash": effective_request_hash,
                },
            )
            await _complete_governed_denial_idempotency(
                idem=idem,
                idem_started=idem_started,
                wallet_id=wallet_id,
                endpoint=idempotency_endpoint,
                idempotency_key=idempotency_key,
                reason=reason,
            )
            raise PermissionError(reason)

    async def _repair_pre_dispatch(exc: BaseException) -> Any:
        """Finalize a remote checkpoint that failed before dispatch.

        Only valid when a prepared attempt exists. Reconciliation is the sole
        owner of an attempt-keyed reservation: it drives the attempt terminal
        and gives the budget back through ``release_dispatch_budget_once``.
        Never release that reservation by hand alongside this -- the stale
        sweep would finalize the attempt later and release it a second time.
        """
        assert dispatch_attempt is not None
        assert idempotency_key
        try:
            await get_mcp_dispatch_reconciliation_service().reconcile_attempt(
                dispatch_attempt.attempt_id,
                prepared_error_code="upstream_pre_dispatch_failed",
            )
            replayed = await idem.begin_with_record(
                wallet_id=wallet_id,
                endpoint=idempotency_endpoint,
                idempotency_key=idempotency_key,
                request_payload=effective_request_payload,
                operation_kind="upstream_mcp",
            )
        except Exception:
            logger.exception(
                "mcp_upstream_pre_dispatch_reconciliation_failed",
                extra={"dispatch_attempt_id": dispatch_attempt.attempt_id},
            )
            raise exc
        if replayed.replay is not None and replayed.replay.response_json is not None:
            await _raise_replayed_error(replayed.replay)
            return replayed.replay.response_json
        raise exc

    try:
        (
            charge_result,
            credits_charged,
            dispatch_attempt,
        ) = await _charge_and_checkpoint(
            money=money,
            idem=idem,
            dispatch_service=dispatch_service,
            dispatch_attempt=dispatch_attempt,
            governed_call=governed_call,
            wallet_id=wallet_id,
            category=category,
            charge_units=charge_units,
            request_path=endpoint,
            description=description,
            idempotency_endpoint=idempotency_endpoint,
            idempotency_key=idempotency_key,
            idempotency_record_id=(
                idem_begin.record_id if governed_call and idem_begin else None
            ),
            registered_cost=registered_cost,
            tool_name=tool_name,
        )
    except LedgerOperationConflictError:
        # A durable owner (a concurrent activation, or the reconciler) advanced
        # this operation while we were working, so this activation may not
        # classify its outcome. That is the same fact DispatchAttemptConflictError
        # carries above, and it earns the same retryable envelope instead of the
        # unclassified catch-all. Restarting the debit transaction can surface
        # this more often -- an attempt may advance during a backoff -- so the
        # mapping belongs with the retry, not after it.
        raise IdempotencyInProgressError("idempotency_in_progress") from None
    except LedgerWriteContendedError:
        if action_identity is not None and dispatch_attempt is not None:
            # Accepted action authority never reopens. Keep preparation and its
            # reservation for action-aware terminal reconciliation.
            raise IdempotencyInProgressError("idempotency_in_progress") from None
        # Every attempt at the debit rolled back whole, so no money moved and
        # nothing was dispatched. What IS committed is the permit reservation
        # and the in-progress idempotency record, and neither heals itself: the
        # reaper deliberately leaves a local record with no debit "exactly as
        # found" (app/services/idempotency.py), and a reservation is reclaimed
        # only when the permit expires or is revoked. Left alone, a caller that
        # follows the documented advice and retries its key would get
        # idempotency_in_progress forever, against a reservation it can never
        # spend. Hand both back, on either backend, so that the retryable answer
        # this raises is one the caller can actually act on.
        if dispatch_service is not None and dispatch_attempt is not None:
            # Remote keeps its reservation on the prepared attempt, and that row
            # also holds a foreign key to the idempotency record, so neither can
            # be unwound by hand from here. This is the primitive that fits:
            # it re-proves the attempt never claimed, never dispatched and never
            # charged -- exactly what this exception guarantees -- then deletes
            # it and returns the reservation in a single transaction, so no
            # later sweep can release the same reservation twice. With the row
            # gone the foreign key is gone, and the key frees like a local one.
            #
            # Reconciling the attempt instead would be safe for the money but
            # wrong for the caller: it publishes a terminal outcome, so a retry
            # of this key could never run, and momentary contention would become
            # a permanent verdict on the remote path while the local path
            # answered the same fault as retryable.
            try:
                await dispatch_service.abandon_effect_free_prepared_attempt(
                    attempt_id=dispatch_attempt.attempt_id,
                    expected_updated_at=dispatch_attempt.updated_at,
                )
            except DispatchAttemptConflictError:
                # Same reasoning as the lost quote above: any dispatch conflict
                # means a durable owner may have advanced the attempt, so
                # cleanup cannot prove it stayed effect-free and reconciliation
                # owns the classification from here.
                raise IdempotencyInProgressError("idempotency_in_progress") from None
            dispatch_attempt = None
        # Local: nothing durable owns this reservation, so unwind it directly.
        # Each compensation is guarded on its own: a failure to unwind must not
        # replace the contention error with a second, less informative one.
        elif governed_call and permit_model is not None:
            # Nothing ran and nothing was charged, and this path's whole answer
            # is "retry this key" -- so a use left consumed here would deny the
            # very retry being advised.
            await _release_local_permit_reservation(
                permit_model,
                registered_cost,
                tool_name,
                reason="ledger_write_contended",
            )
        # Reached only once the refusal is proven to have dispatched nothing:
        # remotely the abandon above re-proved it, locally no durable owner
        # exists. Releasing the enterprise use any earlier would give its
        # max_uses and velocity budget back for an invocation another owner may
        # still send, letting that call escape the cap. The conflict path above
        # keeps the use deliberately -- cleanup could not prove the attempt
        # effect-free, so the budget stays spent.
        await _release_iga_use(
            iga_granted_use, tool_name, reason="ledger_write_contended"
        )
        try:
            await _audit_mcp_invocation(
                effects_committed=False,
                decision=decision,
                endpoint=endpoint,
                transport=transport,
                ok=False,
                error="ledger_write_contended",
                dispatch_attempt=dispatch_attempt,
                extra_metadata=policy_metadata,
            )
        except Exception:
            logger.exception("mcp_contended_audit_failed")
        if governed_call and idem_begin is not None and idempotency_key:
            try:
                # abandon() refuses to delete a record carrying a response or a
                # ledger entry, so this can never erase evidence that money
                # moved. Here neither exists, by construction.
                await idem.abandon(
                    wallet_id=wallet_id,
                    endpoint=idempotency_endpoint,
                    idempotency_key=idempotency_key,
                    expected_record_id=idem_begin.record_id,
                )
            except Exception:
                logger.exception(
                    "mcp_contended_idempotency_abandon_failed",
                    extra={"wallet_id": wallet_id},
                )
        raise
    except Exception as exc:
        if dispatch_attempt is None or idem_begin is None or not idempotency_key:
            raise
        return await _repair_pre_dispatch(exc)
    if isinstance(charge_result, InsufficientFundsResponse):
        # The quote was consumed just above but no credits moved. Hand the
        # commitment back so a wallet top-up inside the window can still use
        # the price it was promised.
        if quoted is not None and quote_id:
            await get_quote_service().release(quote_id)
        denial_reason = charge_result.error
        denial_status = 402 if denial_reason == "insufficient_funds" else 403
        if dispatch_service is not None and dispatch_attempt is not None:
            try:
                dispatch_attempt = await dispatch_service.complete_pre_dispatch_failure(
                    attempt_id=dispatch_attempt.attempt_id,
                    expected_updated_at=dispatch_attempt.updated_at,
                    result_payload={"error": denial_reason},
                    error_code=denial_reason,
                    max_result_bytes=settings.MCP_UPSTREAM_MAX_RESPONSE_BYTES,
                )
            except DispatchAttemptConflictError:
                # Any dispatch conflict here means the durable row may have been
                # advanced by its owner/reconciler; surface the retryable
                # in-progress envelope instead of a generic internal error.
                raise IdempotencyInProgressError("idempotency_in_progress") from None
            # The attempt is now terminal and was never sent, and that is the
            # whole proof the enterprise release needs -- so it runs here, not
            # after the budget cleanup below. That cleanup is independently
            # fallible (retry exhaustion, a permit or attempt row gone missing)
            # and reconciliation answers for its reservation; nothing answers
            # for the process-local use counters, so a use left spent behind a
            # failed budget release would deny a max_uses=1 principal until
            # restart for a call that provably never ran. Releasing any earlier
            # than this -- before the attempt is driven terminal -- would hand
            # the max_uses and velocity budget back for an invocation a
            # conflicting owner may still send, letting that call escape the
            # cap; the conflict path above keeps the use on purpose. The
            # release is best-effort, so it cannot stop the budget going back.
            await _release_iga_use(iga_granted_use, tool_name, reason=denial_reason)
            await get_permit_service().release_dispatch_budget_once(
                dispatch_attempt.attempt_id
            )
        else:
            if governed_call and permit_model:
                # The wallet could not cover the call, so it never ran. This is
                # the denial the caller is expected to act on -- top up and try
                # again -- which makes leaving the per-tool use consumed the
                # most damaging place to do it: on a cap of one, the retry the
                # denial invites is refused for a call that never happened.
                await _release_local_permit_reservation(
                    permit_model,
                    registered_cost,
                    tool_name,
                    reason=denial_reason,
                )
            # Locally no durable owner exists, so the refusal is proven to have
            # dispatched nothing the moment it is raised, and the reservation
            # release above is guarded half by half and never raises.
            await _release_iga_use(iga_granted_use, tool_name, reason=denial_reason)
        audit_event = await _audit_mcp_invocation(
            effects_committed=False,
            decision=decision,
            endpoint=endpoint,
            transport=transport,
            ok=False,
            error=denial_reason,
            dispatch_attempt=dispatch_attempt,
            extra_metadata={
                **policy_metadata,
                **_trust_metadata(
                    permit_id=permit_id,
                    idempotency_key=idempotency_key,
                    request_payload=effective_request_payload,
                    arguments=arguments,
                ),
                **_dispatch_audit_metadata(dispatch_attempt),
                **_approval_metadata(approval_check),
            },
        )
        if governed_call and permit_model:
            receipt_outcome = (
                "failed_refunded"
                if dispatch_attempt is not None
                else (
                    "insufficient_funds"
                    if denial_reason == "insufficient_funds"
                    else "denied"
                )
            )
            receipt_payload = await _finalize_governed_denial(
                idem=idem,
                effects_committed=False,
                approval_consumed=approval_check is not None,
                permit_model=permit_model,
                wallet_id=wallet_id,
                key_id=auth.key_id,
                endpoint=idempotency_endpoint,
                idempotency_key=idempotency_key,
                tool_name=tool_name,
                request_payload=effective_request_payload,
                arguments=arguments,
                registered_cost=registered_cost,
                audit_event_id=audit_event.event_id,
                reason=denial_reason,
                reason_code=denial_reason,
                outcome=receipt_outcome,
                status_code=denial_status,
                idempotency_record_id=(
                    idem_begin.record_id if idem_begin is not None else None
                ),
                dispatch_attempt_id=(
                    dispatch_attempt.attempt_id
                    if dispatch_attempt is not None
                    else None
                ),
                approval_id=(approval_check.approval_id if approval_check else None),
            )
            raise GovernedToolError(
                denial_reason,
                receipt=receipt_payload,
                status_code=denial_status,
                jsonrpc_code=_status_to_jsonrpc_code(
                    denial_status,
                    denial_reason,
                ),
            )
        raise ValueError(denial_reason)

    # money.charge() is only ever invoked here without dry_run=True, so a real
    # (non-simulated) LedgerEntry is the only non-error outcome; this also
    # narrows the type for the ledger_entry_id/entry_id accesses below.
    assert isinstance(charge_result, LedgerEntry), (
        f"Expected LedgerEntry from non-dry-run charge, got {type(charge_result).__name__}"
    )
    assert credits_charged is not None

    if execution_backend == "upstream_mcp":
        assert upstream_executor is not None
        assert dispatch_service is not None
        assert dispatch_attempt is not None
        assert permit_model is not None
        assert idem_begin is not None
        assert idempotency_key is not None
        return await _execute_upstream_after_charge(
            executor=upstream_executor,
            dispatch_service=dispatch_service,
            dispatch_attempt=dispatch_attempt,
            decision=decision,
            money=money,
            idem=idem,
            permit_model=permit_model,
            wallet_id=wallet_id,
            key_id=auth.key_id,
            endpoint=endpoint,
            idempotency_endpoint=idempotency_endpoint,
            transport=transport,
            idempotency_key=idempotency_key,
            idempotency_record_id=idem_begin.record_id,
            native_idempotency_key=(
                action_identity.native_idempotency_key if action_identity else None
            ),
            tool_name=tool_name,
            request_payload=effective_request_payload,
            arguments=arguments,
            registered_cost=registered_cost,
            credits_charged=credits_charged,
            ledger_entry_id=charge_result.entry_id,
            description=description,
            policy_metadata=policy_metadata,
            approval_check=approval_check,
        )

    assert func is not None
    try:
        if inspect.iscoroutinefunction(func):
            result = await func(**arguments)
        else:
            result = func(**arguments)
    except Exception as exc:
        try:
            await money.refund_charge(
                wallet_id=wallet_id,
                charge_entry_id=charge_result.entry_id,
                description=f"Refund {description}",
            )
        except Exception as refund_exc:
            # Two reasons. The diagnostic one carries the refund store's (and,
            # below, the audit store's) own text and goes to the server log
            # and the audit event. The one that travels — error message,
            # replayed envelope, receipt reason — names the tool's error but
            # never infrastructure text, matching the upstream-dispatch shape.
            diagnostic_error = f"refund_failed:{refund_exc}; tool_error:{exc}"
            error = f"refund_failed; tool_error:{exc}"
            logger.error(
                "Failed to refund MCP charge %s after tool error: %s",
                charge_result.entry_id,
                refund_exc,
            )
            audit_event = None
            try:
                audit_event = await _audit_mcp_invocation(
                    effects_committed=True,
                    decision=decision,
                    endpoint=endpoint,
                    transport=transport,
                    ok=False,
                    error=diagnostic_error,
                    extra_metadata={
                        **policy_metadata,
                        **_trust_metadata(
                            permit_id=permit_id,
                            idempotency_key=idempotency_key,
                            request_payload=effective_request_payload,
                            arguments=arguments,
                            ledger_entry_id=charge_result.entry_id,
                        ),
                        "refund_reconciliation_status": "pending",
                        **_approval_metadata(approval_check),
                    },
                )
            except Exception as audit_exc:
                diagnostic_error = f"{diagnostic_error}; audit_failed:{audit_exc}"
                error = f"{error}; audit_failed"
                logger.error(
                    "Failed to audit MCP refund failure for charge %s: %s",
                    charge_result.entry_id,
                    audit_exc,
                )
            if governed_call and permit_model and idempotency_key:
                receipt_payload, reconciliation = await _finalize_unrefunded_failure(
                    permit_model=permit_model,
                    wallet_id=wallet_id,
                    key_id=auth.key_id,
                    endpoint=idempotency_endpoint,
                    idempotency_key=idempotency_key,
                    tool_name=tool_name,
                    request_payload=effective_request_payload,
                    arguments=arguments,
                    registered_cost=registered_cost,
                    credits_charged=credits_charged,
                    audit_event_id=(
                        audit_event.event_id if audit_event is not None else None
                    ),
                    ledger_entry_id=charge_result.entry_id,
                    reason=error,
                    approval_id=(
                        approval_check.approval_id if approval_check else None
                    ),
                )
                try:
                    await record_audit_event(
                        event="mcp.refund_reconciliation.pending",
                        wallet_id=wallet_id,
                        tool=tool_name,
                        endpoint=endpoint,
                        auth_source=decision.auth_source,
                        key_id=decision.key_id,
                        policy_decision_id=decision.decision_id,
                        request_id=decision.request_id,
                        ok=False,
                        error="refund_failed",
                        metadata={
                            "receipt_id": receipt_payload["receipt_id"],
                            "permit_id": permit_model.permit_id,
                            "ledger_entry_id": charge_result.entry_id,
                            "credits": str(credits_charged),
                            "status": "pending",
                            **_approval_metadata(approval_check),
                        },
                    )
                except Exception as audit_exc:
                    logger.error(
                        "Failed to audit pending MCP refund reconciliation for %s: %s",
                        charge_result.entry_id,
                        audit_exc,
                    )
                raise GovernedToolError(
                    error,
                    receipt=receipt_payload,
                    extra_data={"refund_reconciliation": reconciliation},
                    status_code=500,
                    jsonrpc_code=-32603,
                ) from refund_exc
            # Not a ToolExecutionError: that type means compensation is
            # complete, and here the refund itself failed. The diagnostic
            # reason reaches the route's catch-all, which logs it under a
            # correlation id and answers the sanitized internal error.
            raise RuntimeError(diagnostic_error) from refund_exc
        if governed_call and permit_model:
            try:
                await get_permit_service().release_budget(
                    permit_model.permit_id,
                    registered_cost,
                )
            except PermitWriteContendedError:
                # Guarded for the same reason _release_local_permit_reservation
                # guards its own half: compensation runs on a path whose real
                # answer to the caller is the tool's failure, and a release
                # that loses its own writes must not replace that answer with
                # a less informative one.
                #
                # Letting it propagate would be worse here than at the receipt
                # and audit sites, which re-type to TerminalRecordContendedError
                # because their own write already failed. Nothing has failed
                # here yet: the receipt is written further down, by
                # _finalize_governed_denial, so propagating would *manufacture*
                # the "effects committed, no receipt" state that type reports
                # -- destroying the governance artifact for a call that ran in
                # order to report a bookkeeping loss.
                #
                # The refund above already succeeded, so the wallet is whole
                # and no money is indeterminate. What stays wrong is the
                # permit's own spent_credits, left inflated by the reservation
                # this release could not hand back.
                #
                # Narrower than the sibling's bare `except Exception` on
                # purpose: only the contended write is a known, reconcilable
                # loss. Anything else from this call is an unclassified fault
                # and keeps its existing route to internal_error.
                logger.exception(
                    "mcp_release_budget_contended_after_effects",
                    extra={
                        "permit_id": permit_model.permit_id,
                        "tool": tool_name,
                        # The stranded reservation itself, so the drift this
                        # absorb leaves behind is countable straight from the
                        # logs instead of only inferable from the permit row.
                        "credits": str(registered_cost),
                    },
                )
                # Durable, per-permit counterpart to that line: the log records
                # that it happened, the alert records that it is still true.
                # Cannot raise (see record_absorbed_release_drift) -- the
                # receipt this absorb exists to protect is still unwritten
                # below, so an observability write that could fail the request
                # would re-create exactly the loss being prevented.
                await get_permit_service().record_absorbed_release_drift(
                    permit_id=permit_model.permit_id,
                    amount=registered_cost,
                    site="release_budget",
                )
        audit_event = await _audit_mcp_invocation(
            effects_committed=True,
            decision=decision,
            endpoint=endpoint,
            transport=transport,
            ok=False,
            error=str(exc),
            extra_metadata={
                **policy_metadata,
                **_trust_metadata(
                    permit_id=permit_id,
                    idempotency_key=idempotency_key,
                    request_payload=effective_request_payload,
                    arguments=arguments,
                    ledger_entry_id=charge_result.entry_id,
                ),
                **_approval_metadata(approval_check),
            },
        )
        if governed_call and permit_model:
            receipt_payload = await _finalize_governed_denial(
                idem=idem,
                effects_committed=True,
                approval_consumed=approval_check is not None,
                permit_model=permit_model,
                wallet_id=wallet_id,
                key_id=auth.key_id,
                endpoint=idempotency_endpoint,
                idempotency_key=idempotency_key,
                tool_name=tool_name,
                request_payload=effective_request_payload,
                arguments=arguments,
                registered_cost=registered_cost,
                audit_event_id=audit_event.event_id,
                reason=str(exc),
                reason_code="tool_execution_failed",
                outcome="failed_refunded",
                status_code=500,
                ledger_entry_id=charge_result.entry_id,
                approval_id=(approval_check.approval_id if approval_check else None),
            )
            raise GovernedToolError(
                str(exc),
                receipt=receipt_payload,
                status_code=500,
                jsonrpc_code=-32603,
            ) from exc
        raise ToolExecutionError(str(exc)) from exc

    response_payload = {
        "content": [{"type": "text", "text": json.dumps(result, default=str)}],
        "isError": False,
    }

    # Finalization (audit write, receipt, idempotency-complete) is retried as
    # a unit on transient failure -- the wallet was already charged above (and
    # checkpointed via mark_charged for governed calls), so a crash here must
    # not silently report success without ever finishing these writes. Each
    # step only runs once it hasn't already succeeded in an earlier attempt,
    # so a retry can't create a duplicate audit event or receipt for the same
    # tool call. If all attempts are exhausted the original exception
    # propagates (never silently swallowed); reconcile_stuck_records is the
    # repair path for a governed record left stuck after that.
    finalize_attempts = 3
    audit_event = None
    receipt = None
    last_exc: Exception | None = None
    for attempt in range(1, finalize_attempts + 1):
        try:
            if audit_event is None:
                audit_event = await _audit_mcp_invocation(
                    effects_committed=True,
                    decision=decision,
                    endpoint=endpoint,
                    transport=transport,
                    ok=True,
                    error=None,
                    extra_metadata={
                        **policy_metadata,
                        **_trust_metadata(
                            permit_id=permit_id,
                            idempotency_key=idempotency_key,
                            request_payload=effective_request_payload,
                            arguments=arguments,
                            ledger_entry_id=charge_result.entry_id,
                        ),
                        **_approval_metadata(approval_check),
                    },
                )
            if governed_call and permit_model:
                if receipt is None:
                    try:
                        receipt = await get_receipt_service().create_receipt(
                            permit_id=permit_model.permit_id,
                            wallet_id=wallet_id,
                            key_id=auth.key_id,
                            tool=tool_name,
                            request_payload=effective_request_payload,
                            response_payload=response_payload,
                            ledger_entry_id=charge_result.entry_id,
                            credits_authorized=registered_cost,
                            credits_charged=credits_charged,
                            outcome="success",
                            audit_event_id=audit_event.event_id,
                            idempotency_record_id=(
                                idem_begin.record_id if idem_begin is not None else None
                            ),
                            approval_id=(
                                approval_check.approval_id if approval_check else None
                            ),
                            constraints_evaluated=_permit_constraints_snapshot(
                                permit_model
                            ),
                        )
                    except ReceiptWriteContendedError as exc:
                        # Post-charge: the tool ran and the wallet moved, so a
                        # lost receipt insert is neither retryable nor safe to
                        # unwind. See _receipt_contention_after_effects.
                        raise _receipt_contention_after_effects() from exc
                    response_payload["receipt"] = _receipt_response_payload(receipt)
                assert receipt is not None
                await idem.complete(
                    wallet_id=wallet_id,
                    endpoint=idempotency_endpoint,
                    idempotency_key=idempotency_key or "",
                    response_reference=receipt.receipt_id,
                    response_json=response_payload,
                    status_code=200,
                )
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            if attempt == finalize_attempts:
                break
            logger.warning(
                "mcp_finalize_retry attempt=%d/%d ledger_entry_id=%s error=%s",
                attempt,
                finalize_attempts,
                charge_result.entry_id,
                exc,
            )
            await asyncio.sleep(0.05 * attempt)
    if last_exc is not None:
        logger.error(
            "mcp_finalize_failed_after_retries ledger_entry_id=%s wallet_id=%s error=%s",
            charge_result.entry_id,
            wallet_id,
            last_exc,
        )
        # No AuditChainContendedError can reach here: this block's only chain
        # append is the effects_committed=True audit above, which converts its
        # own loss. Guarding it a second time would restate the rule in the one
        # place it is already enforced, and leave the sites that actually
        # needed it -- the upstream helpers, the local refund-success path --
        # still uncovered. That was the original defect.
        raise last_exc
    return response_payload


def _dispatch_audit_metadata(attempt: Any | None) -> dict[str, Any]:
    """Return only operator-safe dispatch identity; never payloads or credentials."""
    if attempt is None:
        return {}
    return {
        "dispatch_attempt_id": attempt.attempt_id,
        "dispatch_state": attempt.state,
        "upstream_tool_name": attempt.upstream_tool_name,
        "upstream_origin": attempt.upstream_origin,
        "dispatch_response_hash": attempt.response_hash,
    }


def _dispatch_response_metadata(attempt: Any) -> dict[str, Any]:
    return {
        "attempt_id": attempt.attempt_id,
        "state": attempt.state,
    }


async def _charge_and_checkpoint(
    *,
    money: AgentMoney,
    idem: Any,
    dispatch_service: McpDispatchAttemptService | None,
    dispatch_attempt: Any | None,
    governed_call: bool,
    wallet_id: str,
    category: ServiceCategory,
    charge_units: Decimal,
    request_path: str,
    description: str,
    idempotency_endpoint: str,
    idempotency_key: str | None,
    idempotency_record_id: str | None,
    registered_cost: Decimal,
    tool_name: str,
) -> tuple[LedgerEntry | InsufficientFundsResponse, Decimal | None, Any | None]:
    """Debit and durably link every pre-dispatch checkpoint.

    Any exception is handled by the caller through targeted reconciliation,
    which inspects the operation-keyed debit even when commit acknowledgement
    was lost. This helper never writes an unsigned terminal idempotency result.
    """
    charge_result = await money.charge(
        wallet_id=wallet_id,
        service_category=category,
        units=charge_units,
        request_path=request_path,
        description=description,
        operation_key=idempotency_record_id if governed_call else None,
    )
    if isinstance(charge_result, InsufficientFundsResponse):
        return charge_result, None, dispatch_attempt
    if not isinstance(charge_result, LedgerEntry):
        raise RuntimeError("governed_charge_result_invalid")

    if governed_call and idempotency_key:
        await idem.mark_charged(
            wallet_id=wallet_id,
            endpoint=idempotency_endpoint,
            idempotency_key=idempotency_key,
            ledger_entry_id=charge_result.entry_id,
        )

    from app.services.governed_metering import aligned_credits_charged

    credits_charged = aligned_credits_charged(
        ledger_amount=charge_result.amount,
        authorized_credits=registered_cost,
        context=f"mcp:{tool_name}",
    )
    if dispatch_service is not None and dispatch_attempt is not None:
        dispatch_attempt = await dispatch_service.attach_charge(
            attempt_id=dispatch_attempt.attempt_id,
            ledger_entry_id=charge_result.entry_id,
            credits_charged=credits_charged,
        )
    return charge_result, credits_charged, dispatch_attempt


async def _execute_upstream_after_charge(
    *,
    executor: Any,
    dispatch_service: McpDispatchAttemptService,
    dispatch_attempt: Any,
    decision: PolicyDecision,
    money: AgentMoney,
    idem: Any,
    permit_model: Any,
    wallet_id: str,
    key_id: str | None,
    endpoint: str,
    idempotency_endpoint: str,
    transport: str,
    idempotency_key: str,
    idempotency_record_id: str,
    tool_name: str,
    request_payload: dict[str, Any],
    arguments: dict[str, Any],
    registered_cost: Decimal,
    credits_charged: Decimal,
    ledger_entry_id: str,
    description: str,
    policy_metadata: dict[str, Any],
    approval_check: Any | None,
    native_idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Dispatch once and durably classify every post-charge remote outcome."""

    async def claim_dispatch() -> None:
        try:
            await dispatch_service.claim_dispatch(dispatch_attempt.attempt_id)
        except DispatchClaimUnavailableError as exc:
            raise UpstreamMcpDispatchClaimUnavailableError() from exc

    async def raise_response_rejected(
        error_code: str,
        *,
        persist_terminal_payload: bool = True,
    ) -> None:
        terminal_payload = {
            "error": "response_rejected",
            "error_code": error_code,
        }
        terminal = await dispatch_service.complete(
            attempt_id=dispatch_attempt.attempt_id,
            state="response_rejected",
            result_payload=(terminal_payload if persist_terminal_payload else None),
            error_code=error_code,
            max_result_bytes=settings.MCP_UPSTREAM_MAX_RESPONSE_BYTES,
        )
        await _raise_charged_upstream_failure(
            reason="response_rejected",
            outcome="response_rejected",
            status_code=502,
            jsonrpc_code=-32006,
            terminal_payload=terminal_payload,
            dispatch_attempt=terminal,
            decision=decision,
            idem=idem,
            permit_model=permit_model,
            wallet_id=wallet_id,
            key_id=key_id,
            endpoint=endpoint,
            idempotency_endpoint=idempotency_endpoint,
            transport=transport,
            idempotency_key=idempotency_key,
            tool_name=tool_name,
            request_payload=request_payload,
            arguments=arguments,
            registered_cost=registered_cost,
            credits_charged=credits_charged,
            ledger_entry_id=ledger_entry_id,
            policy_metadata=policy_metadata,
            approval_check=approval_check,
        )
        raise AssertionError("unreachable")

    def persistence_rejection_code(exc: DispatchResultRejectedError) -> str:
        if isinstance(exc, DispatchResultTooLargeError):
            return "upstream_response_too_large"
        return "upstream_response_invalid"

    try:
        upstream_result = await executor.call_tool(
            arguments,
            invocation_id=idempotency_record_id,
            idempotency_key=native_idempotency_key or idempotency_key,
            before_dispatch=claim_dispatch,
        )
    except UpstreamMcpReturnedError as exc:
        try:
            terminal = await dispatch_service.complete(
                attempt_id=dispatch_attempt.attempt_id,
                state="returned_error",
                result_payload=exc.result.payload,
                error_code="upstream_returned_error",
                max_result_bytes=settings.MCP_UPSTREAM_MAX_RESPONSE_BYTES,
            )
        except DispatchResultRejectedError as persistence_error:
            await raise_response_rejected(
                persistence_rejection_code(persistence_error),
                persist_terminal_payload=False,
            )
            raise AssertionError("unreachable")
        await _raise_refunded_upstream_failure(
            reason="upstream_returned_error",
            terminal_payload=exc.result.payload,
            dispatch_attempt=terminal,
            dispatch_service=dispatch_service,
            decision=decision,
            money=money,
            idem=idem,
            permit_model=permit_model,
            wallet_id=wallet_id,
            key_id=key_id,
            endpoint=endpoint,
            idempotency_endpoint=idempotency_endpoint,
            transport=transport,
            idempotency_key=idempotency_key,
            tool_name=tool_name,
            request_payload=request_payload,
            arguments=arguments,
            registered_cost=registered_cost,
            credits_charged=credits_charged,
            ledger_entry_id=ledger_entry_id,
            description=description,
            policy_metadata=policy_metadata,
            approval_check=approval_check,
        )
        raise AssertionError("unreachable")
    except UpstreamMcpDispatchClaimUnavailableError:
        # Another activation owns this invocation's one-shot send authority.
        # It must finish (or be reconciled) without this loser mutating the
        # shared charge, budget, attempt, receipt, or idempotency record.
        raise IdempotencyInProgressError("idempotency_in_progress") from None
    except UpstreamMcpPreDispatchError as exc:
        terminal_payload = {
            "error": "failed_refunded",
            "error_code": exc.code,
        }
        try:
            terminal = await dispatch_service.complete_pre_dispatch_failure(
                attempt_id=dispatch_attempt.attempt_id,
                expected_updated_at=dispatch_attempt.updated_at,
                ledger_entry_id=ledger_entry_id,
                credits_charged=credits_charged,
                result_payload=terminal_payload,
                error_code=exc.code,
                max_result_bytes=settings.MCP_UPSTREAM_MAX_RESPONSE_BYTES,
            )
        except DispatchAttemptConflictError:
            # Any dispatch conflict here means the durable row may have been
            # advanced by its owner/reconciler; surface the retryable
            # in-progress envelope instead of a generic internal error.
            raise IdempotencyInProgressError("idempotency_in_progress") from None
        await _raise_refunded_upstream_failure(
            reason="upstream_pre_dispatch_failed",
            terminal_payload=terminal_payload,
            dispatch_attempt=terminal,
            dispatch_service=dispatch_service,
            decision=decision,
            money=money,
            idem=idem,
            permit_model=permit_model,
            wallet_id=wallet_id,
            key_id=key_id,
            endpoint=endpoint,
            idempotency_endpoint=idempotency_endpoint,
            transport=transport,
            idempotency_key=idempotency_key,
            tool_name=tool_name,
            request_payload=request_payload,
            arguments=arguments,
            registered_cost=registered_cost,
            credits_charged=credits_charged,
            ledger_entry_id=ledger_entry_id,
            description=description,
            policy_metadata=policy_metadata,
            approval_check=approval_check,
        )
        raise AssertionError("unreachable")
    except UpstreamMcpDeliveryUncertainError:
        terminal_payload = {"error": "delivery_uncertain"}
        terminal = await dispatch_service.complete(
            attempt_id=dispatch_attempt.attempt_id,
            state="delivery_uncertain",
            result_payload=terminal_payload,
            error_code="delivery_uncertain",
            max_result_bytes=settings.MCP_UPSTREAM_MAX_RESPONSE_BYTES,
        )
        logger.warning(
            "mcp_upstream_delivery_uncertain",
            extra={
                "dispatch_attempt_id": terminal.attempt_id,
                "public_tool_id": terminal.public_tool_id,
                "upstream_origin": terminal.upstream_origin,
            },
        )
        await _raise_charged_upstream_failure(
            reason="delivery_uncertain",
            outcome="delivery_uncertain",
            status_code=504,
            jsonrpc_code=-32005,
            terminal_payload=terminal_payload,
            dispatch_attempt=terminal,
            decision=decision,
            idem=idem,
            permit_model=permit_model,
            wallet_id=wallet_id,
            key_id=key_id,
            endpoint=endpoint,
            idempotency_endpoint=idempotency_endpoint,
            transport=transport,
            idempotency_key=idempotency_key,
            tool_name=tool_name,
            request_payload=request_payload,
            arguments=arguments,
            registered_cost=registered_cost,
            credits_charged=credits_charged,
            ledger_entry_id=ledger_entry_id,
            policy_metadata=policy_metadata,
            approval_check=approval_check,
        )
        raise AssertionError("unreachable")
    except UpstreamMcpResponseRejectedError as exc:
        await raise_response_rejected(exc.code)
        raise AssertionError("unreachable")

    try:
        terminal = await dispatch_service.complete(
            attempt_id=dispatch_attempt.attempt_id,
            state="succeeded",
            result_payload=upstream_result.payload,
            error_code=None,
            max_result_bytes=settings.MCP_UPSTREAM_MAX_RESPONSE_BYTES,
        )
    except DispatchResultRejectedError as persistence_error:
        await raise_response_rejected(
            persistence_rejection_code(persistence_error),
            persist_terminal_payload=False,
        )
        raise AssertionError("unreachable")
    audit_event = await _audit_mcp_invocation(
        effects_committed=True,
        decision=decision,
        endpoint=endpoint,
        transport=transport,
        ok=True,
        error=None,
        dispatch_attempt=terminal,
        extra_metadata={
            **policy_metadata,
            **_trust_metadata(
                permit_id=permit_model.permit_id,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
                arguments=arguments,
                ledger_entry_id=ledger_entry_id,
            ),
            **_dispatch_audit_metadata(terminal),
            **_approval_metadata(approval_check),
        },
    )
    try:
        receipt = await get_receipt_service().create_receipt(
            permit_id=permit_model.permit_id,
            wallet_id=wallet_id,
            key_id=key_id,
            tool=tool_name,
            request_payload=request_payload,
            response_payload=upstream_result.payload,
            ledger_entry_id=ledger_entry_id,
            credits_authorized=registered_cost,
            credits_charged=credits_charged,
            outcome="success",
            audit_event_id=audit_event.event_id,
            idempotency_record_id=idempotency_record_id,
            dispatch_attempt_id=terminal.attempt_id,
            response_hash_override=terminal.response_hash,
            approval_id=(approval_check.approval_id if approval_check else None),
            constraints_evaluated=_permit_constraints_snapshot(permit_model),
        )
    except ReceiptWriteContendedError as exc:
        # Post-charge: the tool ran and the wallet moved, so a lost receipt
        # insert is neither retryable nor safe to unwind. See
        # _receipt_contention_after_effects.
        raise _receipt_contention_after_effects() from exc
    response_payload = dict(upstream_result.payload)
    response_payload["receipt"] = _receipt_response_payload(receipt)
    await idem.complete(
        wallet_id=wallet_id,
        endpoint=idempotency_endpoint,
        idempotency_key=idempotency_key,
        response_reference=receipt.receipt_id,
        response_json=response_payload,
        status_code=200,
    )
    return response_payload


async def _raise_refunded_upstream_failure(
    *,
    reason: str,
    terminal_payload: dict[str, Any],
    dispatch_attempt: Any,
    dispatch_service: McpDispatchAttemptService,
    decision: PolicyDecision,
    money: AgentMoney,
    idem: Any,
    permit_model: Any,
    wallet_id: str,
    key_id: str | None,
    endpoint: str,
    idempotency_endpoint: str,
    transport: str,
    idempotency_key: str,
    tool_name: str,
    request_payload: dict[str, Any],
    arguments: dict[str, Any],
    registered_cost: Decimal,
    credits_charged: Decimal,
    ledger_entry_id: str,
    description: str,
    policy_metadata: dict[str, Any],
    approval_check: Any | None,
) -> None:
    """Compensate a confirmed failure and sign its terminal evidence."""
    try:
        await money.refund_charge(
            wallet_id=wallet_id,
            charge_entry_id=ledger_entry_id,
            description=f"Refund {description}",
        )
        await dispatch_service.mark_debit_refunded(
            attempt_id=dispatch_attempt.attempt_id,
            ledger_entry_id=ledger_entry_id,
        )
    except Exception as refund_exc:
        error = f"refund_failed; upstream_error:{reason}"
        audit_event = await _audit_mcp_invocation(
            effects_committed=True,
            decision=decision,
            endpoint=endpoint,
            transport=transport,
            ok=False,
            error=error,
            dispatch_attempt=dispatch_attempt,
            extra_metadata={
                **policy_metadata,
                **_trust_metadata(
                    permit_id=permit_model.permit_id,
                    idempotency_key=idempotency_key,
                    request_payload=request_payload,
                    arguments=arguments,
                    ledger_entry_id=ledger_entry_id,
                ),
                **_dispatch_audit_metadata(dispatch_attempt),
                "refund_reconciliation_status": "pending",
                **_approval_metadata(approval_check),
            },
        )
        receipt_payload, reconciliation = await _finalize_unrefunded_failure(
            permit_model=permit_model,
            wallet_id=wallet_id,
            key_id=key_id,
            endpoint=idempotency_endpoint,
            idempotency_key=idempotency_key,
            tool_name=tool_name,
            request_payload=request_payload,
            arguments=arguments,
            registered_cost=registered_cost,
            credits_charged=credits_charged,
            audit_event_id=audit_event.event_id,
            ledger_entry_id=ledger_entry_id,
            reason=error,
            dispatch_attempt_id=dispatch_attempt.attempt_id,
            response_payload=terminal_payload,
            response_hash_override=dispatch_attempt.response_hash,
            approval_id=(approval_check.approval_id if approval_check else None),
        )
        raise GovernedToolError(
            error,
            receipt=receipt_payload,
            extra_data={"refund_reconciliation": reconciliation},
            status_code=500,
            jsonrpc_code=-32603,
        ) from refund_exc

    try:
        await get_permit_service().release_dispatch_budget_once(
            dispatch_attempt.attempt_id
        )
    except PermitWriteContendedError:
        # The upstream half of the guard above, and the safer of the two: this
        # reservation is attempt-keyed and release_dispatch_budget_once is
        # idempotent on that key, so reconciliation re-runs it from the attempt
        # row (mcp_dispatch_reconciliation) without needing the caller.
        # Escalating instead would skip the audit event and the receipt below
        # for a call that already ran and was refunded.
        logger.exception(
            "mcp_release_dispatch_budget_contended_after_effects",
            extra={
                "attempt_id": dispatch_attempt.attempt_id,
                "permit_id": permit_model.permit_id,
                "credits": str(registered_cost),
            },
        )
        # No billing alert here, unlike the release_budget site. That drift
        # persists until the permit expires and needs somewhere durable to be
        # seen; this one is attempt-keyed and mcp_dispatch_reconciliation
        # re-runs the release from the attempt row within a cleanup pass. An
        # alert for a condition that clears itself in minutes would teach
        # operators to skim past the alert type, costing the site that does
        # need it its signal.
    audit_event = await _audit_mcp_invocation(
        effects_committed=True,
        decision=decision,
        endpoint=endpoint,
        transport=transport,
        ok=False,
        error=reason,
        dispatch_attempt=dispatch_attempt,
        extra_metadata={
            **policy_metadata,
            **_trust_metadata(
                permit_id=permit_model.permit_id,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
                arguments=arguments,
                ledger_entry_id=ledger_entry_id,
            ),
            **_dispatch_audit_metadata(dispatch_attempt),
            **_approval_metadata(approval_check),
        },
    )
    response_extra_data = {
        "upstream_result": terminal_payload,
        "dispatch": _dispatch_response_metadata(dispatch_attempt),
    }
    receipt_payload = await _finalize_governed_denial(
        idem=idem,
        effects_committed=True,
        approval_consumed=approval_check is not None,
        permit_model=permit_model,
        wallet_id=wallet_id,
        key_id=key_id,
        endpoint=idempotency_endpoint,
        idempotency_key=idempotency_key,
        tool_name=tool_name,
        request_payload=request_payload,
        arguments=arguments,
        registered_cost=registered_cost,
        audit_event_id=audit_event.event_id,
        reason=reason,
        reason_code=reason,
        outcome="failed_refunded",
        status_code=502,
        ledger_entry_id=ledger_entry_id,
        idempotency_record_id=dispatch_attempt.idempotency_record_id,
        dispatch_attempt_id=dispatch_attempt.attempt_id,
        response_payload=terminal_payload,
        response_hash_override=dispatch_attempt.response_hash,
        response_extra_data=response_extra_data,
        approval_id=(approval_check.approval_id if approval_check else None),
    )
    raise GovernedToolError(
        reason,
        receipt=receipt_payload,
        extra_data=response_extra_data,
        status_code=502,
        jsonrpc_code=-32006,
    )


async def _raise_charged_upstream_failure(
    *,
    reason: str,
    outcome: str,
    status_code: int,
    jsonrpc_code: int,
    terminal_payload: dict[str, Any],
    dispatch_attempt: Any,
    decision: PolicyDecision,
    idem: Any,
    permit_model: Any,
    wallet_id: str,
    key_id: str | None,
    endpoint: str,
    idempotency_endpoint: str,
    transport: str,
    idempotency_key: str,
    tool_name: str,
    request_payload: dict[str, Any],
    arguments: dict[str, Any],
    registered_cost: Decimal,
    credits_charged: Decimal,
    ledger_entry_id: str,
    policy_metadata: dict[str, Any],
    approval_check: Any | None,
) -> None:
    """Sign an ambiguous/rejected response without refunding or releasing budget."""
    audit_event = await _audit_mcp_invocation(
        effects_committed=True,
        decision=decision,
        endpoint=endpoint,
        transport=transport,
        ok=False,
        error=reason,
        dispatch_attempt=dispatch_attempt,
        extra_metadata={
            **policy_metadata,
            **_trust_metadata(
                permit_id=permit_model.permit_id,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
                arguments=arguments,
                ledger_entry_id=ledger_entry_id,
            ),
            **_dispatch_audit_metadata(dispatch_attempt),
            **_approval_metadata(approval_check),
        },
    )
    try:
        receipt = await get_receipt_service().create_receipt(
            permit_id=permit_model.permit_id,
            wallet_id=wallet_id,
            key_id=key_id,
            tool=tool_name,
            request_payload=request_payload,
            response_payload=(
                terminal_payload if dispatch_attempt.response_hash is not None else None
            ),
            response_hash_override=dispatch_attempt.response_hash,
            ledger_entry_id=ledger_entry_id,
            credits_authorized=registered_cost,
            credits_charged=credits_charged,
            outcome=outcome,
            reason_code=reason,
            audit_event_id=audit_event.event_id,
            idempotency_record_id=dispatch_attempt.idempotency_record_id,
            dispatch_attempt_id=dispatch_attempt.attempt_id,
            approval_id=(approval_check.approval_id if approval_check else None),
        )
    except ReceiptWriteContendedError as exc:
        # Post-charge: the tool ran and the wallet moved, so a lost receipt
        # insert is neither retryable nor safe to unwind. See
        # _receipt_contention_after_effects.
        raise _receipt_contention_after_effects() from exc
    receipt_payload = _receipt_response_payload(receipt)
    response_extra_data = {"dispatch": _dispatch_response_metadata(dispatch_attempt)}
    await idem.complete(
        wallet_id=wallet_id,
        endpoint=idempotency_endpoint,
        idempotency_key=idempotency_key,
        response_reference=receipt.receipt_id,
        response_json=_governed_error_payload(
            reason,
            receipt_payload,
            extra_data=response_extra_data,
        ),
        status_code=status_code,
    )
    raise GovernedToolError(
        reason,
        receipt=receipt_payload,
        extra_data=response_extra_data,
        status_code=status_code,
        jsonrpc_code=jsonrpc_code,
    )


def _permit_constraints_snapshot(permit_model: Any) -> dict[str, Any]:
    """Build a snapshot of permit v2 constraints for receipt signing."""
    return permit_constraints_snapshot(permit_model)


def _registered_tool_cost(
    service: dict[str, Any],
    category: ServiceCategory,
) -> Decimal:
    # Shared with the quote endpoint so a locked quote and the charge that
    # honors it are computed from one definition of price. A price that is
    # not finite and non-negative raises ValueError("tool_price_invalid")
    # here, before idempotency, policy, permit or ledger state is touched; it
    # is deliberately not a classified ValueError, so the caller gets the
    # opaque internal_error and the operator gets the logged traceback.
    return tool_price(service, category)


def _charge_units_for_registered_cost(
    registered_cost: Decimal,
    category: ServiceCategory,
) -> Decimal:
    return charge_units_for(registered_cost, category)


def _receipt_response_payload(receipt: Any) -> dict[str, Any]:
    payload = receipt.model_dump(mode="json")
    charged = payload.get("credits_charged")
    if charged is not None and Decimal(str(charged)) == Decimal("0"):
        payload["credits_charged"] = "0"
    return payload


def _stable_receipt_reason(reason: str) -> str:
    """Collapse dynamic denial detail into a stable signed reason code."""
    if reason.startswith("permit_forbidden_field:"):
        return "permit_forbidden_field"
    return reason


def _governed_error_payload(
    reason: str,
    receipt: dict[str, Any] | None,
    *,
    extra_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "content": [],
        "isError": True,
        "error": reason,
        "receipt": receipt,
    }
    payload.update(extra_data or {})
    return payload


async def _complete_governed_denial_idempotency(
    *,
    idem: Any,
    unaccepted_action_owner_id: str | None = None,
    unaccepted_iga_use: tuple[EnterprisePrincipal, str, str, IGAUseReservation | None]
    | None = None,
    idem_started: bool,
    wallet_id: str,
    endpoint: str,
    idempotency_key: str | None,
    reason: str,
    status_code: int = 403,
    tool_name: str = "",
) -> None:
    if not idem_started or not idempotency_key:
        return
    if endpoint == "/mcp/action/v1" and unaccepted_action_owner_id is not None:
        if not await idem.abandon(
            wallet_id=wallet_id,
            endpoint=endpoint,
            idempotency_key=idempotency_key,
            expected_record_id=unaccepted_action_owner_id,
        ):
            raise IdempotencyInProgressError("idempotency_in_progress")
        await _release_iga_use(unaccepted_iga_use, tool_name, reason=reason)
        return
    await idem.complete(
        wallet_id=wallet_id,
        endpoint=endpoint,
        idempotency_key=idempotency_key,
        response_reference=None,
        response_json=_governed_error_payload(reason, None),
        status_code=status_code,
    )


async def _release_iga_use(
    iga_granted_use: tuple[Any, str, str, IGAUseReservation | None] | None,
    tool_name: str,
    *,
    reason: str,
) -> None:
    """Hand back the enterprise use consumed by an action that never happened.

    ``enforce_tool_call`` records a use atomically with its ALLOW, before any
    of the pre-dispatch gates run. A refusal that charges nothing and
    dispatches nothing must therefore give that use back, or a ``max_uses``
    budget and the velocity window burn down on an action nobody took --
    ``release_tool_use`` names the worst case, a ``max_uses=1`` principal
    locked out forever by a single refusal.

    Best-effort: compensation must never replace the refusal the caller is
    actually being given.
    """
    if iga_granted_use is None:
        return
    try:
        await release_tool_use(
            iga_granted_use[0],
            tool_name,
            group=iga_granted_use[1],
            policy_id=iga_granted_use[2],
            reservation=iga_granted_use[3],
        )
    except Exception:
        logger.exception(
            "iga_use_release_failed",
            extra={"tool": tool_name, "reason": reason},
        )


async def _release_local_permit_reservation(
    permit_model: Any,
    registered_cost: Decimal,
    tool_name: str,
    *,
    reason: str,
) -> None:
    """Give a local governed reservation back in full: the credits and the call.

    ``authorize_and_reserve`` increments the ``max_calls_per_tool`` counter
    atomically with the budget reservation, so handing back only the credits
    leaves the use consumed. A permit capped at one call is then finished, and
    answers its holder's next attempt ``permit_max_calls_exceeded`` for a call
    that never ran, never charged and never dispatched.
    ``PermitService.release_tool_call`` is the documented compensation partner
    and is a no-op on a permit that configures no cap for this tool.

    Local reservations only. The upstream path reserves credits alone --
    ``authorize_reserve_and_prepare`` never touches the counter -- and a permit
    that configures ``max_calls_per_tool`` is refused that backend outright as
    ``permit_constraint_unsupported_for_upstream``, so no remote reservation
    can ever hold a per-tool use to give back. Remote budget goes back through
    ``release_dispatch_budget_once`` instead, which is attempt-keyed.

    Each half is guarded on its own. Compensation runs on paths whose real
    answer to the caller is a denial or a failure, and a release that loses its
    own writes must neither replace that answer with a less informative one nor
    stop the other half from running.
    """
    permits = get_permit_service()
    try:
        await permits.release_budget(permit_model.permit_id, registered_cost)
    except Exception:
        logger.exception(
            "mcp_release_budget_failed",
            extra={"permit_id": permit_model.permit_id, "reason": reason},
        )
    try:
        await permits.release_tool_call(permit_model.permit_id, tool_name)
    except Exception:
        logger.exception(
            "mcp_release_tool_call_failed",
            extra={
                "permit_id": permit_model.permit_id,
                "tool": tool_name,
                "reason": reason,
            },
        )


async def _finalize_governed_denial(
    *,
    idem: Any,
    unaccepted_action_owner_id: str | None = None,
    unaccepted_iga_use: tuple[EnterprisePrincipal, str, str, IGAUseReservation | None]
    | None = None,
    permit_model: Any,
    wallet_id: str,
    key_id: str | None,
    endpoint: str,
    idempotency_key: str | None,
    tool_name: str,
    request_payload: dict[str, Any] | None,
    arguments: dict[str, Any],
    registered_cost: Decimal,
    audit_event_id: str,
    reason: str,
    reason_code: str,
    outcome: str,
    status_code: int,
    effects_committed: bool,
    approval_consumed: bool,
    ledger_entry_id: str | None = None,
    idempotency_record_id: str | None = None,
    dispatch_attempt_id: str | None = None,
    response_payload: dict[str, Any] | None = None,
    response_hash_override: str | None = None,
    response_extra_data: dict[str, Any] | None = None,
    approval_id: str | None = None,
) -> dict[str, Any]:
    """Sign a non-success governed receipt and record the idempotent outcome.

    Shared by every governed terminal-failure branch (permit denied, policy
    denied, insufficient funds, tool execution failure) so the receipt contract
    and idempotency completion are written in exactly one place.

    ``effects_committed`` carries the same fact, and is read the same way, as
    the argument of that name on ``_audit_mcp_invocation``: it says whether the
    call has already run or the wallet has already moved. It exists here
    because the receipt insert can lose a write conflict for its whole budget,
    and what that loss means to the caller depends entirely on the answer.
    Pass the value the branch's own audit call passes.

    ``approval_consumed`` says whether THIS invocation spent the single-use
    human approval named by ``approval_id``. ``approval_id`` alone cannot: a
    rejected, expired or lost-race approval rides on the denial receipt as
    evidence, but ``HumanApprovalService._finalize`` consumes only an approved
    one, so that denial left nothing spent and its key may be retried. It is
    true exactly when ``_require_human_approval`` returned, which it does
    solely for the approval this call consumed.
    """
    detached_action = (
        endpoint == "/mcp/action/v1" and unaccepted_action_owner_id is not None
    )
    if detached_action:
        # Delete only this invocation's still-unprepared owner, under the same
        # lock and accounting invariants used by action cleanup. A concurrent
        # preparation or uncertain checkpoint must keep its authority intact.
        released = await idem.abandon(
            wallet_id=wallet_id,
            endpoint=endpoint,
            idempotency_key=idempotency_key or "",
            expected_record_id=unaccepted_action_owner_id,
        )
        if not released:
            raise IdempotencyInProgressError("idempotency_in_progress")
        # The exact owner is now proven unaccepted. Return only the IGA use
        # consumed by this invocation; prepared/uncertain owners never reach it.
        await _release_iga_use(unaccepted_iga_use, tool_name, reason=reason)
        idempotency_record_id = None
    if not detached_action and idempotency_record_id is None and idempotency_key:
        record = await idem.get_record(
            wallet_id=wallet_id,
            endpoint=endpoint,
            idempotency_key=idempotency_key,
        )
        idempotency_record_id = record.record_id if record is not None else None
    try:
        receipt = await get_receipt_service().create_receipt(
            permit_id=permit_model.permit_id,
            wallet_id=wallet_id,
            key_id=key_id,
            tool=tool_name,
            request_payload=request_payload or arguments,
            response_payload=response_payload or {"error": reason},
            ledger_entry_id=ledger_entry_id,
            credits_authorized=registered_cost,
            credits_charged=Decimal("0"),
            outcome=outcome,
            reason_code=_stable_receipt_reason(reason_code),
            audit_event_id=audit_event_id,
            idempotency_record_id=idempotency_record_id,
            dispatch_attempt_id=dispatch_attempt_id,
            response_hash_override=response_hash_override,
            approval_id=approval_id,
        )
    except ReceiptWriteContendedError as exc:
        if effects_committed:
            raise _receipt_contention_after_effects() from exc
        if dispatch_attempt_id is not None:
            # Remote, and already terminal: complete_pre_dispatch_failure ran
            # before this receipt, so a durable mcp_dispatch_attempts row
            # exists and its NOT NULL idempotency_record_id pins the record.
            # The unwind cannot free what it advertises -- the DELETE raises on
            # the foreign key and is only logged -- so the caller would be told
            # to retry a key that answers idempotency_in_progress until the
            # dispatch reconciler writes the receipt. Name that owner instead:
            # a winner is mid-flight, which is exactly what this error means
            # everywhere else on the dispatch path, and unlike the loss below
            # it resolves on its own.
            raise IdempotencyInProgressError("idempotency_in_progress") from exc
        if approval_consumed:
            # This call spent a single-use human approval before the denial
            # (HumanApprovalService._finalize), and unlike the quote released
            # on the insufficient-funds path it is not compensated. Freeing the
            # key would advertise a retry that cannot reproduce this call: the
            # same key re-reads the same approval and is refused for having
            # consumed it. That spent approval is a committed effect, so this
            # is the after-effects outcome, not a retry the caller cannot make.
            # Keyed on consumption, not on approval_id: a rejected or expired
            # approval is evidence on this receipt too, but nothing was spent,
            # and its denial is exactly the retry the branch below advertises.
            raise _receipt_contention_after_effects() from exc
        # Nothing ran, nothing was charged, and nothing durable pins the key:
        # the receipt service proved no row is durable, and idem.complete()
        # below never ran, so this key holds nothing terminal. Propagated as
        # itself so the retryable handlers can name it and, first, so the
        # wrapper can free the idempotency record this invocation still owns.
        raise
    receipt_payload = _receipt_response_payload(receipt)
    if detached_action:
        return receipt_payload
    await idem.complete(
        wallet_id=wallet_id,
        endpoint=endpoint,
        idempotency_key=idempotency_key or "",
        response_reference=receipt.receipt_id,
        response_json=_governed_error_payload(
            reason,
            receipt_payload,
            extra_data=response_extra_data,
        ),
        status_code=status_code,
    )
    return receipt_payload


async def _finalize_unrefunded_failure(
    *,
    permit_model: Any,
    wallet_id: str,
    key_id: str | None,
    endpoint: str,
    idempotency_key: str,
    tool_name: str,
    request_payload: dict[str, Any] | None,
    arguments: dict[str, Any],
    registered_cost: Decimal,
    credits_charged: Decimal,
    audit_event_id: str | None,
    ledger_entry_id: str,
    reason: str,
    dispatch_attempt_id: str | None = None,
    response_payload: dict[str, Any] | None = None,
    response_hash_override: str | None = None,
    approval_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Persist the signed failure and its exact-once operator work item.

    Both call sites reach here having already charged the caller, run the tool
    and failed the refund, so this is unconditionally a post-effects site --
    unlike ``_finalize_governed_denial``, which serves both sides of the charge
    and has to be told which one it is through ``effects_committed``.

    That is also the whole of the contention handling: ``create_pending`` owns
    one transaction and restarts it itself, and what arrives here is only the
    exhausted case. Re-typing it at this single choke point is what carries the
    answer to every surface -- the two call sites raise through untyped
    ``except Exception`` bodies that do not catch what they raise, the unwind
    in ``_execute_registered_tool`` deliberately does not catch
    ``TerminalRecordContendedError``, and all three transports already answer
    that type. None of those four places needs to know this path exists.
    """
    reconciliation_service = get_refund_reconciliation_service()
    try:
        receipt, reconciliation = await reconciliation_service.create_pending(
            wallet_id=wallet_id,
            endpoint=endpoint,
            idempotency_key=idempotency_key,
            permit_id=permit_model.permit_id,
            key_id=key_id,
            tool_name=tool_name,
            request_payload=request_payload or arguments,
            ledger_entry_id=ledger_entry_id,
            credits_authorized=registered_cost,
            credits_charged=credits_charged,
            audit_event_id=audit_event_id,
            reason=reason,
            dispatch_attempt_id=dispatch_attempt_id,
            response_payload=response_payload,
            response_hash_override=response_hash_override,
            approval_id=approval_id,
        )
    except RefundReconciliationContendedError as exc:
        # The receipt and the operator work item were lost together, so nobody
        # downstream learns that money is owed. That is strictly worse than the
        # losses at the other post-effects sites, and it still may not be
        # called retryable: the charge stands and the tool already ran.
        raise _receipt_contention_after_effects() from exc
    receipt_payload = _receipt_response_payload(receipt)
    return receipt_payload, reconciliation


async def _assert_governed_replay_access(
    *,
    permit_id: str | None,
    wallet_id: str,
    tool_name: str,
    key_id: str | None,
) -> None:
    """Keep terminal replay bound to the permit's stable caller identity."""
    validation = await get_permit_service().validate_replay_access(
        permit_id=permit_id or "",
        wallet_id=wallet_id,
        tool_name=tool_name,
        key_id=key_id,
    )
    if not validation.allowed:
        raise ToolPermissionDenied(validation.reason or "permit_denied")


def _approval_metadata(approval_check: Any) -> dict[str, Any]:
    """Audit-metadata fragment for the human-approval gate (signed via chain)."""
    if approval_check is None:
        return {}
    metadata: dict[str, Any] = {
        "approval_id": approval_check.approval_id,
        "approval_status": approval_check.status,
    }
    if approval_check.sentinel_action_id:
        metadata["sentinel_action_id"] = approval_check.sentinel_action_id
    if approval_check.simulated:
        metadata["approval_simulated"] = True
    return metadata


async def _require_human_approval(
    *,
    decision: PolicyDecision,
    permit_model: Any,
    wallet_id: str,
    key_id: str | None,
    endpoint: str,
    idempotency_endpoint: str,
    transport: str,
    idem: Any,
    idempotency_key: str | None,
    tool_name: str,
    request_payload: dict[str, Any] | None,
    arguments: dict[str, Any],
    registered_cost: Decimal,
    advisory_metadata: dict[str, Any] | None = None,
) -> Any:
    """Enforce the permit's human-approval gate before any budget moves.

    Returns the approved ``ApprovalCheck`` or raises:

    - ``HumanApprovalPendingSignal`` (decision pending / Sentinel unreachable):
      retryable — the idempotency record is abandoned so the same key can be
      retried, and no receipt is written because nothing terminal happened.
    - ``ToolPermissionDenied`` (rejected / expired / misconfigured): terminal —
      a denied receipt is signed and the idempotency record completes, so
      replays of this key return the same denial.
    """
    trust_metadata = _trust_metadata(
        permit_id=permit_model.permit_id,
        idempotency_key=idempotency_key,
        request_payload=request_payload,
        arguments=arguments,
    )
    trust_metadata.update(advisory_metadata or {})

    async def _terminal_denial(reason: str, approval_id: str | None) -> NoReturn:
        audit_event = await _audit_mcp_invocation(
            effects_committed=False,
            decision=decision,
            endpoint=endpoint,
            transport=transport,
            ok=False,
            error=reason,
            extra_metadata=trust_metadata,
        )
        receipt_payload = await _finalize_governed_denial(
            idem=idem,
            effects_committed=False,
            approval_consumed=False,
            permit_model=permit_model,
            wallet_id=wallet_id,
            key_id=key_id,
            endpoint=idempotency_endpoint,
            idempotency_key=idempotency_key,
            tool_name=tool_name,
            request_payload=request_payload,
            arguments=arguments,
            registered_cost=registered_cost,
            audit_event_id=audit_event.event_id,
            reason=reason,
            reason_code=reason,
            outcome="denied",
            status_code=403,
            approval_id=approval_id,
        )
        raise ToolPermissionDenied(reason, receipt=receipt_payload)

    async def _retryable(
        reason: str, *, data: dict[str, Any], status_code: int
    ) -> NoReturn:
        await _audit_mcp_invocation(
            effects_committed=False,
            decision=decision,
            endpoint=endpoint,
            transport=transport,
            ok=False,
            error=reason,
            extra_metadata={**trust_metadata, **data},
        )
        # Nothing was charged and no terminal outcome exists: free the key so
        # the same invoke can be retried once the condition clears.
        await idem.abandon(
            wallet_id=wallet_id,
            endpoint=idempotency_endpoint,
            idempotency_key=idempotency_key or "",
        )
        raise HumanApprovalPendingSignal(reason, data=data, status_code=status_code)

    try:
        approval_check = await get_human_approval_service().ensure_approval(
            wallet_id=wallet_id,
            permit_id=permit_model.permit_id,
            tool_name=tool_name,
            idempotency_key=idempotency_key or "",
            arguments=arguments,
            estimated_credits=registered_cost,
        )
    except HumanApprovalUnavailableError as exc:
        await _retryable(exc.reason, data={}, status_code=503)
    except HumanApprovalError as exc:
        await _terminal_denial(exc.reason, None)
    except Exception:
        # Never let an unexpected error strand the caller's in-progress
        # idempotency record (reconcile can't repair an uncharged one). Fail
        # closed and retryable: release the key, execute nothing, surface an
        # outage. Logged loudly so it isn't silently swallowed.
        logger.exception("human_approval_gate_unexpected_error")
        await _retryable("human_approval_unavailable", data={}, status_code=503)

    approval_data = _approval_metadata(approval_check)
    if approval_check.status == APPROVAL_STATUS_PENDING:
        await _retryable(
            "human_approval_pending",
            data={
                **approval_data,
                "expires_at": approval_check.expires_at.isoformat(),
            },
            status_code=202,
        )
    if approval_check.status != APPROVAL_STATUS_APPROVED:
        # rejected or expired
        await _terminal_denial(
            f"human_approval_{approval_check.status}", approval_check.approval_id
        )
    return approval_check


async def _raise_replayed_error(replay: Any) -> None:
    if replay.status_code < 400 or not replay.response_json:
        return
    reason = str(replay.response_json.get("error") or "governed_call_failed")
    receipt = replay.response_json.get("receipt")
    extra_data = {}
    reconciliation = replay.response_json.get("refund_reconciliation")
    if isinstance(reconciliation, dict):
        if reconciliation.get("status") == "resolved":
            try:
                receipt_id = replay.response_reference
                if (
                    not isinstance(receipt_id, str)
                    or reconciliation.get("receipt_id") != receipt_id
                ):
                    raise ValueError("resolved_replay_linkage_invalid")
                validated = (
                    await get_refund_reconciliation_service().validate_resolved_claim(
                        receipt_id=receipt_id
                    )
                )
                if validated.status != "resolved":
                    raise ValueError("resolved_replay_state_invalid")
            except Exception as exc:
                # Do not replay the claimed resolution or its attached receipt
                # when the ledger cannot substantiate it.
                raise GovernedToolError(
                    "refund_reconciliation_resolution_invalid",
                    status_code=409,
                    jsonrpc_code=-32603,
                ) from exc
        extra_data["refund_reconciliation"] = reconciliation
    upstream_result = replay.response_json.get("upstream_result")
    if isinstance(upstream_result, dict):
        extra_data["upstream_result"] = upstream_result
    dispatch = replay.response_json.get("dispatch")
    if isinstance(dispatch, dict):
        extra_data["dispatch"] = dispatch
    if replay.status_code == 403:
        raise ToolPermissionDenied(reason, receipt=receipt)
    raise GovernedToolError(
        reason,
        receipt=receipt,
        extra_data=extra_data,
        status_code=replay.status_code,
        jsonrpc_code=_status_to_jsonrpc_code(replay.status_code, reason),
    )


def _status_to_jsonrpc_code(status_code: int, reason: str) -> int:
    if status_code == 402 or reason == "insufficient_funds":
        return -32004
    if reason.startswith("Tool not found"):
        return -32001
    if reason.startswith("Tool not executable"):
        return -32002
    if status_code == 403:
        return -32003
    if reason == "delivery_uncertain":
        return -32005
    if reason == "response_rejected":
        return -32006
    if reason in {"upstream_returned_error", "upstream_pre_dispatch_failed"}:
        return -32006
    return -32603


def _trust_metadata(
    *,
    permit_id: str | None,
    idempotency_key: str | None,
    request_payload: dict[str, Any] | None,
    arguments: dict[str, Any],
    ledger_entry_id: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "request_hash": sha256_hex(request_payload or arguments),
    }
    if permit_id:
        metadata["permit_id"] = permit_id
    if idempotency_key:
        metadata["idempotency_key"] = idempotency_key
    if ledger_entry_id:
        metadata["ledger_entry_id"] = ledger_entry_id
    return metadata


def _value_error_jsonrpc_code(message: str) -> int | None:
    """JSON-RPC code for a ``ValueError`` the pipeline raised on purpose.

    Every message here is contract: the Python SDK and existing clients match
    these strings. ``None`` means the text is not one of ours — a library or
    driver error that happens to subclass ``ValueError`` — and the route must
    answer with ``internal_error`` instead of echoing it.
    """
    if message in {"Missing tool name", "Missing wallet_id in mcpContext"}:
        return -32602
    if message.startswith(("Invalid params", "invalid_idempotency_key")):
        return -32602
    if message.startswith("Tool not found"):
        return -32001
    if message.startswith("Tool not executable"):
        return -32002
    if message == "idempotency_key_required":
        return -32003
    if message == "idempotency_in_progress":
        return -32005
    if message == "insufficient_funds":
        return -32004
    if message in {"wallet_frozen", "wallet_expired"}:
        return -32003
    if message == "idempotency_key_reused":
        # Re-raised from IdempotencyConflictError. Stays on -32603 because
        # clients (and the SDK) match the message, not the code.
        return -32603
    return None


def _receipt_contention_after_effects() -> TerminalRecordContendedError:
    """Re-type a receipt-write loss that happened after the call had effects.

    The mirror of ``_audit_mcp_invocation``'s ``effects_committed`` branch, and
    it exists for two reasons rather than one. The retryable handlers must not
    catch it, because "retry this key" would invite a second execution of
    something the caller has already paid for. And the contention unwind in
    ``_execute_registered_tool`` must not catch it either: that unwind frees
    the idempotency record, which is only safe while the call has no effects to
    protect. A charged call with no receipt has no terminal outcome to publish,
    so reconciliation owns the record from here.
    """
    return TerminalRecordContendedError(RECEIPT_WRITE_CONTENDED_AFTER_EFFECTS)


INTERNAL_ERROR_MESSAGE = "internal_error"


def _internal_error(
    exc: BaseException, *, surface: str, request_id: object
) -> dict[str, Any]:
    """Log an unclassified failure and build the client-facing JSON-RPC error.

    The exception text stays on the server. These catch-all branches used to
    return ``str(exc)``, which for a database driver failure is the SQL
    statement itself. The caller gets a fixed message and a correlation id;
    the traceback is logged under that id for the operator.
    """
    correlation_id = uuid.uuid4().hex
    logger.exception(
        "mcp_unclassified_error surface=%s correlation_id=%s request_id=%s",
        surface,
        correlation_id,
        request_id,
        exc_info=exc,
    )
    return {
        "code": -32603,
        "message": INTERNAL_ERROR_MESSAGE,
        "data": {"correlation_id": correlation_id},
    }


def _internal_error_tool_result(
    exc: BaseException, *, surface: str
) -> ToolCallResponse:
    """The legacy HTTP route's shape for the same unclassified failure."""
    error = _internal_error(exc, surface=surface, request_id=None)
    return ToolCallResponse(
        content=[{"type": "text", "text": f"Error: {INTERNAL_ERROR_MESSAGE}"}],
        structuredContent={"error": INTERNAL_ERROR_MESSAGE, **error["data"]},
        isError=True,
    )


async def _audit_mcp_invocation(
    *,
    decision: PolicyDecision,
    endpoint: str,
    transport: str,
    ok: bool,
    error: str | None,
    effects_committed: bool,
    extra_metadata: dict[str, Any] | None = None,
    dispatch_attempt: Any | None = None,
) -> Any:
    """Write the invocation's audit event, answering contention by what ran.

    ``effects_committed`` states whether anything irreversible has already
    happened for this call -- the tool executed, the wallet was charged, or an
    upstream dispatch went out. It has no default on purpose. An audit-chain
    loss is retryable exactly when the answer is no, and every site that writes
    one has to say which side of the charge it is on, so a site added later
    cannot inherit the retryable answer by omission. That inheritance is the
    defect this parameter exists to make unrepresentable: guarding the finalize
    loop alone left the upstream helpers and the local refund-success path free
    to hand a caller who had already run and paid a "retry".

    "No" is necessary but not sufficient, which is what ``dispatch_attempt``
    answers for. A retryable loss is unwound by ``_execute_registered_tool``,
    which frees the idempotency record so the -32005 names a retry the caller
    can actually make -- and a durable dispatch attempt makes that release
    impossible, because its NOT NULL foreign key pins the record.
    """
    record_audit(
        "mcp.invoke",
        tool=decision.tool_name,
        wallet_id=decision.wallet_id,
        transport=transport,
        auth_source=decision.auth_source,
        key_id=decision.key_id,
        policy_decision_id=decision.decision_id,
        request_id=decision.request_id,
        ok=ok,
        error=error,
    )
    try:
        if dispatch_attempt is not None:
            metadata_attempt_id = (extra_metadata or {}).get("dispatch_attempt_id")
            if metadata_attempt_id != dispatch_attempt.attempt_id:
                raise RuntimeError("dispatch_audit_identity_mismatch")
            # Its own append can lose the chain head too: the terminal-dispatch
            # audit goes through record_audit_event like any other.
            return await get_mcp_dispatch_reconciliation_service().get_or_create_terminal_audit(
                dispatch_attempt.attempt_id
            )
        return await record_audit_event(
            event="mcp.invoke",
            wallet_id=decision.wallet_id,
            tool=decision.tool_name,
            endpoint=endpoint,
            auth_source=decision.auth_source,
            key_id=decision.key_id,
            policy_decision_id=decision.decision_id,
            request_id=decision.request_id,
            ok=ok,
            error=error,
            metadata={
                "transport": transport,
                "estimated_cost": decision.estimated_cost,
                "policy_reason": decision.reason,
                **(extra_metadata or {}),
            },
        )
    except AuditChainContendedError as exc:
        if not effects_committed:
            if dispatch_attempt is not None:
                # Remote, and already terminal: complete_pre_dispatch_failure
                # ran before this audit, so a durable mcp_dispatch_attempts row
                # exists and its NOT NULL idempotency_record_id pins the
                # record. The unwind cannot free what the retryable answer
                # advertises -- its DELETE hits the foreign key, raises, and is
                # only logged -- so the caller would be told to retry a key
                # that answers idempotency_in_progress until the dispatch
                # reconciler writes the receipt. Name that owner instead: a
                # winner is mid-flight, which is what this error already means
                # everywhere else on the dispatch path, it is mapped on all
                # three ladders, and the unwind does not catch it, so nothing
                # attempts the impossible delete.
                raise IdempotencyInProgressError("idempotency_in_progress") from exc
            # Nothing ran, nothing was charged, and nothing durable pins the
            # key, so the unwind can release the record this invocation owns.
            #
            # A single-use human approval spent earlier on this path is NOT a
            # reason to withhold that release. The retry does re-read the
            # consumed approval and is refused -- but that refusal is
            # human_approval_consumed, a signed and receipted terminal denial,
            # so the retry reaches an answer. Keeping the key instead would
            # strand it: an uncharged local record matches no
            # reconcile_stuck_records pass (the orphaned-local pass adopts a
            # row only on proof of a committed debit, and there is none when
            # the charge is what refused), so every later use of that key would
            # answer idempotency_in_progress forever, with no receipt, no audit
            # event and no manual-review count. Nor is -32007 owed instead:
            # nothing was charged and nothing ran, so there is no committed
            # effect for that code to name. An accurate retryable answer that
            # terminates beats a deliberated one that wedges.
            raise
        # The call already ran or the wallet already moved, so "retry" would
        # invite a second execution of something the caller has paid for. It
        # cannot be softened into a success either: the receipt is built from
        # this event's id, so no audit event means no signed receipt, and
        # reconcile_stuck_records can only complete a stuck record when a
        # receipt exists -- without one it counts the record for manual review.
        # Re-raised under a type the retryable handlers do not catch, which
        # names the state on the wire instead of spending the unclassified
        # -32603 on a case the code classified deliberately.
        raise TerminalRecordContendedError(AUDIT_CHAIN_CONTENDED_AFTER_EFFECTS) from exc


async def _handle_tools_call(
    params: dict,
    *,
    auth: AuthContext,
    money: AgentMoney,
    transport: str,
    endpoint: str,
    request_id: str | None,
    idempotency_key: str | None = None,
    request_payload: dict[str, Any] | None = None,
) -> dict:
    governed_request = await _mcp_adapter.normalize_request(
        params,
        auth=auth,
        money=money,
        transport=transport,
        endpoint=endpoint,
        request_id=request_id,
        idempotency_key=idempotency_key,
        request_payload=request_payload,
    )
    result = await _mcp_adapter.invoke(governed_request)
    return await _mcp_adapter.normalize_response(result)


@router.post(
    "/tools/{service_id}/invoke",
    name="Invoke MCP Tool",
    summary="Invoke a registered MCP tool",
    deprecated=True,
    description=(
        "Legacy REST-shaped invoke, kept for existing clients. New "
        "integrations should call tools through the standard MCP Streamable "
        "HTTP endpoint at POST /mcp (or the legacy JSON-RPC POST "
        "/mcp/messages); every entry point runs the same governed "
        "permit→meter→receipt path."
    ),
)
async def invoke_tool(
    service_id: str,
    request: ToolCallRequest,
    http_request: Request,
    auth: AuthContext = Depends(get_auth_context),
    money: AgentMoney = Depends(get_agent_money),
) -> ToolCallResponse:
    """
    Invoke an MCP-enabled service.

    This endpoint verifies the API key, applies the governed billing path, and
    executes either a registered local callable or the single configured
    upstream MCP tool. Metadata-only database service registrations are not
    executable through MCP.
    """
    mcp_context = request.mcp_context
    if not mcp_context:
        # Legacy shape: the wallet rides in the arguments. Check its type
        # here; McpContext would otherwise raise a pydantic ValidationError
        # inside the handler, which is a 500, not the 400 it should be.
        argument_wallet = request.arguments.get("wallet_id", "")
        if not isinstance(argument_wallet, str):
            raise HTTPException(status_code=400, detail="Missing wallet_id")
        mcp_context = McpContext(
            wallet_id=argument_wallet,
            request_path=None,
            permit_id=None,
            quote_id=None,
            idempotency_key=None,
        )

    if not mcp_context.wallet_id:
        raise HTTPException(status_code=400, detail="Missing wallet_id")

    # Same rule as the JSON-RPC transports: a present-but-unusable key in the
    # header or the typed context is refused, two present keys must agree, and
    # only a truly absent key proceeds as an un-keyed call.
    try:
        client_idempotency_key = resolve_client_idempotency_key(
            [
                *_header_idempotency_key_sources(http_request),
                *(
                    [("mcp_context.idempotency_key", mcp_context.idempotency_key)]
                    if mcp_context.idempotency_key is not None
                    else []
                ),
            ]
        )
    except InvalidIdempotencyKeyError as exc:
        raise HTTPException(
            status_code=400,
            detail={"message": str(exc), **exc.as_error_data()},
        ) from exc

    mcp_payload = {
        "name": service_id,
        "arguments": request.arguments,
        "mcpContext": {
            "wallet_id": mcp_context.wallet_id,
            "permit_id": mcp_context.permit_id,
            "quote_id": mcp_context.quote_id,
            "idempotency_key": mcp_context.idempotency_key,
        },
    }
    try:
        governed_request = await _mcp_adapter.normalize_request(
            mcp_payload,
            auth=auth,
            money=money,
            transport="http",
            endpoint=f"/mcp/tools/{service_id}/invoke",
            request_id=None,
            idempotency_key=client_idempotency_key,
            request_payload=request.model_dump(mode="json"),
        )
        result = await _mcp_adapter.invoke(governed_request)
        return ToolCallResponse(**await _mcp_adapter.normalize_response(result))
    except HumanApprovalPendingSignal as exc:
        detail: dict[str, Any] = {"error": str(exc)}
        if exc.data:
            detail["approval"] = exc.data
        raise HTTPException(status_code=exc.status_code, detail=detail)
    except (
        IdempotencyInProgressError,
        LedgerWriteContendedError,
        AuditChainContendedError,
        ReceiptWriteContendedError,
        PermitWriteContendedError,
    ) as exc:
        # Same five-way retryable family as /mcp/messages: the caller may
        # retry the same idempotency key. A pre-effect
        # ReceiptWriteContendedError that reached here without this entry fell
        # through to except Exception and answered 200 isError internal_error,
        # which a client cannot tell from an unclassified fault.
        # PermitWriteContendedError was the same omission one write earlier --
        # the contended permit reserve on the way in -- and is matched as its
        # own type rather than as PermitError, whose other reasons
        # (permit_not_found, dispatch_attempt_not_found, the budget denials)
        # are not retryable contention and must keep falling through.
        #
        # What makes the retry safe is NOT "nothing was charged". That holds
        # for the three contention types, but IdempotencyInProgressError also
        # arrives from _execute_upstream_after_charge, which runs past
        # money.charge -- so this branch does answer 409 for an already-debited
        # wallet. It is safe there for a different reason: that debit is
        # operation-keyed on the idempotency record, so the retry deduplicates
        # against the same charge instead of making a second one. Harden this
        # ladder from the operation key, not from an assumption that nothing
        # moved.
        #
        # A contention loss whose effects committed does not arrive here at
        # all: the five receipt sites and the audit site re-raise it as
        # TerminalRecordContendedError, which the next branch answers.
        raise HTTPException(
            status_code=409,
            detail={"error": str(exc)},
        ) from exc
    except TerminalRecordContendedError as exc:
        # Stays 500 -- the server did fail to record the call, and this surface
        # has no better status for "effects committed, outcome indeterminate".
        # What changes is the body: a named reason and its remediation instead
        # of an opaque internal_error, matching the -32007 the JSON-RPC
        # surfaces return for the identical state.
        raise HTTPException(
            status_code=exc.status_code,
            detail=_terminal_record_contended_data(exc.reason),
        ) from exc
    except ToolPermissionDenied as exc:
        detail = {"error": str(exc)}
        if exc.receipt:
            detail["receipt"] = exc.receipt
        if exc.details:
            detail["details"] = exc.details
        raise HTTPException(status_code=403, detail=detail)
    except GovernedToolError as exc:
        detail = {"error": str(exc)}
        if exc.receipt:
            detail["receipt"] = exc.receipt
        detail.update(exc.extra_data)
        raise HTTPException(status_code=exc.status_code, detail=detail)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ToolExecutionError as exc:
        # Legacy (unpermitted) tool failure: compensation is complete and the
        # message is the tool's own error, as on the governed path.
        return ToolCallResponse(
            content=[{"type": "text", "text": f"Error: {exc}"}],
            isError=True,
        )
    except ValueError as exc:
        message = str(exc)
        if _value_error_jsonrpc_code(message) is None:
            return _internal_error_tool_result(
                exc, surface="/mcp/tools/{service_id}/invoke"
            )
        if message == "insufficient_funds":
            raise HTTPException(status_code=402, detail=message)
        if message in {"wallet_frozen", "wallet_expired"}:
            raise HTTPException(status_code=403, detail={"error": message})
        if message.startswith("Tool not found"):
            raise HTTPException(status_code=404, detail=message)
        if message.startswith("Tool not executable"):
            raise HTTPException(status_code=501, detail=message)
        raise HTTPException(status_code=400, detail=message)
    except Exception as exc:
        return _internal_error_tool_result(
            exc, surface="/mcp/tools/{service_id}/invoke"
        )


@router.get(
    "/tools",
    name="List MCP Tools",
    summary="List all available MCP tools (paginated)",
)
async def list_tools(
    request: Request,
    category: ServiceCategory | None = None,
    limit: int = Query(default=100, ge=1, le=500, description="Max tools to return"),
    offset: int = Query(default=0, ge=0, description="Number of tools to skip"),
) -> dict[str, Any]:
    """
    List all available MCP-enabled services with pagination.

    Query Parameters:
        category: Optional service category filter
        limit: Maximum number of tools to return (default 100, max 500)
        offset: Number of tools to skip for pagination

    Returns:
        Paginated list of tool definitions with schemas
    """
    await reject_anonymous_production_catalog(request)
    manifest = await build_mcp_tools_manifest(category=category)
    tools = manifest["tools"]
    total = len(tools)

    paginated_tools = tools[offset : offset + limit]

    return {
        "tools": paginated_tools,
        "count": len(paginated_tools),
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(paginated_tools) < total,
        "generated_at": manifest["generated_at"],
    }


@router.get(
    "/tools/{service_id}",
    name="Get MCP Tool",
    summary="Get a specific MCP tool definition",
)
async def get_tool(service_id: str, request: Request) -> dict[str, Any]:
    """
    Get the MCP tool definition for a specific service.

    Returns the full tool schema including:
    - inputSchema
    - output schema availability metadata
    - pricing and category annotations
    """
    await reject_anonymous_production_catalog(request)
    _ensure_local_mcp_tools_registered()
    registry = get_service_registry()

    service = registry.get_local(service_id)

    if not service:
        raise HTTPException(
            status_code=404,
            detail=f"Executable tool not found: {service_id}",
        )

    generator = get_mcp_generator()
    tool = generator._service_to_mcp_tool(service)
    return tool
