"""Test 11 -- Database restart."""

from __future__ import annotations

import asyncio
import contextlib
import time
from decimal import Decimal
from typing import Any

from failure_lab.configurations import (
    GATEWAY_CONFIGURATIONS,
    Agent,
    AttemptOutcome,
    Configuration,
    Target,
)
from failure_lab.gateway import BoundaryHold, GatewaySnapshot
from failure_lab.identity import OperationIdentity
from failure_lab.refund_tool import RefundRequest
from failure_lab.scenarios.base import (
    ConfigurationResult,
    EventLog,
    Measurements,
    Scenario,
    Verdict,
)

#: The three sub-cases, ordered by how far the operation has travelled when
#: persistence is taken away. ``None`` means "the whole call happens inside
#: the outage"; the others hold the in-flight call at an instrumented durable
#: boundary and take the database away while it is paused.
CASES: tuple[tuple[str, str | None], ...] = (
    ("outage_before_request", None),
    ("outage_after_prepare", "after_prepare"),
    ("outage_after_claim", "after_claim"),
)

#: What the call has durably achieved when the outage starts.
CASE_MEANING: dict[str, str] = {
    "outage_before_request": (
        "nothing; the database is already gone when the request arrives"
    ),
    "outage_after_prepare": (
        "budget reserved and attempt row prepared; no debit, no dispatch claim"
    ),
    "outage_after_claim": (
        "one-shot dispatch claim committed; the send and the terminal write "
        "both happen with no database"
    ),
}

#: Attempt states the product defines. Anything else is a corrupted row.
#: Kept as literals rather than imported from ``app`` so the instrument does
#: not inherit the product's own idea of what is valid.
TERMINAL_ATTEMPT_STATES = frozenset(
    {"succeeded", "returned_error", "delivery_uncertain", "response_rejected"}
)
ACTIVE_ATTEMPT_STATES = frozenset({"prepared", "dispatched", "dispatch_claimed"})
VALID_ATTEMPT_STATES = TERMINAL_ATTEMPT_STATES | ACTIVE_ATTEMPT_STATES

#: Client-visible states that RESOLVE the business operation: the caller is
#: told what happened and needs nothing further. ``explicit_uncertain`` is
#: deliberately absent. It is a truthful answer and it is better than silence,
#: but it is not a resolution -- retrying the same key answers "unknowable"
#: again, for ever. A key the gateway admitted, that recovery left without a
#: terminal state, and whose same-key retry only ever says "uncertain" is
#: stuck: that is the PRD's lost accepted operation, not a safe retry.
RESOLVING_CLIENT_STATES = frozenset(
    {"confirmed_success", "confirmed_replay", "confirmed_rejected"}
)

#: States that are a definitive *answer* (a resolution, or a signed statement
#: that the outcome is unknowable). Reported for context; the verdict uses
#: :data:`RESOLVING_CLIENT_STATES`, which is the stricter set.
DEFINITIVE_CLIENT_STATES = RESOLVING_CLIENT_STATES | frozenset({"explicit_uncertain"})

#: How far back attempt rows are aged so the reconciler treats them as
#: abandoned. The real window a live claim gets is 11,430 seconds.
DEFAULT_BACKDATE_SECONDS = 20_000

#: Ceiling on the wait for an instrumented boundary to be reached.
HOLD_TIMEOUT_SECONDS = 20.0

#: Client patience. The outage makes the gateway fail fast, so this only has
#: to be generous enough that a harness stall is never mistaken for a product
#: behaviour.
DEFAULT_TIMEOUT_SECONDS = 30.0


def _exception_leaves(exc: BaseException, depth: int = 0) -> list[str]:
    """Flatten an ``ExceptionGroup`` into readable ``Type: message`` leaves."""
    if depth > 4:
        return [type(exc).__name__]
    inner = getattr(exc, "exceptions", None)
    if inner:
        leaves: list[str] = []
        for child in inner:
            leaves.extend(_exception_leaves(child, depth + 1))
        return leaves
    message = str(exc).strip().splitlines()
    head = message[0] if message else ""
    return [f"{type(exc).__name__}: {head}"[:200]]


async def _submit(
    agent: Agent,
    identity: OperationIdentity,
    refund: RefundRequest,
    *,
    configuration: str,
    timeout_seconds: float,
) -> AttemptOutcome:
    """Submit, recording an exception raised *through* the ASGI app as an attempt.

    With no database, some of the gateway's request handling raises rather
    than answering, and the lab's in-process transport
    (``httpx.ASGITransport(raise_app_exceptions=True)``) re-raises that
    exception in the caller instead of turning it into a response. A deployed
    server would answer 500 with no body. Both leave the caller in the same
    epistemic position -- no information about the business operation.

    ``GatewayUnderTest.invoke`` now classifies that case itself as
    ``gateway_error``; this is the backstop for any path that does not, so a
    scenario whose whole subject is "the database is gone" reports a row
    rather than erroring out. The exception text is kept verbatim either way.
    """
    started = time.perf_counter()
    try:
        return await agent.submit(identity, refund, timeout_seconds=timeout_seconds)
    except Exception as exc:  # noqa: BLE001 - the raise IS the observation
        leaves = _exception_leaves(exc)
        return AttemptOutcome(
            configuration=configuration,
            identity=identity.as_dict(),
            status="gateway_error",
            client_visible_state="no_information",
            http_status=None,
            latency_ms=(time.perf_counter() - started) * 1000,
            reason="; ".join(leaves)[:400],
            details={
                "raised_through_asgi_transport": True,
                "classified_by": "scenario backstop, not GatewayUnderTest.invoke",
                "exception_type": type(exc).__name__,
                "exception_leaves": leaves,
                "note": (
                    "the gateway app raised instead of answering; the lab's "
                    "in-process transport surfaces that as an exception where "
                    "a deployed server would answer 500 with no body"
                ),
            },
        )


