"""
Governed AWI HTTP actions — close the HTTP bypass of the MCP trust spine.

High-risk ``/v1/awi/*`` mutating routes require the same permit + idempotency
headers as governed MCP tools, then meter and receipt the attempt.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from fastapi import Header, HTTPException, status

from app.core.auth import AuthContext
from app.db.models import PermitModel
from app.schemas.billing import LedgerEntry, ServiceCategory
from app.services.agent_money import get_agent_money
from app.services.billing_engine import LedgerWriteContendedError
from app.services.governed_metering import (
    aligned_credits_charged,
)
from app.services.idempotency import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
    IdempotencyReplay,
    InvalidIdempotencyKeyError,
    decode_idempotency_key_header,
    get_idempotency_service,
    resolve_client_idempotency_key,
)
from app.services.permits import PermitError, get_permit_service
from app.services.receipts import get_receipt_service

logger = logging.getLogger(__name__)

# Tool names must match MCP registry ids where a twin exists.
AWI_HTTP_TOOL_CREDITS: dict[str, Decimal] = {
    "awi_passkey_challenge": Decimal("1"),
    "awi_passkey_verify": Decimal("2"),
    "awi_rag_query": Decimal("3"),
    "awi_memory_index": Decimal("5"),
    "awi_execute": Decimal("3"),
    "awi_dom_sync": Decimal("3"),
}


@dataclass
class AwiHttpGovernedContext:
    """Validated permit context for one AWI HTTP action."""

    auth: AuthContext
    wallet_id: str
    permit_id: str
    idempotency_key: str
    tool_name: str
    credits: Decimal
    permit: PermitModel
    endpoint: str
    #: Identity of this request's idempotency record. Used as the ledger
    #: ``operation_key`` so a retried charge is deduplicated by the database
    #: rather than by hoping the first attempt's outcome was observed.
    record_id: str | None = None
    replay_response: dict[str, Any] | None = None
    replay_status_code: int | None = None
    request_payload: dict[str, Any] = field(default_factory=dict)
    ledger_entry_id: str | None = None
    credits_charged: Decimal = Decimal("0")
    dispatch_started: bool = False
    finalization_started: bool = False
    finalized: bool = False


def _credits_for(tool_name: str) -> Decimal:
    return AWI_HTTP_TOOL_CREDITS.get(tool_name, Decimal("1"))


def _request_identity(
    wallet_id: str, permit_id: str, tool_name: str, arguments: dict[str, Any] | None
) -> dict[str, Any]:
    """Bind authority and full normalized semantics; stores retain only the hash."""
    return {
        "wallet_id": wallet_id,
        "permit_id": permit_id,
        "tool_name": tool_name,
        "arguments": arguments or {},
    }


def consume_awi_http_replay(ctx: AwiHttpGovernedContext) -> dict[str, Any] | None:
    """
    Return a prior success body, or re-raise a prior failure status.

    Handlers should call this immediately after ``begin_awi_http_governed``.
    """
    if ctx.replay_response is None:
        return None
    status_code = ctx.replay_status_code or 200
    if status_code >= 400:
        detail = ctx.replay_response.get("detail", ctx.replay_response)
        raise HTTPException(status_code=status_code, detail=detail)
    return ctx.replay_response


def _context_from_validation(
    *,
    auth: AuthContext,
    wallet_id: str,
    permit_id: str,
    idempotency_key: str,
    tool_name: str,
    credits: Decimal,
    permit: PermitModel,
    endpoint: str,
    record_id: str | None = None,
    replay: IdempotencyReplay | None = None,
) -> AwiHttpGovernedContext:
    return AwiHttpGovernedContext(
        auth=auth,
        wallet_id=wallet_id,
        permit_id=permit_id,
        idempotency_key=idempotency_key,
        tool_name=tool_name,
        credits=credits,
        permit=permit,
        endpoint=endpoint,
        record_id=record_id,
        replay_response=replay.response_json if replay else None,
        replay_status_code=replay.status_code if replay else None,
    )


async def begin_awi_http_governed(
    *,
    auth: AuthContext,
    wallet_id: str | None,
    tool_name: str,
    endpoint: str,
    permit_id: str | None,
    idempotency_key_lines: Sequence[str] | None,
    request_payload: dict[str, Any] | None = None,
) -> AwiHttpGovernedContext:
    """
    Validate permit + idempotency for an AWI HTTP action.

    Clients must send ``X-Permit-Id`` and ``Idempotency-Key``.
    ``idempotency_key_lines`` is every ``Idempotency-Key`` line on the
    request: handlers declare the header as ``list[str]`` so FastAPI hands
    over all of them, because a single-value ``Header`` parameter surfaces
    only the first line and a second line naming a different key went unseen.
    """
    if not wallet_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "wallet_required",
                "message": "Governed AWI actions require a wallet-scoped session or X-Wallet-Id.",
            },
        )

    auth.require_wallet_access(wallet_id)

    # Same key contract as the governed MCP routes: every line the client sent
    # must be usable, all lines must name one key, and that key is used
    # verbatim as the replay identity. This path used to store
    # ``idempotency_key.strip()`` (so ``' k'`` and ``'k'`` collapsed into one
    # record) and applied no length cap (a key wider than the store column
    # reached the database after permit validation instead of being refused
    # here). A present-but-unusable key is refused rather than treated as
    # absent: dropping it would turn the caller's retry into a second charged
    # action. It is checked before the permit header, as on the MCP surfaces,
    # so a request that got both wrong learns about the key defect rather
    # than a bare ``permit_required``. No line at all still keeps
    # ``idempotency_key_required`` below, after the permit-presence check.
    try:
        idempotency_key = resolve_client_idempotency_key(
            [
                ("Idempotency-Key header", decode_idempotency_key_header(line))
                for line in idempotency_key_lines or ()
            ]
        )
    except InvalidIdempotencyKeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": str(exc), **exc.as_error_data(), "tool": tool_name},
        ) from exc

    if not permit_id or not permit_id.strip():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "permit_required",
                "message": (
                    "This AWI HTTP route requires a signed permit "
                    "(header X-Permit-Id). Prefer governed MCP tools when available."
                ),
                "tool": tool_name,
            },
        )

    if idempotency_key is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "idempotency_key_required",
                "message": "Governed AWI actions require an Idempotency-Key header.",
                "tool": tool_name,
            },
        )

    credits = _credits_for(tool_name)
    validation = await get_permit_service().validate_replay_access(
        permit_id=permit_id.strip(),
        wallet_id=wallet_id,
        tool_name=tool_name,
        key_id=auth.key_id,
    )
    if not validation.allowed or validation.permit is None:
        detail: dict[str, Any] = {
            "error": validation.reason or "permit_denied",
            "message": validation.reason or "permit_denied",
            "tool": tool_name,
        }
        if validation.details:
            detail["details"] = validation.details
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=detail,
        )

    idem = get_idempotency_service()
    try:
        begun = await idem.begin_with_record(
            wallet_id=wallet_id,
            endpoint=endpoint,
            idempotency_key=idempotency_key,
            request_payload=_request_identity(
                wallet_id, permit_id.strip(), tool_name, request_payload
            ),
            operation_kind="awi_http",
        )
        replay = begun.replay
        record_id = begun.record_id
    except (IdempotencyConflictError, IdempotencyInProgressError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": str(exc), "message": str(exc)},
        ) from exc

    if replay and replay.response_json:
        return _context_from_validation(
            auth=auth,
            wallet_id=wallet_id,
            permit_id=permit_id.strip(),
            idempotency_key=idempotency_key,
            tool_name=tool_name,
            credits=credits,
            permit=validation.permit,
            endpoint=endpoint,
            record_id=record_id,
            replay=replay,
        )

    ctx = _context_from_validation(
        auth=auth,
        wallet_id=wallet_id,
        permit_id=permit_id.strip(),
        idempotency_key=idempotency_key,
        tool_name=tool_name,
        credits=credits,
        permit=validation.permit,
        endpoint=endpoint,
        record_id=record_id,
    )

    ctx.request_payload = request_payload or {}
    validation = await get_permit_service().validate_for_action(
        permit_id=ctx.permit_id,
        wallet_id=wallet_id,
        tool_name=tool_name,
        estimated_credits=credits,
        key_id=auth.key_id,
        arguments=request_payload,
    )
    if not validation.allowed:
        await raise_awi_http_error(
            ctx,
            status_code=403,
            detail={"error": validation.reason or "permit_denied", "tool": tool_name},
        )
    # Preserve the frozen surface's refusal of unsupported call-limited authority.
    call_limits = json.loads(ctx.permit.max_calls_per_tool_json or "{}")
    if tool_name in call_limits:
        await raise_awi_http_error(
            ctx,
            status_code=403,
            detail={"error": "awi_call_limit_unsupported", "tool": tool_name},
        )
    await _admit_awi_http_governed(ctx)
    return ctx


def _stable_awi_failure_reason(action_status: str, error: Any) -> str:
    """Collapse a typed failure to a receipt-safe reason code.

    A status of "error" whose error text reads "dom_bridge_failed: <detail>"
    yields the reason code dom_bridge_failed (the prefix before the first
    colon); other statuses (passkey_required, paused, ...) name themselves.
    The result is sanitized to the receipt service's reason-code pattern
    rather than raising on adversarial detail text.
    """
    candidate = action_status
    if action_status == "error" and isinstance(error, str) and error:
        candidate = error.split(":", 1)[0]
    cleaned = re.sub(r"[^a-z0-9_.:-]", "_", candidate.strip().lower())
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"awi_{cleaned}" if cleaned else "awi_failed"
    return cleaned[:128]


async def _admit_awi_http_governed(ctx: AwiHttpGovernedContext) -> None:
    """Reserve and checkpoint the debit before a caller can enter its callback.

    Unknown commits keep their owner and reservation for operator review. Only
    a definitive, effect-free contention can release the key for another try.
    """
    from app.services.agent_money import DEFAULT_PRICING, InsufficientFundsResponse

    permits = get_permit_service()
    idem = get_idempotency_service()
    try:
        await permits.reserve_budget(ctx.permit_id, ctx.credits)
    except PermitError as exc:
        detail = {"error": exc.reason, "tool": ctx.tool_name}
        if exc.reason == "permit_write_contended":
            await idem.abandon(
                wallet_id=ctx.wallet_id,
                endpoint=ctx.endpoint,
                idempotency_key=ctx.idempotency_key,
                expected_record_id=ctx.record_id,
            )
            raise HTTPException(status_code=503, detail=detail) from exc
        await raise_awi_http_error(ctx, status_code=403, detail=detail)
    except Exception as exc:
        # A lost reservation acknowledgment may already have consumed budget.
        raise HTTPException(
            status_code=503,
            detail={"error": "awi_admission_incomplete", "tool": ctx.tool_name},
        ) from exc

    unit_price = DEFAULT_PRICING[ServiceCategory.AGENT_COMMS][1]
    try:
        charge = await get_agent_money().charge(
            wallet_id=ctx.wallet_id,
            service_category=ServiceCategory.AGENT_COMMS,
            units=ctx.credits / unit_price,
            request_path=ctx.endpoint,
            description=f"AWI HTTP {ctx.tool_name}",
            operation_key=ctx.record_id,
        )
    except LedgerWriteContendedError as exc:
        # Billing's contract proves every attempt rolled back (including its
        # final operation-key lookup). No callback has started yet.
        if await _release_reservation(permits, ctx):
            await idem.abandon(
                wallet_id=ctx.wallet_id,
                endpoint=ctx.endpoint,
                idempotency_key=ctx.idempotency_key,
                expected_record_id=ctx.record_id,
            )
        raise HTTPException(
            status_code=503,
            detail={"error": "ledger_write_contended", "tool": ctx.tool_name},
        ) from exc
    except Exception as exc:
        # Commit/acknowledgment loss is not evidence that money did not move.
        # Preserve the operation-key link; never release or redispatch here.
        raise HTTPException(
            status_code=503,
            detail={"error": "awi_admission_incomplete", "tool": ctx.tool_name},
        ) from exc

    if isinstance(charge, InsufficientFundsResponse):
        await _release_reservation(permits, ctx)
        await raise_awi_http_error(
            ctx,
            status_code=402,
            detail={"error": "insufficient_funds", "tool": ctx.tool_name},
        )
        return

    # Require an actual debit and persist its ownership before any effect.
    # Even a mismatch keeps the reservation and key; it requires review.
    try:
        if not isinstance(charge, LedgerEntry) or not charge.entry_id:
            raise ValueError("awi_debit_required")
        ctx.ledger_entry_id = charge.entry_id
        await idem.mark_charged(
            wallet_id=ctx.wallet_id,
            endpoint=ctx.endpoint,
            idempotency_key=ctx.idempotency_key,
            ledger_entry_id=ctx.ledger_entry_id,
        )
        ctx.credits_charged = aligned_credits_charged(
            ledger_amount=charge.amount,
            authorized_credits=ctx.credits,
            context=f"awi_http:{ctx.tool_name}",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "awi_admission_incomplete", "tool": ctx.tool_name},
        ) from exc


async def _finalize_awi_http(
    ctx: AwiHttpGovernedContext,
    *,
    response_payload: dict[str, Any],
    status_code: int,
    outcome: str,
    reason_code: str | None,
) -> dict[str, Any]:
    """Retry receipt/completion writes without ever retrying the callback."""
    ctx.finalization_started = True
    for attempt in range(3):
        try:
            receipt = await get_receipt_service().create_receipt(
                permit_id=ctx.permit_id,
                wallet_id=ctx.wallet_id,
                key_id=ctx.auth.key_id,
                tool=ctx.tool_name,
                request_payload=_request_identity(
                    ctx.wallet_id, ctx.permit_id, ctx.tool_name, ctx.request_payload
                ),
                response_payload=response_payload,
                ledger_entry_id=ctx.ledger_entry_id,
                credits_authorized=ctx.credits,
                credits_charged=ctx.credits_charged,
                outcome=outcome,
                reason_code=reason_code,
                audit_event_id=None,
                idempotency_record_id=ctx.record_id,
            )
            body = {
                **response_payload,
                "receipt": {
                    "receipt_id": receipt.receipt_id,
                    "permit_id": receipt.permit_id,
                    "ledger_entry_id": receipt.ledger_entry_id,
                    "outcome": receipt.outcome,
                    "signature": receipt.signature,
                },
            }
            await get_idempotency_service().complete(
                wallet_id=ctx.wallet_id,
                endpoint=ctx.endpoint,
                idempotency_key=ctx.idempotency_key,
                response_reference=receipt.receipt_id,
                response_json={"detail": body} if status_code >= 400 else body,
                status_code=status_code,
            )
            ctx.finalized = True
            return body
        except Exception:
            if attempt == 2:
                # Do not overwrite a committed receipt with a different error
                # outcome. The charged owner remains held for reconciliation.
                raise HTTPException(
                    status_code=503, detail={"error": "awi_finalization_incomplete"}
                ) from None
            await asyncio.sleep(0.05 * (attempt + 1))
    raise AssertionError("unreachable")


async def _refund_undispatched(ctx: AwiHttpGovernedContext) -> None:
    """Compensate only a trusted, explicit proof that no action was dispatched."""
    if ctx.ledger_entry_id is None:
        return
    # Set before compensation: an uncertain refund/release must never fall into
    # a second abort that repeats release_budget against another invocation.
    ctx.finalization_started = True
    await get_agent_money().refund_charge(
        wallet_id=ctx.wallet_id,
        charge_entry_id=ctx.ledger_entry_id,
        description=f"AWI HTTP {ctx.tool_name}: not dispatched",
    )
    ctx.credits_charged = Decimal("0")
    await _release_reservation(get_permit_service(), ctx)


async def complete_awi_http_governed(
    ctx: AwiHttpGovernedContext,
    *,
    request_payload: dict[str, Any],
    response_payload: dict[str, Any],
) -> dict[str, Any]:
    """Receipt an admitted callback's result; never charge or dispatch here."""
    if ctx.replay_response is not None:
        return ctx.replay_response
    action_status = response_payload.get("status")
    failed = action_status is not None and action_status != "success"
    reason = None
    outcome = "success"
    if failed:
        reason = _stable_awi_failure_reason(
            str(action_status), response_payload.get("error")
        )
        if response_payload.get("effect_status") == "not_dispatched":
            await _refund_undispatched(ctx)
            outcome = "failed_refunded"
        else:
            # Zero completed commands and thrown exceptions do not prove that
            # the first command had no effect. Keep the debit and reservation.
            outcome = "delivery_uncertain"
    return await _finalize_awi_http(
        ctx,
        response_payload=response_payload,
        status_code=200,
        outcome=outcome,
        reason_code=reason,
    )


