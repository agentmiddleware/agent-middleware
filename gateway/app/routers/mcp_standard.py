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
top-level ``receipt`` field (the pre-SDK contract). Enabled by default and
always authenticated; set ``ENABLE_STANDARD_MCP_ENDPOINT=false`` to answer 404
and stop advertising it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

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
from app.core.browser_origin import validate_browser_origin
from app.services.audit_chain import AuditChainContendedError
from app.services.receipts import ReceiptWriteContendedError
from app.services.billing_engine import LedgerWriteContendedError
from app.core.config import get_settings
from app.core.time import utc_now
from app.core.oidc_iga import IGAError
from app.services.mcp_retry_key import (
    bucket_for,
    derive_retry_identity,
    is_no_effect_failure,
    principal_binding,
    slot_key,
)
from app.routers.mcp import (
    _MAX_JSON_NESTING_DEPTH,
    GovernedToolError,
    HumanApprovalPendingSignal,
    TerminalRecordContendedError,
    ToolPermissionDenied,
    _ensure_local_mcp_tools_registered,
    _handle_tools_call,
    _handle_tools_list,
    _header_idempotency_key_sources,
    _internal_error,
    _json_nesting_depth_exceeds,
    _loads_strict_json,
    _terminal_record_contended_data,
    _value_error_jsonrpc_code,
    _verified_enterprise_principal,
)
from app.schemas.trust import PermitCreateRequest
from app.services.agent_money import get_agent_money
from app.services.idempotency import (
    IdempotencyConflictError,
    IdempotencyFence,
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


_IDEMPOTENCY_KEY_REQUIRED_DATA: dict[str, Any] = {
    "error": "idempotency_key_required",
    "reason_code": "idempotency_key_required",
    "remediation": {
        "type": "retry_with_idempotency_key",
        "detail": (
            "Send an Idempotency-Key header (or params._meta"
            '["io.agentmiddleware/idempotency_key"]) and reuse the same key '
            "when retrying the same call."
        ),
    },
}


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


def _auto_permit_mint_key(client_key: str) -> str:
    return "smcp-" + hashlib.sha256(client_key.encode("utf-8")).hexdigest()[:48]


_UNKEYED_RETRY_LOCK_ENDPOINT = "/mcp#unkeyed-retry-lock"
_UNKEYED_RETRY_LOCK_WAIT_SECONDS = 5.0
# Far above the worst-case hold (a few DB round trips plus one permit mint).
_UNKEYED_RETRY_LOCK_STALE_SECONDS = 120
# A pre-dispatch (no side effect) failure rolls the call forward to a fresh
# attempt slot; bounded so a permanently failing upstream eventually replays
# its failure instead of being re-run forever.
_UNKEYED_RETRY_MAX_ATTEMPTS = 3


def _unkeyed_clock() -> float:
    """Epoch seconds for window bucketing (patched in tests)."""
    return time.time()


def _auto_permit_ttl_seconds(
    *, tenant: str | None, requires_human_approval: bool
) -> int:
    """TTL in seconds of the auto permit ``_mint_auto_permit`` would mint.

    An approval-gated permit must stay valid for the whole decision window
    plus the normal TTL as execution margin: permit expiry is validated
    before the approval is polled, so a shorter-lived permit would strand a
    decision made late in the window as a terminal permit_expired denial.
    Demo permits are further capped by the demo TTL limits.
    """
    settings = get_settings()
    ttl_seconds = settings.STANDARD_MCP_PERMIT_TTL_SECONDS
    if requires_human_approval:
        ttl_seconds += approval_window_seconds()
    if tenant == "demo":
        ttl_seconds = min(ttl_seconds, settings.DEMO_AUTO_PERMIT_TTL_SECONDS)
        if settings.DEMO_MAX_PERMIT_TTL_MINUTES is not None:
            ttl_seconds = min(ttl_seconds, settings.DEMO_MAX_PERMIT_TTL_MINUTES * 60)
    return ttl_seconds


def _effective_unkeyed_retry_window(
    *, tenant: str | None = None, requires_human_approval: bool = False
) -> int:
    """Retry window in seconds, capped at the TTL of the auto permit minted.

    A joined slot reuses the permit minted for the first attempt, so a window
    longer than that permit's lifetime would resolve a late retry to an
    expired permit (a denial instead of a replay or a fresh mint). The cap is
    the TTL the mint will actually use, including the demo limits. 0 disables.
    """
    window = get_settings().MCP_UNKEYED_RETRY_WINDOW_SECONDS
    if window <= 0:
        return 0
    ttl_seconds = _auto_permit_ttl_seconds(
        tenant=tenant, requires_human_approval=requires_human_approval
    )
    return min(window, max(ttl_seconds, 0))


async def _acquire_unkeyed_retry_lock(wallet_id: str, lock_key: str) -> str:
    """Serialize slot selection for one call identity; return the lock row id.

    The lock is an uncompleted idempotency row: the unique insert is the
    mutex, ``abandon`` releases it. A row left by a crashed holder is
    released once it is older than the stale threshold, so a holder that
    merely stalls can lose the lock: it must re-check ownership with
    its record id (the mint-row insert is fenced on it) before acting and release only its own
    row (``expected_record_id``).
    """
    idem = get_idempotency_service()
    deadline = time.monotonic() + _UNKEYED_RETRY_LOCK_WAIT_SECONDS
    while True:
        try:
            begun = await idem.begin_with_record(
                wallet_id=wallet_id,
                endpoint=_UNKEYED_RETRY_LOCK_ENDPOINT,
                idempotency_key=lock_key,
                request_payload={"kind": "unkeyed-retry-lock"},
            )
            if begun.replay is not None:
                # Never expected for a lock row (it is never completed).
                raise IdempotencyInProgressError("idempotency_in_progress")
            return begun.record_id
        except IdempotencyInProgressError:
            held = await idem.get_record(
                wallet_id=wallet_id,
                endpoint=_UNKEYED_RETRY_LOCK_ENDPOINT,
                idempotency_key=lock_key,
            )
            if held is not None and held.created_at < utc_now() - timedelta(
                seconds=_UNKEYED_RETRY_LOCK_STALE_SECONDS
            ):
                await idem.abandon(
                    wallet_id=wallet_id,
                    endpoint=_UNKEYED_RETRY_LOCK_ENDPOINT,
                    idempotency_key=lock_key,
                    expected_record_id=held.record_id,
                )
                continue
            if time.monotonic() >= deadline:
                raise
            await anyio.sleep(0.02)


async def _resolve_unkeyed_retry(
    *,
    auth: AuthContext,
    tool_name: str,
    record: dict[str, Any],
    arguments: dict[str, Any],
    requires_human_approval: bool,
    map_mcp_errors: bool = True,
) -> tuple[str, str] | None:
    """Pick the retry slot for an unkeyed mutating call and mint its permit.

    Returns ``(idempotency_key, permit_id)``, or None when no slot applies and
    the caller must refuse the unkeyed call (SEL-498): read-only tools (a replay would serve a
    stale read), the window disabled, or an enterprise bearer that fails
    verification (the governed pipeline denies that call regardless).

    Under a per-identity lock:
    * a live slot from the previous window (first minted less than one window
      ago) is joined, so a retry or a concurrent twin straddling the bucket
      boundary resolves to the same key: the window slides from the first
      call instead of resetting at fixed boundaries;
    * an attempt whose stored result is a refunded pre-dispatch failure (no
      side effects) rolls forward to the next attempt slot, so the retry runs
      again instead of replaying that failure;
    * the auto permit is minted before the lock is released, so any later
      caller sees the chosen slot.
    """
    window = _effective_unkeyed_retry_window(
        tenant=auth.tenant, requires_human_approval=requires_human_approval
    )
    wallet_id = auth.wallet_id
    if window <= 0 or not wallet_id or record.get("read_only"):
        return None
    try:
        principal = _verified_enterprise_principal(auth.enterprise_bearer_token)
    except IGAError:
        return None
    identity = derive_retry_identity(
        wallet_id=wallet_id,
        key_id=auth.key_id,
        principal=principal_binding(principal),
        tool_name=tool_name,
        arguments=arguments,
    )
    idem = get_idempotency_service()
    lock_key = "ulock-" + identity[:48]
    try:
        lock_record_id = await _acquire_unkeyed_retry_lock(wallet_id, lock_key)
    except IdempotencyInProgressError as exc:
        if map_mcp_errors:
            raise _mcp_error(-32005, "idempotency_in_progress") from exc
        raise
    try:
        bucket = bucket_for(_unkeyed_clock(), window)
        chosen = bucket
        previous = await idem.get_record(
            wallet_id=wallet_id,
            endpoint=_AUTO_PERMIT_ENDPOINT,
            idempotency_key=_auto_permit_mint_key(
                slot_key(identity, window_seconds=window, bucket=bucket - 1, attempt=0)
            ),
        )
        if previous is not None and previous.created_at > utc_now() - timedelta(
            seconds=window
        ):
            chosen = bucket - 1
        key = slot_key(identity, window_seconds=window, bucket=chosen, attempt=0)
        for attempt in range(_UNKEYED_RETRY_MAX_ATTEMPTS):
            key = slot_key(
                identity, window_seconds=window, bucket=chosen, attempt=attempt
            )
            stored = await idem.get_governed_mcp_record(
                wallet_id=wallet_id, idempotency_key=key
            )
            if stored is None or not is_no_effect_failure(stored.response_json):
                break
        # The mint-row insert is fenced on this lock row: it is written only
        # if we still own the lock, in the same transaction. A holder that
        # stalled past the stale threshold and was taken over gets -32005
        # and never mints or dispatches.
        permit_id = await _mint_auto_permit(
            auth=auth,
            tool_name=tool_name,
            record=record,
            client_key=key,
            lock_fence=(_UNKEYED_RETRY_LOCK_ENDPOINT, lock_key, lock_record_id),
            requires_human_approval=requires_human_approval,
            map_mcp_errors=map_mcp_errors,
        )
        return key, permit_id
    finally:
        # Shielded so a cancelled request still releases its lock row
        # instead of blocking the identity until the stale threshold.
        with anyio.CancelScope(shield=True):
            await idem.abandon(
                wallet_id=wallet_id,
                endpoint=_UNKEYED_RETRY_LOCK_ENDPOINT,
                idempotency_key=lock_key,
                expected_record_id=lock_record_id,
            )


async def _mint_auto_permit(
    *,
    auth: AuthContext,
    tool_name: str,
    record: dict[str, Any],
    client_key: str | None,
    requires_human_approval: bool = False,
    map_mcp_errors: bool = True,
    lock_fence: IdempotencyFence | None = None,
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
    ttl_seconds = _auto_permit_ttl_seconds(
        tenant=auth.tenant, requires_human_approval=requires_human_approval
    )
    budget = _tool_call_budget(record)
    if auth.tenant == "demo":
        demo_tools = settings.demo_allowed_tools_list
        if (
            tool_name != "partner.echo"
            or tool_name not in demo_tools
            or (auth.allowed_tools is not None and tool_name not in auth.allowed_tools)
        ):
            error = PermitError("demo_tool_not_allowed")
            if map_mcp_errors:
                raise _permit_error(error, tool_name=tool_name, record=record)
            raise error
        if requires_human_approval:
            error = PermitError("human_approval_required")
            if map_mcp_errors:
                raise _permit_error(error, tool_name=tool_name, record=record)
            raise error
        credit_cap = settings.DEMO_AUTO_PERMIT_MAX_CREDITS
        if settings.DEMO_MAX_PERMIT_CREDITS is not None:
            credit_cap = min(credit_cap, settings.DEMO_MAX_PERMIT_CREDITS)
        if budget > credit_cap:
            error = PermitError("demo_auto_permit_budget_exceeded")
            if map_mcp_errors:
                raise _permit_error(error, tool_name=tool_name, record=record)
            raise error
    permit_request = PermitCreateRequest(
        issuer_wallet_id=wallet_id,
        subject_wallet_id=wallet_id,
        subject_key_id=auth.key_id,
        allowed_tools=[tool_name],
        max_credits=budget,
        expires_at=utc_now() + timedelta(seconds=ttl_seconds),
        requires_human_approval=requires_human_approval,
    )

    if not client_key:
        try:
            permit = await get_permit_service().create_permit(
                permit_request, subject_key_id=auth.key_id, auth=auth
            )
        except PermitError as exc:
            if map_mcp_errors:
                raise _permit_error(exc, tool_name=tool_name, record=record) from exc
            raise
        return permit.permit_id

    idem = get_idempotency_service()
    mint_key = _auto_permit_mint_key(client_key)
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
        mint_begun = await idem.begin_with_record(
            wallet_id=wallet_id,
            endpoint=_AUTO_PERMIT_ENDPOINT,
            idempotency_key=mint_key,
            request_payload=mint_payload,
            fence=lock_fence,
        )
    except IdempotencyConflictError as exc:
        if map_mcp_errors:
            raise _mcp_error(-32003, "idempotency_key_reused") from exc
        raise
    except IdempotencyInProgressError as exc:
        if map_mcp_errors:
            raise _mcp_error(-32005, "idempotency_in_progress") from exc
        raise
    mint_replay = mint_begun.replay
    if mint_replay and mint_replay.response_json:
        return mint_replay.response_json["permit_id"]

    # Any mint failure (PermitError, PermitWriteContendedError, another
    # exception, or cancellation) releases only this attempt's row, so a
    # retry can mint again instead of waiting on an orphaned in-progress row.
    try:
        permit = await get_permit_service().create_permit(
            permit_request, subject_key_id=auth.key_id, auth=auth
        )
    except BaseException as exc:
        with anyio.CancelScope(shield=True):
            await idem.abandon(
                wallet_id=wallet_id,
                endpoint=_AUTO_PERMIT_ENDPOINT,
                idempotency_key=mint_key,
                expected_record_id=mint_begun.record_id,
            )
        if isinstance(exc, PermitError) and map_mcp_errors:
            raise _permit_error(exc, tool_name=tool_name, record=record) from exc
        raise
    await idem.complete(
        wallet_id=wallet_id,
        endpoint=_AUTO_PERMIT_ENDPOINT,
        idempotency_key=mint_key,
        response_reference=permit.permit_id,
        response_json={"permit_id": permit.permit_id},
        status_code=201,
    )
    return permit.permit_id


def _delivery_uncertain_tool_result(
    exc: GovernedToolError, *, tool_name: str
) -> dict[str, Any]:
    """Build the model-readable result for an uncertain delivery.

    The call was already charged and carries a signed receipt, so the
    outcome is returned as a ``CallToolResult`` with ``isError`` set,
    instead of a JSON-RPC error which reads as retryable. The text tells
    the model not to resend, and the receipt plus outcome metadata give it
    the identifiers needed to reconcile.
    """
    receipt = dict(exc.receipt) if exc.receipt else None
    receipt_id = receipt.get("receipt_id") if receipt else None
    reconcile = (
        f" Reconcile with receipt '{receipt_id}' via GET /v1/receipts "
        "before taking any further action."
        if receipt_id
        else " Reconcile via GET /v1/receipts before taking any further action."
    )
    text = (
        f"Delivery uncertain for tool '{tool_name}': the call may already "
        "have executed and the wallet was charged. Do not resend or retry "
        "this call, with the same or a new idempotency key." + reconcile
    )
    meta: dict[str, Any] = {"outcome": "delivery_uncertain"}
    if receipt is not None:
        meta[_RECEIPT_META_KEY] = receipt
    dispatch = exc.extra_data.get("dispatch")
    if dispatch is not None:
        meta["dispatch"] = dispatch
    payload: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
        "isError": True,
        "_meta": meta,
    }
    if receipt is not None:
        payload["receipt"] = receipt
    return payload


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

    # Flag-gated local tools register lazily; a fresh gateway must not
    # require a prior tools/list before they resolve here.
    _ensure_local_mcp_tools_registered()
    record = get_service_registry().get_local(tool_name)
    if record is None and not permit_id:
        # No permit is minted for a tool that does not exist. With an explicit
        # permit the governed pipeline decides instead: it checks the permit's
        # allowlist first, so an unnamed tool is a -32003 permit denial with a
        # receipt, and only a tool the permit names falls back to -32001.
        raise _mcp_error(-32001, f"Tool not found: {tool_name}")

    if (
        record is not None
        and get_service_registry().get_action_binding(record) is not None
        and not permit_id
    ):
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

    auto_minted = permit_id is None
    # Unkeyed mutating calls get a deterministic retry slot so a lost-response
    # retry resolves to the same auto permit and replays the original receipt
    # (SEL-484). Keyed calls are untouched.
    resolved = None
    if client_idempotency_key is None and permit_id is None:
        # The -32001 above already refused unknown tools without a permit, so
        # record is resolved for this unkeyed auto-mint path (#232 + #233).
        assert record is not None
        resolved = await _resolve_unkeyed_retry(
            auth=auth,
            tool_name=tool_name,
            record=record,
            arguments=arguments,
            requires_human_approval=requires_human_approval,
        )
    if resolved is not None:
        idempotency_key, permit_id = resolved
    else:
        if not client_idempotency_key:
            # No client key and no deterministic retry slot (read-only tool,
            # window 0, or an unverifiable enterprise bearer). A random
            # per-attempt key would let a lost-response retry dispatch and
            # bill twice, so refuse before any permit, dispatch, or charge
            # (SEL-498 / #208).
            raise _mcp_error(
                -32003, "idempotency_key_required", _IDEMPOTENCY_KEY_REQUIRED_DATA
            )
        idempotency_key = client_idempotency_key
        if not permit_id:
            if record is None:  # Checked before any auto-mint.
                raise RuntimeError("auto-permit mint requires a resolved tool record")
            permit_id = await _mint_auto_permit(
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
        result = await _handle_tools_call(
            call_params,
            auth=auth,
            money=get_agent_money(),
            transport="jsonrpc",
            endpoint="/mcp",
            request_id=request_id,
            idempotency_key=idempotency_key,
            request_payload=request_payload,
        )
        if auto_minted and auth.tenant == "demo":
            result["permit_id"] = permit_id
        return result
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
        if str(e) == "delivery_uncertain":
            # An uncertain outcome is already charged and receipted, so it
            # is returned as a model-readable tool result (isError) rather
            # than a JSON-RPC error: an error on that channel reads as
            # retryable, and a retry would risk a second charged dispatch.
            return _delivery_uncertain_tool_result(e, tool_name=tool_name)
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
        text result. The one exception is ``delivery_uncertain``: that
        outcome is already charged and receipted, so it is returned as an
        ``isError`` tool result telling the model not to resend.
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
    validate_browser_origin(request)


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
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["jsonrpc"],
                        "properties": {
                            "jsonrpc": {"type": "string", "const": "2.0"},
                        },
                        "oneOf": [
                            {
                                "title": "JSON-RPC request",
                                "required": ["method", "id"],
                                "not": {
                                    "anyOf": [
                                        {"required": ["result"]},
                                        {"required": ["error"]},
                                    ]
                                },
                                "properties": {
                                    "method": {"type": "string"},
                                    "params": {
                                        "anyOf": [{"type": "object"}, {"type": "null"}]
                                    },
                                    "id": {
                                        "anyOf": [
                                            {"type": "integer"},
                                            {"type": "string"},
                                        ]
                                    },
                                },
                            },
                            {
                                "title": "JSON-RPC notification",
                                "required": ["method"],
                                "not": {
                                    "anyOf": [
                                        {"required": ["id"]},
                                        {"required": ["result"]},
                                        {"required": ["error"]},
                                    ]
                                },
                                "properties": {
                                    "method": {"type": "string"},
                                    "params": {
                                        "anyOf": [{"type": "object"}, {"type": "null"}]
                                    },
                                },
                            },
                            {
                                "title": "JSON-RPC response",
                                "required": ["id"],
                                "not": {"required": ["method"]},
                                "oneOf": [
                                    {"required": ["result"]},
                                    {"required": ["error"]},
                                ],
                                "properties": {
                                    "id": {
                                        "anyOf": [
                                            {"type": "integer"},
                                            {"type": "string"},
                                            {"type": "null"},
                                        ]
                                    },
                                    "result": {},
                                    "error": {"type": "object"},
                                },
                            },
                        ],
                    }
                }
            },
        }
    },
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
