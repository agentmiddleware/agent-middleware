"""Crash-safe reconciliation for governed upstream MCP dispatch attempts."""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from types import FunctionType, SimpleNamespace
from typing import Any, cast

from mcp.client.session import ClientSession
from mcp.types import CallToolResult
from sqlalchemy import select
from sqlalchemy.sql.elements import ColumnElement

from app.core.config import DuplicateGuardMode, get_settings
from app.core.time import to_naive_utc, utc_now
from app.db.database import get_session_factory
from app.db.models import (
    ControlPlaneAuditEventModel,
    LedgerEntryModel,
    McpDispatchAttemptModel,
    PermitModel,
)
from app.schemas.trust import ReceiptResponse
from app.services.agent_money import AgentMoney, get_agent_money
from app.services.audit_log import AuditEvent, record_audit_event
from app.services.jev_guard_metadata import jev_audit_id, load_jev_guard_metadata
from app.services.idempotency import (
    ACTION_MCP_IDEMPOTENCY_ENDPOINT,
    IdempotencyService,
    get_idempotency_service,
)
from app.services.ownership import CallerIdentity
from app.services.mcp_dispatch_attempts import (
    DISPATCH_CLAIMED,
    DISPATCH_SENT_STATES,
    DISPATCH_PREPARED,
    DISPATCH_TERMINAL_STATES,
    DispatchAttemptContext,
    DispatchAttemptConflictError,
    DispatchAttemptError,
    DispatchResultRejectedError,
    McpDispatchAttemptService,
    get_mcp_dispatch_attempt_service,
)
from app.services.permits import (
    PermitService,
    get_permit_service,
    permit_constraints_snapshot,
)
from app.services.receipts import ReceiptError, ReceiptService, get_receipt_service
from app.services.service_registry import get_service_registry
from app.services.signing_keys import canonical_json, sha256_hex
from app.services.upstream_mcp import UpstreamMcpAdapter

logger = logging.getLogger(__name__)
_PERMIT_SNAPSHOT_KEYS = frozenset(
    {
        "max_calls_per_tool",
        "aggregate_value_cap",
        "forbidden_fields",
        "recipient_domain",
    }
)
_LEGACY_UNSNAPSHOTTED_OUTCOMES = frozenset(
    {"success", "failed_refunded", "delivery_uncertain", "response_rejected"}
)
# Per-lookup bound on awaiting a status lookup. A hang falls back to
# delivery_uncertain. Lookups run one after another, so a sweep can spend up
# to limit * this value awaiting them. The bound cannot interrupt synchronous
# work, which is why only coroutine functions are accepted as lookups.
STATUS_LOOKUP_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class UpstreamDispatchStatusQuery:
    """Read-only identity of one stale claimed dispatch.

    ``invocation_id`` is the value the gateway forwarded upstream as
    ``io.agentmiddleware/invocation_id``. ``idempotency_key`` is the key stored
    on the gateway's idempotency record. ``forwarded_idempotency_key`` is the
    value actually sent as ``io.agentmiddleware/idempotency_key``. It is None
    for action-permit calls: those forward a derived native key that the
    attempt row does not record, so match on ``invocation_id`` instead.
    """

    attempt_id: str
    invocation_id: str
    idempotency_key: str
    forwarded_idempotency_key: str | None
    upstream_origin: str
    upstream_tool_name: str
    request_hash: str
    dispatched_at: datetime | None


@dataclass(frozen=True)
class UpstreamDispatchStatus:
    """Authoritative upstream answer for one stale claimed dispatch.

    ``delivered`` must be a real ``bool``. ``delivered=True`` requires the raw
    MCP ``CallToolResult`` payload, which goes through the live path's result
    checks: a normal result finalizes as ``succeeded`` (the charge stands), and
    an ``isError`` result finalizes as ``returned_error`` and is refunded,
    exactly as on the live path. ``delivered=False`` is logged but does not
    change the outcome: the attempt stays ``delivery_uncertain`` (see
    ``_proven_outcome``).

    The reflected-credential check compares against the binding's current
    bearer token only. The attempt does not record which token was used at
    dispatch, so a payload echoing an older, rotated token is not caught here.
    A lookup must not return provider data that can contain credentials.
    """

    delivered: bool
    result_payload: dict[str, Any] | None = None


# A status lookup may only read upstream state. Returning None, raising,
# timing out, or returning a malformed answer means "not proven either way"
# and leaves the attempt delivery_uncertain.
UpstreamDispatchStatusLookup = Callable[
    [UpstreamDispatchStatusQuery], Awaitable[UpstreamDispatchStatus | None]
]

# Process-wide lookups keyed by upstream origin. The gateway registers none,
# so this is inert until a deployment calls register_upstream_status_lookup()
# from its own startup code. There is deliberately no environment setting.
_REGISTERED_STATUS_LOOKUPS: dict[str, UpstreamDispatchStatusLookup] = {}


def _require_async_lookup(lookup: object) -> None:
    """Reject lookups whose body would run synchronously on the event loop.

    ``asyncio.wait_for`` can only bound awaiting. A plain function runs to
    completion before the timeout starts, so a blocking one stalls the loop.
    Accepts ``async def`` functions, bound async methods, objects with an
    ``async def __call__``, and ``functools.partial`` wrappers of these.
    Checks the real code object's ``CO_COROUTINE`` flag instead of
    ``inspect.iscoroutinefunction``, which trusts inspect.markcoroutinefunction
    and so would accept a marked synchronous function.
    """
    target = lookup
    # Bounded: builtins' type(x).__call__ chains never reach a FunctionType.
    for _ in range(16):
        if isinstance(target, functools.partial):
            target = target.func
        elif inspect.ismethod(target):
            target = target.__func__
        elif isinstance(target, FunctionType) or isinstance(target, type):
            break
        elif callable(target):
            target = type(target).__call__
        else:
            break
    if not (
        isinstance(target, FunctionType)
        and target.__code__.co_flags & inspect.CO_COROUTINE
    ):
        raise TypeError("status lookup must be an async callable")


