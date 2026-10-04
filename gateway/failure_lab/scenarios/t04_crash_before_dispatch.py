"""Test 4 -- Crash before dispatch."""

from __future__ import annotations

from typing import Any

from failure_lab.configurations import (
    GATEWAY_CONFIGURATIONS,
    AttemptOutcome,
    Configuration,
    Target,
)
from failure_lab.gateway import CRASH_BOUNDARY_DESCRIPTIONS, GatewaySnapshot
from failure_lab.identity import KeyPolicy, OperationIdentity
from failure_lab.scenarios.base import (
    ConfigurationResult,
    EventLog,
    Measurements,
    Scenario,
    Verdict,
)

#: Every instrumented durable boundary that commits strictly BEFORE the
#: one-shot dispatch claim. A crash at any of them is provably non-delivered.
PRE_DISPATCH_BOUNDARIES = (
    "after_idempotency_begin",
    "after_prepare",
    "after_debit",
    "after_attach_charge",
)

#: How far the lab moves the attempt clock back before sweeping. The real
#: window a live claim gets is 11,430 seconds; the lab cannot wait it out, so
#: it ages the rows instead and says so in ``limitations``.
DEFAULT_BACKDATE_SECONDS = 20_000

#: Client patience. Nothing here is slow -- the crash raises immediately and
#: the retries are local -- so this only has to be generous enough that a
#: harness stall is never mistaken for a product behaviour.
DEFAULT_TIMEOUT_SECONDS = 15.0

#: The outcome a reconciled pre-dispatch crash is documented to sign.
REFUNDED_OUTCOME = "failed_refunded"

#: Outcomes that would assert the gateway knows the call reached the tool.
#: Nothing crossed the send boundary here, so either one is a false claim.
OVERCLAIMED_OUTCOMES = ("success", "delivery_uncertain")


def _added(
    before: list[dict[str, Any]], after: list[dict[str, Any]], key: str
) -> list[dict[str, Any]]:
    """Rows in ``after`` that were not in ``before``, matched on ``key``."""
    seen = {row[key] for row in before}
    return [row for row in after if row[key] not in seen]


async def _restore_never_dispatched(attempt_ids: list[str]) -> int:
    """Undo the one field ``backdate_attempts`` must not have written.

    ``GatewayUnderTest.backdate_attempts`` ages a wallet's attempts by writing
    ``updated_at``, ``created_at`` **and** ``dispatched_at``. The first two are
    the clock; the third is durable evidence that the send boundary was
    crossed. Pre-dispatch crash recovery refuses, correctly, to compensate an
    attempt whose ``dispatched_at`` is set (``dispatch_claim_unavailable`` in
    ``complete_pre_dispatch_failure``), so aging a ``prepared`` row with that
    helper makes the product look as though it failed to refund a call that
    never left the building.

    This restores ``dispatched_at = NULL`` on exactly the rows that had it
    NULL immediately before the helper ran, so the aging stands and the forged
    evidence does not. The count is reported in ``extra`` for every sub-case.
    A fix in :mod:`failure_lab.gateway` is requested rather than made here,
    because that module is shared.
    """
    if not attempt_ids:
        return 0
    from sqlalchemy import update as sa_update

    from app.db.database import get_session_factory
    from app.db.models import McpDispatchAttemptModel

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            sa_update(McpDispatchAttemptModel)
            .where(McpDispatchAttemptModel.attempt_id.in_(attempt_ids))
            .values(dispatched_at=None)
        )
        await session.commit()
        return int(result.rowcount or 0)


