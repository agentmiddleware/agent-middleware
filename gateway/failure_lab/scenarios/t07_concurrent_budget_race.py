"""Test 7 -- Concurrent budget race."""

from __future__ import annotations

import asyncio
from collections import Counter as StatusTally
from decimal import Decimal
from typing import Any

from failure_lab.configurations import (
    GATEWAY_CONFIGURATIONS,
    AttemptOutcome,
    Configuration,
    Target,
)
from failure_lab.identity import OperationIdentity
from failure_lab.scenarios.base import (
    REFUSED_STATUSES,
    ConfigurationResult,
    EventLog,
    Measurements,
    Scenario,
    Verdict,
    percentile,
)

#: The reason code a permit out of budget must refuse with. Any other refusal
#: reason on this workload is reported, not folded into the budget count: an
#: operator told "expired" or "contended" goes to a different remedy than one
#: told the delegated budget is spent.
BUDGET_DENIAL_REASON = "permit_budget_exceeded"

#: The permit size the scenario's Case A names.
CASE_A_MAX_CREDITS = Decimal("10")

#: Case B default: how many calls the permit is sized for, and how many
#: distinct operations race for it.
CASE_B_AUTHORIZED_CALLS = 3
DEFAULT_CONCURRENCY = 20

#: Client patience, in seconds. Far above the gateway's own call timeout so
#: what is recorded is the gateway's answer, not the harness's impatience.
DEFAULT_TIMEOUT_SECONDS = 30.0

#: Statuses that mean the client never got a gateway answer at all. These can
#: be the harness buckling under the requested concurrency (in-process asyncio
#: over SQLite) rather than the product -- but an attempt with no answer is an
#: attempt whose refusal guarantee this scenario did not observe, and the
#: framework calls an unobserved protection a FAIL. They are counted, named in
#: the failure text as possible harness contention, and never silently excused.
UNANSWERED_STATUSES = frozenset({"timeout", "transport_error"})

#: Marks the failure strings that mean the budget was actually overrun, as
#: opposed to some other breach of the case's conditions.
OVERSPEND_PREFIX = "over-authorization: "

#: Above this median, the price of being refused is worth reporting on its own.
SLOW_REFUSAL_MS = 1000.0


def _amount(row: dict[str, Any]) -> Decimal:
    """The absolute size of one ledger entry, in credits."""
    value = row.get("amount")
    return abs(Decimal(str(value))) if value is not None else Decimal("0")


def _credits(value: Any) -> Decimal:
    return Decimal(str(value)) if value is not None else Decimal("0")


def _status_tally(attempts: list[AttemptOutcome]) -> dict[str, int]:
    return dict(sorted(StatusTally(a.status for a in attempts).items()))


def _reason_tally(attempts: list[AttemptOutcome]) -> dict[str, int]:
    return dict(sorted(StatusTally(a.reason or "none" for a in attempts).items()))


