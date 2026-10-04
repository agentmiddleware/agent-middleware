"""Test 8 -- Permit revocation race."""

from __future__ import annotations

import asyncio
import contextlib
from decimal import Decimal
from typing import Any

from failure_lab.configurations import (
    GATEWAY_CONFIGURATIONS,
    AttemptOutcome,
    Configuration,
    Target,
)
from failure_lab.gateway import BoundaryHold, GatewaySnapshot
from failure_lab.identity import OperationIdentity
from failure_lab.scenarios.base import (
    REFUSED_STATUSES,
    ConfigurationResult,
    EventLog,
    Scenario,
    Verdict,
)

#: The four interleavings, ordered by how far the in-flight call has travelled
#: when the revocation lands. ``None`` means "revoke before submitting", which
#: is the control: nothing is in flight, so revocation must bite.
INTERLEAVINGS: tuple[tuple[str, str | None], ...] = (
    ("before_request", None),
    ("after_prepare", "after_prepare"),
    ("after_attach_charge", "after_attach_charge"),
    ("after_claim", "after_claim"),
)

#: What the call has durably achieved by the time each interleaving revokes.
BOUNDARY_MEANING: dict[str, str] = {
    "before_request": "nothing; the call has not been submitted",
    "after_prepare": "permit authorized, budget reserved, attempt row prepared",
    "after_attach_charge": "wallet debit committed and linked to the attempt",
    "after_claim": "one-shot dispatch claim committed; network send not started",
}

#: Receipt outcomes the product documents as RETAINING the charge.
CHARGE_RETAINED_OUTCOMES = frozenset(
    {"success", "delivery_uncertain", "response_rejected", "failed_unrefunded"}
)
#: Receipt outcomes the product documents as leaving no net charge.
CHARGE_RELEASED_OUTCOMES = frozenset(
    {"denied", "insufficient_funds", "failed_refunded"}
)

#: A refusal is only evidence that *revocation* bit if the product says
#: revocation is why. ``insufficient_funds``, ``key_conflict``,
#: ``invalid_params`` and ``conflict`` are refusals too, and counting them
#: would let an unrelated breakage masquerade as a working revocation.
REVOCATION_REASON_MARKER = "revok"

#: Ceiling on the wait for an instrumented boundary to be reached. The held
#: call only has to get as far as a local database commit, so anything near
#: this means the harness stalled rather than the product being slow.
HOLD_TIMEOUT_SECONDS = 20.0

#: Client patience. The hold is released after one revoke round-trip, so this
#: only has to be generous enough that the harness is never the thing timing
#: out and being mistaken for a product behaviour.
SUBMIT_TIMEOUT_SECONDS = 30.0

#: Budget for each single-call permit. One governed call costs 5 credits.
PERMIT_MAX_CREDITS = Decimal("50")


def _added(
    before: list[dict[str, Any]], after: list[dict[str, Any]], key: str
) -> list[dict[str, Any]]:
    """Rows in ``after`` that were not in ``before``, matched on ``key``."""
    seen = {row[key] for row in before}
    return [row for row in after if row[key] not in seen]


def _credits(rows: list[dict[str, Any]]) -> Decimal:
    """Signed credit total of a set of ledger rows (debits are negative)."""
    total = Decimal("0")
    for row in rows:
        amount = row.get("amount")
        if amount is not None:
            total += Decimal(str(amount))
    return total


def _revocation_attributable(
    reason: Any, reason_codes: list[Any] | None = None
) -> bool:
    """Does the product say *revocation* is why this call was refused?"""
    candidates = [reason, *(reason_codes or [])]
    return any(
        REVOCATION_REASON_MARKER in str(c).lower() for c in candidates if c is not None
    )