class CrashBeforeDispatch(Scenario):
    """Kill the gateway between the debit and the send. Nothing left the building.

    WHAT TO IMPLEMENT
    -----------------
    For each boundary in ``("after_idempotency_begin", "after_prepare",
    "after_debit", "after_attach_charge")`` -- all strictly before the
    one-shot dispatch claim -- run an independent sub-case with its own
    payment id and key:

    1. ``with gateway.crash_at(boundary) as crash:`` submit once. The attempt
       returns ``gateway_process_died``; assert ``crash["fired"]``.
    2. Restart: reconciliation is what a restarted gateway runs. Call
       ``await gateway.backdate_attempts(tenant, seconds=...)`` (the lab
       cannot wait out the real idle window; record this as a limitation),
       then ``await gateway.reconcile(idle_seconds=0)``.
    3. Retry the SAME key, then retry with a FRESH key, and record both.
    4. Determine and record, per boundary: whether any dispatch occurred
       (fault-layer crossings for that operation), what persisted (attempt
       state, debit, refund, receipt), whether the retry dispatched, and the
       resulting accounting state.
    5. Verdict: ``PASS`` iff, for every boundary, downstream_executions == 0
       AND gateway_dispatches == 0 AND the wallet ends with no net debit for
       that operation (refunded or never charged) AND the same-key retry does
       not dispatch. ``FAIL`` on any downstream effect, any net charge for an
       operation that never left the gateway, or a same-key retry that
       dispatches.
    6. Put a per-boundary table in ``extra["boundaries"]``: boundary,
       description (``CRASH_BOUNDARY_DESCRIPTIONS``), dispatches, executions,
       attempt state after reconcile, net debit, receipt outcome, same-key
       retry status, fresh-key retry status.

    Use one fresh operation id per boundary and pass every one of them to
    ``self.measure(..., operation_ids=[...])`` so the totals cover the whole
    sub-case set.
    """

    test_id = "T04"
    title = "Crash before dispatch"
    claim = (
        "A gateway that dies before its one-shot dispatch claim provably sent "
        "nothing, so recovery refunds the operation and a retry of the same "
        "key never reaches the downstream tool."
    )
    tier = "slow"
    configurations = GATEWAY_CONFIGURATIONS
    inapplicable_reason = (
        "a direct integration has no gateway to crash between a debit and a send"
    )
    #: FAIL, and it is the claim that fails rather than the product. The rule
    #: in :meth:`_verdict` fires at ``after_idempotency_begin`` because the
    #: same-key retry does reach the tool there -- correctly, per window E of
    #: docs/failure-semantics.md. The last entry in :attr:`limitations` makes
    #: the whole argument.
    #:
    #: THIS VALUE AND THAT RULE MOVE TOGETHER. Setting these back to PASS
    #: while the rule still fires makes observation and documentation
    #: disagree on every run, which reds the suite; deleting the rule to
    #: justify PASS removes the only assertion this scenario makes about the
    #: retry. Changing the claim is a decision for whoever owns it, taken in
    #: the open, and it changes both sites at once.
    expected = {
        Configuration.DIRECT_NAIVE.value: Verdict.NOT_APPLICABLE.value,
        Configuration.DIRECT_NATIVE.value: Verdict.NOT_APPLICABLE.value,
        Configuration.GATEWAY_NATIVE.value: Verdict.FAIL.value,
        Configuration.GATEWAY_NAIVE.value: Verdict.FAIL.value,
    }
    limitations = (
        "The crash is a simulated process death raised at an instrumented "
        "durable boundary in-process, not a SIGKILL of a separate OS process. "
        "Committed state stays committed and uncommitted state rolls back, "
        "which matches a kill's footprint, but it is not the same event. The "
        "repository's two-process PostgreSQL kill proof covers that.",
        "Attempt rows are backdated so reconciliation treats them as "
        "abandoned; the real idle window is far longer.",
        "Runs against SQLite, not the PostgreSQL row-lock path.",
        "THIS SCENARIO FAILS, AND THE PRODUCT IS NOT WHAT FAILED. The claim "
        "asserts that a same-key retry never reaches the tool. At "
        "after_idempotency_begin it does, and that is correct, documented "
        "behaviour: the crash landed before any reservation, debit, attempt "
        "or receipt, so there is nothing to compensate and the effect-free "
        "sweep releases the key (docs/failure-semantics.md, window E -- 'the "
        "record is deleted so the same key can genuinely retry... Nothing "
        "ever moved'). The retry then performs an operation that never "
        "happened: the effect ledger shows one execution in total and none "
        "before it. Holding the claim as written would mean refusing that "
        "refund for ever because a crashed attempt once wrote a row, which "
        "is strictly worse for the customer than performing it once. The "
        "claim is too strong and wants rewriting by whoever owns it, "
        "together with the expectation above. Until then the measurement is "
        "reported as it stands rather than softened, because a rule that "
        "cannot fire is not a weaker assertion -- it is no assertion.",
    )

    async def run_configuration(
        self, target: Target, log: EventLog
    ) -> ConfigurationResult:
        target.require_gateway()
        backdate_seconds = int(
            self.options.get("backdate_seconds", DEFAULT_BACKDATE_SECONDS)
        )
        timeout_seconds = float(
            self.options.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        )

        attempts: list[AttemptOutcome] = []
        operation_ids: list[str] = []
        rows: list[dict[str, Any]] = []

        for boundary in PRE_DISPATCH_BOUNDARIES:
            row, sub_attempts, operation_id = await self._run_boundary(
                target,
                log,
                boundary,
                backdate_seconds=backdate_seconds,
                timeout_seconds=timeout_seconds,
            )
            rows.append(row)
            attempts.extend(sub_attempts)
            operation_ids.append(operation_id)

        measurements = await self.measure(target, attempts, operation_ids=operation_ids)
        return self._verdict(
            target,
            log,
            rows,
            attempts,
            measurements,
            backdate_seconds=backdate_seconds,
        )

    # -- one boundary ----------------------------------------------------

    async def _run_boundary(
        self,
        target: Target,
        log: EventLog,
        boundary: str,
        *,
        backdate_seconds: int,
        timeout_seconds: float,
    ) -> tuple[dict[str, Any], list[AttemptOutcome], str]:
        """Crash at ``boundary``, recover, retry twice, and record everything."""
        gateway, tenant, _permit = target.require_gateway()
        configuration = target.configuration.value
        description = CRASH_BOUNDARY_DESCRIPTIONS[boundary]

        operation_id, refund = self.refund(f"pay_t04_{boundary}")
        identity = OperationIdentity.first_attempt(operation_id)

        before = await gateway.snapshot(tenant)
        log.emit(
            "boundary.start",
            f"{boundary}: {description}",
            scenario=self.test_id,
            configuration=configuration,
            boundary=boundary,
            boundary_description=description,
            operation_id=operation_id,
            idempotency_key=identity.idempotency_key,
        )

        # 1. Die at the boundary. Nothing has crossed the send boundary yet.
        with gateway.crash_at(boundary) as crash:
            crashed = await target.agent.submit(
                identity, refund, timeout_seconds=timeout_seconds
            )
        crash_fired = bool(crash["fired"])
        after_crash = await gateway.snapshot(tenant)
        dispatches_at_crash = target.injector.dispatch_count(operation_id)
        executions_at_crash = target.ledger.execution_count(operation_id)

        crashed_attempts = _added(before.attempts, after_crash.attempts, "attempt_id")
        crashed_attempt_ids = {row["attempt_id"] for row in crashed_attempts}
        # Read before backdating: ``backdate_attempts`` writes ``dispatched_at``
        # on every attempt row of the wallet, including rows that never
        # dispatched, so the snapshot's ``sent`` flag stops being evidence
        # after the lab ages the clock. This value is taken while it still is.
        marked_sent_at_crash = [row["sent"] for row in crashed_attempts]
        crashed_debits = _added(before.debits, after_crash.debits, "entry_id")
        crashed_records = _added(
            before.idempotency_records, after_crash.idempotency_records, "record_id"
        )
        crashed_record_ids = {row["record_id"] for row in crashed_records}

        log.emit(
            "boundary.crash",
            (
                f"{boundary}: gateway died -> status={crashed.status}, "
                f"fired={crash_fired}; {len(crashed_attempts)} attempt row(s), "
                f"{len(crashed_debits)} debit(s), {len(crashed_records)} "
                f"idempotency record(s) survived the death"
            ),
            scenario=self.test_id,
            configuration=configuration,
            boundary=boundary,
            crash_fired=crash_fired,
            crash_fired_at=crash.get("fired_at"),
            status=crashed.status,
            client_visible_state=crashed.client_visible_state,
            reason=crashed.reason,
            attempt_states=[row["state"] for row in crashed_attempts],
            attempt_marked_sent=marked_sent_at_crash,
            debit_entry_ids=[row["entry_id"] for row in crashed_debits],
            downstream_requests=dispatches_at_crash,
            downstream_executions=executions_at_crash,
        )

        # 2. Restart: what a restarted gateway runs is reconciliation.
        never_dispatched = [
            row["attempt_id"]
            for row in after_crash.attempts
            if row["dispatched_at"] is None
        ]
        backdated = await gateway.backdate_attempts(tenant, seconds=backdate_seconds)
        restored = await _restore_never_dispatched(never_dispatched)
        reconciliation = await gateway.reconcile(idle_seconds=0)
        after_reconcile = await gateway.snapshot(tenant)
        dispatches_after_reconcile = target.injector.dispatch_count(operation_id)
        executions_after_reconcile = target.ledger.execution_count(operation_id)

        attempt_states_after = [
            row["state"]
            for row in after_reconcile.attempts
            if row["attempt_id"] in crashed_attempt_ids
        ]
        attempt_state = (
            attempt_states_after[0] if attempt_states_after else "no_attempt_row"
        )
        attempt_sent_after = [
            row["sent"]
            for row in after_reconcile.attempts
            if row["attempt_id"] in crashed_attempt_ids
        ]
        refund_ids = {f"refund-{row['entry_id']}" for row in crashed_debits}
        crash_refunds = [
            row for row in after_reconcile.refunds if row["entry_id"] in refund_ids
        ]
        recovery_receipts = _added(
            before.receipts, after_reconcile.receipts, "receipt_id"
        )
        receipt_outcome = recovery_receipts[0]["outcome"] if recovery_receipts else None
        records_after = [
            row
            for row in after_reconcile.idempotency_records
            if row["record_id"] in crashed_record_ids
        ]
        net_debit = len(crashed_debits) - len(crash_refunds)

        log.emit(
            "boundary.reconcile",
            (
                f"{boundary}: reconciled -> attempt state {attempt_state!r}, "
                f"{len(crashed_debits)} debit(s) and {len(crash_refunds)} "
                f"correlated refund(s) (net {net_debit}), receipt outcome "
                f"{receipt_outcome!r}"
            ),
            scenario=self.test_id,
            configuration=configuration,
            boundary=boundary,
            attempts_backdated=backdated,
            backdate_seconds=backdate_seconds,
            dispatched_at_restored=restored,
            reconciliation=reconciliation,
            attempt_state=attempt_state,
            attempt_marked_sent=attempt_sent_after,
            net_debit=net_debit,
            receipt_outcome=receipt_outcome,
            idempotency_record_present=bool(records_after),
            idempotency_record_completed=[row["completed"] for row in records_after],
            downstream_requests=dispatches_after_reconcile,
            downstream_executions=executions_after_reconcile,
        )

        # 3a. Retry the SAME key.
        retry_identity = identity.retry()
        same_key = await target.agent.submit(
            retry_identity, refund, timeout_seconds=timeout_seconds
        )
        dispatches_after_same_key = target.injector.dispatch_count(operation_id)
        executions_after_same_key = target.ledger.execution_count(operation_id)
        same_key_dispatched = dispatches_after_same_key > dispatches_after_reconcile
        log.emit(
            "boundary.retry.same_key",
            (
                f"{boundary}: same-key retry -> status={same_key.status} "
                f"({same_key.client_visible_state}); dispatched="
                f"{same_key_dispatched}"
            ),
            scenario=self.test_id,
            configuration=configuration,
            boundary=boundary,
            idempotency_key=retry_identity.idempotency_key,
            same_key_as_crash=(
                retry_identity.idempotency_key == identity.idempotency_key
            ),
            status=same_key.status,
            client_visible_state=same_key.client_visible_state,
            reason=same_key.reason,
            receipt_id=same_key.receipt_id,
            receipt_outcome=(
                same_key.receipt.get("outcome") if same_key.receipt else None
            ),
            dispatched=same_key_dispatched,
            downstream_requests=dispatches_after_same_key,
            downstream_executions=executions_after_same_key,
        )

        # 3b. Retry with a FRESH key for the same business operation.
        fresh_identity = OperationIdentity.first_attempt(
            operation_id, key_policy=KeyPolicy.ATTEMPT
        )
        fresh_key = await target.agent.submit(
            fresh_identity, refund, timeout_seconds=timeout_seconds
        )
        dispatches_after_fresh_key = target.injector.dispatch_count(operation_id)
        executions_after_fresh_key = target.ledger.execution_count(operation_id)
        fresh_key_dispatched = dispatches_after_fresh_key > dispatches_after_same_key
        after_fresh = await gateway.snapshot(tenant)
        log.emit(
            "boundary.retry.fresh_key",
            (
                f"{boundary}: fresh-key retry (same business operation, new "
                f"idempotency key) -> status={fresh_key.status} "
                f"({fresh_key.client_visible_state}); dispatched="
                f"{fresh_key_dispatched}"
            ),
            scenario=self.test_id,
            configuration=configuration,
            boundary=boundary,
            idempotency_key=fresh_identity.idempotency_key,
            status=fresh_key.status,
            client_visible_state=fresh_key.client_visible_state,
            reason=fresh_key.reason,
            receipt_id=fresh_key.receipt_id,
            receipt_outcome=(
                fresh_key.receipt.get("outcome") if fresh_key.receipt else None
            ),
            dispatched=fresh_key_dispatched,
            downstream_requests=dispatches_after_fresh_key,
            downstream_executions=executions_after_fresh_key,
            wallet_balance=after_fresh.wallet_balance,
        )

        row = {
            # ``case`` is the suite-wide name for a sub-case row, so a report
            # that renders all fourteen scenarios side by side can key on one
            # field. ``boundary`` is kept because this scenario's
            # specification names it.
            "case": boundary,
            "boundary": boundary,
            "description": description,
            "operation_id": operation_id,
            "idempotency_key": identity.idempotency_key,
            "crash_fired": crash_fired,
            "crash_status": crashed.status,
            "crash_client_visible_state": crashed.client_visible_state,
            # "dispatches"/"executions" are the crashed operation's own numbers,
            # measured after the crash AND after reconciliation and before any
            # retry: this is the "nothing left the building" claim.
            "dispatches": dispatches_after_reconcile,
            "executions": executions_after_reconcile,
            "dispatches_at_crash": dispatches_at_crash,
            "executions_at_crash": executions_at_crash,
            "attempt_state_after_reconcile": attempt_state,
            "attempt_marked_sent_at_crash": marked_sent_at_crash,
            "attempt_marked_sent_after_reconcile": attempt_sent_after,
            "attempt_sent_flag_note": (
                "after_reconcile 'sent' is not evidence: backdate_attempts "
                "writes dispatched_at on every attempt row of the wallet, "
                "including rows that never dispatched. The at_crash value and "
                "the fault layer are the trustworthy instruments."
            ),
            "attempt_rows_created_by_crash": len(crashed_attempts),
            "debits_created_by_crash": len(crashed_debits),
            "refunds_correlated_to_those_debits": len(crash_refunds),
            "net_debit": net_debit,
            "credits_debited": [entry["amount"] for entry in crashed_debits],
            "credits_refunded": [entry["amount"] for entry in crash_refunds],
            "receipt_outcome": receipt_outcome,
            "receipt_matches_documented_outcome": (receipt_outcome == REFUNDED_OUTCOME),
            "receipt_outcomes_from_recovery": [
                entry["outcome"] for entry in recovery_receipts
            ],
            "receipt_credits_charged": [
                entry["credits_charged"] for entry in recovery_receipts
            ],
            "idempotency_record_survived_reconcile": bool(records_after),
            "idempotency_record_completed": [
                entry["completed"] for entry in records_after
            ],
            "reconciliation": reconciliation,
            "attempts_backdated": backdated,
            "dispatched_at_restored_after_backdating": restored,
            "backdating_note": (
                "gateway.backdate_attempts writes dispatched_at on every "
                "attempt of the wallet, including rows that never dispatched. "
                "This sub-case restores dispatched_at=NULL on exactly the rows "
                "that had it NULL before the helper ran; without that, "
                "pre-dispatch recovery refuses to compensate with "
                "dispatch_claim_unavailable and the product is blamed for a "
                "harness artifact."
            ),
            "same_key_retry_status": same_key.status,
            "same_key_retry_client_visible_state": same_key.client_visible_state,
            "same_key_retry_reason": same_key.reason,
            "same_key_retry_receipt_outcome": (
                same_key.receipt.get("outcome") if same_key.receipt else None
            ),
            "same_key_retry_dispatched": same_key_dispatched,
            "executions_after_same_key_retry": executions_after_same_key,
            "fresh_key_retry_status": fresh_key.status,
            "fresh_key_retry_client_visible_state": fresh_key.client_visible_state,
            "fresh_key_retry_reason": fresh_key.reason,
            "fresh_key_retry_receipt_outcome": (
                fresh_key.receipt.get("outcome") if fresh_key.receipt else None
            ),
            "fresh_key_retry_dispatched": fresh_key_dispatched,
            "executions_after_fresh_key_retry": executions_after_fresh_key,
            "wallet_balance_after_boundary": after_fresh.wallet_balance,
        }
        return row, [crashed, same_key, fresh_key], operation_id

    # -- verdict ---------------------------------------------------------

    def _verdict(
        self,
        target: Target,
        log: EventLog,
        rows: list[dict[str, Any]],
        attempts: list[AttemptOutcome],
        measurements: Measurements,
        *,
        backdate_seconds: int,
    ) -> ConfigurationResult:
        failures: list[str] = []
        for row in rows:
            boundary = row["boundary"]
            if not row["crash_fired"]:
                failures.append(
                    f"{boundary}: the instrumented crash never fired, so this "
                    "sub-case measured nothing"
                )
            if row["crash_status"] != "gateway_process_died":
                failures.append(
                    f"{boundary}: the crashed call returned "
                    f"'{row['crash_status']}', want 'gateway_process_died'"
                )
            if row["executions"] != 0:
                failures.append(
                    f"{boundary}: downstream executions (effect ledger) = "
                    f"{row['executions']} for a call that died before the "
                    "dispatch claim, want 0"
                )
            if row["dispatches"] != 0:
                failures.append(
                    f"{boundary}: gateway dispatches (fault layer) = "
                    f"{row['dispatches']} for a call that died before the "
                    "dispatch claim, want 0"
                )
            if row["net_debit"] != 0:
                failures.append(
                    f"{boundary}: net debit = {row['net_debit']} for an "
                    "operation that never left the gateway, want 0 (refunded "
                    "or never charged)"
                )
            # The rule this scenario's specification states, implemented as
            # stated: "FAIL on ... a same-key retry that dispatches."
            #
            # A previous revision replaced this with
            # `executions_after_same_key_retry > 1`. That condition cannot
            # produce a failure of its own: the loop already fails whenever
            # `executions != 0`, so with zero executions through recovery a
            # single same-key retry can add at most one, and any run that
            # could trip it has already failed above. Swapping a rule for a
            # condition that can never change a verdict is not relocating the
            # assertion -- it is deleting it. Whether the claim SHOULD assert
            # the absence of a dispatch (window E of
            # docs/failure-semantics.md makes a real argument that it should
            # not) is a question for whoever owns the claim; it is not a
            # question this file may answer by rewriting its own claim to
            # match what it measured.
            # If this rule is ever removed or relaxed, `expected` above has to
            # change in the same commit -- see the note on it.
            if row["same_key_retry_dispatched"]:
                failures.append(
                    f"{boundary}: the same-key retry reached the downstream "
                    f"tool (status '{row['same_key_retry_status']}'), and the "
                    "claim says it never does"
                )
            # Kept as a second, independent condition: even where a dispatch
            # is arguably safe, a DUPLICATE effect never is.
            if row["executions_after_same_key_retry"] > 1:
                failures.append(
                    f"{boundary}: the operation has "
                    f"{row['executions_after_same_key_retry']} downstream "
                    "execution(s) after a same-key retry, want at most 1 -- a "
                    "retry of a key whose call died before the send boundary "
                    "must never produce a duplicate effect"
                )
            overclaimed = [
                outcome
                for outcome in row["receipt_outcomes_from_recovery"]
                if outcome in OVERCLAIMED_OUTCOMES
            ]
            if overclaimed:
                failures.append(
                    f"{boundary}: recovery signed a "
                    f"{', '.join(repr(o) for o in overclaimed)} receipt for a "
                    "call that provably never crossed the send boundary"
                )
            if any(row["attempt_marked_sent_at_crash"]):
                failures.append(
                    f"{boundary}: the crashed attempt row is marked as having "
                    "crossed the send boundary, which the fault layer contradicts"
                )

        verdict = Verdict.PASS if not failures else Verdict.FAIL

        crashed_dispatch_total = sum(row["dispatches"] for row in rows)
        crashed_execution_total = sum(row["executions"] for row in rows)
        net_debit_total = sum(row["net_debit"] for row in rows)
        same_key_dispatched = [
            row["boundary"] for row in rows if row["same_key_retry_dispatched"]
        ]
        fresh_key_dispatched = [
            row["boundary"] for row in rows if row["fresh_key_retry_dispatched"]
        ]
        snapshot: GatewaySnapshot | None = measurements.snapshot

        extra: dict[str, Any] = {
            # Same list under both names: "cases" is the suite-wide key for a
            # per-sub-case table, "boundaries" is the name this scenario's
            # specification uses.
            "cases": rows,
            "boundaries": rows,
            "boundary_order": list(PRE_DISPATCH_BOUNDARIES),
            # Named so failure_lab.evidence._FAULT_KEYS_IN_EXTRA can find the
            # injected failures. A gateway crash never crosses the fault
            # layer, so without this the evidence bundle's fault-injection
            # table would show nothing at all for this scenario.
            "crash_boundary": list(PRE_DISPATCH_BOUNDARIES),
            "backdate_seconds": backdate_seconds,
            "crashed_operations_dispatches_total": crashed_dispatch_total,
            "crashed_operations_executions_total": crashed_execution_total,
            "crashed_operations_net_debit_total": net_debit_total,
            "same_key_retry_dispatched_at": same_key_dispatched,
            "fresh_key_retry_dispatched_at": fresh_key_dispatched,
            "receipt_outcomes_from_recovery": [row["receipt_outcome"] for row in rows],
            "attempt_states_after_reconcile": [
                row["attempt_state_after_reconcile"] for row in rows
            ],
            "measured_scope_note": (
                "'dispatches' and 'executions' in the boundary table are the "
                "crashed operation's own counts, taken after the crash and "
                "after reconciliation and before either retry. The "
                "configuration-level counters cover the whole sub-case set, "
                "retries included, so they are deliberately larger."
            ),
            "verdict_failures": failures,
            "wallet_balance": snapshot.wallet_balance if snapshot else None,
            "gateway_dispatches_all_cases": measurements.counters.gateway_dispatches,
            "downstream_executions_all_cases": measurements.downstream_executions,
            "gateway_debits_all_cases": measurements.counters.gateway_debits,
            "gateway_refunds_all_cases": measurements.counters.gateway_refunds,
            "gateway_net_debits_all_cases": measurements.counters.gateway_net_debits,
            "receipts_all_cases": measurements.counters.receipts,
            "receipt_outcomes_all_cases": measurements.receipt_outcomes(),
        }

        lines = [
            (
                f"{row['boundary']} ({row['description']}): crash status "
                f"{row['crash_status']}; fault layer saw {row['dispatches']} "
                f"request(s) and the effect ledger {row['executions']} "
                f"execution(s) for this operation through recovery; attempt "
                f"state after reconcile {row['attempt_state_after_reconcile']}; "
                f"{row['debits_created_by_crash']} debit(s) and "
                f"{row['refunds_correlated_to_those_debits']} correlated "
                f"refund(s) (net {row['net_debit']}); recovery receipt "
                f"{row['receipt_outcome']}; same-key retry "
                f"{row['same_key_retry_status']} "
                f"(dispatched={row['same_key_retry_dispatched']}); fresh-key "
                f"retry {row['fresh_key_retry_status']} "
                f"(dispatched={row['fresh_key_retry_dispatched']})"
            )
            for row in rows
        ]

        observation = (
            "The gateway was killed at each of the four durable boundaries that "
            "commit before the one-shot dispatch claim, and a restarted "
            f"gateway's reconciliation sweep was run after each death. "
            f"Independent instruments across the four crashed calls: "
            f"{crashed_dispatch_total} request(s) crossed the fault layer and "
            f"{crashed_execution_total} downstream execution(s) were recorded "
            "by the effect ledger, so no crashed call reached the tool. "
            f"Gateway-reported accounting for those same calls nets to "
            f"{net_debit_total} debit(s) after correlated refunds. Per boundary "
            "-- " + "; ".join(lines) + ". "
            "Both retries are reported because they answer different "
            "questions: the same-key retry asks what a well-built agent that "
            "re-derives its key from the business operation sees, and the "
            "fresh-key retry asks what an agent that mints a new key sees. "
            f"The fresh-key retry dispatched at {fresh_key_dispatched or 'no'} "
            "boundary/boundaries, which is the documented consequence of a "
            "guarantee keyed on the idempotency key rather than on the "
            "business operation; Test 6 is the scenario that measures that gap "
            "directly. The configuration-level counters below cover all twelve "
            "submissions, retries included, and are therefore larger than the "
            "crashed-call numbers quoted here."
        )
        if same_key_dispatched:
            observation += (
                " DIVERGENCE FROM THE CLAIM: the claim states that a retry of "
                "the same key never reaches the downstream tool, but at "
                f"{', '.join(same_key_dispatched)} it did. The product "
                "documents this window (docs/failure-semantics.md, window E): "
                "a crash before any reservation, debit, attempt or receipt "
                "leaves nothing to compensate, so the effect-free sweep "
                "deletes the idempotency record and the same key is genuinely "
                "free to run the operation. That is a safe retry of an "
                "operation that never happened, not a duplicate -- the effect "
                "ledger shows no execution before it, and the operation ends "
                "with one execution in total. There is a real argument that "
                "the claim as written asks for more than safety requires, and "
                "a real argument that a key a caller has already spent should "
                "not silently become spendable again. Adjudicating that is a "
                "job for whoever owns the claim and the expectation map. What "
                "this instrument may not do is rewrite its own claim to match "
                "its own measurement, so the divergence is recorded as a "
                "failure of the claim as written rather than hidden."
            )
        if failures:
            observation += " Guarantee not met: " + "; ".join(failures) + "."

        risks = [
            "The crash is raised in-process at an instrumented boundary. It "
            "reproduces a kill's durable footprint -- committed state stays, "
            "uncommitted state rolls back -- but it is not an OS process kill. "
            "The repository's two-process PostgreSQL kill proof is what covers "
            "that; this scenario does not.",
            "Reconciliation here runs against backdated attempt rows with a "
            "zero idle window. In production a crashed pre-dispatch attempt "
            "waits out a fixed 11,430-second window plus sweep latency before "
            "it is touched, so the money is held and the key is blocked for "
            "roughly three hours. This scenario measures the outcome of the "
            "sweep, not the delay in reaching it.",
            "A refunded pre-dispatch crash gives the agent a terminal "
            "'failed_refunded' answer, not a completed refund. The business "
            "operation still has not happened, and nothing in the gateway "
            "drives it to completion -- the agent has to notice and start a "
            "new operation identity.",
            "The gateway's protection is keyed on the idempotency key. An "
            "agent that mints a fresh key for the same business operation is a "
            "new operation as far as the gateway is concerned, which is why "
            "the fresh-key retry dispatches and is charged.",
        ]
        if measurements.downstream_executions:
            risks.append(
                "The downstream executions counted at configuration level are "
                "the retries', not the crashed calls'. Reading that counter "
                "without the per-boundary table would misattribute them."
            )

        log.emit(
            "verdict",
            f"{target.configuration.value} -> {verdict.value}",
            scenario=self.test_id,
            configuration=target.configuration.value,
            crashed_operations_dispatches_total=crashed_dispatch_total,
            crashed_operations_executions_total=crashed_execution_total,
            crashed_operations_net_debit_total=net_debit_total,
            receipt_outcomes=[row["receipt_outcome"] for row in rows],
            same_key_retry_dispatched_at=same_key_dispatched,
            fresh_key_retry_dispatched_at=fresh_key_dispatched,
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