def _credits(rows: list[dict[str, Any]]) -> Decimal:
    """Signed credit total of a set of ledger rows (debits are negative)."""
    total = Decimal("0")
    for row in rows:
        amount = row.get("amount")
        if amount is not None:
            total += Decimal(str(amount))
    return total


def _key_view(snapshot: GatewaySnapshot, idempotency_key: str) -> dict[str, Any]:
    """Every gateway row that belongs to one idempotency key.

    Attribution is by identity, not by snapshot diffing: the idempotency
    record carries the key, attempts and receipts carry the record id, and a
    governed debit carries the record id as its ``operation_key``. A refund
    is keyed ``refund-{entry_id}`` of the debit it compensates. This survives
    a record being deleted and re-created by a retry of the same key, which
    diffing would not.
    """
    records = [
        row
        for row in snapshot.idempotency_records
        if row["idempotency_key"] == idempotency_key
    ]
    record_ids = {row["record_id"] for row in records}
    attempts = [
        row for row in snapshot.attempts if row["idempotency_record_id"] in record_ids
    ]
    debits = [row for row in snapshot.debits if row.get("operation_key") in record_ids]
    refund_ids = {f"refund-{row['entry_id']}" for row in debits}
    refunds = [
        row
        for row in snapshot.refunds
        if row["entry_id"] in refund_ids or row.get("operation_key") in record_ids
    ]
    receipts = [
        row for row in snapshot.receipts if row["idempotency_record_id"] in record_ids
    ]
    refunded_entry_ids = {
        str(row["entry_id"])[len("refund-") :]
        for row in refunds
        if str(row["entry_id"]).startswith("refund-")
    }
    receipted_entry_ids = {
        row["ledger_entry_id"] for row in receipts if row.get("ledger_entry_id")
    }
    unsettled = [
        row["entry_id"]
        for row in debits
        if row["entry_id"] not in refunded_entry_ids
        and row["entry_id"] not in receipted_entry_ids
    ]
    return {
        "records": records,
        "record_ids": sorted(record_ids),
        "record_completed": [bool(row["completed"]) for row in records],
        "attempts": attempts,
        "attempt_ids": [row["attempt_id"] for row in attempts],
        "attempt_states": [row["state"] for row in attempts],
        "attempt_marked_sent": [bool(row["sent"]) for row in attempts],
        "debits": debits,
        "refunds": refunds,
        "receipts": receipts,
        "receipt_outcomes": [str(row["outcome"]) for row in receipts],
        "debit_count": len(debits),
        "refund_count": len(refunds),
        "net_debit_count": len(debits) - len(refunds),
        "net_charge_credits": str(-(_credits(debits) + _credits(refunds))),
        #: Debits with neither a correlated refund nor a receipt that names
        #: the ledger entry. This is the PRD's "inconsistent debit".
        "unsettled_debit_entry_ids": unsettled,
    }