class ConcurrentBudgetRace(Scenario):
    """Two sevens must not fit inside a ten.

    WHAT TO IMPLEMENT
    -----------------
    The gateway charges ``credits_per_call`` (5 by default via
    ``LabEnvironment``) per call. Mint a permit sized so the race is tight:

    Case A (the PRD's example): issue a permit with
    ``max_credits`` = 10 and fire 2 concurrent calls, each of which will cost
    more than half the budget. Use ``gateway.issue_permit(tenant,
    max_credits=...)`` and drive with ``target.gateway_agent(permit_id)``.
    Each call MUST use a different payment id and a different key -- these are
    distinct business operations racing for one budget, not retries.

    Case B (high concurrency): a permit sized for exactly K calls, with
    ``self.options.get("concurrency", 20)`` calls fired at once. At most K may
    be authorized.

    For each case compute ``authorized`` (attempts that were charged) and
    assert ``authorized * credits_per_call <= max_credits``.

    Verdict: ``PASS`` iff for both cases the total credits actually debited
    never exceeds the permit's ``max_credits``, the permit's final
    ``spent_credits`` does not exceed ``max_credits``, downstream_executions
    equals the number of authorized calls, and every refused call is refused
    with ``permit_budget_exceeded`` and produced no downstream effect.
    ``FAIL`` on any over-authorization.

    Record in ``extra["cases"]``: max_credits, credits_per_call, concurrency,
    authorized count, denied count, total debited, permit spent_credits,
    downstream executions.
    """

    test_id = "T07"
    title = "Concurrent budget race"
    claim = (
        "Concurrent budget consumption never exceeds the permit's authorized "
        "limit, however many calls race for it."
    )
    tier = "slow"
    configurations = GATEWAY_CONFIGURATIONS
    inapplicable_reason = "a direct integration has no permit budget to race for"
    expected = {
        Configuration.DIRECT_NAIVE.value: Verdict.NOT_APPLICABLE.value,
        Configuration.DIRECT_NATIVE.value: Verdict.NOT_APPLICABLE.value,
        Configuration.GATEWAY_NATIVE.value: Verdict.PASS.value,
        Configuration.GATEWAY_NAIVE.value: Verdict.PASS.value,
    }
    limitations = (
        "Runs against SQLite, where the reservation is a single guarded "
        "UPDATE rather than a row lock. The PostgreSQL row-lock path is "
        "covered by the repository's own concurrency suite.",
        "In-process asyncio concurrency, not distributed load.",
    )

    async def run_configuration(
        self, target: Target, log: EventLog
    ) -> ConfigurationResult:
        gateway, tenant, _ = target.require_gateway()
        credits_per_call = Decimal(gateway.credits_per_call)
        concurrency = max(2, int(self.options.get("concurrency", DEFAULT_CONCURRENCY)))
        timeout_seconds = float(
            self.options.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        )
        case_b_calls = max(
            1, int(self.options.get("budget_calls", CASE_B_AUTHORIZED_CALLS))
        )

        plans: list[dict[str, Any]] = [
            {
                "name": "A_prd_example",
                "max_credits": CASE_A_MAX_CREDITS,
                "calls": 2,
                "note": (
                    f"The PRD's example sizing verbatim: a {CASE_A_MAX_CREDITS}-credit "
                    f"permit and 2 concurrent calls. At this lab's configured price of "
                    f"{credits_per_call} credits per call the two calls fill the permit "
                    f"exactly, so what this case measures is that a permit sized for two "
                    f"admits two and that the cap is consumed, not overshot."
                ),
            }
        ]
        if credits_per_call * 2 <= CASE_A_MAX_CREDITS:
            plans.append(
                {
                    "name": "A_tight",
                    "max_credits": credits_per_call * 2 - 1,
                    "calls": 2,
                    "note": (
                        f"The PRD's example also requires each racer to cost MORE than "
                        f"half the budget, which a {CASE_A_MAX_CREDITS}-credit permit "
                        f"cannot do at {credits_per_call} credits per call. This case "
                        f"restores that property -- the permit is sized one credit below "
                        f"two calls -- so the two racers provably cannot both fit."
                    ),
                }
            )
        plans.append(
            {
                "name": "B_high_concurrency",
                "max_credits": credits_per_call * case_b_calls,
                "calls": concurrency,
                "note": (
                    f"A permit sized for exactly {case_b_calls} call(s), with "
                    f"{concurrency} distinct business operations fired at once. At most "
                    f"{case_b_calls} may be authorized."
                ),
            }
        )

        cases: list[dict[str, Any]] = []
        all_attempts: list[AttemptOutcome] = []
        all_operation_ids: list[str] = []
        for plan in plans:
            record, attempts, operation_ids = await self._run_case(
                target,
                log,
                name=str(plan["name"]),
                note=str(plan["note"]),
                max_credits=Decimal(plan["max_credits"]),
                calls=int(plan["calls"]),
                credits_per_call=credits_per_call,
                timeout_seconds=timeout_seconds,
            )
            cases.append(record)
            all_attempts.extend(attempts)
            all_operation_ids.extend(operation_ids)

        measurements = await self.measure(
            target, all_attempts, operation_ids=all_operation_ids
        )

        failures = [
            f"{case['case']}: {item}" for case in cases for item in case["failures"]
        ]
        over_authorization = [
            f"{case['case']}: {item}"
            for case in cases
            for item in case["failures"]
            if item.startswith(OVERSPEND_PREFIX)
        ]
        unanswered = sum(int(case["unanswered_attempts"]) for case in cases)
        verdict = Verdict.PASS if not failures else Verdict.FAIL

        extra: dict[str, Any] = {
            "credits_per_call": str(credits_per_call),
            "concurrency": concurrency,
            "case_b_authorized_calls": case_b_calls,
            "client_timeout_seconds": timeout_seconds,
            "cases": cases,
            "verdict_failures": failures,
            "over_authorizations": over_authorization,
            "unanswered_attempts": unanswered,
            "wallet_balance": (
                measurements.snapshot.wallet_balance
                if measurements.snapshot is not None
                else None
            ),
            "prd_sizing_note": (
                f"The scenario's Case A names max_credits={CASE_A_MAX_CREDITS} AND says "
                f"each racer costs more than half of it. The gateway's price in this lab "
                f"is a flat {credits_per_call} credits per call, so both clauses cannot "
                f"hold at once. Both sizings were run rather than picking one: "
                f"'A_prd_example' is the literal number and 'A_tight' is the literal "
                f"property."
            ),
        }

        observation = self._observation(
            cases, credits_per_call, measurements, unanswered
        )
        if failures:
            observation += " Guarantee not met: " + "; ".join(failures) + "."

        risks: list[str] = [
            "The reservation observed here is SQLite's guarded UPDATE under "
            "in-process asyncio concurrency, not PostgreSQL's row lock and not "
            "distributed load across gateway processes.",
            "Budget safety is measured on the gateway's own reservation and debit. "
            "Whether a refused call left no trace downstream is asserted from the "
            "independent effect ledger and fault layer for these operation ids only.",
        ]
        if unanswered:
            risks.append(
                f"{unanswered} attempt(s) ended in a client timeout or transport error "
                f"rather than a gateway answer, and each one failed its case: a call "
                f"whose outcome the client never learned is a call whose refusal "
                f"guarantee this run did not observe. The budget arithmetic still "
                f"stands on its own because it is read from the permit, the gateway "
                f"ledger and the effect ledger, not from the client's view -- so read "
                f"extra['over_authorizations'] before reading this as an overspend. "
                f"Re-run with --option concurrency=<lower> to separate in-process "
                f"harness contention from product behaviour."
            )
        slowest_refusal = max(
            (case["refused_latency_p50_ms"] or 0.0 for case in cases), default=0.0
        )
        if slowest_refusal >= SLOW_REFUSAL_MS:
            risks.append(
                f"Refusing a call is not free: the median refused call took "
                f"{round(slowest_refusal)} ms, because every racer queues behind the "
                f"same guarded reservation. The budget decision itself is settled "
                f"before any dispatch, but a caller whose patience is shorter than "
                f"that learns nothing and may retry a call the gateway already refused."
            )
        for case in cases:
            if case["under_authorized"]:
                risks.append(
                    f"{case['case']}: the permit had room for "
                    f"{case['budget_capacity_calls']} call(s) and "
                    f"{case['authorized']} were charged, so "
                    f"{case['budget_capacity_calls'] - case['authorized']} call(s) that "
                    f"fit the budget were not charged. That is safe in the direction "
                    f"the claim cares about and wasteful in the other; it is reported "
                    f"here, not counted as a breach."
                )
            if case["charged_but_unanswered"]:
                risks.append(
                    f"{case['case']}: {case['charged_but_unanswered']} call(s) were "
                    f"charged against the permit but the client never received the "
                    f"matching success response, so the payer cannot tell from its own "
                    f"view what it paid for. The charge is real; only the client's "
                    f"knowledge of it is missing."
                )
            if case["refunded_credits"] != "0":
                risks.append(
                    f"{case['case']}: {case['refunded_credits']} credit(s) came back as "
                    f"refunds, so the gross debit above is not the net cost. No fault "
                    f"was injected in this scenario, so a refund here is itself a "
                    f"finding."
                )

        log.emit(
            "verdict",
            f"{target.configuration.value} -> {verdict.value}",
            scenario=self.test_id,
            configuration=target.configuration.value,
            cases=[case["case"] for case in cases],
            failures=failures,
            over_authorizations=over_authorization,
        )
        return self.result(
            target,
            verdict=verdict,
            observation=observation,
            measurements=measurements,
            attempts=all_attempts,
            remaining_risks=risks,
            extra=extra,
        )

    # -- one permit, one storm --------------------------------------------

    async def _run_case(
        self,
        target: Target,
        log: EventLog,
        *,
        name: str,
        note: str,
        max_credits: Decimal,
        calls: int,
        credits_per_call: Decimal,
        timeout_seconds: float,
    ) -> tuple[dict[str, Any], list[AttemptOutcome], list[str]]:
        """Race ``calls`` distinct operations for one freshly minted permit."""
        gateway, tenant, _ = target.require_gateway()
        before = await gateway.snapshot(tenant)
        seen_debits = {row["entry_id"] for row in before.debits}
        seen_refunds = {row["entry_id"] for row in before.refunds}
        seen_receipts = {row["receipt_id"] for row in before.receipts}

        permit = await gateway.issue_permit(tenant, max_credits=max_credits)
        permit_id = str(permit["permit_id"])
        capacity = int(max_credits // credits_per_call)
        agent = target.gateway_agent(permit_id)

        operations = []
        for index in range(calls):
            operation_id, refund = self.refund(f"pay_t07_{name}_{index}")
            operations.append(
                (operation_id, OperationIdentity.first_attempt(operation_id), refund)
            )
        operation_ids = [operation_id for operation_id, _, _ in operations]
        distinct_keys = {identity.idempotency_key for _, identity, _ in operations}

        log.emit(
            "case.plan",
            (
                f"{name}: {calls} distinct business operations racing for a permit of "
                f"{max_credits} credits at {credits_per_call} credits per call "
                f"(room for {capacity})"
            ),
            scenario=self.test_id,
            configuration=target.configuration.value,
            case=name,
            permit_id=permit_id,
            max_credits=str(max_credits),
            credits_per_call=str(credits_per_call),
            calls=calls,
            budget_capacity_calls=capacity,
            distinct_operations=len(set(operation_ids)),
            distinct_idempotency_keys=len(distinct_keys),
            note=note,
        )

        attempts: list[AttemptOutcome] = list(
            await asyncio.gather(
                *(
                    agent.submit(identity, refund, timeout_seconds=timeout_seconds)
                    for _, identity, refund in operations
                )
            )
        )

        after = await gateway.snapshot(tenant)
        new_debits = [row for row in after.debits if row["entry_id"] not in seen_debits]
        new_refunds = [
            row for row in after.refunds if row["entry_id"] not in seen_refunds
        ]
        debited = sum((_amount(row) for row in new_debits), Decimal("0"))
        refunded = sum((_amount(row) for row in new_refunds), Decimal("0"))
        case_receipts = [
            row
            for row in after.receipts
            if row["permit_id"] == permit_id and row["receipt_id"] not in seen_receipts
        ]
        charged_receipts = [
            row for row in case_receipts if _credits(row.get("credits_charged")) > 0
        ]
        permit_row = next(
            (row for row in after.permits if row["permit_id"] == permit_id), None
        )
        spent = (
            _credits(permit_row.get("spent_credits"))
            if permit_row is not None
            else None
        )

        #: Authorized == charged, read from the gateway's own receipts rather
        #: than from what each client believed it got.
        authorized = len(charged_receipts)
        succeeded = [a for a in attempts if a.status == "success"]
        denied = [a for a in attempts if a.status == "denied"]
        budget_denied = [a for a in denied if a.reason == BUDGET_DENIAL_REASON]
        refused = [a for a in attempts if a.status in REFUSED_STATUSES]
        misrefused = [a for a in refused if a.reason != BUDGET_DENIAL_REASON]
        strained = [a for a in attempts if a.status in UNANSWERED_STATUSES]
        unclassified = [
            a
            for a in attempts
            if a.status not in REFUSED_STATUSES
            and a.status != "success"
            and a.status not in UNANSWERED_STATUSES
        ]

        executions = {op: target.ledger.execution_count(op) for op in operation_ids}
        total_executions = sum(executions.values())
        dispatches = sum(target.injector.dispatch_count(op) for op in operation_ids)
        refused_ops = sorted(
            {str(a.identity["business_operation_id"]) for a in refused}
        )
        refused_with_effect = [op for op in refused_ops if executions.get(op, 0) > 0]
        #: The effect ledger says whether a refused call CHANGED anything; the
        #: fault layer says whether it was ever sent. A refused call that was
        #: dispatched but whose effect the tool happened to drop is still the
        #: gateway deciding after it dispatched, so both are asked.
        refused_with_dispatch = [
            op for op in refused_ops if target.injector.dispatch_count(op) > 0
        ]

        failures: list[str] = []
        if debited > max_credits:
            failures.append(
                f"{OVERSPEND_PREFIX}{debited} credit(s) were debited against a permit "
                f"authorized for {max_credits}"
            )
        if spent is None:
            failures.append("the permit could not be read back from the gateway")
        elif spent > max_credits:
            failures.append(
                f"{OVERSPEND_PREFIX}the permit's spent_credits settled at {spent} "
                f"against max_credits {max_credits}"
            )
        if credits_per_call * authorized > max_credits:
            failures.append(
                f"{OVERSPEND_PREFIX}{authorized} call(s) were charged at "
                f"{credits_per_call} credits each against a permit of {max_credits}"
            )
        if authorized > capacity:
            failures.append(
                f"{OVERSPEND_PREFIX}{authorized} call(s) were charged where the permit "
                f"had room for {capacity}"
            )
        if total_executions != authorized:
            failures.append(
                f"downstream executions (independent effect ledger) = {total_executions}, "
                f"want {authorized} (one per charged call)"
            )
        if misrefused:
            failures.append(
                f"{len(misrefused)} refused call(s) were not refused with "
                f"{BUDGET_DENIAL_REASON}: {_reason_tally(misrefused)}"
            )
        if refused_with_effect:
            failures.append(
                f"{len(refused_with_effect)} refused call(s) left a downstream effect: "
                f"{refused_with_effect}"
            )
        if refused_with_dispatch:
            failures.append(
                f"{len(refused_with_dispatch)} refused call(s) were dispatched past the "
                f"fault layer toward the tool, so the budget decision did not land "
                f"before dispatch for them: {refused_with_dispatch}"
            )
        if unclassified:
            failures.append(
                f"{len(unclassified)} call(s) ended neither charged nor refused: "
                f"{_status_tally(unclassified)}"
            )
        if strained:
            failures.append(
                f"no gateway answer for {len(strained)} of {calls} call(s) "
                f"({_status_tally(strained)}), so this case cannot say whether they were "
                f"admitted or refused and the refusal guarantee went unobserved for them. "
                f"The budget arithmetic above is unaffected -- it is read from the permit, "
                f"the gateway ledger and the effect ledger, not from the client -- so this "
                f"is NOT an over-authorization. It may be in-process harness contention "
                f"rather than the product; re-run with --option concurrency=<lower> to "
                f"tell the two apart."
            )

        record: dict[str, Any] = {
            "case": name,
            "charged_latency_p50_ms": percentile(
                [a.latency_ms for a in succeeded], 0.5
            ),
            "refused_latency_p50_ms": percentile([a.latency_ms for a in refused], 0.5),
            "note": note,
            "permit_id": permit_id,
            "max_credits": str(max_credits),
            "credits_per_call": str(credits_per_call),
            "concurrency": calls,
            "budget_capacity_calls": capacity,
            "authorized": authorized,
            "denied": len(denied),
            "budget_denied": len(budget_denied),
            "total_debited": str(debited),
            "refunded_credits": str(refunded),
            "net_debited": str(debited - refunded),
            "permit_spent_credits": str(spent) if spent is not None else None,
            "permit_status": permit_row["status"] if permit_row is not None else None,
            "downstream_executions": total_executions,
            "downstream_requests": dispatches,
            "client_success_responses": len(succeeded),
            "charged_but_unanswered": max(0, authorized - len(succeeded)),
            "attempt_statuses": _status_tally(attempts),
            "denial_reasons": _reason_tally(refused),
            "receipt_outcomes": sorted(str(row["outcome"]) for row in case_receipts),
            "refused_with_downstream_effect": refused_with_effect,
            "refused_with_downstream_dispatch": refused_with_dispatch,
            "unanswered_attempts": len(strained),
            "under_authorized": authorized < capacity,
            "distinct_idempotency_keys": len(distinct_keys),
            "failures": failures,
        }

        log.emit(
            "case.result",
            (
                f"{name}: {authorized} of {calls} call(s) charged, {len(denied)} denied "
                f"({len(budget_denied)} with {BUDGET_DENIAL_REASON}); {debited} credit(s) "
                f"debited and permit spent_credits {spent} against max_credits "
                f"{max_credits}; effect ledger recorded {total_executions} execution(s)"
            ),
            scenario=self.test_id,
            configuration=target.configuration.value,
            **{k: v for k, v in record.items() if k != "note"},
        )
        return record, attempts, operation_ids

    def _observation(
        self,
        cases: list[dict[str, Any]],
        credits_per_call: Decimal,
        measurements: Measurements,
        unanswered: int,
    ) -> str:
        parts = [
            (
                f"Each case mints its own permit and races that many DISTINCT business "
                f"operations for it -- different payment ids, different idempotency keys, "
                f"so nothing here is a retry and nothing can be collapsed by replay. The "
                f"gateway's price is {credits_per_call} credits per call."
            )
        ]
        for case in cases:
            parts.append(
                f"[{case['case']}] {case['note']} Permit {case['max_credits']} credits "
                f"(room for {case['budget_capacity_calls']} call(s)); "
                f"{case['concurrency']} concurrent call(s) fired. Gateway-reported: "
                f"{case['authorized']} charged, {case['denied']} denied "
                f"({case['budget_denied']} of them with {BUDGET_DENIAL_REASON}), "
                f"{case['total_debited']} credit(s) debited, permit spent_credits "
                f"{case['permit_spent_credits']}. Independently observed: the effect "
                f"ledger recorded {case['downstream_executions']} downstream execution(s) "
                f"and the fault layer counted {case['downstream_requests']} request(s) "
                f"crossing it on the way to the tool, with "
                f"{len(case['refused_with_downstream_dispatch'])} refused call(s) "
                f"reaching that layer and "
                f"{len(case['refused_with_downstream_effect'])} leaving any effect "
                f"behind."
            )
        parts.append(
            f"Across all cases the client saw p50 {measurements.counters.latency_p50_ms} ms "
            f"and p95 {measurements.counters.latency_p95_ms} ms per call, and the wallet "
            f"was left holding {measurements.gateway['wallet_balance'] if measurements.gateway else 'unknown'} "
            f"credit(s)."
        )
        if unanswered:
            parts.append(
                f"{unanswered} attempt(s) ended in a client timeout or transport error "
                f"with no gateway answer at all, which fails the case they belong to: "
                f"their outcome is unobserved, not benign."
            )
        return " ".join(parts)