async def _release_reservation(permits: Any, ctx: AwiHttpGovernedContext) -> bool:
    try:
        await permits.release_budget(ctx.permit_id, ctx.credits)
        return True
    except Exception:
        logger.exception(
            "awi_governed_release_budget_failed", extra={"permit_id": ctx.permit_id}
        )
        await permits.record_absorbed_release_drift(
            permit_id=ctx.permit_id,
            amount=ctx.credits,
            site="awi_http",
        )
        return False


async def raise_awi_http_error(
    ctx: AwiHttpGovernedContext,
    *,
    status_code: int,
    detail: dict[str, Any],
) -> None:
    """Receipt a rejection or uncertain callback error without losing its owner."""
    if ctx.replay_response is not None or ctx.finalized:
        raise HTTPException(status_code=status_code, detail=detail)
    if ctx.finalization_started:
        raise HTTPException(
            status_code=503, detail={"error": "awi_finalization_incomplete"}
        )
    if ctx.dispatch_started:
        outcome = "delivery_uncertain"
    elif ctx.ledger_entry_id:
        await _refund_undispatched(ctx)
        outcome = "failed_refunded"
    else:
        outcome = "insufficient_funds" if status_code == 402 else "denied"
    body = await _finalize_awi_http(
        ctx,
        response_payload=detail,
        status_code=status_code,
        outcome=outcome,
        reason_code=_stable_awi_failure_reason("error", detail.get("error")),
    )
    raise HTTPException(status_code=status_code, detail=body)


def parse_governed_headers(
    x_permit_id: str | None = Header(None, alias="X-Permit-Id"),
    idempotency_key_lines: list[str] | None = Header(None, alias="Idempotency-Key"),
    x_wallet_id: str | None = Header(None, alias="X-Wallet-Id"),
) -> dict[str, str | list[str] | None]:
    """Optional FastAPI dependency returning governance headers.

    ``idempotency_key_lines`` carries every ``Idempotency-Key`` line, in the
    shape :func:`begin_awi_http_governed` expects.
    """
    return {
        "permit_id": x_permit_id,
        "idempotency_key_lines": idempotency_key_lines,
        "wallet_id": x_wallet_id,
    }