def register_upstream_status_lookup(
    upstream_origin: str,
    lookup: UpstreamDispatchStatusLookup,
) -> None:
    """Register the read-only status lookup for one upstream origin.

    ``upstream_origin`` must equal the stored attempt origin exactly
    (scheme://host[:port], no path). The reconciliation singleton reads this
    registry on every sweep, so registering before or after it was created
    both work. Registering an origin again replaces its lookup.

    Token rotation: result redaction only compares against the bearer token
    current at reconciliation time. A recorded result that echoes a token
    from before a rotation is not caught, so rotate tokens only after any
    attempts dispatched with the old token have been reconciled.
    """
    _require_async_lookup(lookup)
    _REGISTERED_STATUS_LOOKUPS[upstream_origin] = lookup


@dataclass(frozen=True)
class DispatchReconciliationResult:
    """Sanitized result of one bounded reconciliation sweep."""

    prepared_finalized: int
    dispatched_uncertain: int
    terminal_recovered: int
    idempotency_recovered: int
    budget_released: int
    failed_attempt_ids: tuple[str, ...]
    status_resolved: int = 0

    @property
    def repaired(self) -> int:
        return (
            self.prepared_finalized
            + self.dispatched_uncertain
            + self.terminal_recovered
            + self.idempotency_recovered
            + self.budget_released
            + self.status_resolved
        )


