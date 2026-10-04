"""Standard MCP endpoint (``/mcp``) served by the official MCP SDK.

The official python SDK's Streamable HTTP transport owns the whole protocol
surface: ``initialize`` and capability negotiation, protocol-version
validation, JSON-RPC framing and parse errors, notifications (202), ``ping``,
and error envelopes. This module contributes only what the SDK cannot know —
the transaction-integrity boundary. ``tools/list`` serves the governed
manifest, and ``tools/call`` runs the logical-action -> permit -> meter ->
evidence pipeline. Configured upstream tools additionally use the at-most-one
gateway dispatch and explicit outcome/uncertainty state machine.

Standard clients cannot supply the wallet/permit context the governed adapter
requires, so ``tools/call`` mints a bounded, signed, single-tool, short-lived
permit on behalf of the caller's wallet (derived from its API key) and then
delegates to the same governed invoke path as ``/mcp/messages``. The full
permit -> meter -> receipt -> audit loop applies unchanged; only the minting
step moves server-side. Bootstrap/admin keys have no wallet and are refused:
there is no wallet to encumber, and this endpoint must never bypass metering.

Wallet policy decides the shape of the auto-minted permit. When an active
policy bundle for the caller's wallet sets ``human_approval_required``, the
permit is minted with ``requires_human_approval=true``, so the invoke pauses
on the existing human-approval gate instead of failing: the agent calls the
tool normally, and the middleware materializes the approval workflow. Such
calls require a client ``Idempotency-Key`` so the retry that polls the
decision resolves to the same permit and the same approval instead of paging
a human again.

Denials on this surface are machine-actionable: they carry the same
``details`` the governed endpoints emit (which limit was hit, by how much)
plus, where a next step exists, a ``remediation`` block naming it
(fund the wallet, ask a human via ``POST /v1/permit-requests``, retry the
same call once an approval is decided).

Replay safety: a client-supplied ``Idempotency-Key`` header (or the
``io.agentmiddleware/idempotency_key`` entry in ``params._meta``) is honored
exactly like the governed endpoints. A key that is *present but unusable* —
not a string, blank, longer than 128 characters (the durable column width the
Python SDK also enforces), carrying control characters, or supplied in two
sources that disagree — is refused with invalid params (``-32602``) before a
permit is minted, anything is metered, or anything is dispatched. Generating
a key on the caller's behalf in that case would silently defeat the retry
protection the caller asked for. Only a genuinely absent key (no header, no
metadata entry, or a JSON ``null`` entry) gets a generated one, and then
every call, retries included, is a new charged operation — the same contract
as an un-keyed governed invoke. Clients that retry must always send their own
persistent key.

The endpoint is stateless: each POST gets a fresh SDK transport in JSON mode
(no ``Mcp-Session-Id``, responses are plain JSON, never SSE), so there is no
server-initiated stream and GET/DELETE answer 405. The signed receipt is
returned on every ``tools/call`` result both under
``_meta["io.agentmiddleware/receipt"]`` (the spec's extension point) and as a
top-level ``receipt`` field (the pre-SDK contract). Disabled by default —
``ENABLE_STANDARD_MCP_ENDPOINT`` must be set explicitly, so the surface cannot
be advertised (or preflighted by the registry publish gate) before an operator
turns it on.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

import anyio
import mcp.types as mcp_types
from anyio.abc import TaskStatus
from fastapi import APIRouter, Depends, HTTPException, Request
from mcp.server.lowlevel import Server
from mcp.server.streamable_http import StreamableHTTPServerTransport
from mcp.server.streamable_http_manager import RequestBodyLimitMiddleware
from mcp.shared.exceptions import McpError
from starlette.responses import JSONResponse, Response
from starlette.types import Message, Receive, Scope, Send

from app.core.auth import AuthContext, get_auth_context
from app.services.audit_chain import AuditChainContendedError
from app.services.receipts import ReceiptWriteContendedError
from app.services.billing_engine import LedgerWriteContendedError
from app.core.config import get_settings
from app.core.time import utc_now
from app.routers.mcp import (
    _MAX_JSON_NESTING_DEPTH,
    GovernedToolError,
    HumanApprovalPendingSignal,
    TerminalRecordContendedError,
    ToolPermissionDenied,
    _handle_tools_call,
    _handle_tools_list,
    _header_idempotency_key_sources,
    _internal_error,
    _json_nesting_depth_exceeds,
    _loads_strict_json,
    _terminal_record_contended_data,
    _value_error_jsonrpc_code,
)
from app.schemas.trust import PermitCreateRequest
from app.services.agent_money import get_agent_money
from app.services.idempotency import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
    InvalidIdempotencyKeyError,
    get_idempotency_service,
    resolve_client_idempotency_key,
)
from app.services.mcp_generator import MCP_SERVER_VERSION, McpGenerator
from app.services.permits import (
    PermitError,
    PermitWriteContendedError,
    get_permit_service,
)
from app.services.service_registry import get_service_registry
from app.trust import approval_window_seconds, wallet_human_approval_required
from app.trust.adapters import GovernedRequestInvalid

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp", tags=["MCP Standard Endpoint"])

_IDEMPOTENCY_META_KEY = "io.agentmiddleware/idempotency_key"
_RECEIPT_META_KEY = "io.agentmiddleware/receipt"
_META_KEY_SOURCE = 'params._meta["io.agentmiddleware/idempotency_key"]'
# A legacy-transport client may send its key in params.mcpContext; the
# source label names that location in the conflict/refusal payload.
_LEGACY_CONTEXT_KEY_SOURCE = "params.mcpContext.idempotency_key"

# Idempotency scope for auto-minted permits; distinct from the governed-call
# scope so mint replay and call replay never collide.
_AUTO_PERMIT_ENDPOINT = "/mcp#auto-permit"

_SERVER_INSTRUCTIONS = (
    "Transaction-integrity boundary for consequential MCP actions on the "
    "configured upstream-MCP path. Every "
    "tools/call is bound to one logical action, authorized by a server-minted "
    "wallet-bounded permit, and metered against the caller's configured "
    "allowance. For a configured upstream tool, an accepted Idempotency-Key "
    "permits at most one gateway dispatch and debit; delivery_uncertain is "
    "preserved rather than automatically redispatched. Authenticate with a "
    "wallet-scoped API key."
    " The serverInfo.name is a stable legacy compatibility identifier; the "
    "instructions describe the current product category."
)


def _mcp_error(code: int, message: str, data: dict[str, Any] | None = None) -> McpError:
    return McpError(mcp_types.ErrorData(code=code, message=message, data=data))


def _permit_error(
    exc: PermitError,
    *,
    tool_name: str | None = None,
    record: dict[str, Any] | None = None,
) -> McpError:
    """Map an auto-mint failure to a structured, machine-actionable error.

    The budget case names the concrete gap and the two ways out. The caller's
    own tool cost is disclosed (it is public in discovery); the wallet balance
    is not restated because the caller can read it via ``/v1/me``.
    """
    if exc.reason == "permit_budget_exceeds_wallet_balance":
        data: dict[str, Any] = {
            "error": "authority_required",
            "reason_code": exc.reason,
            "remediation": {
                "type": "fund_wallet_or_request_authority",
                "check_authority": "GET /v1/me/authority",
                "request_authority": "POST /v1/permit-requests",
            },
        }
        if tool_name is not None:
            requested: dict[str, Any] = {"tool": tool_name}
            if record is not None:
                requested["estimated_credits"] = str(_tool_call_budget(record))
            data["requested"] = requested
        return _mcp_error(-32004, exc.reason, data)
    return _mcp_error(-32003, exc.reason)


def _tool_call_budget(record: dict[str, Any]) -> Decimal:
    """Bound the auto-minted permit to the tool's per-call cost.

    Permits must carry a positive budget, so a zero-cost tool is bounded at
    one credit; nothing is charged beyond the actual metered cost either way.
    """
    exact = record.get("credits_per_unit_exact")
    if exact is not None:
        try:
            cost = Decimal(str(exact))
        except InvalidOperation:
            cost = Decimal(str(record.get("credits_per_unit", 1.0)))
    else:
        cost = Decimal(str(record.get("credits_per_unit", 1.0)))
    return cost if cost > 0 else Decimal("1")


def _meta_idempotency_key_source(
    params: mcp_types.CallToolRequestParams,
) -> tuple[str, object] | None:
    """Return the ``_meta`` key source when the caller sent one, else ``None``.

    Presence is judged by the entry existing, not by its value being usable:
    an entry that is present but malformed must reach the validator and be
    refused, never fall through to a generated key. A JSON ``null`` entry is
    the one value that reads as absent, matching an omitted entry.
    """
    meta = params.meta
    extra = getattr(meta, "model_extra", None) if meta is not None else None
    if not extra or _IDEMPOTENCY_META_KEY not in extra:
        return None
    value = extra[_IDEMPOTENCY_META_KEY]
    if value is None:
        return None
    return (_META_KEY_SOURCE, value)


def _action_permit_reference(params: mcp_types.CallToolRequestParams) -> str | None:
    meta = getattr(params.meta, "model_extra", None) or {}
    extra = params.model_extra or {}
    legacy = extra.get("mcpContext", {})
    values = []
    if "io.agentmiddleware/permit_id" in meta:
        values.append(meta["io.agentmiddleware/permit_id"])
    if isinstance(legacy, dict) and "permit_id" in legacy:
        values.append(legacy["permit_id"])
    if any(type(value) is not str or not value.strip() for value in values):
        raise _mcp_error(-32602, "invalid_permit_reference")
    if len(set(values)) > 1:
        raise _mcp_error(-32602, "permit_reference_conflict")
    return values[0] if values else None


def _client_idempotency_key(
    http_request: Request | None, params: mcp_types.CallToolRequestParams
) -> str | None:
    """Resolve the caller's replay key from every source it actually sent.

    Runs before the auto-permit mint, so a refused key has no effect at all:
    no permit, no idempotency record, no debit, no dispatch. ``None`` means
    the caller sent no key anywhere.
    """
    sources: list[tuple[str, object]] = []
    try:
        if http_request is not None:
            sources.extend(_header_idempotency_key_sources(http_request))
        meta_source = _meta_idempotency_key_source(params)
        if meta_source is not None:
            sources.append(meta_source)
        # A client written against the legacy transport may send its key in
        # params.mcpContext. This endpoint mints its own permit and ignores
        # the rest of that object, but a key the caller explicitly sent must
        # not be silently dropped: it is one more source, validated and
        # conflict-checked like the others. JSON null reads as absent, as
        # everywhere else.
        legacy_context = (params.model_extra or {}).get("mcpContext")
        if isinstance(legacy_context, dict):
            context_value = legacy_context.get("idempotency_key")
            if context_value is not None:
                sources.append((_LEGACY_CONTEXT_KEY_SOURCE, context_value))
        elif legacy_context is not None:
            raise _mcp_error(-32602, "Invalid params: mcpContext must be an object")
        return resolve_client_idempotency_key(sources)
    except InvalidIdempotencyKeyError as exc:
        raise _mcp_error(-32602, str(exc), exc.as_error_data()) from exc


async def _mint_auto_permit(
    *,
    auth: AuthContext,
    tool_name: str,
    record: dict[str, Any],
    client_key: str | None,
    requires_human_approval: bool = False,
) -> str:
    """Mint the bounded single-tool permit backing this call.

    The governed replay hash covers the permit id, so a retried idempotency
    key must resolve to the SAME auto-minted permit — mint through an
    idempotency record keyed off the client's key. Generated keys are unique
    per call and skip this bookkeeping.
    """
    wallet_id = auth.wallet_id
    if not wallet_id:  # narrowed by the caller; fail closed regardless
        raise _mcp_error(-32003, "wallet_scoped_key_required")
    settings = get_settings()
    # An approval-gated permit must stay valid for the whole decision window
    # plus the normal TTL as execution margin: permit expiry is validated
    # before the approval is polled, so a shorter-lived permit would strand a
    # decision made late in the window as a terminal permit_expired denial.
    ttl_seconds = settings.STANDARD_MCP_PERMIT_TTL_SECONDS
    if requires_human_approval:
        ttl_seconds += approval_window_seconds()
    permit_request = PermitCreateRequest(
        issuer_wallet_id=wallet_id,
        subject_wallet_id=wallet_id,
        subject_key_id=auth.key_id,
        allowed_tools=[tool_name],
        max_credits=_tool_call_budget(record),
        expires_at=utc_now() + timedelta(seconds=ttl_seconds),
        requires_human_approval=requires_human_approval,
    )

    if not client_key:
        try:
            permit = await get_permit_service().create_permit(
                permit_request, subject_key_id=auth.key_id
            )
        except PermitError as exc:
            raise _permit_error(exc, tool_name=tool_name, record=record) from exc
        return permit.permit_id

    idem = get_idempotency_service()
    mint_key = "smcp-" + hashlib.sha256(client_key.encode("utf-8")).hexdigest()[:48]
    mint_payload: dict[str, Any] = {
        "kind": "standard-mcp-auto-permit",
        "tool": tool_name,
        "wallet_id": wallet_id,
        "subject_key_id": auth.key_id,
    }
    # Included only when set so pre-existing mint records (created before the
    # approval bit existed) still replay. A policy flip between attempts
    # changes the payload and conflicts — fail closed, use a fresh key.
    if requires_human_approval:
        mint_payload["requires_human_approval"] = True
    try:
        mint_replay = await idem.begin(
            wallet_id=wallet_id,
            endpoint=_AUTO_PERMIT_ENDPOINT,
            idempotency_key=mint_key,
            request_payload=mint_payload,
        )
    except IdempotencyConflictError as exc:
        raise _mcp_error(-32003, "idempotency_key_reused") from exc
    except IdempotencyInProgressError as exc:
        raise _mcp_error(-32005, "idempotency_in_progress") from exc
    if mint_replay and mint_replay.response_json:
        return mint_replay.response_json["permit_id"]

    try:
        permit = await get_permit_service().create_permit(
            permit_request, subject_key_id=auth.key_id
        )
    except PermitError as exc:
        await idem.abandon(
            wallet_id=wallet_id,
            endpoint=_AUTO_PERMIT_ENDPOINT,
            idempotency_key=mint_key,
        )
        raise _permit_error(exc, tool_name=tool_name, record=record) from exc
    await idem.complete(
        wallet_id=wallet_id,
        endpoint=_AUTO_PERMIT_ENDPOINT,
        idempotency_key=mint_key,
        response_reference=permit.permit_id,
        response_json={"permit_id": permit.permit_id},
        status_code=201,
    )
    return permit.permit_id


async def _governed_tools_call(
    *,
    auth: AuthContext,
    tool_name: str,
    arguments: dict[str, Any],
    client_idempotency_key: str | None,
    request_id: str | None,
    permit_id: str | None = None,
) -> dict[str, Any]:
    """Auto-mint the permit, then run the single governed invoke pipeline."""
    if not tool_name:
        raise _mcp_error(-32602, "Missing tool name")

    if not auth.wallet_id:
        raise _mcp_error(
            -32003,
            "wallet_scoped_key_required: this endpoint mints permits from the "
            "caller's wallet; authenticate with a wallet-scoped API key",
        )

    record = get_service_registry().get_local(tool_name)
    if record is None:
        raise _mcp_error(-32001, f"Tool not found: {tool_name}")

    if get_service_registry().get_action_binding(record) is not None and not permit_id:
        raise _mcp_error(-32003, "action_permit_required")

    # Wallet policy shapes the permit this surface mints. A policy demanding a
    # human decision is materialized as an approval-gated permit rather than
    # surfacing as an unsatisfiable denial: the agent calls the tool normally
    # and the middleware runs the approval workflow.
    requires_human_approval = await wallet_human_approval_required(auth.wallet_id)
    if requires_human_approval and not client_idempotency_key and not permit_id:
        # Without a client key every retry would mint a fresh permit and page
        # a human again instead of polling the pending decision.
        raise _mcp_error(
            -32003,
            "idempotency_key_required_for_human_approval",
            {
                "error": "authority_required",
                "reason_code": "idempotency_key_required_for_human_approval",
                "remediation": {
                    "type": "retry_with_idempotency_key",
                    "detail": (
                        "This wallet's policy requires human approval. Send an "
                        "Idempotency-Key header (or params._meta"
                        '["io.agentmiddleware/idempotency_key"]) and retry the '
                        "same call with the same key to poll the decision."
                    ),
                },
            },
        )

    idempotency_key = client_idempotency_key or f"mcp-auto-{uuid.uuid4().hex}"
    permit_id = permit_id or await _mint_auto_permit(
        auth=auth,
        tool_name=tool_name,
        record=record,
        client_key=client_idempotency_key,
        requires_human_approval=requires_human_approval,
    )

    call_params = {
        "name": tool_name,
        "arguments": arguments,
        "mcpContext": {
            "wallet_id": auth.wallet_id,
            "permit_id": permit_id,
            "idempotency_key": idempotency_key,
        },
    }
    request_payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    try:
        return await _handle_tools_call(
            call_params,
            auth=auth,
            money=get_agent_money(),
            transport="jsonrpc",
            endpoint="/mcp",
            request_id=request_id,
            idempotency_key=idempotency_key,
            request_payload=request_payload,
        )
    except HumanApprovalPendingSignal as e:
        reason = str(e)
        pending_data = dict(e.data)
        if reason == "human_approval_pending":
            pending_data.setdefault("error", "authority_required")
            pending_data.setdefault("status", "pending_human_approval")
            pending_data.setdefault(
                "remediation",
                {
                    "type": "await_human_decision",
                    "detail": (
                        "Nothing was charged. Retry the identical tools/call "
                        "with the same Idempotency-Key once the approval is "
                        "decided; the same permit and approval are reused."
                    ),
                },
            )
        raise _mcp_error(e.jsonrpc_code, reason, pending_data or None) from e
    except IdempotencyInProgressError as e:
        raise _mcp_error(-32005, "idempotency_in_progress") from e
    except LedgerWriteContendedError as e:
        # Its own branch because the line above hardcodes its reason rather
        # than passing str(e) through; sharing it would report contention as
        # an in-progress winner that does not exist.
        raise _mcp_error(-32005, "ledger_write_contended") from e
    except AuditChainContendedError as e:
        # Same shape again, and reachable only from a refusal that ran nothing:
        # the finalize loop re-raises its own audit loss as a non-retryable
        # type, because there the tool already ran and was charged.
        raise _mcp_error(-32005, "audit_chain_head_contention") from e
    except ReceiptWriteContendedError as e:
        # The last write on the governed path, and the last one to get a
        # restart. Reachable here on the same terms as the audit loss above:
        # every receipt site past the charge converts its own contention to a
        # non-retryable type, so what arrives is a refusal that ran nothing and
        # whose idempotency record has already been released.
        raise _mcp_error(-32005, ReceiptWriteContendedError.reason) from e
    except PermitWriteContendedError as e:
        # The first guarded write on the way in, and the one this ladder was
        # missing: a contended permit reserve fell past every branch here into
        # the catch-all and answered -32603 internal_error, which a client
        # cannot separate from an unclassified fault.
        #
        # Its own branch, and matched on the subclass rather than PermitError,
        # because the base type also carries permit_not_found,
        # dispatch_attempt_not_found and the budget denials -- catching the
        # base here would relabel every one of those a retryable contention.
        #
        # Reachable on the same terms as the two losses above: the release
        # that runs after a tool has executed and been charged converts its
        # own contention to a non-retryable type, so what arrives here is a
        # reserve that never ran anything.
        raise _mcp_error(-32005, PermitWriteContendedError.reason) from e
    except TerminalRecordContendedError as e:
        # The non-retryable half of both errors above: the audit event or the
        # receipt was lost for a call that had already run or already paid.
        # -32007 exists so this cannot be confused with either neighbour --
        # not -32005, which would invite a second execution of a paid call, and
        # not the unclassified -32603, which is for failures the pipeline did
        # not classify rather than ones it classified and chose to refuse.
        raise _mcp_error(
            e.jsonrpc_code, e.reason, _terminal_record_contended_data(e.reason)
        ) from e
    except ToolPermissionDenied as e:
        denial_data: dict[str, Any] = {}
        if e.receipt:
            denial_data["receipt"] = e.receipt
        if e.details:
            denial_data["details"] = e.details
        raise _mcp_error(-32003, str(e), denial_data or None) from e
    except GovernedToolError as e:
        data = dict(e.extra_data)
        if e.receipt:
            data["receipt"] = e.receipt
        raise _mcp_error(e.jsonrpc_code, str(e), data or None) from e
    except PermissionError as e:
        raise _mcp_error(-32003, str(e)) from e
    except InvalidIdempotencyKeyError as e:
        raise _mcp_error(-32602, str(e), e.as_error_data()) from e
    except GovernedRequestInvalid as e:
        raise _mcp_error(-32602, str(e)) from e
    except ValueError as e:
        code = _value_error_jsonrpc_code(str(e))
        if code is None:
            error = _internal_error(e, surface="/mcp", request_id=request_id)
            raise _mcp_error(error["code"], error["message"], error["data"]) from e
        raise _mcp_error(code, str(e)) from e
    except McpError:
        raise
    except Exception as e:
        error = _internal_error(e, surface="/mcp", request_id=request_id)
        raise _mcp_error(error["code"], error["message"], error["data"]) from e


def _build_standard_mcp_server() -> Server:
    server = Server(
        McpGenerator.MANIFEST_NAME,
        version=MCP_SERVER_VERSION,
        instructions=_SERVER_INSTRUCTIONS,
    )

    @server.list_tools()
    async def list_tools() -> list[mcp_types.Tool]:
        result = await _handle_tools_list({})
        tools = []
        for tool in result["tools"]:
            payload = dict(tool)
            payload.setdefault("inputSchema", {"type": "object"})
            tools.append(mcp_types.Tool.model_validate(payload))
        return tools

    async def handle_call_tool(
        req: mcp_types.CallToolRequest,
    ) -> mcp_types.ServerResult:
        """Governed tools/call.

        Registered directly (not through the ``@server.call_tool()``
        decorator) so governance denials keep their JSON-RPC error codes and
        receipt-bearing ``data`` instead of collapsing into an ``isError``
        text result.
        """
        ctx = server.request_context
        http_request = ctx.request if isinstance(ctx.request, Request) else None
        auth: AuthContext | None = None
        if http_request is not None:
            auth = http_request.scope.get("state", {}).get("auth_context")
        if auth is None:
            # The ASGI wrapper authenticates every request before the SDK
            # transport runs; a missing context means this handler is being
            # driven by an unexpected transport. Fail closed.
            raise _mcp_error(-32603, "auth_context_missing")

        request_id = str(ctx.request_id) if ctx.request_id is not None else None
        try:
            # Validated before anything is minted or metered: a present-but-
            # unusable key is refused here, and only a truly absent key
            # proceeds to the generated-key path inside _governed_tools_call.
            client_key = _client_idempotency_key(http_request, req.params)

            result = await _governed_tools_call(
                auth=auth,
                tool_name=req.params.name,
                arguments=req.params.arguments or {},
                client_idempotency_key=client_key,
                permit_id=_action_permit_reference(req.params),
                request_id=request_id,
            )

            payload = dict(result)
            receipt = payload.get("receipt")
            if receipt is not None:
                meta = dict(payload.get("_meta") or {})
                meta[_RECEIPT_META_KEY] = receipt
                payload["_meta"] = meta
            return mcp_types.ServerResult(
                mcp_types.CallToolResult.model_validate(payload)
            )
        except McpError:
            raise
        except Exception as e:
            # The SDK reports any other exception that escapes a handler as
            # str(e). This is the last boundary before it, so a failure in
            # the registry lookup, the wallet-policy read, the auto-permit
            # mint, or result validation is sanitized the same way as one
            # inside the governed call.
            error = _internal_error(e, surface="/mcp", request_id=request_id)
            raise _mcp_error(error["code"], error["message"], error["data"]) from e

    server.request_handlers[mcp_types.CallToolRequest] = handle_call_tool
    return server


_standard_mcp_server = _build_standard_mcp_server()


def _require_enabled() -> None:
    if not get_settings().ENABLE_STANDARD_MCP_ENDPOINT:
        raise HTTPException(status_code=404, detail="Not Found")


def _validate_origin(request: Request) -> None:
    """Validate browser Origin exactly and bind production Host to PUBLIC_URL.

    Non-browser MCP clients send no Origin header and pass through.
    """
    public_url = get_settings().PUBLIC_URL.rstrip("/")
    request_origin = f"{request.url.scheme}://{request.url.netloc}"
    if public_url and request.url.netloc != urlsplit(public_url).netloc:
        raise HTTPException(status_code=421, detail={"error": "host_not_allowed"})

    origin = request.headers.get("origin")
    if not origin:
        return
    # A reverse proxy may expose its internal HTTP scheme through the ASGI
    # request even though the canonical browser origin is HTTPS. Once an
    # operator configures PUBLIC_URL, it is the only origin authority;
    # request-derived origin is a local/unconfigured fallback only.
    allowed = {public_url} if public_url else {request_origin}
    if origin.rstrip("/") not in allowed:
        raise HTTPException(status_code=403, detail={"error": "origin_not_allowed"})


class _SdkTransportResponse(Response):
    """Delegates the whole HTTP exchange to a fresh stateless SDK transport.

    Each POST gets its own transport in JSON-response mode — the exact
    composition ``StreamableHTTPSessionManager`` uses per request in stateless
    mode, inlined here so no lifespan-scoped task group is required. The SDK
    owns everything after authentication: JSON-RPC parsing and parse errors,
    Accept/Content-Type validation, lifecycle and protocol-version
    negotiation, notifications, and error envelopes.

    Takes the :class:`Server` to run so every stateless surface (standard,
    public) shares one transport composition instead of drifting copies.
    """

    def __init__(
        self,
        server: Server,
        *,
        max_request_body_size: int | None = None,
    ) -> None:
        super().__init__()
        self._server = server
        self._max_request_body_size = max_request_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._max_request_body_size is not None:
            limited = RequestBodyLimitMiddleware(
                self._handle_request,
                self._max_request_body_size,
            )
            await limited(scope, receive, send)
            return
        await self._handle_request(scope, receive, send)

    async def _handle_request(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        # Refuse absurdly nested bodies before the SDK parses them. On
        # Python 3.11 json.loads raises RecursionError at ~1000 frames and
        # the SDK's broad handler turns that into a 500; on 3.12+ the parse
        # succeeds and pydantic rejects the depth as invalid params. Scanning
        # the raw bytes gives both versions the same -32602 answer.
        messages: list[Message] = []
        body = bytearray()
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request":
                break
            body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break

        if _json_nesting_depth_exceeds(bytes(body), _MAX_JSON_NESTING_DEPTH):
            error_response = JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32602,
                        "message": "request JSON exceeds the supported nesting depth",
                    },
                },
                status_code=200,
            )
            await error_response(scope, receive, send)
            return

        # Refuse a body the reply could not carry before the SDK dispatches
        # it. A JSON "\ud800" escape decodes to a lone surrogate that the
        # SDK's JSON serializer cannot encode; as an ``id`` it failed the
        # response *after* the governed tools/call had run and charged the
        # wallet, so the client saw a 500 for a call that happened. Non-UTF-8
        # bytes likewise escaped the SDK's JSONDecodeError handler as a 500.
        # Malformed JSON is left to the SDK, which answers -32700 itself.
        refusal: tuple[int, int, str] | None = None
        try:
            _loads_strict_json(bytes(body))
        except json.JSONDecodeError:
            pass
        except UnicodeDecodeError:
            refusal = (400, -32700, "Parse error: request body is not valid UTF-8")
        except ValueError as exc:
            refusal = (400, -32600, f"Invalid Request: {exc}")
        if refusal is not None:
            status_code, code, reason = refusal
            error_response = JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": code, "message": reason},
                },
                status_code=status_code,
            )
            await error_response(scope, receive, send)
            return

        replayed = iter(messages)

        async def replay_receive() -> Message:
            for message in replayed:
                return message
            return await receive()

        await self._dispatch_to_transport(scope, replay_receive, send)

    async def _dispatch_to_transport(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        transport = StreamableHTTPServerTransport(
            mcp_session_id=None,
            is_json_response_enabled=True,
        )

        async with anyio.create_task_group() as tg:

            async def run_server(
                *, task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED
            ) -> None:
                async with transport.connect() as (read_stream, write_stream):
                    task_status.started()
                    try:
                        await self._server.run(
                            read_stream,
                            write_stream,
                            self._server.create_initialization_options(),
                            stateless=True,
                        )
                    except Exception:  # pragma: no cover - response already sent
                        logger.exception("MCP stateless session crashed")

            await tg.start(run_server)
            await transport.handle_request(scope, receive, send)
            await transport.terminate()


@router.post(
    "",
    name="MCP Streamable HTTP Endpoint",
    summary=(
        "Standard MCP endpoint (official MCP SDK, stateless Streamable HTTP, "
        "JSON responses)"
    ),
)
async def handle_standard_mcp(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> Response:
    """Hand one standard MCP message to the official SDK's transport.

    Authentication and origin validation happen here at the HTTP layer; the
    SDK owns the protocol. The governed ``tools/call`` handler reads the
    authenticated caller back from ``request.state``.
    """
    _require_enabled()
    _validate_origin(request)
    request.scope.setdefault("state", {})["auth_context"] = auth
    return _SdkTransportResponse(_standard_mcp_server)


@router.api_route("", methods=["GET", "DELETE"], include_in_schema=False)
async def standard_mcp_no_stream(
    auth: AuthContext = Depends(get_auth_context),
) -> Response:
    """Stateless server: no server-initiated stream, no session to delete."""
    _require_enabled()
    return Response(status_code=405, headers={"Allow": "POST"})
