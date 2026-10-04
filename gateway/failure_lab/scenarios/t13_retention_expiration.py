"""Test 13 -- Retention expiration."""

from __future__ import annotations

from typing import Any

from failure_lab.configurations import (
    GATEWAY_CONFIGURATIONS,
    AttemptOutcome,
    Configuration,
    Target,
)
from failure_lab.gateway import CRASH_BOUNDARY_DESCRIPTIONS
from failure_lab.identity import OperationIdentity
from failure_lab.scenarios.base import (
    ConfigurationResult,
    EventLog,
    Measurements,
    Scenario,
    Verdict,
)

#: How far the lab moves a record's clock back. Thirteen months: past any
#: retention window a payments platform would plausibly publish, and far past
#: the reconciler's idle windows. Time is simulated, never waited out.
DEFAULT_AGE_SECONDS = 400 * 86_400

#: Client patience. Nothing in this scenario is slow; the timeout only has to
#: be generous enough that a harness stall is never read as a product answer.
DEFAULT_TIMEOUT_SECONDS = 15.0

#: The only durable boundary that commits an idempotency record while leaving
#: no reservation, debit, attempt or receipt behind it.
CRASH_BOUNDARY = "after_idempotency_begin"


def _added(
    before: list[dict[str, Any]], after: list[dict[str, Any]], key: str
) -> list[dict[str, Any]]:
    """Rows in ``after`` that were not in ``before``, matched on ``key``."""
    seen = {row[key] for row in before}
    return [row for row in after if row[key] not in seen]


async def _age_operation_rows(record_ids: list[str], *, seconds: int) -> dict[str, int]:
    """Backdate one operation's record, attempt and receipt by ``seconds``.

    ``GatewayUnderTest.backdate_attempts`` ages a whole wallet's attempt rows
    and nothing else. This scenario needs the *idempotency record* and the
    *receipt* aged too, and it needs the aging scoped to one operation so the
    other sub-cases in the same wallet keep their real timestamps. So it
    writes ``created_at``/``updated_at`` directly, exactly as the docstring
    prescribes, through ``app.db.database.get_session_factory``.

    ``dispatched_at`` and ``completed_at`` are moved only on rows that already
    had them set: writing either on a row that never reached that point would
    forge evidence that the send boundary was crossed.
    """
    counts = {
        "idempotency_records": 0,
        "dispatch_attempts": 0,
        "dispatch_attempts_dispatched_at": 0,
        "dispatch_attempts_completed_at": 0,
        "receipts": 0,
    }
    if not record_ids:
        return counts

    from sqlalchemy import update as sa_update

    from app.core.time import utc_now
    from app.db.database import get_session_factory
    from app.db.models import (
        IdempotencyRecordModel,
        McpDispatchAttemptModel,
        ReceiptModel,
    )

    from datetime import timedelta

    stale = utc_now() - timedelta(seconds=seconds)
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            sa_update(IdempotencyRecordModel)
            .where(IdempotencyRecordModel.record_id.in_(record_ids))
            .values(created_at=stale)
        )
        counts["idempotency_records"] = int(result.rowcount or 0)

        result = await session.execute(
            sa_update(McpDispatchAttemptModel)
            .where(McpDispatchAttemptModel.idempotency_record_id.in_(record_ids))
            .values(created_at=stale, updated_at=stale)
        )
        counts["dispatch_attempts"] = int(result.rowcount or 0)

        result = await session.execute(
            sa_update(McpDispatchAttemptModel)
            .where(
                McpDispatchAttemptModel.idempotency_record_id.in_(record_ids),
                McpDispatchAttemptModel.dispatched_at.is_not(None),
            )
            .values(dispatched_at=stale)
        )
        counts["dispatch_attempts_dispatched_at"] = int(result.rowcount or 0)

        result = await session.execute(
            sa_update(McpDispatchAttemptModel)
            .where(
                McpDispatchAttemptModel.idempotency_record_id.in_(record_ids),
                McpDispatchAttemptModel.completed_at.is_not(None),
            )
            .values(completed_at=stale)
        )
        counts["dispatch_attempts_completed_at"] = int(result.rowcount or 0)

        result = await session.execute(
            sa_update(ReceiptModel)
            .where(ReceiptModel.idempotency_record_id.in_(record_ids))
            .values(created_at=stale)
        )
        counts["receipts"] = int(result.rowcount or 0)
        await session.commit()
    return counts