class McpDispatchReconciliationService:
    """Finalize crash-orphaned dispatch rows without ever redispatching them."""

    def __init__(
        self,
        *,
        dispatch_service: McpDispatchAttemptService | None = None,
        money: AgentMoney | None = None,
        permit_service: PermitService | None = None,
        receipt_service: ReceiptService | None = None,
        idempotency_service: IdempotencyService | None = None,
        max_result_bytes: int | None = None,
        status_lookups: Mapping[str, UpstreamDispatchStatusLookup] | None = None,
        status_lookup_timeout_seconds: float = STATUS_LOOKUP_TIMEOUT_SECONDS,
    ) -> None:
        # Optional, keyed by upstream_origin. None means "use the process-wide
        # registry". An origin without a lookup keeps the conservative
        # stale-claim -> delivery_uncertain behaviour.
        for lookup in (status_lookups or {}).values():
            _require_async_lookup(lookup)
        self._status_lookups = (
            dict(status_lookups) if status_lookups is not None else None
        )
        self._status_lookup_timeout = status_lookup_timeout_seconds
        self._dispatch = dispatch_service or get_mcp_dispatch_attempt_service()
        self._money = money or get_agent_money()
        self._permits = permit_service or get_permit_service()
        self._receipts = receipt_service or get_receipt_service()
        self._idempotency = idempotency_service or get_idempotency_service()
        self._max_result_bytes = (
            max_result_bytes
            if max_result_bytes is not None
            else get_settings().MCP_UPSTREAM_MAX_RESPONSE_BYTES
        )

    async def reconcile(
        self,
        *,
        idle_seconds: int = 300,
        terminal_idle_seconds: int | None = None,
        limit: int = 100,
    ) -> DispatchReconciliationResult:
        """Repair a bounded batch of stale and unfinalized attempts.

        Active rows are terminalized first and finalized in the same sweep.
        Terminal rows are never sent back to an upstream server, so callers
        may give them a shorter repair grace than a possibly live claim.
        """
        terminal_idle = (
            idle_seconds if terminal_idle_seconds is None else terminal_idle_seconds
        )
        prepared_finalized = 0
        dispatched_uncertain = 0
        terminal_recovered = 0
        idempotency_recovered = 0
        budget_released = 0
        status_resolved = 0
        failed: list[str] = []
        processed: set[str] = set()

        stale = await self._dispatch.list_stale_contexts(
            idle_seconds=idle_seconds,
            limit=limit,
        )
        for context in stale:
            attempt_id = context.attempt.attempt_id
            processed.add(attempt_id)
            try:
                outcome = await self._reconcile_active(context)
                if outcome == "prepared_finalized":
                    prepared_finalized += 1
                elif outcome == "dispatched_uncertain":
                    dispatched_uncertain += 1
                elif outcome == "terminal_recovered":
                    terminal_recovered += 1
                elif outcome == "status_resolved":
                    status_resolved += 1
            except Exception as exc:
                failed.append(attempt_id)
                logger.warning(
                    "mcp_dispatch_reconciliation_failed attempt_id=%s state=%s error=%s",
                    attempt_id,
                    context.attempt.state,
                    type(exc).__name__,
                )

        incomplete = await self._dispatch.list_idempotency_incomplete_terminal_contexts(
            idle_seconds=terminal_idle,
            limit=limit,
        )
        for context in incomplete:
            attempt_id = context.attempt.attempt_id
            if attempt_id in processed:
                continue
            try:
                await self._finalize_terminal(context)
                idempotency_recovered += 1
            except Exception as exc:
                failed.append(attempt_id)
                logger.warning(
                    "mcp_dispatch_idempotency_recovery_failed "
                    "attempt_id=%s state=%s error=%s",
                    attempt_id,
                    context.attempt.state,
                    type(exc).__name__,
                )

        terminal = await self._dispatch.list_unfinalized_terminal_contexts(
            idle_seconds=terminal_idle,
            limit=limit,
        )
        for context in terminal:
            attempt_id = context.attempt.attempt_id
            if attempt_id in processed:
                continue
            try:
                await self._finalize_terminal(context)
                terminal_recovered += 1
            except Exception as exc:
                failed.append(attempt_id)
                logger.warning(
                    "mcp_dispatch_terminal_recovery_failed attempt_id=%s state=%s error=%s",
                    attempt_id,
                    context.attempt.state,
                    type(exc).__name__,
                )

        # Reservations the live path could not give back. A contended
        # release on the upstream failure path is absorbed there rather than
        # propagated -- propagating would skip the audit event and the receipt
        # for a call that really was dispatched -- so finishing it is this
        # sweep's job. Nothing else picks these up: the attempt is terminal,
        # so it is not stale-active, and the live path completed its
        # idempotency record, so the two queries above both skip it.
        #
        # release_dispatch_budget_once is the once-only gate on every engine,
        # so re-running it against an attempt that raced to release in the
        # meantime returns False without touching the permit.
        unreleased = await self._dispatch.list_unreleased_budget_attempts(
            idle_seconds=terminal_idle,
            limit=limit,
        )
        for attempt in unreleased:
            if attempt.attempt_id in processed:
                continue
            # Release only against durable proof that the money came back, or
            # that it never left. The query cannot express this: a
            # returned_error attempt whose refund failed earlier in this very
            # sweep still matches `budget_released_at IS NULL`, and handing
            # its reservation back while the debit stands would cut
            # spent_credits below what the wallet actually paid -- letting the
            # next call spend past max_credits. That over-spend is the
            # opposite failure from the stranded reservation this sweep exists
            # to repair, and the worse of the two.
            #
            # A missing operation debit means the call was refused before it
            # was ever charged (the pre-dispatch insufficient-funds path),
            # which still holds a reservation and is safe to give back.
            # Anything with a live, unrefunded debit belongs to the
            # compensation path, not here.
            if attempt.debit_refunded_at is None:
                if await self._find_operation_debit(attempt) is not None:
                    continue
            try:
                if await self._permits.release_dispatch_budget_once(
                    attempt.attempt_id,
                    caller=CallerIdentity.for_wallet(attempt.wallet_id),
                ):
                    budget_released += 1
            except Exception as exc:
                failed.append(attempt.attempt_id)
                logger.warning(
                    "mcp_dispatch_budget_release_recovery_failed "
                    "attempt_id=%s error=%s",
                    attempt.attempt_id,
                    type(exc).__name__,
                )

        return DispatchReconciliationResult(
            prepared_finalized=prepared_finalized,
            dispatched_uncertain=dispatched_uncertain,
            terminal_recovered=terminal_recovered,
            idempotency_recovered=idempotency_recovered,
            budget_released=budget_released,
            failed_attempt_ids=tuple(dict.fromkeys(failed)),
            status_resolved=status_resolved,
        )

    async def reconcile_attempt(
        self,
        attempt_id: str,
        *,
        prepared_error_code: str = "upstream_pre_dispatch_failed",
        idle_seconds: int | None = None,
    ) -> None:
        """Immediately finalize one known failed checkpoint without redispatch.

        This is used when charging or checkpointing raises before the upstream
        call. It performs the same operation-key debit inspection and
        compensation as the periodic sweep, but does not leave a deterministic
        failure unsigned for five minutes. When ``idle_seconds`` is provided,
        active attempts are repaired only after that idle window; this allows
        retry-triggered recovery without stealing a live dispatch owner.
        """
        if idle_seconds is not None and idle_seconds < 0:
            raise DispatchAttemptError("dispatch_reconciliation_query_invalid")
        context = await self._required_context(attempt_id)
        attempt = context.attempt
        if idle_seconds is not None and (
            attempt.state == DISPATCH_PREPARED or attempt.state in DISPATCH_SENT_STATES
        ):
            cutoff = to_naive_utc(utc_now() - timedelta(seconds=idle_seconds))
            if to_naive_utc(attempt.updated_at) >= cutoff:
                return
            await self._reconcile_active(context)
            return
        if attempt.state == DISPATCH_PREPARED:
            terminal = await self._complete_prepared_failure(
                attempt,
                error_code=prepared_error_code,
            )
            if terminal is None:
                return
            await self._finalize_terminal(terminal)
            return
        elif attempt.state in DISPATCH_SENT_STATES:
            # A durable claim is fresh until the normal stale window proves
            # its owner stopped making progress. Targeted pre-dispatch repair
            # must not race that live owner or manufacture ambiguity.
            return
        elif attempt.state not in DISPATCH_TERMINAL_STATES:
            raise DispatchAttemptError("dispatch_reconciliation_state_invalid")
        await self._finalize_terminal(context)

    async def _reconcile_active(
        self,
        context: DispatchAttemptContext,
    ) -> str | None:
        fresh = await self._required_context(context.attempt.attempt_id)
        attempt = fresh.attempt
        if attempt.state in DISPATCH_TERMINAL_STATES:
            await self._finalize_terminal(fresh)
            return "terminal_recovered"

        # The stale scan is only a snapshot. Any active-state or timestamp
        # change after selection is fresh progress, so this sweep yields. The
        # next sweep may act only after the normal idle window elapses again.
        if (
            attempt.state != context.attempt.state
            or attempt.updated_at != context.attempt.updated_at
        ):
            return None

        if attempt.state == DISPATCH_PREPARED:
            terminal = await self._complete_prepared_failure(
                attempt,
                error_code="reconciled_stale_prepared",
            )
            if terminal is None:
                return None
            await self._finalize_terminal(terminal)
            return "prepared_finalized"
        elif attempt.state in DISPATCH_SENT_STATES:
            resolved = await self._lookup_status(fresh)
            if resolved is not None:
                try:
                    await self._dispatch.complete(
                        attempt_id=attempt.attempt_id,
                        max_result_bytes=self._max_result_bytes,
                        **resolved,
                    )
                except DispatchResultRejectedError:
                    # An unstorable answer is not proof; fall back below.
                    logger.warning(
                        "mcp_dispatch_status_lookup_result_rejected attempt_id=%s",
                        attempt.attempt_id,
                    )
                except DispatchAttemptConflictError:
                    raced = await self._required_context(attempt.attempt_id)
                    if raced.attempt.state not in DISPATCH_TERMINAL_STATES:
                        raise
                    await self._finalize_terminal(raced)
                    return "terminal_recovered"
                else:
                    terminal = await self._required_context(attempt.attempt_id)
                    await self._finalize_terminal(terminal)
                    return "status_resolved"
            try:
                await self._dispatch.complete(
                    attempt_id=attempt.attempt_id,
                    state="delivery_uncertain",
                    result_payload={"error": "delivery_uncertain"},
                    error_code="delivery_uncertain",
                    max_result_bytes=self._max_result_bytes,
                )
            except DispatchAttemptConflictError:
                raced = await self._required_context(attempt.attempt_id)
                if raced.attempt.state not in DISPATCH_TERMINAL_STATES:
                    raise
                await self._finalize_terminal(raced)
                return "terminal_recovered"
            terminal = await self._required_context(attempt.attempt_id)
            await self._finalize_terminal(terminal)
            return "dispatched_uncertain"
        else:
            raise DispatchAttemptError("dispatch_reconciliation_state_invalid")

    async def _lookup_status(
        self,
        context: DispatchAttemptContext,
    ) -> dict[str, Any] | None:
        """Ask the optional per-upstream hook how a stale claim ended.

        Returns ``complete()`` keyword arguments for a proven outcome, or None
        when the attempt must stay delivery_uncertain. The gateway never
        resends here and hands the hook only an immutable query, not the
        executor. The hook itself is operator code, so what it calls is the
        operator's responsibility. Legacy ``dispatched`` rows are skipped: ``complete()`` only lets them
        become delivery_uncertain.
        """
        attempt = context.attempt
        lookups = (
            self._status_lookups
            if self._status_lookups is not None
            else _REGISTERED_STATUS_LOOKUPS
        )
        lookup = lookups.get(attempt.upstream_origin)
        if lookup is None or attempt.state != DISPATCH_CLAIMED:
            return None
        # Non-action calls forward the stored key unchanged. Action calls
        # forward a derived native key that is not persisted on the attempt.
        forwarded_key = (
            None
            if context.endpoint == ACTION_MCP_IDEMPOTENCY_ENDPOINT
            else context.idempotency_key
        )
        query = UpstreamDispatchStatusQuery(
            attempt_id=attempt.attempt_id,
            invocation_id=attempt.idempotency_record_id,
            idempotency_key=context.idempotency_key,
            forwarded_idempotency_key=forwarded_key,
            upstream_origin=attempt.upstream_origin,
            upstream_tool_name=attempt.upstream_tool_name,
            request_hash=attempt.request_hash,
            dispatched_at=attempt.dispatched_at,
        )

        async def _call_lookup() -> object:
            # The call itself runs inside wait_for, so a sync function that
            # only claims to be async (markcoroutinefunction) cannot run
            # before the timeout scope or return a bare value as an answer.
            pending = lookup(query)
            if not inspect.isawaitable(pending):
                raise DispatchAttemptError("dispatch_status_lookup_not_awaitable")
            return await pending

        try:
            status = await asyncio.wait_for(
                _call_lookup(), timeout=self._status_lookup_timeout
            )
            return await self._proven_outcome(attempt, status)
        except Exception as exc:
            # Includes TimeoutError and any malformed answer: not proof.
            logger.warning(
                "mcp_dispatch_status_lookup_failed attempt_id=%s error=%s",
                attempt.attempt_id,
                type(exc).__name__,
            )
            return None

    @staticmethod
    async def _proven_outcome(
        attempt: McpDispatchAttemptModel,
        status: object,
    ) -> dict[str, Any] | None:
        """Map a lookup answer to ``complete()`` kwargs, or None for uncertain.

        Raises on a malformed answer; the caller treats that as not proven.
        """
        if status is None:
            return None
        if not isinstance(status, UpstreamDispatchStatus):
            raise DispatchAttemptError("dispatch_status_lookup_answer_invalid")
        if type(status.delivered) is not bool:
            raise DispatchAttemptError("dispatch_status_lookup_answer_invalid")
        if not status.delivered:
            # Not proof of non-delivery. The claim is a one-shot hash with no
            # lease or fence, so a paused worker that already holds it can
            # still resume and call call_tool() after the provider answered.
            # The guarded complete() would protect the row, not the send.
            # Refunding here could pay back a call that then executes, so the
            # attempt stays delivery_uncertain for manual reconciliation.
            logger.warning(
                "mcp_dispatch_status_lookup_not_delivered_unfenced attempt_id=%s",
                attempt.attempt_id,
            )
            return None
        if not isinstance(status.result_payload, dict):
            raise DispatchAttemptError("dispatch_status_lookup_result_missing")
        # Reuse the live path's own result gate (shape, isError, reflected
        # bearer token, size bound) through the executor for this exact
        # binding. Without it the answer cannot be checked, so stay uncertain.
        executor = get_service_registry().get_executor(attempt.public_tool_id)
        if (
            not isinstance(executor, UpstreamMcpAdapter)
            or executor.configuration.origin != attempt.upstream_origin
            or executor.configuration.tool_name != attempt.upstream_tool_name
        ):
            raise DispatchAttemptError("dispatch_status_lookup_executor_unavailable")
        # The live path refuses to call an undiscovered tool, so there is no
        # output schema to check against: not proof.
        discovered = executor.discovered_tool
        if discovered is None:
            raise DispatchAttemptError("dispatch_status_lookup_executor_unavailable")
        call_result = CallToolResult.model_validate(status.result_payload)
        if not call_result.isError:
            # The live path seeds ClientSession._tool_output_schemas with the
            # discovered schema and lets the SDK validate structuredContent
            # inside call_tool(); a failure there ends delivery_uncertain.
            # Run that same SDK validator on the same seed. The name is
            # present, so it never lists tools; a None schema skips the
            # check exactly as it does live.
            seeded = SimpleNamespace(
                _tool_output_schemas={
                    attempt.upstream_tool_name: deepcopy(discovered.output_schema)
                }
            )
            await ClientSession._validate_tool_result(
                cast(ClientSession, seeded), attempt.upstream_tool_name, call_result
            )
        result = executor._canonicalize_result(call_result)
        if result.is_error:
            # Same terminal shape as the live UpstreamMcpReturnedError path,
            # which _finalize_terminal refunds through compensation.
            return {
                "state": "returned_error",
                "result_payload": result.payload,
                "error_code": "upstream_returned_error",
            }
        return {
            "state": "succeeded",
            "result_payload": result.payload,
            "error_code": None,
        }

    async def _complete_prepared_failure(
        self,
        attempt: McpDispatchAttemptModel,
        *,
        error_code: str,
    ) -> DispatchAttemptContext | None:
        debit = await self._find_operation_debit(attempt)
        try:
            await self._dispatch.complete_pre_dispatch_failure(
                attempt_id=attempt.attempt_id,
                expected_updated_at=attempt.updated_at,
                ledger_entry_id=(debit.entry_id if debit is not None else None),
                credits_charged=(abs(debit.amount) if debit is not None else None),
                result_payload={
                    "error": "failed_refunded",
                    "error_code": error_code,
                },
                error_code=error_code,
                max_result_bytes=self._max_result_bytes,
            )
        except DispatchAttemptConflictError:
            raced = await self._required_context(attempt.attempt_id)
            if raced.attempt.state in DISPATCH_SENT_STATES:
                return None
            if raced.attempt.state in DISPATCH_TERMINAL_STATES:
                return raced
            if (
                raced.attempt.state == DISPATCH_PREPARED
                and raced.attempt.updated_at != attempt.updated_at
            ):
                return None
            raise
        return await self._required_context(attempt.attempt_id)

    async def _finalize_terminal(self, context: DispatchAttemptContext) -> None:
        context = await self._required_context(context.attempt.attempt_id)
        attempt = context.attempt
        if attempt.state not in DISPATCH_TERMINAL_STATES:
            raise DispatchAttemptError("dispatch_reconciliation_state_invalid")

        if attempt.state == "returned_error":
            await self._compensate_returned_error(attempt)
            context = await self._required_context(attempt.attempt_id)
            attempt = context.attempt

        # Fetch permit constraints for the receipt snapshot
        constraints_evaluated = await self._permit_constraints_snapshot(
            attempt.permit_id
        )

        result_payload = self._result_payload(attempt)
        outcome = self._receipt_outcome(attempt.state)
        audit = await self._get_or_create_audit(context)
        receipt = await self._get_or_create_receipt(
            attempt=attempt,
            result_payload=result_payload,
            outcome=outcome,
            audit_event_id=audit.event_id,
            constraints_evaluated=constraints_evaluated,
        )
        response, status_code = self._replay_response(
            attempt=attempt,
            result_payload=result_payload,
            receipt=receipt,
        )
        await self._idempotency.complete(
            wallet_id=attempt.wallet_id,
            endpoint=context.endpoint,
            idempotency_key=context.idempotency_key,
            response_reference=receipt.receipt_id,
            response_json=response,
            status_code=status_code,
        )

    async def _permit_constraints_snapshot(
        self,
        permit_id: str,
    ) -> dict[str, Any] | None:
        """Build a snapshot of permit v2 constraints for receipt signing."""
        factory = get_session_factory()
        async with factory() as session:
            permit = await session.get(PermitModel, permit_id)
        if permit is None:
            return None
        # Shared with the live invoke path so a receipt minted during crash
        # recovery signs byte-identical constraints — str(Decimal("10.00"))
        # here previously diverged from the live path's normalized "10".
        ce = permit_constraints_snapshot(permit)
        return ce if ce else None

    async def _compensate_returned_error(
        self,
        attempt: McpDispatchAttemptModel,
    ) -> None:
        existing_receipt = await self._receipts.get_receipt_by_idempotency_record_id(
            attempt.idempotency_record_id
        )
        debit = await self._find_operation_debit(attempt)
        if debit is not None:
            if attempt.ledger_entry_id is None:
                attempt = await self._dispatch.attach_charge(
                    attempt_id=attempt.attempt_id,
                    ledger_entry_id=debit.entry_id,
                    credits_charged=abs(debit.amount),
                )
            if attempt.debit_refunded_at is None:
                await self._money.refund_charge(
                    wallet_id=attempt.wallet_id,
                    charge_entry_id=debit.entry_id,
                    description=(
                        f"Reconcile refunded upstream MCP dispatch {attempt.attempt_id}"
                    ),
                    caller=CallerIdentity.for_wallet(attempt.wallet_id),
                )
                await self._dispatch.mark_debit_refunded(
                    attempt_id=attempt.attempt_id,
                    ledger_entry_id=debit.entry_id,
                )
        # The normal write order is refund -> release budget -> audit ->
        # receipt, so a signed failed_refunded receipt usually means the
        # release already landed. It is no longer *proof* of that, which is
        # why this reads the fact instead of inferring it.
        #
        # `_raise_refunded_upstream_failure` absorbs a contended
        # `release_dispatch_budget_once` and goes on to write the receipt --
        # deliberately, because propagating there would destroy the receipt
        # for a call that really was dispatched. That makes the state this
        # early return once assumed impossible -- a failed_refunded receipt
        # with `budget_released_at` still unset -- reachable on the live path.
        # Skipping on the receipt alone would then strand the reservation
        # until the permit expires, and this reconciler is the thing that is
        # supposed to give it back.
        #
        # Releasing again when the flag is already set costs nothing:
        # `release_dispatch_budget_once` returns False without touching the
        # permit, and its guarded `budget_released_at IS NULL` UPDATE is the
        # once-only gate on every engine, so a stale read here cannot
        # double-subtract either.
        if existing_receipt is not None:
            if existing_receipt.outcome != "failed_refunded":
                raise DispatchAttemptError("dispatch_receipt_outcome_conflict")
            if attempt.budget_released_at is not None:
                return
        await self._permits.release_dispatch_budget_once(
            attempt.attempt_id,
            caller=CallerIdentity.for_wallet(attempt.wallet_id),
        )

    async def _find_operation_debit(
        self,
        attempt: McpDispatchAttemptModel,
    ) -> LedgerEntryModel | None:
        factory = get_session_factory()
        async with factory() as session:
            if attempt.ledger_entry_id is not None:
                debit = await session.get(LedgerEntryModel, attempt.ledger_entry_id)
            else:
                debit = (
                    await session.execute(
                        select(LedgerEntryModel).where(
                            cast(
                                ColumnElement[bool],
                                LedgerEntryModel.wallet_id == attempt.wallet_id,
                            ),
                            cast(
                                ColumnElement[bool],
                                LedgerEntryModel.operation_key
                                == attempt.idempotency_record_id,
                            ),
                            cast(
                                ColumnElement[bool],
                                LedgerEntryModel.action == "debit",
                            ),
                        )
                    )
                ).scalar_one_or_none()
        if debit is None:
            return None
        if (
            debit.wallet_id != attempt.wallet_id
            or debit.action != "debit"
            or debit.operation_key != attempt.idempotency_record_id
            or debit.amount >= Decimal("0")
        ):
            raise DispatchAttemptError("dispatch_reconciliation_debit_invalid")
        return debit

    async def _required_context(self, attempt_id: str) -> DispatchAttemptContext:
        context = await self._dispatch.get_context(attempt_id)
        if context is None:
            raise DispatchAttemptError("dispatch_attempt_not_found")
        return context

    @staticmethod
    def _result_payload(
        attempt: McpDispatchAttemptModel,
    ) -> dict[str, Any] | None:
        if attempt.result_json is None:
            if attempt.response_hash is not None:
                raise DispatchAttemptError("dispatch_result_hash_without_result")
            return None
        try:
            result = json.loads(attempt.result_json)
        except json.JSONDecodeError as exc:
            raise DispatchAttemptError("dispatch_stored_result_invalid") from exc
        if not isinstance(result, dict):
            raise DispatchAttemptError("dispatch_stored_result_invalid")
        # Hash the exact canonical bytes retained by the dispatch service.
        # Re-serializing parsed Unicode with the signing serializer's legacy
        # ASCII-escaping default would produce a different byte sequence.
        if sha256_hex(attempt.result_json) != attempt.response_hash:
            raise DispatchAttemptError("dispatch_stored_result_hash_mismatch")
        return result

    async def get_or_create_terminal_audit(self, attempt_id: str) -> AuditEvent:
        """Return the one canonical signed audit for a terminal dispatch.

        Both the request worker and a crash reconciler use this entry point, so
        a stale sweep racing a delayed direct finalizer cannot create a second
        event with transport-specific metadata.
        """
        context = await self._required_context(attempt_id)
        if context.attempt.state not in DISPATCH_TERMINAL_STATES:
            raise DispatchAttemptError("dispatch_reconciliation_state_invalid")
        return await self._get_or_create_audit(context)

    async def _get_or_create_audit(
        self,
        context: DispatchAttemptContext,
    ) -> AuditEvent:
        existing = await self._find_existing_audit(context)
        if existing is not None:
            return existing
        attempt = context.attempt
        ok = attempt.state == "succeeded"
        metadata: dict[str, Any] = {
            "permit_id": attempt.permit_id,
            "request_hash": attempt.request_hash,
            "ledger_entry_id": attempt.ledger_entry_id,
            "dispatch_attempt_id": attempt.attempt_id,
            "dispatch_state": attempt.state,
            "upstream_tool_name": attempt.upstream_tool_name,
            "upstream_origin": attempt.upstream_origin,
            "dispatch_response_hash": attempt.response_hash,
        }
        if attempt.approval_id is not None:
            metadata["approval_id"] = attempt.approval_id
        if get_settings().JEV_RISK_GUARD != DuplicateGuardMode.OFF:
            jev = await load_jev_guard_metadata(
                jev_audit_id(
                    attempt.wallet_id, context.endpoint, context.idempotency_key
                ),
                attempt.wallet_id,
            )
            if jev is not None:
                metadata["jev_risk_guard"] = jev
        return await record_audit_event(
            event="mcp.invoke",
            # The event id is part of the signed audit payload and its primary
            # key. Deriving it from the durable attempt gives every replica the
            # same append identity without an external lock or mutable lease.
            event_id=f"audit-dsp-{sha256_hex(attempt.attempt_id)[:32]}",
            created_at=attempt.completed_at or attempt.updated_at,
            wallet_id=attempt.wallet_id,
            tool=attempt.public_tool_id,
            endpoint=context.endpoint,
            auth_source="governed_dispatch",
            key_id=attempt.key_id,
            request_id=attempt.attempt_id,
            ok=ok,
            error=None if ok else self._public_error(attempt),
            metadata=metadata,
        )

    async def _find_existing_audit(
        self,
        context: DispatchAttemptContext,
    ) -> AuditEvent | None:
        attempt = context.attempt
        transport_endpoints = {
            "jsonrpc": "/mcp/messages",
            "http": f"/mcp/tools/{attempt.public_tool_id}/invoke",
        }
        candidate_endpoints = {context.endpoint, *transport_endpoints.values()}
        linked_receipt = await self._receipts.get_receipt_by_idempotency_record_id(
            attempt.idempotency_record_id
        )
        factory = get_session_factory()
        rows: Sequence[ControlPlaneAuditEventModel]
        async with factory() as session:
            if linked_receipt is not None and linked_receipt.audit_event_id is not None:
                linked = await session.get(
                    ControlPlaneAuditEventModel,
                    linked_receipt.audit_event_id,
                )
                rows = [linked] if linked is not None else []
            else:
                rows = (
                    (
                        await session.execute(
                            select(ControlPlaneAuditEventModel)
                            .where(
                                cast(
                                    ColumnElement[bool],
                                    ControlPlaneAuditEventModel.wallet_id
                                    == attempt.wallet_id,
                                ),
                                cast(
                                    ColumnElement[bool],
                                    ControlPlaneAuditEventModel.tool
                                    == attempt.public_tool_id,
                                ),
                                cast(
                                    ColumnElement[bool],
                                    cast(Any, ControlPlaneAuditEventModel.endpoint).in_(
                                        candidate_endpoints
                                    ),
                                ),
                            )
                            .order_by(
                                cast(
                                    ColumnElement[Any], ControlPlaneAuditEventModel.seq
                                )
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
        for row in rows:
            try:
                metadata = json.loads(row.metadata_json or "{}")
            except json.JSONDecodeError:
                continue
            if not isinstance(metadata, dict):
                continue
            if metadata.get("dispatch_attempt_id") != attempt.attempt_id:
                continue
            expected = {
                "permit_id": attempt.permit_id,
                "request_hash": attempt.request_hash,
                "ledger_entry_id": attempt.ledger_entry_id,
                "dispatch_state": attempt.state,
                "dispatch_response_hash": attempt.response_hash,
                "upstream_tool_name": attempt.upstream_tool_name,
                "upstream_origin": attempt.upstream_origin,
                "approval_id": attempt.approval_id,
            }
            if any(metadata.get(key) != value for key, value in expected.items()):
                raise DispatchAttemptError("dispatch_audit_linkage_conflict")
            recorded_transport = metadata.get("transport")
            endpoint_matches = row.endpoint == context.endpoint or (
                isinstance(recorded_transport, str)
                and row.endpoint == transport_endpoints.get(recorded_transport)
            )
            if (
                row.wallet_id != attempt.wallet_id
                or row.tool != attempt.public_tool_id
                or not endpoint_matches
                or row.ok != (attempt.state == "succeeded")
            ):
                raise DispatchAttemptError("dispatch_audit_linkage_conflict")
            return AuditEvent(
                event_id=row.event_id,
                created_at=row.created_at,
                event=row.event,
                wallet_id=row.wallet_id,
                tool=row.tool,
                endpoint=row.endpoint,
                auth_source=row.auth_source,
                key_id=row.key_id,
                policy_decision_id=row.policy_decision_id,
                request_id=row.request_id,
                ok=row.ok,
                error=row.error,
                metadata=metadata,
                payload_hash=row.payload_hash,
                previous_hash=row.previous_hash,
                chain_hash=row.chain_hash,
                signature=row.signature,
                signature_key_id=row.signature_key_id,
            )
        return None

    async def _get_or_create_receipt(
        self,
        *,
        attempt: McpDispatchAttemptModel,
        result_payload: dict[str, Any] | None,
        outcome: str,
        audit_event_id: str,
        constraints_evaluated: dict[str, Any] | None = None,
    ) -> ReceiptResponse:
        async def adopt_existing(existing: ReceiptResponse) -> ReceiptResponse:
            valid, _, _ = await self._receipts.verify_receipt(existing.receipt_id)
            if not valid:
                raise DispatchAttemptError("dispatch_receipt_signature_invalid")
            await self._receipts.assert_action_receipt_binding(existing)
            effective_constraints = await self._receipts.effective_constraints(
                constraints_evaluated,
                audit_event_id,
                attempt.wallet_id,
                include_jev="jev_risk_guard" in existing.constraints_evaluated,
            )
            self._assert_receipt_match(
                existing,
                attempt=attempt,
                outcome=outcome,
                audit_event_id=audit_event_id,
                constraints_evaluated=effective_constraints,
            )
            return existing

        existing = await self._receipts.get_receipt_by_idempotency_record_id(
            attempt.idempotency_record_id
        )
        if existing is not None:
            return await adopt_existing(existing)
        credits_charged = (
            Decimal("0")
            if attempt.state == "returned_error"
            else attempt.credits_charged
        )
        reason_code = (
            None if attempt.state == "succeeded" else self._public_error(attempt)
        )
        try:
            return await self._receipts.create_receipt(
                idempotency_record_id=attempt.idempotency_record_id,
                dispatch_attempt_id=attempt.attempt_id,
                response_hash_override=attempt.response_hash,
                permit_id=attempt.permit_id,
                wallet_id=attempt.wallet_id,
                key_id=attempt.key_id,
                tool=attempt.public_tool_id,
                request_payload=None,
                request_hash=attempt.request_hash,
                response_payload=result_payload,
                ledger_entry_id=attempt.ledger_entry_id,
                credits_authorized=attempt.credits_authorized,
                credits_charged=credits_charged,
                outcome=outcome,
                audit_event_id=audit_event_id,
                reason_code=reason_code,
                approval_id=attempt.approval_id,
                constraints_evaluated=constraints_evaluated,
            )
        except ReceiptError:
            # Another process may have finalized the same attempt after our
            # initial read. Adopt only an invariant-equivalent receipt.
            existing = await self._receipts.get_receipt_by_idempotency_record_id(
                attempt.idempotency_record_id
            )
            if existing is None:
                raise
            return await adopt_existing(existing)

    @staticmethod
    def _assert_receipt_match(
        receipt: ReceiptResponse,
        *,
        attempt: McpDispatchAttemptModel,
        outcome: str,
        audit_event_id: str,
        constraints_evaluated: dict[str, Any] | None,
    ) -> None:
        expected_charged = (
            Decimal("0")
            if attempt.state == "returned_error"
            else attempt.credits_charged
        )
        expected = {
            "idempotency_record_id": attempt.idempotency_record_id,
            "dispatch_attempt_id": attempt.attempt_id,
            "permit_id": attempt.permit_id,
            "wallet_id": attempt.wallet_id,
            "key_id": attempt.key_id,
            "tool": attempt.public_tool_id,
            "request_hash": attempt.request_hash,
            "response_hash": attempt.response_hash,
            "ledger_entry_id": attempt.ledger_entry_id,
            "credits_authorized": attempt.credits_authorized,
            "credits_charged": expected_charged,
            "outcome": outcome,
            "audit_event_id": audit_event_id,
            "approval_id": attempt.approval_id,
        }
        if any(getattr(receipt, key) != value for key, value in expected.items()):
            raise DispatchAttemptError("dispatch_receipt_linkage_conflict")
        signed_constraints = receipt.constraints_evaluated
        expected_constraints = constraints_evaluated or {}
        if canonical_json(signed_constraints) != canonical_json(expected_constraints):
            # Older direct finalization paths signed valid receipts before permit-v2
            # snapshots were attached. Recover only that exact legacy shape;
            # a receipt with any signed permit evidence must match in full.
            legacy_omission = (
                outcome in _LEGACY_UNSNAPSHOTTED_OUTCOMES
                and set(signed_constraints) <= {"jev_risk_guard"}
                and set(expected_constraints)
                <= _PERMIT_SNAPSHOT_KEYS | {"jev_risk_guard"}
                and canonical_json(signed_constraints)
                == canonical_json(
                    {
                        key: value
                        for key, value in expected_constraints.items()
                        if key == "jev_risk_guard"
                    }
                )
            )
            if not legacy_omission:
                raise DispatchAttemptError("dispatch_receipt_linkage_conflict")
        expected_reason_code = (
            None
            if attempt.state == "succeeded"
            else McpDispatchReconciliationService._public_error(attempt)
        )
        # Receipts minted before migration 032 did not carry this optional
        # signed field. Preserve their recoverability; any present code must
        # still match the terminal attempt exactly.
        if (
            receipt.reason_code is not None
            and receipt.reason_code != expected_reason_code
        ):
            raise DispatchAttemptError("dispatch_receipt_linkage_conflict")

    @classmethod
    def _replay_response(
        cls,
        *,
        attempt: McpDispatchAttemptModel,
        result_payload: dict[str, Any] | None,
        receipt: ReceiptResponse,
    ) -> tuple[dict[str, Any], int]:
        receipt_payload = receipt.model_dump(mode="json")
        charged = receipt_payload.get("credits_charged")
        if charged is not None and Decimal(str(charged)) == Decimal("0"):
            receipt_payload["credits_charged"] = "0"
        dispatch_payload = {
            "attempt_id": attempt.attempt_id,
            "state": attempt.state,
        }
        if attempt.state == "succeeded":
            response = dict(result_payload or {})
            response["receipt"] = receipt_payload
            return response, 200

        reason = cls._public_error(attempt)
        error_response: dict[str, Any] = {
            "content": [],
            "isError": True,
            "error": reason,
            "receipt": receipt_payload,
            "dispatch": dispatch_payload,
        }
        if attempt.state == "returned_error" and result_payload is not None:
            error_response["upstream_result"] = result_payload
        status_code = 504 if attempt.state == "delivery_uncertain" else 502
        if attempt.error_code == "insufficient_funds":
            status_code = 402
        elif attempt.error_code in {"wallet_expired", "wallet_frozen"}:
            status_code = 403
        return error_response, status_code

    @staticmethod
    def _receipt_outcome(state: str) -> str:
        return {
            "succeeded": "success",
            "returned_error": "failed_refunded",
            "delivery_uncertain": "delivery_uncertain",
            "response_rejected": "response_rejected",
        }[state]

    @staticmethod
    def _public_error(attempt: McpDispatchAttemptModel) -> str:
        if attempt.state == "delivery_uncertain":
            return "delivery_uncertain"
        if attempt.state == "response_rejected":
            return "response_rejected"
        if attempt.error_code == "insufficient_funds":
            return "insufficient_funds"
        if attempt.error_code in {"wallet_expired", "wallet_frozen"}:
            return attempt.error_code
        if attempt.error_code == "upstream_returned_error":
            return "upstream_returned_error"
        if attempt.error_code == "reconciled_stale_prepared":
            return "failed_refunded"
        return "upstream_pre_dispatch_failed"


_service: McpDispatchReconciliationService | None = None


def get_mcp_dispatch_reconciliation_service() -> McpDispatchReconciliationService:
    global _service
    if _service is None:
        _service = McpDispatchReconciliationService()
    return _service