async def _wait_for_boundary(
    hold: BoundaryHold, task: asyncio.Task[AttemptOutcome], timeout: float
) -> str:
    """Wait until the held call reaches the boundary, or until it finishes.

    Returns ``"reached"``, ``"call_finished_first"`` (the call was refused or
    completed without ever pausing) or ``"harness_timeout"``. The three are
    kept apart on purpose. Returning on task completion stops a call refused
    *before* the instrumented boundary from stalling the harness for the full
    timeout -- but only ``"reached"`` actually stages the race this scenario
    claims to measure, and a harness timeout must never be reported as though
    it were a product observation.
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
    if hold.reached.is_set():
        return "reached"
    if task.done():
        return "call_finished_first"
    return "harness_timeout"


def _evaluate(record: dict[str, Any]) -> tuple[str, list[str]]:
    """Classify one interleaving as a coherent disposition, or say why not.

    The premise is checked first, and it is not a formality. An interleaving
    whose call never paused at its boundary was never raced against a
    revocation, and one whose permit does not read back as revoked never had
    its authority withdrawn. Either way the call was an ordinary governed
    invocation, and scoring it ``admitted_once`` would manufacture evidence
    for an experiment that did not happen. Those return ``not_staged``, which
    is never a coherent disposition and never feeds the authoritative point.

    Coherent (a) ``denied``: nothing dispatched, nothing executed, no net
    charge. Coherent (b) ``admitted_once``: at most one dispatch, at most one
    execution, exactly one receipt, and a charge that matches what that
    receipt says about the money.
    """
    premise: list[str] = []
    if not record.get("staged"):
        premise.append(
            f"the race was never staged ({record.get('staging_note')}), so no "
            f"revocation ever raced this in-flight call"
        )
    if not record.get("permit_revocation_confirmed"):
        premise.append(
            f"the permit read back as {record.get('permit_final_status')!r} "
            f"rather than 'revoked', so no authority was actually withdrawn"
        )
    if premise:
        return "not_staged", premise

    dispatches = int(record["dispatches"])
    executions = int(record["downstream_executions"])
    outcomes = [str(o) for o in record["receipt_outcomes"]]
    net_credits = Decimal(str(record["net_charge_credits"]))
    net_count = int(record["net_debit_count"])
    problems: list[str] = []

    if dispatches > 1:
        problems.append(f"{dispatches} dispatches for one key")
    if executions > 1:
        problems.append(f"{executions} downstream executions for one key")
    if len(outcomes) > 1:
        problems.append(f"{len(outcomes)} receipts for one key")
    if net_credits > 0 and not outcomes:
        problems.append(f"net charge of {net_credits} credits with no receipt")
    for outcome in outcomes:
        if outcome == "success" and executions == 0:
            problems.append(
                "a receipt signed 'success' while the effect ledger recorded no "
                "downstream execution for this operation: the signature would be "
                "standing in for an action nothing independent observed"
            )
        if outcome in CHARGE_RETAINED_OUTCOMES and net_credits <= 0:
            problems.append(
                f"receipt outcome {outcome!r} retains the charge but the net "
                f"charge is {net_credits} credits"
            )
        if outcome in CHARGE_RELEASED_OUTCOMES and net_credits > 0:
            problems.append(
                f"receipt outcome {outcome!r} leaves no charge but "
                f"{net_credits} credits stand debited"
            )
    if problems:
        return "incoherent", problems

    refused = str(record["status"]) in REFUSED_STATUSES
    if (
        refused
        and dispatches == 0
        and executions == 0
        and net_count == 0
        and net_credits == 0
    ):
        return "denied", []
    if dispatches <= 1 and executions <= 1 and len(outcomes) == 1:
        return "admitted_once", []
    return "incoherent", [
        f"status {record['status']!r} with {dispatches} dispatch(es), "
        f"{executions} execution(s), {len(outcomes)} receipt(s) and a net "
        f"charge of {net_credits} credits is neither a clean denial nor a "
        f"single admitted call"
    ]


class PermitRevocationRace(Scenario):
    """Revoke authority while a call is in flight, at several interleavings.

    The product must define the authoritative point at which authorization is
    evaluated. This scenario's job is to find that point empirically and
    report it -- not to assert a preferred answer.

    WHAT TO IMPLEMENT
    -----------------
    Four interleavings, each with its own permit, payment id and key:

    ``before_request``   revoke, then submit. Control case.
    ``after_prepare``    hold the in-flight call with
                         ``gateway.hold_at("after_prepare")``, revoke while it
                         is paused, release, observe.
    ``after_attach_charge`` same, held after the debit is linked.
    ``after_claim``      same, held after the one-shot dispatch claim.

    Mechanics for a held case::

        with gateway.hold_at(boundary) as hold:
            task = asyncio.create_task(agent.submit(identity, refund))
            await asyncio.wait_for(hold.reached.wait(), timeout=10)
            await gateway.revoke_permit(tenant, permit_id)
            hold.release.set()
            outcome = await task

    Record per interleaving: the attempt status, whether a dispatch crossed
    the fault layer, downstream executions, net debit, receipt outcome, and
    the permit's final status.

    Verdict: ``PASS`` iff every interleaving lands on one of two coherent
    dispositions and never on an incoherent one.
      * Coherent: (a) denied with zero dispatches, zero executions and no net
        charge; or (b) admitted exactly once -- at most one dispatch, at most
        one execution, exactly one receipt, and the charge matching the
        receipt's outcome.
      * Incoherent, and therefore ``FAIL``: more than one dispatch or
        execution for one key, a charge with no receipt, a receipt with no
        corresponding accounting, or a revoked permit admitting a *second*
        distinct operation after revocation completed.
    Then, in the observation, name the boundary at which revocation stopped
    being effective, and record it in ``extra["authoritative_point"]``.
    Also submit a fresh operation under the revoked permit afterwards and
    confirm it is denied -- revocation must be effective for anything not
    already in flight. Record as ``extra["post_revocation_control"]``.
    """

    test_id = "T08"
    title = "Permit revocation race"
    claim = (
        "Authorization is evaluated at one well-defined point; a revocation "
        "arriving after it cannot produce a second dispatch, an unreceipted "
        "charge, or a newly admitted operation."
    )
    tier = "slow"
    configurations = GATEWAY_CONFIGURATIONS
    inapplicable_reason = "a direct integration has no permit to revoke"
    expected = {
        Configuration.DIRECT_NAIVE.value: Verdict.NOT_APPLICABLE.value,
        Configuration.DIRECT_NATIVE.value: Verdict.NOT_APPLICABLE.value,
        Configuration.GATEWAY_NATIVE.value: Verdict.PASS.value,
        Configuration.GATEWAY_NAIVE.value: Verdict.PASS.value,
    }
    limitations = (
        "A call already past the authorization point completes under the "
        "authority it had when it was admitted. This test records where that "
        "point is; it does not argue that the location is correct.",
        "Interleavings are forced at instrumented boundaries, so they are "
        "deterministic rather than a natural race.",
    )

    async def run_configuration(
        self, target: Target, log: EventLog
    ) -> ConfigurationResult:
        gateway, tenant, _ = target.require_gateway()
        attempts: list[AttemptOutcome] = []
        operation_ids: list[str] = []
        records: list[dict[str, Any]] = []
        permits: list[tuple[str, str]] = []

        for index, (name, boundary) in enumerate(INTERLEAVINGS, start=1):
            record, outcome = await self._run_interleaving(
                target, log, index=index, name=name, boundary=boundary
            )
            records.append(record)
            attempts.append(outcome)
            operation_ids.append(str(record["operation_id"]))
            permits.append((name, str(record["permit_id"])))

        control, control_attempts, control_ops = await self._post_revocation_control(
            target, log, permits
        )
        attempts.extend(control_attempts)
        operation_ids.extend(control_ops)

        measurements = await self.measure(target, attempts, operation_ids=operation_ids)

        incoherent = [r for r in records if r["disposition"] == "incoherent"]
        not_staged = [r for r in records if r["disposition"] == "not_staged"]
        admitted = [r for r in records if r["disposition"] == "admitted_once"]
        denied = [r for r in records if r["disposition"] == "denied"]

        first_admitted = admitted[0]["interleaving"] if admitted else None
        last_denied = denied[-1]["interleaving"] if denied else None
        if not_staged:
            # An unstaged interleaving is not a boundary at which revocation
            # was observed to work or not work; inferring a point from the
            # rest would read as a finding about the product when it is a
            # finding about the harness.
            authoritative_point = "not_established"
            point_sentence = (
                "the authorization point could not be established, because "
                + "; ".join(
                    f"{r['interleaving']} proved nothing ("
                    + "; ".join(r["problems"])
                    + ")"
                    for r in not_staged
                )
            )
        elif first_admitted is None:
            authoritative_point = "after_claim_or_later"
            point_sentence = (
                "revocation was still effective at every instrumented "
                "boundary, so the authorization point is at or after "
                "after_claim"
            )
        else:
            authoritative_point = first_admitted
            point_sentence = (
                f"revocation stopped being effective once the call had passed "
                f"{first_admitted} ({BOUNDARY_MEANING[first_admitted]}); a "
                f"revocation landing at or after that boundary did not stop "
                f"the in-flight call"
            )

        positive = control["positive_control"]
        positive_ok = bool(positive["admitted"])
        control_ok = bool(control["all_refused"]) and bool(control["all_attributable"])
        verdict = (
            Verdict.PASS
            if not incoherent and not not_staged and control_ok and positive_ok
            else Verdict.FAIL
        )

        disposition_text = ", ".join(
            f"{r['interleaving']}={r['disposition']}" for r in records
        )
        parts = [
            f"Four interleavings, each with its own permit, payment id and "
            f"key: {disposition_text}.",
            f"Authorization point: {point_sentence}.",
            (
                f"Post-revocation control: "
                f"{control['refused']}/{control['submitted']} fresh operations "
                f"submitted under an already-revoked permit were refused, "
                f"{control['attributable']}/{control['submitted']} of them for a "
                f"reason naming the revocation ({control['dispatches']} "
                f"dispatch(es) at the fault layer, "
                f"{control['downstream_executions']} execution(s) in the effect "
                f"ledger)."
            ),
            (
                "Live-path control: a fresh permit submitted after every test "
                "permit was revoked was "
                + (
                    f"admitted ({positive['dispatches']} dispatch, "
                    f"{positive['downstream_executions']} execution), so the "
                    f"refusals above mean revoked, not broken."
                    if positive_ok
                    else f"NOT admitted (status {positive['status']!r}, reason "
                    f"{positive['reason']!r}), so this run cannot tell a "
                    f"refusal caused by revocation from a broken governed path."
                )
            ),
        ]
        if not_staged:
            for record in not_staged:
                parts.append(
                    f"NOT STAGED at {record['interleaving']}: "
                    + "; ".join(record["problems"])
                    + ". This run does not substantiate the claim at that "
                    "boundary."
                )
        if control["all_refused"] and not control["all_attributable"]:
            parts.append(
                "The post-revocation control was refused, but not every refusal "
                "named the revocation as the reason, so the refusals do not "
                "establish that revocation is what bit."
            )
        if incoherent:
            for record in incoherent:
                parts.append(
                    f"INCOHERENT at {record['interleaving']}: "
                    + "; ".join(record["problems"])
                    + "."
                )
        if not control["all_refused"]:
            parts.append(
                "INCOHERENT: a revoked permit admitted a second, distinct "
                "operation after revocation had completed."
            )
        if verdict is Verdict.PASS:
            parts.append(
                "Every interleaving was staged (the call really paused at its "
                "boundary and the permit really read back revoked), and none "
                "produced a second dispatch, a second execution, a charge "
                "without a receipt, or a receipt whose accounting did not "
                "match it."
            )
        observation = " ".join(parts)

        log.emit(
            "t08.verdict",
            f"{target.configuration.value} -> {verdict.value}",
            scenario=self.test_id,
            configuration=target.configuration.value,
            authoritative_point=authoritative_point,
            dispositions={r["interleaving"]: r["disposition"] for r in records},
            staged={r["interleaving"]: r["staged"] for r in records},
            control_refused=control["refused"],
            control_attributable=control["attributable"],
            control_submitted=control["submitted"],
            live_path_control_admitted=positive_ok,
        )

        remaining_risks = [
            "The boundaries are instrumented pause points, not a natural "
            "race: they prove where authorization is evaluated, not how "
            "likely each interleaving is in production.",
            "There is no instrumented boundary between the request arriving "
            "and the authorize/reserve/prepare commit, so this test brackets "
            "the authorization point to 'at or before after_prepare' rather "
            "than pinpointing it inside that transaction.",
            "The interleavings run in one process against SQLite, where the "
            "permit read and the authorize/reserve/prepare commit are "
            "serialized by a single writer. PostgreSQL's row-lock path, "
            "multiple gateway processes and replica lag are not exercised, so "
            "where authorization linearizes here is not by itself a statement "
            "about where it linearizes in production.",
            "The reason a control refusal is attributed to revocation is the "
            "gateway's own reason code, which is not an independent "
            "observation. What is independent is that the fault layer saw no "
            "dispatch and the effect ledger no execution, and that a live "
            "permit submitted at the same point was still admitted.",
        ]
        if first_admitted is not None:
            remaining_risks.append(
                f"A revocation issued while a call is past {first_admitted} "
                f"does not stop that call. Revocation bounds what can be "
                f"newly admitted, not what is already admitted."
            )

        return self.result(
            target,
            verdict=verdict,
            observation=observation,
            measurements=measurements,
            attempts=attempts,
            remaining_risks=remaining_risks,
            extra={
                "authoritative_point": authoritative_point,
                "authoritative_point_detail": {
                    "boundary": authoritative_point,
                    "boundary_meaning": BOUNDARY_MEANING.get(
                        authoritative_point,
                        "the harness could not establish it"
                        if authoritative_point == "not_established"
                        else "at or after the dispatch claim",
                    ),
                    "bracket": (
                        f"at or before {authoritative_point}"
                        if authoritative_point in BOUNDARY_MEANING
                        else authoritative_point
                    ),
                    "staged": {r["interleaving"]: r["staged"] for r in records},
                    "last_interleaving_revocation_denied": last_denied,
                    "first_interleaving_revocation_did_not_stop": first_admitted,
                    "summary": point_sentence,
                },
                # Same list under both names: "cases" is the suite-wide key
                # for a per-sub-case table.
                "cases": records,
                "interleavings": records,
                "post_revocation_control": control,
            },
        )

    # -- one interleaving -------------------------------------------------

    async def _run_interleaving(
        self,
        target: Target,
        log: EventLog,
        *,
        index: int,
        name: str,
        boundary: str | None,
    ) -> tuple[dict[str, Any], AttemptOutcome]:
        gateway, tenant, _ = target.require_gateway()
        permit = await gateway.issue_permit(tenant, max_credits=PERMIT_MAX_CREDITS)
        permit_id = str(permit["permit_id"])
        payment_id = f"pay_t08_{index}_{name}"
        operation_id, refund = self.refund(payment_id)
        identity = OperationIdentity.first_attempt(operation_id)
        agent = target.gateway_agent(permit_id)

        log.emit(
            "t08.interleaving.start",
            f"{name}: permit {permit_id} issued for {operation_id}",
            scenario=self.test_id,
            configuration=target.configuration.value,
            interleaving=name,
            boundary=boundary,
            permit_id=permit_id,
            operation_id=operation_id,
            idempotency_key=identity.idempotency_key,
        )

        before = await gateway.snapshot(tenant)
        boundary_outcome: str | None = None
        revoked_while_in_flight = False

        if boundary is None:
            revoke = await gateway.revoke_permit(tenant, permit_id)
            log.emit(
                "t08.revoke",
                f"{name}: permit revoked before the call was submitted",
                scenario=self.test_id,
                configuration=target.configuration.value,
                interleaving=name,
                permit_id=permit_id,
                revoke_status=revoke.get("status"),
            )
            outcome = await agent.submit(
                identity, refund, timeout_seconds=SUBMIT_TIMEOUT_SECONDS
            )
        else:
            with gateway.hold_at(boundary) as hold:
                task = asyncio.create_task(
                    agent.submit(
                        identity, refund, timeout_seconds=SUBMIT_TIMEOUT_SECONDS
                    )
                )
                boundary_outcome = await _wait_for_boundary(
                    hold, task, HOLD_TIMEOUT_SECONDS
                )
                if boundary_outcome == "reached":
                    revoke = await gateway.revoke_permit(tenant, permit_id)
                    revoked_while_in_flight = True
                    log.emit(
                        "t08.revoke",
                        f"{name}: permit revoked while the call was paused at {boundary}",
                        scenario=self.test_id,
                        configuration=target.configuration.value,
                        interleaving=name,
                        permit_id=permit_id,
                        boundary=boundary,
                        revoke_status=revoke.get("status"),
                    )
                else:
                    log.emit(
                        "t08.boundary_not_reached",
                        f"{name}: the call never paused at {boundary} "
                        f"({boundary_outcome}); this interleaving stages no race",
                        scenario=self.test_id,
                        configuration=target.configuration.value,
                        interleaving=name,
                        boundary=boundary,
                        boundary_outcome=boundary_outcome,
                    )
                hold.release.set()
                outcome = await task
            if not revoked_while_in_flight:
                await gateway.revoke_permit(tenant, permit_id)

        if boundary is None:
            staged = True
            staging_note = "revocation committed before the call was submitted"
        elif boundary_outcome == "reached":
            staged = True
            staging_note = (
                f"the call paused at {boundary} and the revocation committed "
                f"while it was paused"
            )
        elif boundary_outcome == "call_finished_first":
            staged = False
            staging_note = (
                f"the call finished before ever pausing at {boundary}, so the "
                f"revocation landed after it, not during it"
            )
        else:
            staged = False
            staging_note = (
                f"the harness waited {HOLD_TIMEOUT_SECONDS}s and the call neither "
                f"paused at {boundary} nor finished, so what happened next was "
                f"the harness giving up, not the product"
            )

        after = await gateway.snapshot(tenant)
        record = self._accounting(
            target,
            before=before,
            after=after,
            operation_id=operation_id,
            outcome=outcome,
        )
        record.update(
            {
                # ``case`` is the suite-wide name for a sub-case row, so a
                # report rendering all fourteen scenarios can key on one
                # field; ``interleaving`` is this scenario's own name and is
                # kept because its specification uses it.
                "case": name,
                "interleaving": name,
                "boundary": boundary,
                "boundary_meaning": BOUNDARY_MEANING[name],
                "boundary_outcome": boundary_outcome,
                "boundary_reached": boundary_outcome == "reached"
                if boundary is not None
                else None,
                "revoked_while_in_flight": revoked_while_in_flight,
                "staged": staged,
                "staging_note": staging_note,
                "permit_id": permit_id,
                "operation_id": operation_id,
                "idempotency_key": identity.idempotency_key,
                "payment_id": payment_id,
            }
        )
        permit_after = await gateway.get_permit(tenant, permit_id)
        record["permit_final_status"] = permit_after.get("status")
        record["permit_revoked_at"] = permit_after.get("revoked_at")
        record["permit_spent_credits"] = (
            str(permit_after["spent_credits"])
            if permit_after.get("spent_credits") is not None
            else None
        )
        # The premise of every interleaving: the authority really was
        # withdrawn. If ``revoke_permit`` silently no-ops the whole scenario
        # is measuring nothing, and must say so rather than report PASS.
        record["permit_revocation_confirmed"] = (
            str(permit_after.get("status")) == "revoked"
            and permit_after.get("revoked_at") is not None
        )

        disposition, problems = _evaluate(record)
        record["disposition"] = disposition
        record["problems"] = problems

        log.emit(
            "t08.interleaving.finish",
            f"{name}: status={outcome.status} -> {disposition}",
            scenario=self.test_id,
            configuration=target.configuration.value,
            interleaving=name,
            status=outcome.status,
            client_visible_state=outcome.client_visible_state,
            dispatches=record["dispatches"],
            downstream_executions=record["downstream_executions"],
            net_debit_count=record["net_debit_count"],
            net_charge_credits=record["net_charge_credits"],
            receipt_outcomes=record["receipt_outcomes"],
            permit_final_status=record["permit_final_status"],
            staged=staged,
            staging_note=staging_note,
            disposition=disposition,
            problems=problems,
        )
        return record, outcome

    # -- accounting -------------------------------------------------------

    def _accounting(
        self,
        target: Target,
        *,
        before: GatewaySnapshot,
        after: GatewaySnapshot,
        operation_id: str,
        outcome: AttemptOutcome,
    ) -> dict[str, Any]:
        """Attribute the gateway rows this one call added, plus ground truth."""
        added_debits = _added(before.debits, after.debits, "entry_id")
        added_refunds = _added(before.refunds, after.refunds, "entry_id")
        added_receipts = _added(before.receipts, after.receipts, "receipt_id")
        added_attempts = _added(before.attempts, after.attempts, "attempt_id")
        net_charge = -(_credits(added_debits) + _credits(added_refunds))
        reason_codes = [r.get("reason_code") for r in added_receipts]
        return {
            "status": outcome.status,
            "client_visible_state": outcome.client_visible_state,
            "reason": outcome.reason,
            "receipt_reason_codes": [
                str(c) if c is not None else None for c in reason_codes
            ],
            "refusal_attributable_to_revocation": _revocation_attributable(
                outcome.reason, reason_codes
            ),
            "http_status": outcome.http_status,
            "dispatches": target.injector.dispatch_count(operation_id=operation_id),
            "downstream_executions": target.ledger.execution_count(
                operation_id=operation_id
            ),
            "debit_count": len(added_debits),
            "refund_count": len(added_refunds),
            "net_debit_count": len(added_debits) - len(added_refunds),
            "net_charge_credits": str(net_charge),
            "receipt_outcomes": [str(r["outcome"]) for r in added_receipts],
            "receipt_ids": [str(r["receipt_id"]) for r in added_receipts],
            "receipt_credits_charged": [
                r.get("credits_charged") for r in added_receipts
            ],
            "gateway_attempt_states": [
                {
                    "state": a["state"],
                    "sent": a["sent"],
                    "claim_hash_present": a["claim_hash_present"],
                    "credits_charged": a.get("credits_charged"),
                    "debit_refunded_at": a.get("debit_refunded_at"),
                }
                for a in added_attempts
            ],
        }

    # -- the control ------------------------------------------------------

    async def _post_revocation_control(
        self,
        target: Target,
        log: EventLog,
        permits: list[tuple[str, str]],
    ) -> tuple[dict[str, Any], list[AttemptOutcome], list[str]]:
        """A fresh operation under each already-revoked permit must be refused."""
        gateway, tenant, _ = target.require_gateway()
        entries: list[dict[str, Any]] = []
        outcomes: list[AttemptOutcome] = []
        operation_ids: list[str] = []

        for name, permit_id in permits:
            payment_id = f"pay_t08_control_{name}"
            operation_id, refund = self.refund(payment_id)
            identity = OperationIdentity.first_attempt(operation_id)
            agent = target.gateway_agent(permit_id)
            before = await gateway.snapshot(tenant)
            outcome = await agent.submit(
                identity, refund, timeout_seconds=SUBMIT_TIMEOUT_SECONDS
            )
            after = await gateway.snapshot(tenant)
            record = self._accounting(
                target,
                before=before,
                after=after,
                operation_id=operation_id,
                outcome=outcome,
            )
            refused = (
                outcome.status in REFUSED_STATUSES
                and record["dispatches"] == 0
                and record["downstream_executions"] == 0
                and Decimal(record["net_charge_credits"]) == 0
            )
            # Refused is not the same as refused *because of the revocation*.
            # ``insufficient_funds``, ``key_conflict`` and ``invalid_params``
            # are all refusals, so a control that accepted any of them would
            # report a working revocation for an exhausted wallet.
            attributable = refused and bool(
                record["refusal_attributable_to_revocation"]
            )
            entry = {
                "permit_from_interleaving": name,
                "permit_id": permit_id,
                "operation_id": operation_id,
                "idempotency_key": identity.idempotency_key,
                "refused": refused,
                "attributable_to_revocation": attributable,
                **record,
            }
            entries.append(entry)
            outcomes.append(outcome)
            operation_ids.append(operation_id)
            log.emit(
                "t08.post_revocation_control",
                f"fresh operation under revoked permit from {name}: "
                f"status={outcome.status} refused={refused}",
                scenario=self.test_id,
                configuration=target.configuration.value,
                interleaving=name,
                permit_id=permit_id,
                operation_id=operation_id,
                status=outcome.status,
                reason=outcome.reason,
                dispatches=record["dispatches"],
                downstream_executions=record["downstream_executions"],
                net_charge_credits=record["net_charge_credits"],
                refused=refused,
                attributable_to_revocation=attributable,
            )

        positive, positive_outcome, positive_op = await self._positive_control(
            target, log
        )
        outcomes.append(positive_outcome)
        operation_ids.append(positive_op)

        control = {
            "submitted": len(entries),
            "refused": sum(1 for e in entries if e["refused"]),
            "attributable": sum(1 for e in entries if e["attributable_to_revocation"]),
            "all_refused": all(e["refused"] for e in entries) if entries else False,
            "all_attributable": (
                all(e["attributable_to_revocation"] for e in entries)
                if entries
                else False
            ),
            "dispatches": sum(int(e["dispatches"]) for e in entries),
            "downstream_executions": sum(
                int(e["downstream_executions"]) for e in entries
            ),
            "attempts": entries,
            "positive_control": positive,
        }
        return control, outcomes, operation_ids

    async def _positive_control(
        self, target: Target, log: EventLog
    ) -> tuple[dict[str, Any], AttemptOutcome, str]:
        """Prove the governed path still works once every permit is revoked.

        Without this, four refusals are ambiguous: they are equally consistent
        with "revocation is effective" and with "the tool, the wallet or the
        downstream is broken and everything is being refused". A fresh permit
        and a fresh operation, submitted after the revocations, must still be
        admitted -- dispatch seen by the fault layer, execution seen by the
        effect ledger, one receipt. Only then does a refusal mean *revoked*.
        """
        gateway, tenant, _ = target.require_gateway()
        permit = await gateway.issue_permit(tenant, max_credits=PERMIT_MAX_CREDITS)
        permit_id = str(permit["permit_id"])
        payment_id = "pay_t08_control_live_path"
        operation_id, refund = self.refund(payment_id)
        identity = OperationIdentity.first_attempt(operation_id)
        agent = target.gateway_agent(permit_id)

        before = await gateway.snapshot(tenant)
        outcome = await agent.submit(
            identity, refund, timeout_seconds=SUBMIT_TIMEOUT_SECONDS
        )
        after = await gateway.snapshot(tenant)
        record = self._accounting(
            target,
            before=before,
            after=after,
            operation_id=operation_id,
            outcome=outcome,
        )
        admitted = (
            outcome.status not in REFUSED_STATUSES
            and int(record["dispatches"]) == 1
            and int(record["downstream_executions"]) == 1
            and record["receipt_outcomes"] == ["success"]
            and Decimal(record["net_charge_credits"]) > 0
        )
        positive = {
            "purpose": (
                "a live, unrevoked permit submitted after every test permit was "
                "revoked; if this is not admitted, the refusals above say nothing "
                "about revocation"
            ),
            "permit_id": permit_id,
            "operation_id": operation_id,
            "idempotency_key": identity.idempotency_key,
            "admitted": admitted,
            **record,
        }
        log.emit(
            "t08.positive_control",
            f"live-path control under a fresh permit: status={outcome.status} "
            f"admitted={admitted}",
            scenario=self.test_id,
            configuration=target.configuration.value,
            permit_id=permit_id,
            operation_id=operation_id,
            status=outcome.status,
            reason=outcome.reason,
            dispatches=record["dispatches"],
            downstream_executions=record["downstream_executions"],
            receipt_outcomes=record["receipt_outcomes"],
            net_charge_credits=record["net_charge_credits"],
            admitted=admitted,
        )
        return positive, outcome, operation_id