async def _record_expiry_fields(record_ids: list[str]) -> list[dict[str, Any]]:
    """Read each record's ``created_at`` and ``expires_at`` straight from the row.

    ``idempotency_records.expires_at`` is the column a retention policy would
    have to use. Reading it is how this scenario answers "is there any
    automatic expiry?" by measurement rather than by reading the source.
    """
    if not record_ids:
        return []

    from sqlalchemy import select

    from app.db.database import get_session_factory
    from app.db.models import IdempotencyRecordModel

    factory = get_session_factory()
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(IdempotencyRecordModel).where(
                        IdempotencyRecordModel.record_id.in_(record_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
    return [
        {
            "record_id": row.record_id,
            "idempotency_key": row.idempotency_key,
            "operation_kind": row.operation_kind,
            "completed": row.response_json is not None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            "ledger_entry_id": row.ledger_entry_id,
        }
        for row in rows
    ]


async def _purge_idempotency_records(record_ids: list[str]) -> dict[str, Any]:
    """Remove the records the way a retention job would have to.

    A plain ``DELETE`` is tried first, because that is what "the record aged
    out" means. The schema points two foreign keys at the row -- the 1:1
    dispatch attempt (NOT NULL) and the signed receipt (nullable) -- and
    foreign keys are enforced, so the direct delete may be refused. When it
    is, the minimal purge the schema permits is performed instead and every
    step is recorded, so the report can say precisely how much has to be
    destroyed for the record to go away.
    """
    outcome: dict[str, Any] = {
        "steps": [],
        "direct_delete_succeeded": None,
        "records_deleted": 0,
        "attempts_deleted": 0,
        "receipts_detached": 0,
    }
    if not record_ids:
        return outcome

    from sqlalchemy import delete as sa_delete
    from sqlalchemy import select
    from sqlalchemy import update as sa_update

    from app.db.database import get_session_factory
    from app.db.models import (
        IdempotencyRecordModel,
        McpDispatchAttemptModel,
        ReceiptModel,
    )

    factory = get_session_factory()

    async def _delete_records() -> int:
        async with factory() as session:
            result = await session.execute(
                sa_delete(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.record_id.in_(record_ids)
                )
            )
            await session.commit()
            return int(result.rowcount or 0)

    try:
        deleted = await _delete_records()
    except Exception as exc:  # noqa: BLE001 - the refusal is the measurement
        outcome["direct_delete_succeeded"] = False
        outcome["steps"].append(
            {
                "step": "delete_idempotency_record",
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}"[:300],
                "note": (
                    "the dispatch attempt (NOT NULL, 1:1) and the signed "
                    "receipt both carry a foreign key to this row"
                ),
            }
        )
    else:
        outcome["direct_delete_succeeded"] = True
        outcome["records_deleted"] = deleted
        outcome["steps"].append(
            {"step": "delete_idempotency_record", "ok": True, "rows": deleted}
        )
        return outcome

    async with factory() as session:
        attempts = (
            (
                await session.execute(
                    select(McpDispatchAttemptModel).where(
                        McpDispatchAttemptModel.idempotency_record_id.in_(record_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        attempt_ids = [row.attempt_id for row in attempts]

    async with factory() as session:
        result = await session.execute(
            sa_update(ReceiptModel)
            .where(ReceiptModel.idempotency_record_id.in_(record_ids))
            .values(idempotency_record_id=None, dispatch_attempt_id=None)
        )
        await session.commit()
        outcome["receipts_detached"] = int(result.rowcount or 0)
    outcome["steps"].append(
        {
            "step": "detach_receipt_from_record_and_attempt",
            "ok": True,
            "rows": outcome["receipts_detached"],
            "note": (
                "the receipt row and its signature survive; only the two "
                "foreign keys that pin it to the purged record are cleared"
            ),
        }
    )

    async with factory() as session:
        result = await session.execute(
            sa_delete(McpDispatchAttemptModel).where(
                McpDispatchAttemptModel.idempotency_record_id.in_(record_ids)
            )
        )
        await session.commit()
        outcome["attempts_deleted"] = int(result.rowcount or 0)
    outcome["steps"].append(
        {
            "step": "delete_dispatch_attempt",
            "ok": True,
            "rows": outcome["attempts_deleted"],
            "attempt_ids": attempt_ids,
            "note": (
                "the attempt's foreign key to the record is NOT NULL and 1:1, "
                "so a record purge necessarily takes the dispatch history with it"
            ),
        }
    )

    outcome["records_deleted"] = await _delete_records()
    outcome["steps"].append(
        {
            "step": "delete_idempotency_record",
            "ok": True,
            "rows": outcome["records_deleted"],
            "note": "succeeded once the referencing rows were cleared",
        }
    )
    return outcome


class RetentionExpiration(Scenario):
    """What is left of the replay guarantee once the record ages out?

    WHAT TO IMPLEMENT
    -----------------
    The question the report must answer is: after the idempotency retention
    period, what guarantee remains? Answer it by measurement.

    ``aged_record``  Run one clean call. Backdate its idempotency record,
                     receipt and attempt far into the past by updating
                     ``created_at``/``updated_at`` directly through
                     ``app.db.database.get_session_factory``. Run
                     ``gateway.reconcile(idle_seconds=0)``. Retry the same
                     key. Record whether the replay still returns the original
                     receipt and whether any new dispatch occurred.

    ``effect_free_release``  Crash at ``after_idempotency_begin`` so a record
                     exists with no reservation, debit, attempt or receipt.
                     Backdate it, reconcile, then retry the same key. This is
                     the one path that deliberately releases a key, and it is
                     safe precisely because nothing happened. Record that the
                     retry now executes and that total executions for the
                     operation is still 1.

    ``record_removed``  Run one clean call, then DELETE its idempotency record
                     directly -- standing in for a hypothetical retention
                     purge that the product does not currently implement.
                     Retry the same key and record what happens. If the retry
                     dispatches and produces a second downstream effect, that
                     is the honest answer to "what guarantee remains after
                     expiration": none. Report it as such.

    Verdict: ``PASS`` iff the aged record still replays without a new dispatch
    AND the effect-free release produces exactly one downstream execution in
    total. ``FAIL`` if an aged but intact record stops replaying, or if the
    effect-free release lets a *second* effect land.

    The ``record_removed`` case is descriptive and must NOT be folded into the
    verdict -- it measures the consequence of removing the record, which is
    not something the gateway does. Record it in
    ``extra["record_removed"]`` and put the conclusion in
    ``remaining_risks``: the replay guarantee lasts exactly as long as the
    idempotency record does, and no automatic expiry currently shortens that.
    State in ``extra["retention_policy_observed"]`` what the run shows about
    whether any automatic expiry exists.
    """

    test_id = "T13"
    title = "Retention expiration"
    claim = (
        "The replay guarantee persists for as long as the idempotency record "
        "does; the only automatic release is a provably effect-free one."
    )
    tier = "slow"
    configurations = GATEWAY_CONFIGURATIONS
    inapplicable_reason = "a direct integration has no gateway retention window"
    expected = {
        Configuration.DIRECT_NAIVE.value: Verdict.NOT_APPLICABLE.value,
        Configuration.DIRECT_NATIVE.value: Verdict.NOT_APPLICABLE.value,
        Configuration.GATEWAY_NATIVE.value: Verdict.PASS.value,
        Configuration.GATEWAY_NAIVE.value: Verdict.PASS.value,
    }
    limitations = (
        "Time is simulated by backdating rows, not by waiting.",
        "The record-removal case models a retention purge the product does "
        "not implement; it is reported to answer the question, not to "
        "describe current behaviour.",
    )

    # ------------------------------------------------------------------ #
    # orchestration                                                       #
    # ------------------------------------------------------------------ #

    async def run_configuration(
        self, target: Target, log: EventLog
    ) -> ConfigurationResult:
        target.require_gateway()
        age_seconds = int(self.options.get("age_seconds", DEFAULT_AGE_SECONDS))
        timeout_seconds = float(
            self.options.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        )

        attempts: list[AttemptOutcome] = []
        operation_ids: list[str] = []

        aged, aged_attempts, aged_operation = await self._aged_record(
            target, log, age_seconds=age_seconds, timeout_seconds=timeout_seconds
        )
        attempts.extend(aged_attempts)
        operation_ids.append(aged_operation)

        release, release_attempts, release_operation = await self._effect_free_release(
            target, log, age_seconds=age_seconds, timeout_seconds=timeout_seconds
        )
        attempts.extend(release_attempts)
        operation_ids.append(release_operation)

        removed, removed_attempts, removed_operation = await self._record_removed(
            target, log, timeout_seconds=timeout_seconds
        )
        attempts.extend(removed_attempts)
        operation_ids.append(removed_operation)

        measurements = await self.measure(target, attempts, operation_ids=operation_ids)
        return self._verdict(
            target,
            log,
            aged=aged,
            release=release,
            removed=removed,
            attempts=attempts,
            measurements=measurements,
            age_seconds=age_seconds,
        )

    # ------------------------------------------------------------------ #
    # case 1: an aged but intact record                                   #
    # ------------------------------------------------------------------ #

    async def _aged_record(
        self,
        target: Target,
        log: EventLog,
        *,
        age_seconds: int,
        timeout_seconds: float,
    ) -> tuple[dict[str, Any], list[AttemptOutcome], str]:
        """One clean call, aged thirteen months, swept, then replayed."""
        gateway, tenant, _permit = target.require_gateway()
        configuration = target.configuration.value

        operation_id, refund = self.refund("pay_t13_aged")
        identity = OperationIdentity.first_attempt(operation_id)
        before = await gateway.snapshot(tenant)

        first = await target.agent.submit(
            identity, refund, timeout_seconds=timeout_seconds
        )
        after_first = await gateway.snapshot(tenant)
        dispatches_after_first = target.injector.dispatch_count(operation_id)
        executions_after_first = target.ledger.execution_count(operation_id)

        record_rows = _added(
            before.idempotency_records, after_first.idempotency_records, "record_id"
        )
        record_ids = [row["record_id"] for row in record_rows]
        receipt_rows = _added(before.receipts, after_first.receipts, "receipt_id")
        attempt_rows = _added(before.attempts, after_first.attempts, "attempt_id")

        log.emit(
            "aged_record.first_call",
            (
                f"clean call -> status={first.status}; receipt "
                f"{first.receipt_id}; {len(record_ids)} idempotency record(s), "
                f"{len(attempt_rows)} attempt row(s), {len(receipt_rows)} "
                f"receipt(s); {dispatches_after_first} dispatch(es), "
                f"{executions_after_first} downstream execution(s)"
            ),
            scenario=self.test_id,
            configuration=configuration,
            case="aged_record",
            operation_id=operation_id,
            idempotency_key=identity.idempotency_key,
            status=first.status,
            client_visible_state=first.client_visible_state,
            receipt_id=first.receipt_id,
            receipt_outcome=(first.receipt.get("outcome") if first.receipt else None),
            record_ids=record_ids,
            downstream_requests=dispatches_after_first,
            downstream_executions=executions_after_first,
        )

        expiry_before = await _record_expiry_fields(record_ids)
        aged_rows = await _age_operation_rows(record_ids, seconds=age_seconds)
        reconciliation = await gateway.reconcile(idle_seconds=0)
        after_reconcile = await gateway.snapshot(tenant)
        expiry_after = await _record_expiry_fields(record_ids)

        wanted_records = set(record_ids)
        surviving = [
            row
            for row in after_reconcile.idempotency_records
            if row["record_id"] in wanted_records
        ]
        # Every record the call created must still be there. "Any of them
        # survived" would call a partial sweep a survival.
        record_survived = bool(record_ids) and len(surviving) == len(record_ids)
        record_completed = [row["completed"] for row in surviving]

        log.emit(
            "aged_record.aged_and_swept",
            (
                f"record, attempt and receipt backdated {age_seconds}s "
                f"(~{age_seconds / 86_400:.0f} days); a restarted gateway's "
                f"reconcile(idle_seconds=0) then ran; record still present="
                f"{record_survived}, completed={record_completed}"
            ),
            scenario=self.test_id,
            configuration=configuration,
            case="aged_record",
            operation_id=operation_id,
            age_seconds=age_seconds,
            rows_aged=aged_rows,
            reconciliation=reconciliation,
            record_survived=record_survived,
            record_completed=record_completed,
            record_expires_at_before=[row["expires_at"] for row in expiry_before],
            record_expires_at_after=[row["expires_at"] for row in expiry_after],
            record_created_at_after=[row["created_at"] for row in expiry_after],
        )

        retry_identity = identity.retry()
        replay = await target.agent.submit(
            retry_identity, refund, timeout_seconds=timeout_seconds
        )
        dispatches_after_retry = target.injector.dispatch_count(operation_id)
        executions_after_retry = target.ledger.execution_count(operation_id)
        after_retry = await gateway.snapshot(tenant)

        new_dispatch = dispatches_after_retry > dispatches_after_first
        new_execution = executions_after_retry > executions_after_first
        same_receipt = (
            replay.receipt_id is not None and replay.receipt_id == first.receipt_id
        )
        same_refund_id = (
            replay.refund is not None
            and first.refund is not None
            and replay.refund.get("refund_id") == first.refund.get("refund_id")
        )
        new_receipts = _added(
            after_reconcile.receipts, after_retry.receipts, "receipt_id"
        )
        new_debits = _added(after_reconcile.debits, after_retry.debits, "entry_id")

        log.emit(
            "aged_record.replay",
            (
                f"same-key retry against the aged record -> status="
                f"{replay.status}; receipt {replay.receipt_id} (the original: "
                f"{same_receipt}); new dispatch={new_dispatch}; new downstream "
                f"execution={new_execution}; {len(new_receipts)} new "
                f"receipt(s), {len(new_debits)} new debit(s)"
            ),
            scenario=self.test_id,
            configuration=configuration,
            case="aged_record",
            operation_id=operation_id,
            idempotency_key=retry_identity.idempotency_key,
            same_key_as_first=(
                retry_identity.idempotency_key == identity.idempotency_key
            ),
            status=replay.status,
            client_visible_state=replay.client_visible_state,
            reason=replay.reason,
            receipt_id=replay.receipt_id,
            replayed_same_receipt=same_receipt,
            replayed_same_refund_id=same_refund_id,
            new_dispatch=new_dispatch,
            downstream_requests=dispatches_after_retry,
            downstream_executions=executions_after_retry,
        )

        row = {
            "case": "aged_record",
            "operation_id": operation_id,
            "idempotency_key": identity.idempotency_key,
            "age_seconds": age_seconds,
            "age_days": round(age_seconds / 86_400, 1),
            "rows_aged": aged_rows,
            "first_call_status": first.status,
            "first_call_receipt_id": first.receipt_id,
            "first_call_receipt_outcome": (
                first.receipt.get("outcome") if first.receipt else None
            ),
            "dispatches_after_first_call": dispatches_after_first,
            "executions_after_first_call": executions_after_first,
            "record_ids": record_ids,
            "record_expires_at_before_aging": [
                entry["expires_at"] for entry in expiry_before
            ],
            "record_expires_at_after_sweep": [
                entry["expires_at"] for entry in expiry_after
            ],
            "record_created_at_after_aging": [
                entry["created_at"] for entry in expiry_after
            ],
            "reconciliation": reconciliation,
            "record_survived_sweep": record_survived,
            "record_completed_after_sweep": record_completed,
            "replay_status": replay.status,
            "replay_client_visible_state": replay.client_visible_state,
            "replay_reason": replay.reason,
            "replay_receipt_id": replay.receipt_id,
            "replay_returned_original_receipt": same_receipt,
            "replay_returned_original_refund_id": same_refund_id,
            "replay_dispatched": new_dispatch,
            "replay_produced_new_execution": new_execution,
            "dispatches_after_replay": dispatches_after_retry,
            "executions_after_replay": executions_after_retry,
            "new_receipts_from_replay": [r["receipt_id"] for r in new_receipts],
            "new_debits_from_replay": [d["entry_id"] for d in new_debits],
            "wallet_balance_after_case": after_retry.wallet_balance,
        }
        return row, [first, replay], operation_id

    # ------------------------------------------------------------------ #
    # case 2: the one deliberate release                                  #
    # ------------------------------------------------------------------ #

    async def _effect_free_release(
        self,
        target: Target,
        log: EventLog,
        *,
        age_seconds: int,
        timeout_seconds: float,
    ) -> tuple[dict[str, Any], list[AttemptOutcome], str]:
        """Crash before anything but the record exists, age it, sweep, retry."""
        gateway, tenant, _permit = target.require_gateway()
        configuration = target.configuration.value
        description = CRASH_BOUNDARY_DESCRIPTIONS[CRASH_BOUNDARY]

        operation_id, refund = self.refund("pay_t13_release")
        identity = OperationIdentity.first_attempt(operation_id)
        before = await gateway.snapshot(tenant)

        with gateway.crash_at(CRASH_BOUNDARY) as crash:
            crashed = await target.agent.submit(
                identity, refund, timeout_seconds=timeout_seconds
            )
        crash_fired = bool(crash["fired"])
        after_crash = await gateway.snapshot(tenant)
        dispatches_at_crash = target.injector.dispatch_count(operation_id)
        executions_at_crash = target.ledger.execution_count(operation_id)

        record_rows = _added(
            before.idempotency_records, after_crash.idempotency_records, "record_id"
        )
        record_ids = [row["record_id"] for row in record_rows]
        attempt_rows = _added(before.attempts, after_crash.attempts, "attempt_id")
        debit_rows = _added(before.debits, after_crash.debits, "entry_id")
        receipt_rows = _added(before.receipts, after_crash.receipts, "receipt_id")
        crash_left_an_effect = bool(attempt_rows or debit_rows or receipt_rows)

        log.emit(
            "effect_free_release.crash",
            (
                f"died at {CRASH_BOUNDARY} ({description}) -> status="
                f"{crashed.status}, fired={crash_fired}; left "
                f"{len(record_ids)} idempotency record(s), {len(attempt_rows)} "
                f"attempt row(s), {len(debit_rows)} debit(s), "
                f"{len(receipt_rows)} receipt(s); {dispatches_at_crash} "
                f"dispatch(es), {executions_at_crash} downstream execution(s)"
            ),
            scenario=self.test_id,
            configuration=configuration,
            case="effect_free_release",
            operation_id=operation_id,
            idempotency_key=identity.idempotency_key,
            boundary=CRASH_BOUNDARY,
            boundary_description=description,
            crash_fired=crash_fired,
            crash_fired_at=crash.get("fired_at"),
            status=crashed.status,
            client_visible_state=crashed.client_visible_state,
            record_ids=record_ids,
            attempt_rows_left=len(attempt_rows),
            debits_left=len(debit_rows),
            receipts_left=len(receipt_rows),
            downstream_requests=dispatches_at_crash,
            downstream_executions=executions_at_crash,
        )

        expiry_before = await _record_expiry_fields(record_ids)
        aged_rows = await _age_operation_rows(record_ids, seconds=age_seconds)
        reconciliation = await gateway.reconcile(idle_seconds=0)
        after_reconcile = await gateway.snapshot(tenant)
        wanted_records = set(record_ids)
        surviving = [
            row
            for row in after_reconcile.idempotency_records
            if row["record_id"] in wanted_records
        ]
        record_released = bool(record_ids) and not surviving

        log.emit(
            "effect_free_release.sweep",
            (
                f"record backdated {age_seconds}s and swept -> released="
                f"{record_released}; the sweep reports idempotency_repaired="
                f"{reconciliation.get('idempotency_repaired')}, "
                f"idempotency_needs_review="
                f"{reconciliation.get('idempotency_needs_review')}"
            ),
            scenario=self.test_id,
            configuration=configuration,
            case="effect_free_release",
            operation_id=operation_id,
            age_seconds=age_seconds,
            rows_aged=aged_rows,
            reconciliation=reconciliation,
            record_released=record_released,
            record_operation_kind=[entry["operation_kind"] for entry in expiry_before],
        )

        retry_identity = identity.retry()
        retry = await target.agent.submit(
            retry_identity, refund, timeout_seconds=timeout_seconds
        )
        dispatches_total = target.injector.dispatch_count(operation_id)
        executions_total = target.ledger.execution_count(operation_id)
        after_retry = await gateway.snapshot(tenant)
        retry_dispatched = dispatches_total > dispatches_at_crash

        log.emit(
            "effect_free_release.retry",
            (
                f"same-key retry after the release -> status={retry.status}; "
                f"dispatched={retry_dispatched}; the operation now has "
                f"{executions_total} downstream execution(s) in total"
            ),
            scenario=self.test_id,
            configuration=configuration,
            case="effect_free_release",
            operation_id=operation_id,
            idempotency_key=retry_identity.idempotency_key,
            same_key_as_crash=(
                retry_identity.idempotency_key == identity.idempotency_key
            ),
            status=retry.status,
            client_visible_state=retry.client_visible_state,
            reason=retry.reason,
            receipt_id=retry.receipt_id,
            receipt_outcome=(retry.receipt.get("outcome") if retry.receipt else None),
            retry_dispatched=retry_dispatched,
            downstream_requests=dispatches_total,
            downstream_executions=executions_total,
        )

        row = {
            "case": "effect_free_release",
            "operation_id": operation_id,
            "idempotency_key": identity.idempotency_key,
            "boundary": CRASH_BOUNDARY,
            "boundary_description": description,
            "crash_fired": crash_fired,
            "crash_status": crashed.status,
            "crash_client_visible_state": crashed.client_visible_state,
            "record_ids": record_ids,
            "record_operation_kind": [
                entry["operation_kind"] for entry in expiry_before
            ],
            "record_expires_at_before_aging": [
                entry["expires_at"] for entry in expiry_before
            ],
            "attempt_rows_left_by_crash": len(attempt_rows),
            "debits_left_by_crash": len(debit_rows),
            "receipts_left_by_crash": len(receipt_rows),
            "crash_left_an_effect_to_compensate": crash_left_an_effect,
            "dispatches_at_crash": dispatches_at_crash,
            "executions_at_crash": executions_at_crash,
            "age_seconds": age_seconds,
            "rows_aged": aged_rows,
            "reconciliation": reconciliation,
            "record_released_by_sweep": record_released,
            "retry_status": retry.status,
            "retry_client_visible_state": retry.client_visible_state,
            "retry_reason": retry.reason,
            "retry_receipt_id": retry.receipt_id,
            "retry_receipt_outcome": (
                retry.receipt.get("outcome") if retry.receipt else None
            ),
            "retry_dispatched": retry_dispatched,
            "dispatches_total_for_operation": dispatches_total,
            "executions_total_for_operation": executions_total,
            "wallet_balance_after_case": after_retry.wallet_balance,
        }
        return row, [crashed, retry], operation_id

    # ------------------------------------------------------------------ #
    # case 3: the record is gone (descriptive; never folded into a verdict)
    # ------------------------------------------------------------------ #

    async def _record_removed(
        self,
        target: Target,
        log: EventLog,
        *,
        timeout_seconds: float,
    ) -> tuple[dict[str, Any], list[AttemptOutcome], str]:
        """Delete a completed record outright, then retry its key."""
        gateway, tenant, _permit = target.require_gateway()
        configuration = target.configuration.value

        operation_id, refund = self.refund("pay_t13_removed")
        identity = OperationIdentity.first_attempt(operation_id)
        before = await gateway.snapshot(tenant)

        first = await target.agent.submit(
            identity, refund, timeout_seconds=timeout_seconds
        )
        after_first = await gateway.snapshot(tenant)
        dispatches_after_first = target.injector.dispatch_count(operation_id)
        executions_after_first = target.ledger.execution_count(operation_id)

        record_rows = _added(
            before.idempotency_records, after_first.idempotency_records, "record_id"
        )
        record_ids = [row["record_id"] for row in record_rows]
        own_receipts = {
            row["receipt_id"]
            for row in _added(before.receipts, after_first.receipts, "receipt_id")
        }

        log.emit(
            "record_removed.first_call",
            (
                f"clean call -> status={first.status}; receipt "
                f"{first.receipt_id}; {executions_after_first} downstream "
                f"execution(s)"
            ),
            scenario=self.test_id,
            configuration=configuration,
            case="record_removed",
            operation_id=operation_id,
            idempotency_key=identity.idempotency_key,
            status=first.status,
            client_visible_state=first.client_visible_state,
            receipt_id=first.receipt_id,
            record_ids=record_ids,
            downstream_requests=dispatches_after_first,
            downstream_executions=executions_after_first,
        )

        purge = await _purge_idempotency_records(record_ids)
        after_purge = await gateway.snapshot(tenant)
        wanted_records = set(record_ids)
        record_gone = bool(record_ids) and not [
            row
            for row in after_purge.idempotency_records
            if row["record_id"] in wanted_records
        ]
        receipts_surviving = [
            row["receipt_id"]
            for row in after_purge.receipts
            if row["receipt_id"] in own_receipts
        ]

        log.emit(
            "record_removed.purge",
            (
                "simulated retention purge of the idempotency record -> "
                f"record gone={record_gone}; direct DELETE accepted="
                f"{purge['direct_delete_succeeded']}; steps="
                f"{[step['step'] for step in purge['steps']]}; the signed "
                f"receipt(s) {receipts_surviving} survived the purge"
            ),
            scenario=self.test_id,
            configuration=configuration,
            case="record_removed",
            operation_id=operation_id,
            record_ids=record_ids,
            purge=purge,
            record_gone=record_gone,
            receipts_surviving_purge=receipts_surviving,
        )

        retry_identity = identity.retry()
        retry = await target.agent.submit(
            retry_identity, refund, timeout_seconds=timeout_seconds
        )
        dispatches_after_retry = target.injector.dispatch_count(operation_id)
        executions_after_retry = target.ledger.execution_count(operation_id)
        after_retry = await gateway.snapshot(tenant)

        retry_dispatched = dispatches_after_retry > dispatches_after_first
        second_effect = executions_after_retry > executions_after_first
        new_receipts = _added(after_purge.receipts, after_retry.receipts, "receipt_id")
        new_debits = _added(after_purge.debits, after_retry.debits, "entry_id")
        same_receipt = (
            retry.receipt_id is not None and retry.receipt_id == first.receipt_id
        )

        log.emit(
            "record_removed.retry",
            (
                f"same-key retry with no record to replay from -> status="
                f"{retry.status}; dispatched={retry_dispatched}; a second "
                f"downstream effect landed={second_effect}; {len(new_debits)} "
                f"new debit(s), {len(new_receipts)} new receipt(s)"
            ),
            scenario=self.test_id,
            configuration=configuration,
            case="record_removed",
            operation_id=operation_id,
            idempotency_key=retry_identity.idempotency_key,
            status=retry.status,
            client_visible_state=retry.client_visible_state,
            reason=retry.reason,
            receipt_id=retry.receipt_id,
            replayed_same_receipt=same_receipt,
            retry_dispatched=retry_dispatched,
            second_downstream_effect=second_effect,
            downstream_requests=dispatches_after_retry,
            downstream_executions=executions_after_retry,
        )

        row = {
            "case": "record_removed",
            "descriptive_only": True,
            "models": (
                "a retention purge of the idempotency record. The gateway does "
                "not implement one; this case removes the row by hand to "
                "answer what the guarantee would be worth if it did."
            ),
            "operation_id": operation_id,
            "idempotency_key": identity.idempotency_key,
            "first_call_status": first.status,
            "first_call_receipt_id": first.receipt_id,
            "dispatches_after_first_call": dispatches_after_first,
            "executions_after_first_call": executions_after_first,
            "record_ids": record_ids,
            "purge": purge,
            "record_gone_after_purge": record_gone,
            "receipts_surviving_purge": receipts_surviving,
            "retry_status": retry.status,
            "retry_client_visible_state": retry.client_visible_state,
            "retry_reason": retry.reason,
            "retry_receipt_id": retry.receipt_id,
            "retry_replayed_original_receipt": same_receipt,
            "retry_dispatched": retry_dispatched,
            "second_downstream_effect": second_effect,
            "dispatches_total_for_operation": dispatches_after_retry,
            "executions_total_for_operation": executions_after_retry,
            "new_receipts_from_retry": [r["receipt_id"] for r in new_receipts],
            "new_debits_from_retry": [d["entry_id"] for d in new_debits],
            "downstream_native_idempotency": target.configuration.native_idempotency,
            "wallet_balance_after_case": after_retry.wallet_balance,
        }
        return row, [first, retry], operation_id

    # ------------------------------------------------------------------ #
    # verdict                                                             #
    # ------------------------------------------------------------------ #

    def _verdict(
        self,
        target: Target,
        log: EventLog,
        *,
        aged: dict[str, Any],
        release: dict[str, Any],
        removed: dict[str, Any],
        attempts: list[AttemptOutcome],
        measurements: Measurements,
        age_seconds: int,
    ) -> ConfigurationResult:
        failures: list[str] = []

        # -- aged but intact record ------------------------------------
        if aged["first_call_status"] != "success":
            failures.append(
                "aged_record: the call being aged returned "
                f"'{aged['first_call_status']}', so there was no completed "
                "record to age"
            )
        # The control for this case: the call being aged must really have
        # dispatched and really have landed downstream. Without it, "the
        # replay produced no new execution" is a statement about an operation
        # that never happened, and the gateway's own 'success' is the only
        # thing vouching for it. Both counts below come from instruments the
        # gateway cannot reach -- the fault layer and the effect ledger.
        if aged["dispatches_after_first_call"] < 1:
            failures.append(
                "aged_record: the fault layer saw "
                f"{aged['dispatches_after_first_call']} crossing(s) for the "
                "call being aged, so nothing was ever dispatched for the aged "
                "record to go on suppressing"
            )
        if aged["executions_after_first_call"] != 1:
            failures.append(
                "aged_record: the effect ledger recorded "
                f"{aged['executions_after_first_call']} downstream "
                "execution(s) for the call being aged, want 1 -- there was no "
                "single real effect for the replay to be a replay of"
            )
        if not aged["record_survived_sweep"]:
            failures.append(
                "aged_record: the completed idempotency record was gone after "
                f"aging it {aged['age_days']} days and running the sweep a "
                "restarted gateway runs -- an intact record must not be "
                "released by age alone"
            )
        if aged["replay_status"] != "success":
            failures.append(
                "aged_record: the same-key retry returned "
                f"'{aged['replay_status']}' rather than replaying the stored "
                "envelope"
            )
        if not aged["replay_returned_original_receipt"]:
            failures.append(
                "aged_record: the same-key retry returned receipt "
                f"{aged['replay_receipt_id']} rather than the original "
                f"{aged['first_call_receipt_id']}"
            )
        if aged["replay_dispatched"]:
            failures.append(
                "aged_record: the same-key retry crossed the fault layer "
                "again, so the aged record stopped suppressing dispatch"
            )
        if aged["replay_produced_new_execution"]:
            failures.append(
                "aged_record: the same-key retry produced a second downstream "
                "effect for an operation whose record was intact"
            )

        # -- the one deliberate release --------------------------------
        if not release["crash_fired"]:
            failures.append(
                "effect_free_release: the instrumented crash never fired, so "
                "this case measured nothing"
            )
        if release["crash_status"] != "gateway_process_died":
            failures.append(
                "effect_free_release: the crashed call returned "
                f"'{release['crash_status']}', want 'gateway_process_died'"
            )
        if (
            release["record_released_by_sweep"]
            and release["crash_left_an_effect_to_compensate"]
        ):
            failures.append(
                "effect_free_release: the sweep released the key although the "
                f"crash had left {release['attempt_rows_left_by_crash']} "
                f"attempt row(s), {release['debits_left_by_crash']} debit(s) "
                f"and {release['receipts_left_by_crash']} receipt(s) behind -- "
                "a release is only safe when there is provably nothing to "
                "compensate"
            )
        # The check above reads the gateway's own attempt, debit and receipt
        # rows. "Nothing happened" is exactly the claim a governed system
        # cannot be trusted to make about itself, so the same question is put
        # to the two instruments outside it. A crash that already crossed the
        # fault layer, or already landed an effect, was not effect-free -- and
        # releasing its key is then unsafe whatever the gateway's tables say.
        if release["dispatches_at_crash"] != 0:
            failures.append(
                "effect_free_release: the fault layer saw "
                f"{release['dispatches_at_crash']} crossing(s) before the key "
                f"was released, so the crash at {CRASH_BOUNDARY} was not the "
                "pre-dispatch, effect-free crash this case requires"
            )
        if release["executions_at_crash"] != 0:
            failures.append(
                "effect_free_release: the effect ledger recorded "
                f"{release['executions_at_crash']} downstream execution(s) "
                "before the key was released, so the released key had a real "
                "effect behind it and the release was not provably safe"
            )
        release_executions = release["executions_total_for_operation"]
        if release_executions > 1:
            failures.append(
                f"effect_free_release: the operation has {release_executions} "
                "downstream executions after the released key was retried; a "
                "released key must never yield a duplicate effect"
            )
        elif release_executions < 1:
            failures.append(
                "effect_free_release: the operation has "
                f"{release_executions} downstream execution(s) after the "
                f"retry (retry status '{release['retry_status']}'), so the "
                "effect-free release did not let the stranded operation "
                "complete"
            )

        verdict = Verdict.PASS if not failures else Verdict.FAIL

        automatic_expiry_observed = not aged["record_survived_sweep"]
        expires_at_values = list(aged["record_expires_at_before_aging"])
        expires_at_stamped = any(value is not None for value in expires_at_values)

        retention_policy_observed = {
            "automatic_expiry_observed": automatic_expiry_observed,
            "summary": (
                "No automatic expiry was observed. A completed governed "
                f"idempotency record was backdated {aged['age_days']} days -- "
                "record, dispatch attempt and receipt together -- and the "
                "sweep a restarted gateway runs, reconcile(idle_seconds=0), "
                "left it in place; the same key still replayed the original "
                f"receipt ({aged['first_call_receipt_id']}) with no new "
                "dispatch. The one release this product performs is keyed on "
                "provable absence of effect, not on age: the sweep deletes a "
                "governed record only when it has no response, no linked "
                "ledger entry, no dispatch attempt and no receipt."
                if not automatic_expiry_observed
                else (
                    "An automatic expiry WAS observed: the aged completed "
                    "record was gone after the sweep, so the replay guarantee "
                    "does have a time limit and this scenario's claim needs "
                    "to be rewritten around it."
                )
            ),
            "age_applied_seconds": age_seconds,
            "age_applied_days": aged["age_days"],
            "rows_aged": aged["rows_aged"],
            "sweep_run": "gateway.reconcile(idle_seconds=0)",
            "sweep_result_for_aged_record": aged["reconciliation"],
            "aged_record_present_after_sweep": aged["record_survived_sweep"],
            "aged_record_still_replayed_original_receipt": aged[
                "replay_returned_original_receipt"
            ],
            "aged_record_replay_dispatched": aged["replay_dispatched"],
            "expires_at_column_exists": True,
            "expires_at_stamped_on_the_completed_record": expires_at_stamped,
            "expires_at_values_before_aging": expires_at_values,
            "expires_at_values_after_sweep": aged["record_expires_at_after_sweep"],
            "expires_at_note": (
                "idempotency_records carries an expires_at column. It was NULL "
                "on the completed governed record both before and after the "
                "sweep, so nothing stamps a retention deadline and nothing "
                "reads one."
                if not expires_at_stamped
                else (
                    "idempotency_records.expires_at was populated on the "
                    "completed governed record, so a retention deadline does "
                    "exist in the data even though the sweep did not act on "
                    "it in this run."
                )
            ),
            "only_automatic_release_observed": (
                "the effect-free release at "
                f"{CRASH_BOUNDARY} -- released="
                f"{release['record_released_by_sweep']} with "
                f"{release['attempt_rows_left_by_crash']} attempt row(s), "
                f"{release['debits_left_by_crash']} debit(s) and "
                f"{release['receipts_left_by_crash']} receipt(s) to compensate "
                "in the gateway's own tables, and -- from the two instruments "
                "outside it -- "
                f"{release['dispatches_at_crash']} fault-layer crossing(s) and "
                f"{release['executions_at_crash']} downstream execution(s) "
                "before the release"
            ),
            "guarantee_lifetime": (
                "as long as the idempotency record row lives; this run "
                "exercised one sweep (reconcile) and saw nothing shorten that, "
                "which is not the same as proving no other mechanism could"
            ),
        }

        extra: dict[str, Any] = {
            # "cases" is the suite-wide key for a per-sub-case table, so a
            # report rendering all fourteen scenarios side by side can key on
            # one field. The three named keys below are kept because this
            # scenario's specification uses them, and each row is the same
            # object as the value under its own name.
            "cases": [
                {"case": "aged_record", "in_verdict": True, **aged},
                {"case": "effect_free_release", "in_verdict": True, **release},
                {"case": "record_removed", "in_verdict": False, **removed},
            ],
            "aged_record": aged,
            "effect_free_release": release,
            "record_removed": removed,
            # Named so failure_lab.evidence._FAULT_KEYS_IN_EXTRA finds the
            # injected failure: a gateway crash never crosses the fault layer,
            # so without this the evidence bundle's fault-injection table
            # would record nothing for this scenario.
            "crash_boundary": CRASH_BOUNDARY,
            "retention_policy_observed": retention_policy_observed,
            "cases_in_verdict": ["aged_record", "effect_free_release"],
            "cases_descriptive_only": ["record_removed"],
            "record_removed_excluded_from_verdict_because": (
                "the gateway does not purge idempotency records. This case "
                "removes the row by hand to answer what the guarantee would "
                "be worth after a retention purge, so it describes a "
                "hypothetical, not current behaviour, and scoring it would be "
                "scoring the harness."
            ),
            "verdict_failures": failures,
            "age_seconds": age_seconds,
            "measured_scope_note": (
                "Per-case dispatch and execution counts are scoped to that "
                "case's own operation id. The configuration-level counters "
                "cover all three operations and all six submissions, so they "
                "are deliberately larger. Provenance: every 'dispatch'/"
                "'crossing' count comes from the fault layer and every "
                "'execution' count from the effect ledger -- both outside the "
                "gateway. The debit, refund, receipt and attempt counts, the "
                "wallet balance and every row in the three case dictionaries "
                "that names an idempotency record, attempt or receipt are "
                "the gateway's own report of itself and prove only what it "
                "wrote down."
            ),
            "wallet_balance": (
                measurements.snapshot.wallet_balance if measurements.snapshot else None
            ),
            "gateway_dispatches_all_cases": measurements.counters.gateway_dispatches,
            "downstream_executions_all_cases": measurements.downstream_executions,
            "gateway_debits_all_cases": measurements.counters.gateway_debits,
            "gateway_refunds_all_cases": measurements.counters.gateway_refunds,
            "gateway_net_debits_all_cases": measurements.counters.gateway_net_debits,
            "receipts_all_cases": measurements.counters.receipts,
            "receipt_outcomes_all_cases": measurements.receipt_outcomes(),
        }

        observation = (
            "Retention was simulated by backdating rows, never by waiting. "
            f"aged_record: one clean call was completed, then its idempotency "
            f"record, dispatch attempt and receipt were backdated "
            f"{aged['age_days']} days and the sweep a restarted gateway runs "
            f"was executed. The record was still present "
            f"({aged['record_survived_sweep']}); the same key then returned "
            f"status '{aged['replay_status']}' carrying receipt "
            f"{aged['replay_receipt_id']} -- the original receipt: "
            f"{aged['replay_returned_original_receipt']} -- with "
            f"{aged['dispatches_after_replay'] - aged['dispatches_after_first_call']} "
            "further request(s) across the fault layer and "
            f"{aged['executions_after_replay']} downstream execution(s) "
            "recorded by the effect ledger for that operation in total. "
            f"effect_free_release: the gateway was killed at {CRASH_BOUNDARY} "
            f"({release['boundary_description']}), leaving "
            f"{release['attempt_rows_left_by_crash']} attempt row(s), "
            f"{release['debits_left_by_crash']} debit(s) and "
            f"{release['receipts_left_by_crash']} receipt(s); after aging and "
            f"the sweep the record was released "
            f"({release['record_released_by_sweep']}), the same-key retry "
            f"returned '{release['retry_status']}' and dispatched "
            f"({release['retry_dispatched']}), and the effect ledger counted "
            f"{release['executions_at_crash']} downstream execution(s) for "
            "that operation before the release and "
            f"{release['executions_total_for_operation']} in total after the "
            "retry. "
            f"record_removed (descriptive, outside the verdict): the "
            "idempotency record was deleted by hand (record gone: "
            f"{removed['record_gone_after_purge']}); the same key then "
            f"returned '{removed['retry_status']}', dispatched "
            f"({removed['retry_dispatched']}), produced a second downstream "
            f"effect ({removed['second_downstream_effect']}), and left "
            f"{len(removed['new_debits_from_retry'])} further debit(s) and "
            f"{len(removed['new_receipts_from_retry'])} further receipt(s). "
            f"Removing the record took {len(removed['purge']['steps'])} step(s) "
            f"(direct DELETE accepted: "
            f"{removed['purge']['direct_delete_succeeded']}), and the signed "
            f"receipt(s) {removed['receipts_surviving_purge']} outlived it. "
            + (
                "The answer to what remains after expiration is therefore: "
                "the replay guarantee lasts as long as the idempotency record "
                "row and not one moment longer, and the one sweep this run "
                "exercised did not age such a row out."
                if not automatic_expiry_observed
                else (
                    "The aged record did NOT survive the sweep, so an "
                    "automatic expiry does exist and the claim this scenario "
                    "tests -- that the only automatic release is a provably "
                    "effect-free one -- does not hold as written."
                )
            )
        )
        if (
            target.configuration.native_idempotency
            and removed["retry_dispatched"]
            and not removed["second_downstream_effect"]
        ):
            observation += (
                " In this configuration the second dispatch did not become a "
                "second downstream effect, because the tool itself honours the "
                "business operation_id. That protection is the downstream's, "
                "not the gateway's: the gateway dispatched again -- the fault "
                "layer counted the crossing -- and charged again, leaving "
                f"{len(removed['new_debits_from_retry'])} further debit(s) and "
                f"{len(removed['new_receipts_from_retry'])} further receipt(s)."
            )
        if failures:
            observation += " Guarantee not met: " + "; ".join(failures) + "."

        risks = [
            "The replay guarantee lasts exactly as long as the idempotency "
            "record does. This run aged a completed record "
            f"{aged['age_days']} days and the sweep "
            + ("left it alone" if aged["record_survived_sweep"] else "removed it")
            + ", so the lifetime observed here is a property of a database "
            "row, not of a published retention policy. Nothing in the product "
            "states how long that row is kept, and the day an operator adds a "
            "purge job, a TTL index or a table trim, every key it touches "
            "silently becomes replayable again with no corresponding change "
            "to any documented promise.",
            "The record-removal case shows what that day looks like: with the "
            "record gone the same key is a new operation. Here the gateway "
            + (
                "dispatched again -- the fault layer counted the crossing -- "
                f"and wrote {len(removed['new_debits_from_retry'])} further "
                "debit(s)"
                if removed["retry_dispatched"]
                else (
                    f"returned '{removed['retry_status']}' without crossing "
                    "the fault layer again"
                )
            )
            + ", and whether a second downstream effect landed (it "
            + ("did" if removed["second_downstream_effect"] else "did not")
            + ") depended on whether the downstream tool had idempotency of "
            "its own -- not on anything the gateway did.",
        ]
        if removed["record_gone_after_purge"] and removed["receipts_surviving_purge"]:
            risks.append(
                "The signed receipt outlives the record that made the key "
                f"unrepeatable: receipt(s) {removed['receipts_surviving_purge']} "
                "were still present after the record was purged. Evidence that "
                "the call happened therefore persists longer than the "
                "mechanism that stops it happening twice, so a receipt search "
                "is not a substitute for the replay guarantee."
            )
        risks += [
            "The effect-free release is keyed on the absence of a dispatch "
            "attempt, a ledger link and a receipt -- not on age. Aging only "
            "decides when the sweep is allowed to look. A record that is "
            "effect-free is released whatever its age; a record that is not "
            "is retained whatever its age.",
            "Time here is moved by rewriting timestamps, and the sweep is run "
            "with a zero idle window. This measures what the sweep decides, "
            "not how long a real deployment waits before it decides it.",
            "The crash is a simulated process death raised at an instrumented "
            "durable boundary in-process, not a SIGKILL of a separate OS "
            "process, and the whole run is against SQLite rather than the "
            "PostgreSQL row-lock path.",
        ]
        if removed["purge"]["direct_delete_succeeded"] is False:
            risks.append(
                "A plain DELETE of the idempotency record was refused by the "
                "foreign keys the dispatch attempt and the receipt hold on it. "
                "That is a real obstacle to an accidental purge, but it is not "
                "a retention policy: "
                + (
                    "the purge succeeded as soon as those references were "
                    "cleared, which is exactly what a deliberate retention job "
                    "would do"
                    if removed["record_gone_after_purge"]
                    else (
                        "in this run the record was still present after the "
                        "referencing rows were cleared, so the purge did not "
                        "complete and the rest of this case describes a record "
                        "that never went away"
                    )
                )
                + "."
            )

        log.emit(
            "verdict",
            f"{target.configuration.value} -> {verdict.value}",
            scenario=self.test_id,
            configuration=target.configuration.value,
            aged_record_survived=aged["record_survived_sweep"],
            aged_record_replayed_original_receipt=aged[
                "replay_returned_original_receipt"
            ],
            aged_record_replay_dispatched=aged["replay_dispatched"],
            aged_record_executions_before_aging=aged["executions_after_first_call"],
            effect_free_release_released=release["record_released_by_sweep"],
            effect_free_release_dispatches_at_crash=release["dispatches_at_crash"],
            effect_free_release_executions_at_crash=release["executions_at_crash"],
            effect_free_release_executions_total=release[
                "executions_total_for_operation"
            ],
            record_removed_retry_dispatched=removed["retry_dispatched"],
            record_removed_second_effect=removed["second_downstream_effect"],
            automatic_expiry_observed=automatic_expiry_observed,
            failures=failures,
        )
        return self.result(
            target,
            verdict=verdict,
            observation=observation,
            measurements=measurements,
            attempts=attempts,
            remaining_risks=risks,
            extra=extra,
        )