async def _wait_for_boundary(
    hold: BoundaryHold, task: asyncio.Task[AttemptOutcome], timeout: float
) -> bool:
    """Wait until the held call reaches the boundary, or until it finishes.

    Returning on task completion matters: a call that fails before the
    instrumented boundary never pauses, and blocking for the full timeout
    would turn that real observation into a harness stall.
    """
    waiter = asyncio.create_task(hold.reached.wait())
    try:
        await asyncio.wait(
            {waiter, task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        waiter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiter
    return hold.reached.is_set()


async def _restore_never_dispatched(attempt_ids: list[str]) -> int:
    """Undo the one field ``backdate_attempts`` must not have written.

    ``GatewayUnderTest.backdate_attempts`` ages a wallet's attempts by writing
    ``updated_at``, ``created_at`` **and** ``dispatched_at``. The first two are
    the clock; the third is durable evidence that the send boundary was
    crossed. Pre-dispatch recovery refuses, correctly, to compensate an
    attempt whose ``dispatched_at`` is set, so aging a ``prepared`` row with
    that helper would make the product look as though it failed to refund a
    call that never left the building.

    This restores ``dispatched_at = NULL`` on exactly the rows that had it
    NULL immediately before the helper ran, so the aging stands and the forged
    evidence does not. The count is reported per sub-case. A fix in
    :mod:`failure_lab.gateway` is requested rather than made here, because
    that module is shared.
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


def _evaluate(case: dict[str, Any]) -> tuple[str, list[str], dict[str, int]]:
    """Classify one sub-case and count the PRD's damage measures.

    The counted measures are: duplicate dispatches (fault-layer crossings
    beyond the first for one key), duplicate downstream executions
    (effect-ledger rows beyond the first), inconsistent debits (a debit with
    neither a receipt nor a refund -- checked both immediately after
    reconciliation and again at the end of the sub-case, because in the
    sub-case that was never admitted the key's only debit is the one the
    post-recovery retry creates, so the after-recovery window alone is empty
    for it and would make the measure vacuous), corrupted state (an attempt
    row in no valid state), ``admitted_without_terminal_state`` (the PRD's
    phrase counted literally), and ``lost_accepted_operations`` -- the subset
    of those the same-key retry could not resolve either, which is what makes
    an admitted operation actually lost.

    It also raises the problems that make the *measurement* trustworthy
    rather than merely favourable: the sub-case has to have landed where it
    says it did, the outage has to have refused something, and a sub-case
    that reports "nothing happened" has to show that the same path works once
    the database is back -- otherwise "refused" is indistinguishable from
    "the tool never works" and every zero in the table is vacuous.
    """
    dispatches = int(case["dispatches_total"])
    executions = int(case["executions_total"])
    recovered = case["after_recovery"]
    duplicate_dispatches = max(0, dispatches - 1)
    duplicate_executions = max(0, executions - 1)
    unsettled_after_recovery = [str(x) for x in recovered["unsettled_debit_entry_ids"]]
    unsettled_at_end = [str(x) for x in case["final_unsettled_debit_entry_ids"]]
    unsettled_debits = sorted(set(unsettled_after_recovery) | set(unsettled_at_end))
    inconsistent_debits = len(unsettled_debits)
    corrupted_state = sum(
        1 for state in recovered["attempt_states"] if state not in VALID_ATTEMPT_STATES
    )

    admitted = bool(case["admitted_records"]) or bool(case["admitted_attempts"])
    attempt_states = list(recovered["attempt_states"])
    ended_terminal = bool(
        admitted
        and attempt_states
        and all(state in TERMINAL_ATTEMPT_STATES for state in attempt_states)
        and recovered["receipts"]
    )
    retry_state = case["retry_client_visible_state"]
    retry_definitive = retry_state in DEFINITIVE_CLIENT_STATES
    retry_resolves = retry_state in RESOLVING_CLIENT_STATES
    safely_retryable = bool(
        retry_resolves and not duplicate_dispatches and not duplicate_executions
    )
    #: The PRD's phrase, counted literally: the gateway admitted the operation
    #: and recovery left it without a terminal state. Reported on its own
    #: because it is not automatically a loss -- an admitted record that
    #: recovery *released* so the same key can genuinely retry ends with no
    #: terminal state and no damage.
    admitted_without_terminal = int(bool(admitted and not ended_terminal))
    lost = int(bool(admitted_without_terminal and not safely_retryable))

    problems: list[str] = []
    if duplicate_dispatches:
        problems.append(f"{dispatches} dispatches crossed the fault layer for one key")
    if duplicate_executions:
        problems.append(
            f"{executions} downstream executions (effect ledger) for one key"
        )
    if inconsistent_debits:
        problems.append(
            f"{inconsistent_debits} debit(s) left with neither a receipt nor a "
            f"refund: {', '.join(unsettled_debits)} (unsettled after "
            f"reconciliation: {unsettled_after_recovery or 'none'}; still "
            f"unsettled at the end of the sub-case: "
            f"{unsettled_at_end or 'none'})"
        )
    if corrupted_state:
        problems.append(
            f"{corrupted_state} attempt row(s) in no valid state: "
            f"{[s for s in attempt_states if s not in VALID_ATTEMPT_STATES]}"
        )
    if lost:
        problems.append(
            f"the gateway admitted this key (records="
            f"{len(case['admitted_records'])}, attempts="
            f"{len(case['admitted_attempts'])}) and recovery left it neither "
            f"terminal (states={attempt_states}, receipts="
            f"{recovered['receipt_outcomes']}) nor safely retryable "
            f"(same-key retry -> {case['retry_status']}/{retry_state}"
            f"{'' if retry_definitive else ', which is not even an answer'}"
            f"{', which is an answer but not a resolution' if retry_definitive and not retry_resolves else ''})"
        )
    if case["boundary"] is not None and not case["boundary_reached"]:
        problems.append(
            f"the call never paused at {case['boundary']}, so the outage did "
            "not land where this sub-case says it does"
        )
    if not case["refused_connections"]:
        problems.append(
            "the outage refused no connection, so nothing in this sub-case "
            "was measured against a missing database"
        )
    #: The premise each held sub-case states, checked rather than assumed.
    #: ``after_prepare`` pauses before the one-shot claim, so nothing may have
    #: crossed the fault layer; a send without a committed claim would be a
    #: breach of the dispatch discipline this whole test is about.
    if case["boundary"] == "after_prepare" and case["dispatches_during_outage"]:
        problems.append(
            f"{case['dispatches_during_outage']} dispatch(es) crossed the "
            "fault layer while the call was held before the one-shot claim, "
            "so the gateway sent without a committed claim"
        )
    #: ``after_claim`` pauses after the claim, so the send is supposed to
    #: happen and the TERMINAL WRITE is what the outage breaks. If nothing
    #: crossed the fault layer the sub-case never exercised that, and its
    #: zeros are vacuous rather than reassuring.
    if case["boundary"] == "after_claim" and case["dispatches_during_outage"] < 1:
        problems.append(
            "nothing crossed the fault layer while the database was gone, so "
            "this sub-case did not exercise the failed terminal write it "
            "names and its zero duplicate-execution count means nothing"
        )

    if corrupted_state:
        disposition = "corrupted"
    elif lost:
        disposition = "lost"
    elif ended_terminal:
        disposition = "terminal"
    elif admitted and safely_retryable:
        disposition = "safely_retryable"
    elif not admitted and case["dispatches_during_outage"] == 0:
        disposition = "failed_closed"
    else:
        # Everything else is excluded above, so this is the one remaining
        # shape: nothing durable was admitted, yet something crossed the
        # fault layer. The gateway sent and kept no record of having sent.
        disposition = "unrecorded_dispatch"
        problems.append(
            f"{case['dispatches_during_outage']} dispatch(es) crossed the "
            f"fault layer (status {case['outage_status']!r}) for a key the "
            "gateway kept no durable record of"
        )

    #: The control. ``failed_closed`` is the scenario's most comfortable
    #: reading -- nothing was admitted, nothing was sent, nothing was charged
    #: -- and it is exactly the reading a permanently broken tool would also
    #: produce. The key was never admitted, so it is genuinely fresh: once
    #: the database is back, the same key retried must actually do the
    #: business operation, reach the downstream through the fault layer and
    #: land one row in the independent effect ledger. Without that, every
    #: zero this sub-case reports is unfalsifiable.
    if disposition == "failed_closed" and not (
        int(case["retry_dispatches"]) >= 1
        and int(case["retry_executions"]) >= 1
        and retry_resolves
    ):
        problems.append(
            "the control did not hold: this sub-case reports that nothing "
            "happened during the outage, but after recovery the same key "
            f"retried produced {case['retry_dispatches']} fault-layer "
            f"dispatch(es) and {case['retry_executions']} effect-ledger "
            f"execution(s) and returned {case['retry_status']}/{retry_state}, "
            "so a refused operation cannot be told apart from a downstream "
            "that never works"
        )

    measures = {
        "duplicate_dispatches": duplicate_dispatches,
        "duplicate_downstream_executions": duplicate_executions,
        "inconsistent_debits": inconsistent_debits,
        "corrupted_state": corrupted_state,
        "lost_accepted_operations": lost,
        "admitted_without_terminal_state": admitted_without_terminal,
    }
    return disposition, problems, measures


class DatabaseRestart(Scenario):
    """Take the gateway's persistence away mid-operation and measure the damage.

    WHAT TO IMPLEMENT
    -----------------
    ``gateway.database_outage()`` is an async context manager that disposes
    the pool and refuses every new connection for its duration.

    Three sub-cases, each with its own payment id and key:

    ``outage_before_request``  submit entirely inside the outage. Expect a
                               failure with no downstream effect at all.
    ``outage_after_prepare``   hold with ``gateway.hold_at("after_prepare")``,
                               enter the outage while paused, release inside
                               it, exit the outage, then reconcile and retry.
    ``outage_after_claim``     the same, held after the dispatch claim, so the
                               terminal write is what the outage breaks.

    After each sub-case, exit the outage, run ``gateway.backdate_attempts``
    and ``gateway.reconcile(idle_seconds=0)``, then retry the same key.

    Measure and report exactly the PRD's list: lost accepted operations
    (records admitted but left with no terminal state after recovery),
    duplicate dispatches (fault-layer crossings > 1 for one key), inconsistent
    debits (a debit with no receipt and no refund after recovery), corrupted
    state (an attempt row in no valid state), and recovery behavior.

    Verdict: ``PASS`` iff across all sub-cases there is no duplicate dispatch,
    no duplicate downstream execution, no charge left without either a receipt
    or a refund after reconciliation, and every admitted key ends either
    terminal or safely retryable. ``FAIL`` otherwise. An operation that simply
    failed during the outage with nothing dispatched is a correct outcome, not
    a loss -- classify it as ``failed_closed`` rather than ``lost``.

    Record per sub-case rows in ``extra["cases"]`` and the outage's
    ``refused_connections`` count.
    """

    test_id = "T11"
    title = "Database restart"
    claim = (
        "A persistence outage during an operation never produces a duplicate "
        "dispatch, a duplicate downstream effect, or a charge with neither a "
        "receipt nor a refund."
    )
    tier = "slow"
    configurations = GATEWAY_CONFIGURATIONS
    inapplicable_reason = "a direct integration has no gateway database in the path"
    expected = {
        Configuration.DIRECT_NAIVE.value: Verdict.NOT_APPLICABLE.value,
        Configuration.DIRECT_NATIVE.value: Verdict.NOT_APPLICABLE.value,
        Configuration.GATEWAY_NATIVE.value: Verdict.PASS.value,
        Configuration.GATEWAY_NAIVE.value: Verdict.PASS.value,
    }
    limitations = (
        "The outage is simulated by refusing new connections at the engine, "
        "not by restarting a database server. It reproduces 'the database is "
        "unreachable', not crash recovery of the storage engine itself.",
        "Runs against SQLite. Production runs PostgreSQL, where failover and "
        "replication behaviour differ and are not covered here.",
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
        #: Off only to demonstrate what the harness artifact described in
        #: :func:`_restore_never_dispatched` does to the measurement.
        restore_dispatched_at = bool(self.options.get("restore_dispatched_at", True))

        attempts: list[AttemptOutcome] = []
        operation_ids: list[str] = []
        cases: list[dict[str, Any]] = []

        for name, boundary in CASES:
            case, case_attempts, operation_id = await self._run_case(
                target,
                log,
                name=name,
                boundary=boundary,
                backdate_seconds=backdate_seconds,
                timeout_seconds=timeout_seconds,
                restore_dispatched_at=restore_dispatched_at,
            )
            cases.append(case)
            attempts.extend(case_attempts)
            operation_ids.append(operation_id)

        measurements = await self.measure(target, attempts, operation_ids=operation_ids)
        return self._verdict(
            target,
            log,
            cases,
            attempts,
            measurements,
            backdate_seconds=backdate_seconds,
        )

    # -- one sub-case -----------------------------------------------------

    async def _run_case(
        self,
        target: Target,
        log: EventLog,
        *,
        name: str,
        boundary: str | None,
        backdate_seconds: int,
        timeout_seconds: float,
        restore_dispatched_at: bool = True,
    ) -> tuple[dict[str, Any], list[AttemptOutcome], str]:
        """Take the database away at ``boundary``, recover, retry the same key."""
        gateway, tenant, _permit = target.require_gateway()
        configuration = target.configuration.value

        operation_id, refund = self.refund(f"pay_t11_{name}")
        identity = OperationIdentity.first_attempt(operation_id)
        key = identity.idempotency_key

        before = await gateway.snapshot(tenant)
        log.emit(
            "t11.case.start",
            f"{name}: {CASE_MEANING[name]}",
            scenario=self.test_id,
            configuration=configuration,
            case=name,
            boundary=boundary,
            case_meaning=CASE_MEANING[name],
            operation_id=operation_id,
            idempotency_key=key,
            wallet_balance_before=before.wallet_balance,
        )

        # 1. The outage. Nothing in here may touch the gateway database --
        #    including this harness, which is why every snapshot is taken
        #    outside the block.
        outage_state: dict[str, Any]
        boundary_reached: bool | None = None
        if boundary is None:
            async with gateway.database_outage() as outage_state:
                outcome = await _submit(
                    target.agent,
                    identity,
                    refund,
                    configuration=configuration,
                    timeout_seconds=timeout_seconds,
                )
        else:
            with gateway.hold_at(boundary) as hold:
                task = asyncio.create_task(
                    _submit(
                        target.agent,
                        identity,
                        refund,
                        configuration=configuration,
                        timeout_seconds=timeout_seconds,
                    )
                )
                boundary_reached = await _wait_for_boundary(
                    hold, task, HOLD_TIMEOUT_SECONDS
                )
                async with gateway.database_outage() as outage_state:
                    hold.release.set()
                    outcome = await task
        refused_connections = int(outage_state["refused_connections"])

        after_outage = await gateway.snapshot(tenant)
        outage_view = _key_view(after_outage, key)
        dispatches_during_outage = target.injector.dispatch_count(operation_id)
        executions_during_outage = target.ledger.execution_count(operation_id)

        log.emit(
            "t11.case.outage",
            (
                f"{name}: call under outage -> status={outcome.status} "
                f"({outcome.client_visible_state}); the engine refused "
                f"{refused_connections} connection(s); fault layer saw "
                f"{dispatches_during_outage} request(s), effect ledger "
                f"{executions_during_outage} execution(s)"
            ),
            scenario=self.test_id,
            configuration=configuration,
            case=name,
            boundary=boundary,
            boundary_reached=boundary_reached,
            refused_connections=refused_connections,
            status=outcome.status,
            client_visible_state=outcome.client_visible_state,
            http_status=outcome.http_status,
            reason=outcome.reason,
            attempt_states=outage_view["attempt_states"],
            attempt_marked_sent=outage_view["attempt_marked_sent"],
            idempotency_records=len(outage_view["records"]),
            idempotency_record_completed=outage_view["record_completed"],
            debit_count=outage_view["debit_count"],
            downstream_requests=dispatches_during_outage,
            downstream_executions=executions_during_outage,
        )

        # 2. Recovery: the database is back, and a restarted gateway sweeps.
        never_dispatched = [
            row["attempt_id"]
            for row in after_outage.attempts
            if row["dispatched_at"] is None
        ]
        backdated = await gateway.backdate_attempts(tenant, seconds=backdate_seconds)
        restored = (
            await _restore_never_dispatched(never_dispatched)
            if restore_dispatched_at
            else 0
        )
        reconciliation = await gateway.reconcile(idle_seconds=0)
        after_reconcile = await gateway.snapshot(tenant)
        recovery_view = _key_view(after_reconcile, key)
        dispatches_after_reconcile = target.injector.dispatch_count(operation_id)
        executions_after_reconcile = target.ledger.execution_count(operation_id)

        log.emit(
            "t11.case.recovery",
            (
                f"{name}: recovered -> attempt states "
                f"{recovery_view['attempt_states']}, receipts "
                f"{recovery_view['receipt_outcomes']}, "
                f"{recovery_view['debit_count']} debit(s) and "
                f"{recovery_view['refund_count']} refund(s) (net charge "
                f"{recovery_view['net_charge_credits']} credits); fault layer "
                f"still {dispatches_after_reconcile} request(s)"
            ),
            scenario=self.test_id,
            configuration=configuration,
            case=name,
            attempts_backdated=backdated,
            backdate_seconds=backdate_seconds,
            dispatched_at_restored=restored,
            reconciliation=reconciliation,
            attempt_states=recovery_view["attempt_states"],
            receipt_outcomes=recovery_view["receipt_outcomes"],
            net_debit_count=recovery_view["net_debit_count"],
            net_charge_credits=recovery_view["net_charge_credits"],
            unsettled_debits=recovery_view["unsettled_debit_entry_ids"],
            idempotency_record_completed=recovery_view["record_completed"],
            downstream_requests=dispatches_after_reconcile,
            downstream_executions=executions_after_reconcile,
        )

        # 3. The same key, retried by an agent that never learned what happened.
        retry_identity = identity.retry()
        retry = await _submit(
            target.agent,
            retry_identity,
            refund,
            configuration=configuration,
            timeout_seconds=timeout_seconds,
        )
        dispatches_total = target.injector.dispatch_count(operation_id)
        executions_total = target.ledger.execution_count(operation_id)
        after_retry = await gateway.snapshot(tenant)
        final_view = _key_view(after_retry, key)

        log.emit(
            "t11.case.retry",
            (
                f"{name}: same-key retry -> status={retry.status} "
                f"({retry.client_visible_state}); dispatched="
                f"{dispatches_total > dispatches_after_reconcile}; totals for "
                f"this key: {dispatches_total} request(s), "
                f"{executions_total} execution(s)"
            ),
            scenario=self.test_id,
            configuration=configuration,
            case=name,
            idempotency_key=retry_identity.idempotency_key,
            same_key_as_outage=retry_identity.idempotency_key == key,
            status=retry.status,
            client_visible_state=retry.client_visible_state,
            reason=retry.reason,
            receipt_id=retry.receipt_id,
            receipt_outcome=(retry.receipt.get("outcome") if retry.receipt else None),
            dispatched=dispatches_total > dispatches_after_reconcile,
            downstream_requests=dispatches_total,
            downstream_executions=executions_total,
            wallet_balance=after_retry.wallet_balance,
        )

        case: dict[str, Any] = {
            "case": name,
            "boundary": boundary,
            "case_meaning": CASE_MEANING[name],
            "boundary_reached": boundary_reached,
            "operation_id": operation_id,
            "idempotency_key": key,
            "refused_connections": refused_connections,
            # -- the call made under the outage
            "outage_status": outcome.status,
            "outage_client_visible_state": outcome.client_visible_state,
            "outage_http_status": outcome.http_status,
            "outage_reason": outcome.reason,
            "outage_receipt_outcome": (
                outcome.receipt.get("outcome") if outcome.receipt else None
            ),
            "dispatches_during_outage": dispatches_during_outage,
            "executions_during_outage": executions_during_outage,
            "admitted_records": outage_view["record_ids"],
            "admitted_attempts": outage_view["attempt_ids"],
            "attempt_states_during_outage": outage_view["attempt_states"],
            "attempt_marked_sent_during_outage": outage_view["attempt_marked_sent"],
            "debits_during_outage": outage_view["debit_count"],
            # -- what a restarted gateway made of it
            "reconciliation": reconciliation,
            "attempts_backdated": backdated,
            "backdate_seconds": backdate_seconds,
            "dispatched_at_restore_enabled": restore_dispatched_at,
            "dispatched_at_restored_after_backdating": restored,
            "backdating_note": (
                "gateway.backdate_attempts writes dispatched_at on every "
                "attempt row of the wallet, including rows that never "
                "dispatched. This sub-case restores dispatched_at=NULL on "
                "exactly the rows that had it NULL before the helper ran; "
                "without that, pre-dispatch recovery refuses to compensate "
                "and the product is blamed for a harness artifact."
            ),
            "after_recovery": {
                "attempt_states": recovery_view["attempt_states"],
                "attempt_marked_sent": recovery_view["attempt_marked_sent"],
                "receipt_outcomes": recovery_view["receipt_outcomes"],
                "receipts": recovery_view["receipts"],
                "debit_count": recovery_view["debit_count"],
                "refund_count": recovery_view["refund_count"],
                "net_debit_count": recovery_view["net_debit_count"],
                "net_charge_credits": recovery_view["net_charge_credits"],
                "unsettled_debit_entry_ids": recovery_view["unsettled_debit_entry_ids"],
                "idempotency_records": len(recovery_view["records"]),
                "idempotency_record_completed": recovery_view["record_completed"],
            },
            "dispatches_after_recovery": dispatches_after_reconcile,
            "executions_after_recovery": executions_after_reconcile,
            # -- the retry
            "retry_status": retry.status,
            "retry_client_visible_state": retry.client_visible_state,
            "retry_reason": retry.reason,
            "retry_http_status": retry.http_status,
            "retry_receipt_outcome": (
                retry.receipt.get("outcome") if retry.receipt else None
            ),
            "retry_dispatched": dispatches_total > dispatches_after_reconcile,
            #: What the retry itself did, independently observed: fault-layer
            #: crossings and effect-ledger rows added after recovery.
            "retry_dispatches": dispatches_total - dispatches_after_reconcile,
            "retry_executions": executions_total - executions_after_reconcile,
            # -- totals for this key, every stage included
            "dispatches_total": dispatches_total,
            "executions_total": executions_total,
            "final_attempt_states": final_view["attempt_states"],
            "final_receipt_outcomes": final_view["receipt_outcomes"],
            "final_net_debit_count": final_view["net_debit_count"],
            "final_net_charge_credits": final_view["net_charge_credits"],
            "final_unsettled_debit_entry_ids": final_view["unsettled_debit_entry_ids"],
            "wallet_balance_after_case": after_retry.wallet_balance,
        }
        disposition, problems, measures = _evaluate(case)
        case["disposition"] = disposition
        case["problems"] = problems
        case["measures"] = measures

        log.emit(
            "t11.case.finish",
            f"{name}: {disposition}",
            scenario=self.test_id,
            configuration=configuration,
            case=name,
            disposition=disposition,
            problems=problems,
            **measures,
        )
        return case, [outcome, retry], operation_id

    # -- verdict ----------------------------------------------------------

    def _verdict(
        self,
        target: Target,
        log: EventLog,
        cases: list[dict[str, Any]],
        attempts: list[AttemptOutcome],
        measurements: Measurements,
        *,
        backdate_seconds: int,
    ) -> ConfigurationResult:
        failures = [
            f"{case['case']}: {problem}"
            for case in cases
            for problem in case["problems"]
        ]
        totals = {
            measure: sum(int(case["measures"][measure]) for case in cases)
            for measure in (
                "duplicate_dispatches",
                "duplicate_downstream_executions",
                "inconsistent_debits",
                "corrupted_state",
                "lost_accepted_operations",
                "admitted_without_terminal_state",
            )
        }
        verdict = Verdict.PASS if not failures else Verdict.FAIL
        snapshot: GatewaySnapshot | None = measurements.snapshot

        lines = [
            (
                f"{case['case']} ({case['case_meaning']}): the engine refused "
                f"{case['refused_connections']} connection(s); the call under "
                f"the outage returned {case['outage_status']} "
                f"({case['outage_client_visible_state']}) with "
                f"{case['dispatches_during_outage']} dispatch(es) and "
                f"{case['executions_during_outage']} downstream execution(s); "
                f"recovery left attempt state(s) "
                f"{case['after_recovery']['attempt_states']} with receipt(s) "
                f"{case['after_recovery']['receipt_outcomes']} and a net "
                f"charge of {case['after_recovery']['net_charge_credits']} "
                f"credits; the same-key retry returned {case['retry_status']} "
                f"({case['retry_client_visible_state']}, dispatched="
                f"{case['retry_dispatched']}); totals for this key "
                f"{case['dispatches_total']} dispatch(es) and "
                f"{case['executions_total']} execution(s) -> "
                f"{case['disposition']}."
            )
            for case in cases
        ]
        summary = (
            f"Across the {len(cases)} sub-case(s): "
            f"{totals['lost_accepted_operations']} lost accepted operation(s), "
            f"{totals['duplicate_dispatches']} duplicate dispatch(es), "
            f"{totals['duplicate_downstream_executions']} duplicate downstream "
            f"execution(s), {totals['inconsistent_debits']} inconsistent "
            f"debit(s) (a debit with neither a receipt nor a refund, counted "
            f"both immediately after reconciliation and again at the end of "
            f"its sub-case), and {totals['corrupted_state']} attempt row(s) in no "
            f"valid state. "
            f"{totals['admitted_without_terminal_state']} admitted key(s) ended "
            f"without a terminal state (of which "
            f"{totals['lost_accepted_operations']} were also not safely "
            f"retryable, which is what makes a loss)."
        )
        recovery_behavior = (
            "Recovery behavior: "
            + "; ".join(
                f"{case['case']} -> {case['disposition']} "
                f"(reconciler finalized "
                f"{case['reconciliation']['dispatch_prepared_finalized']} "
                f"prepared, "
                f"{case['reconciliation']['dispatch_uncertain']} uncertain, "
                f"{case['reconciliation']['dispatch_terminal_recovered']} "
                f"terminal, repaired "
                f"{case['reconciliation']['idempotency_repaired']} "
                f"idempotency record(s))"
                for case in cases
            )
            + "."
        )
        controls = [case for case in cases if case["disposition"] == "failed_closed"]
        if controls:
            control_text = (
                "Control: "
                + "; ".join(
                    f"{case['case']} was never admitted, and after recovery "
                    f"the same key retried crossed the fault layer "
                    f"{case['retry_dispatches']} time(s) and added "
                    f"{case['retry_executions']} row(s) to the independent "
                    f"effect ledger, returning {case['retry_status']} "
                    f"({case['retry_client_visible_state']})"
                    for case in controls
                )
                + " -- so an operation the outage refused is distinguishable "
                "from a downstream that never works."
            )
        else:
            control_text = (
                "Control: no sub-case ended failed_closed, so the "
                "never-admitted path was not available as a control here; "
                "the working tool is evidenced only by the sub-cases below."
            )
        sources = (
            "Sources: dispatch counts are fault-layer crossings and "
            "downstream executions are rows in the effect ledger, both "
            "outside the gateway; attempt states, receipts, debits, refunds "
            "and credit totals are the gateway's own records, and this "
            "scenario reads those rows without verifying their signatures."
        )
        parts = [*lines, summary, control_text, recovery_behavior, sources]
        if failures:
            parts.append("FAILURES: " + "; ".join(failures) + ".")
        else:
            parts.append(
                "No sub-case produced a second dispatch, a second downstream "
                "execution, a charge without a receipt or a refund, or an "
                "admitted key left neither terminal nor safely retryable."
            )
        observation = " ".join(parts)

        log.emit(
            "t11.verdict",
            f"{target.configuration.value} -> {verdict.value}",
            scenario=self.test_id,
            configuration=target.configuration.value,
            dispositions={case["case"]: case["disposition"] for case in cases},
            failures=failures,
            **totals,
        )

        remaining_risks = [
            "The outage refuses new connections at the engine. A real "
            "database restart can also lose an in-flight COMMIT "
            "acknowledgement, which this shape does not reproduce.",
            "Recovery is driven by calling the reconciler directly on "
            "backdated rows. In production the same work happens on a "
            "five-minute sweep after a much longer idle window, so an "
            "operation is uncertain to its caller for far longer than this "
            "test makes it look.",
            "A reservation held on a still-active permit by a call the outage "
            "killed is released only when that permit is revoked or expires. "
            "That is authorized-but-unspent budget, not a charge, and this "
            "test does not wait for it.",
        ]
        if any(
            "delivery_uncertain" in case["after_recovery"]["receipt_outcomes"]
            for case in cases
        ):
            remaining_risks.append(
                "An outage after the dispatch claim ends in a "
                "delivery_uncertain receipt with the charge retained. That is "
                "a truthful statement that the downstream outcome is "
                "unknowable to the gateway, not a resolution of it: settling "
                "with the downstream system is the caller's job. The receipt "
                "is read from the gateway's own table here; this scenario "
                "does not verify its signature, so nothing below rests on "
                "the receipt being cryptographically established."
            )

        extra: dict[str, Any] = {
            "cases": cases,
            "case_order": [name for name, _ in CASES],
            # Named so failure_lab.evidence._FAULT_KEYS_IN_EXTRA finds the
            # injected failures. A database outage and a held boundary never
            # cross the fault layer, so without this the evidence bundle's
            # fault-injection table would record nothing for this scenario.
            "injected": [
                {
                    "case": name,
                    "failure": "database_outage",
                    "held_at_boundary": boundary,
                }
                for name, boundary in CASES
            ],
            "refused_connections": {
                case["case"]: case["refused_connections"] for case in cases
            },
            "refused_connections_total": sum(
                int(case["refused_connections"]) for case in cases
            ),
            "lost_accepted_operations": totals["lost_accepted_operations"],
            "admitted_without_terminal_state": totals[
                "admitted_without_terminal_state"
            ],
            "duplicate_dispatches": totals["duplicate_dispatches"],
            "duplicate_downstream_executions": totals[
                "duplicate_downstream_executions"
            ],
            "inconsistent_debits": totals["inconsistent_debits"],
            "corrupted_state": totals["corrupted_state"],
            "dispositions": {case["case"]: case["disposition"] for case in cases},
            "recovery_behavior": {
                case["case"]: {
                    "reconciliation": case["reconciliation"],
                    "attempt_states_after_recovery": case["after_recovery"][
                        "attempt_states"
                    ],
                    "receipt_outcomes_after_recovery": case["after_recovery"][
                        "receipt_outcomes"
                    ],
                    "net_charge_credits_after_recovery": case["after_recovery"][
                        "net_charge_credits"
                    ],
                    "same_key_retry": {
                        "status": case["retry_status"],
                        "client_visible_state": case["retry_client_visible_state"],
                        "dispatched": case["retry_dispatched"],
                        "fault_layer_dispatches": case["retry_dispatches"],
                        "effect_ledger_executions": case["retry_executions"],
                        "receipt_outcome": case["retry_receipt_outcome"],
                    },
                }
                for case in cases
            },
            "backdate_seconds": backdate_seconds,
            "measured_scope_note": (
                "Per-case 'dispatches_total' and 'executions_total' are that "
                "key's own counts across the outage, recovery and the retry. "
                "The configuration-level counters cover all three sub-cases "
                "together, so they are deliberately larger."
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

        return self.result(
            target,
            verdict=verdict,
            observation=observation,
            measurements=measurements,
            attempts=attempts,
            remaining_risks=remaining_risks,
            extra=extra,
        )
