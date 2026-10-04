"""Test 1 -- Concurrent identical retry."""

from __future__ import annotations

import asyncio
from collections import Counter as StatusTally
from typing import Any

from failure_lab.configurations import (
    ALL_CONFIGURATIONS,
    AttemptOutcome,
    Configuration,
    Target,
)
from failure_lab.identity import OperationIdentity
from failure_lab.scenarios.base import (
    ConfigurationResult,
    EventLog,
    Measurements,
    Scenario,
    Verdict,
)

#: Statuses a healthy run of this workload produces when the agent talks to the
#: downstream tool directly. Anything else is an unexpected *product* status and
#: is reported, never smoothed over.
DIRECT_EXPECTED_STATUSES = frozenset({"succeeded", "replayed"})

#: Statuses a healthy run produces through the gateway: the holder of the
#: idempotency key completes, and every other caller is either served the stored
#: envelope or told the key is still in flight.
GATEWAY_EXPECTED_STATUSES = frozenset({"success", "in_progress"})

#: Statuses that can mean the harness itself, rather than the product, buckled
#: under the requested concurrency (in-process asyncio over SQLite).
HARNESS_STRAIN_STATUSES = frozenset({"timeout", "transport_error"})

#: Client patience, in seconds. Deliberately far above the gateway's own call
#: timeout and above its bounded idempotency wait, so what this test records is
#: the gateway's classification of the storm and not the harness's impatience.
DEFAULT_TIMEOUT_SECONDS = 30.0

#: The framework's own default client patience (``Agent.submit``), recorded so
#: the report can say what a less patient caller would have seen.
FRAMEWORK_DEFAULT_TIMEOUT_SECONDS = 5.0

#: Below this the workload cannot exercise the claim at all: one request in
#: flight satisfies every counter in the verdict rule by construction, so a
#: PASS would be true of the arithmetic rather than of the product.
MIN_CONCURRENCY = 2

#: When attempts end in harness strain the client stops waiting while the
#: gateway is still working, so the instruments can be read mid-write. They are
#: re-read until they stop moving, bounded by this budget. Re-reading can only
#: reveal more rows, so it can turn a PASS into a FAIL and never the reverse.
STRAIN_SETTLE_SECONDS = 12.0
STRAIN_SETTLE_INTERVAL_SECONDS = 0.5

#: How many consecutive unchanged readings count as quiescent when there is no
#: gateway to ask whether work is still outstanding. Counters that have not
#: moved for this long are the only evidence available on the direct paths.
STRAIN_SETTLE_STABLE_READINGS = 4

#: Minor units per refund, mirroring :meth:`Scenario.refund`'s default.
AMOUNT_MINOR_UNITS = 5000


def _fingerprint(measurements: Measurements) -> tuple[int | None, ...]:
    """The counters a verdict is computed from, for a quiescence check."""
    counters = measurements.counters
    return (
        counters.downstream_executions,
        counters.downstream_requests,
        counters.gateway_dispatches,
        counters.gateway_sent_attempts,
        counters.gateway_debits,
        counters.receipts,
    )


def _tally(attempts: list[AttemptOutcome]) -> dict[str, int]:
    return dict(sorted(StatusTally(a.status for a in attempts).items()))


def _visible_tally(attempts: list[AttemptOutcome]) -> dict[str, int]:
    return dict(sorted(StatusTally(a.client_visible_state for a in attempts).items()))


class ConcurrentIdenticalRetry(Scenario):
    """Send one accepted operation concurrently N times and count everything.

    WHAT TO IMPLEMENT
    -----------------
    1. Build one refund (``self.refund("pay_t01")``) and one identity
       (``OperationIdentity.first_attempt(operation_id)``).
    2. Fire ``self.options.get("concurrency", 100)`` submissions of that
       *identical* identity concurrently via ``asyncio.gather`` on
       ``target.agent.submit(identity.retry(), refund, timeout_seconds=...)``.
       ``identity.retry()`` keeps the idempotency key and the business
       operation id and varies only ``request_id`` -- which is the point:
       100 distinct wire requests, one business operation, one key.
    3. Measure with ``await self.measure(target, attempts, operation_ids=[op])``.
    4. Verdict rules:
       * ``DIRECT_NAIVE``  -> always ``OBSERVED``. Record the duplicate count.
       * ``DIRECT_NATIVE`` -> ``PASS`` iff downstream_executions == 1.
       * gateway configs   -> ``PASS`` iff downstream_executions == 1 AND
         gateway_dispatches == 1 AND gateway_debits == 1 AND receipts == 1 AND
         every non-``in_progress`` attempt that succeeded carries the SAME
         receipt_id. Anything else is ``FAIL``.
    5. The observation string MUST NOT use the words "exactly once" about the
       downstream effect. Say what was counted and by which instrument.

    If the requested concurrency produces harness-level errors (SQLite write
    contention, not a product failure), reduce it until the run is clean,
    record the level actually used in ``extra["concurrency"]``, and add a
    limitation saying so. Do not silence a product failure this way: an
    attempt that returns an unexpected *product* status is a finding.
    """

    test_id = "T01"
    title = "Concurrent identical retry"
    claim = (
        "One accepted idempotency key admits at most one gateway dispatch, "
        "at most one ledger debit, and one receipt, however many identical "
        "requests arrive at once."
    )
    tier = "slow"
    configurations = ALL_CONFIGURATIONS
    expected = {
        Configuration.DIRECT_NAIVE.value: Verdict.OBSERVED.value,
        Configuration.DIRECT_NATIVE.value: Verdict.PASS.value,
        Configuration.GATEWAY_NATIVE.value: Verdict.PASS.value,
        Configuration.GATEWAY_NAIVE.value: Verdict.PASS.value,
    }
    limitations = (
        "Counts gateway dispatches and downstream executions; does not prove "
        "exactly-once downstream execution in general.",
        "Concurrency is in-process asyncio against SQLite, not a multi-node load test.",
    )

    async def run_configuration(
        self, target: Target, log: EventLog
    ) -> ConfigurationResult:
        requested = int(self.options.get("concurrency", 100))
        concurrency = max(MIN_CONCURRENCY, requested)
        timeout_seconds = float(
            self.options.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        )
        operation_id, refund = self.refund("pay_t01")
        identity = OperationIdentity.first_attempt(operation_id)

        log.emit(
            "workload.plan",
            f"{concurrency} concurrent wire requests, one business operation, one key",
            scenario=self.test_id,
            configuration=target.configuration.value,
            concurrency=concurrency,
            concurrency_requested=requested,
            concurrency_floor_applied=concurrency != requested,
            operation_id=operation_id,
            idempotency_key=identity.idempotency_key,
            key_policy=identity.key_policy.value,
            client_timeout_seconds=timeout_seconds,
        )

        submissions = [identity.retry() for _ in range(concurrency)]
        attempts: list[AttemptOutcome] = list(
            await asyncio.gather(
                *(
                    target.agent.submit(one, refund, timeout_seconds=timeout_seconds)
                    for one in submissions
                )
            )
        )

        status_tally = _tally(attempts)
        identities = [a.identity for a in attempts]
        distinct_request_ids = len({i["request_id"] for i in identities})
        distinct_keys = len({i["idempotency_key"] for i in identities})
        distinct_operations = len({i["business_operation_id"] for i in identities})
        log.emit(
            "workload.complete",
            f"{len(attempts)} attempts returned: {status_tally}",
            scenario=self.test_id,
            configuration=target.configuration.value,
            statuses=status_tally,
            client_visible_states=_visible_tally(attempts),
            distinct_request_ids=distinct_request_ids,
            distinct_idempotency_keys=distinct_keys,
            distinct_business_operations=distinct_operations,
        )

        allowed = (
            GATEWAY_EXPECTED_STATUSES
            if target.uses_gateway
            else DIRECT_EXPECTED_STATUSES
        )
        unexpected = _tally([a for a in attempts if a.status not in allowed])
        strain = {k: v for k, v in unexpected.items() if k in HARNESS_STRAIN_STATUSES}
        product_anomalies = {k: v for k, v in unexpected.items() if k not in strain}
        if unexpected:
            log.emit(
                "measure.unexpected_statuses",
                f"attempt statuses outside the healthy set: {unexpected}",
                scenario=self.test_id,
                configuration=target.configuration.value,
                unexpected=unexpected,
                possible_harness_strain=strain,
                product_anomalies=product_anomalies,
            )

        measurements = await self.measure(
            target, attempts, operation_ids=[operation_id]
        )
        settle_seconds = 0.0
        settled = True
        if strain:
            measurements, settle_seconds, settled = await self._settle(
                target, attempts, operation_id, measurements, log
            )
        ledger_count = target.ledger.execution_count(operation_id)
        layer_requests = target.injector.dispatch_count(operation_id)
        log.emit(
            "measure.instruments",
            (
                f"effect ledger (independent): {ledger_count} execution(s); "
                f"fault layer (independent): {layer_requests} request(s) into the tool"
            ),
            scenario=self.test_id,
            configuration=target.configuration.value,
            downstream_executions=ledger_count,
            downstream_requests=layer_requests,
            gateway_debits=measurements.counters.gateway_debits,
            gateway_receipts=measurements.counters.receipts,
            gateway_sent_attempts=measurements.counters.gateway_sent_attempts,
        )

        preconditions = self._preconditions(
            attempts=attempts,
            concurrency=concurrency,
            distinct_request_ids=distinct_request_ids,
            distinct_keys=distinct_keys,
            distinct_operations=distinct_operations,
            measurements=measurements,
            ledger_count=ledger_count,
            layer_requests=layer_requests,
        )
        if preconditions:
            log.emit(
                "measure.preconditions_unmet",
                "this run did not establish the conditions the claim is about: "
                + "; ".join(preconditions),
                scenario=self.test_id,
                configuration=target.configuration.value,
                precondition_failures=preconditions,
            )

        extra: dict[str, Any] = {
            "concurrency": concurrency,
            "concurrency_requested": requested,
            "concurrency_floor_applied": concurrency != requested,
            "client_timeout_seconds": timeout_seconds,
            "framework_default_timeout_seconds": FRAMEWORK_DEFAULT_TIMEOUT_SECONDS,
            "operation_id": operation_id,
            "idempotency_key": identity.idempotency_key,
            "distinct_request_ids": distinct_request_ids,
            "distinct_idempotency_keys": distinct_keys,
            "distinct_business_operations": distinct_operations,
            "attempt_statuses": status_tally,
            "client_visible_states": _visible_tally(attempts),
            "unexpected_statuses": unexpected,
            "possible_harness_strain": strain,
            "product_anomalies": product_anomalies,
            "precondition_failures": preconditions,
            "instrument_settle_seconds": settle_seconds,
            "instruments_settled": settled,
            "effect_ledger_execution_count": ledger_count,
            "fault_layer_downstream_requests": layer_requests,
        }
        risks: list[str] = []
        if concurrency != requested:
            risks.append(
                f"A concurrency of {requested} was requested; {concurrency} was run. "
                "A single request in flight satisfies every counter in the verdict rule "
                "by construction, so this scenario will not report a verdict on a storm "
                "of one; it ran the smallest workload that can test the claim instead."
            )
        if preconditions:
            risks.append(
                "The conditions this test's claim is about were not established on this "
                f"run: {'; '.join(preconditions)}. The verdict below reflects that, and "
                "the counters should not be read as a measurement of the guarantee."
            )
        if product_anomalies:
            risks.append(
                "Attempt statuses outside the healthy set for this configuration "
                f"were observed and are reported, not discarded: {product_anomalies}."
            )
        if strain:
            risks.append(
                f"{sum(strain.values())} attempt(s) ended in {sorted(strain)} at "
                f"concurrency {concurrency}. The client-side counters below are "
                "therefore a lower bound on what the product resolved; the "
                "independent ledger and fault-layer counts are not affected. The "
                f"instruments were re-read for {settle_seconds} s afterwards, "
                + (
                    "until the run was quiescent, so they are not a half-written state."
                    if settled
                    else "and the run had not reached a state this scenario can call "
                    "quiescent when that budget ran out, so the counts below may be "
                    "partial and are a lower bound."
                )
                + " Re-run with --option concurrency=<lower> to separate harness "
                "contention from product behaviour."
            )

        if target.uses_gateway:
            return self._gateway_result(
                target,
                log,
                attempts,
                measurements,
                extra,
                risks,
                ledger_count,
                layer_requests,
                preconditions,
            )
        if target.configuration is Configuration.DIRECT_NATIVE:
            return self._direct_native_result(
                target,
                log,
                attempts,
                measurements,
                extra,
                risks,
                ledger_count,
                layer_requests,
                preconditions,
            )
        return self._direct_naive_result(
            target,
            log,
            attempts,
            measurements,
            extra,
            risks,
            ledger_count,
            layer_requests,
            preconditions,
        )

    # -- measurement hygiene ---------------------------------------------

    @staticmethod
    def _gateway_busy(measurements: Measurements) -> bool | None:
        """Is the gateway still working on this storm? ``None`` when there is none.

        Counters that have not moved are not evidence of a finished run: a
        gateway that has not yet written its first row reads the same as one
        that has written its last. So the gateway is asked directly -- an
        idempotency record that is not completed, or a dispatch attempt with no
        completion time, means work is outstanding. No durable record at all,
        after a storm that was admitted, means it has not started writing yet.
        """
        snapshot = measurements.snapshot
        if snapshot is None:
            return None
        records = list(snapshot.idempotency_records)
        if not records:
            return True
        if any(not record.get("completed") for record in records):
            return True
        return any(attempt.get("completed_at") is None for attempt in snapshot.attempts)

    async def _settle(
        self,
        target: Target,
        attempts: list[AttemptOutcome],
        operation_id: str,
        measurements: Measurements,
        log: EventLog,
    ) -> tuple[Measurements, float, bool]:
        """Re-read the instruments until the run is quiescent, or the budget ends.

        A caller that gave up leaves the product working, so the first reading
        can miss a dispatch, a debit or a receipt that is still being written.
        Only called when attempts ended in harness strain. Later readings can
        only reveal more rows, so this can turn a PASS into a FAIL and never
        the other way round.
        """
        waited = 0.0
        stable = 0
        settled = False
        while waited < STRAIN_SETTLE_SECONDS:
            await asyncio.sleep(STRAIN_SETTLE_INTERVAL_SECONDS)
            waited = round(waited + STRAIN_SETTLE_INTERVAL_SECONDS, 3)
            current = await self.measure(target, attempts, operation_ids=[operation_id])
            stable = (
                stable + 1 if _fingerprint(current) == _fingerprint(measurements) else 0
            )
            measurements = current
            busy = self._gateway_busy(current)
            if busy is False and stable >= 1:
                settled = True
                break
            if busy is None and stable >= STRAIN_SETTLE_STABLE_READINGS:
                settled = True
                break
        log.emit(
            "measure.settled",
            f"instruments re-read for {waited} s after harness strain "
            f"({'quiescent' if settled else 'not quiescent within the budget'})",
            scenario=self.test_id,
            configuration=target.configuration.value,
            settle_seconds=waited,
            settled=settled,
        )
        return measurements, waited, settled

    def _preconditions(
        self,
        *,
        attempts: list[AttemptOutcome],
        concurrency: int,
        distinct_request_ids: int,
        distinct_keys: int,
        distinct_operations: int,
        measurements: Measurements,
        ledger_count: int,
        layer_requests: int,
    ) -> list[str]:
        """What must be true for the counters to mean what the claim means.

        Every one of these can only be false if the workload degenerated or an
        instrument contradicted itself. Reported as verdict failures rather
        than assumed, because a storm that was not a storm, or a count read
        from an instrument that disagrees with itself, would otherwise satisfy
        the arithmetic of the verdict rule and report PASS.
        """
        unmet: list[str] = []
        if len(attempts) != concurrency:
            unmet.append(
                f"{len(attempts)} attempt(s) returned for a concurrency of {concurrency}"
            )
        if distinct_request_ids != len(attempts):
            unmet.append(
                f"the storm was not {len(attempts)} distinct wire requests "
                f"({distinct_request_ids} distinct request id(s))"
            )
        if distinct_keys != 1:
            unmet.append(
                f"the storm did not carry one idempotency key "
                f"({distinct_keys} distinct key(s))"
            )
        if distinct_operations != 1:
            unmet.append(
                f"the storm did not name one business operation "
                f"({distinct_operations} distinct operation id(s))"
            )
        if ledger_count != measurements.counters.downstream_executions:
            unmet.append(
                f"the effect ledger disagrees with itself: execution_count="
                f"{ledger_count}, effect rows={measurements.counters.downstream_executions}"
            )
        if layer_requests != measurements.counters.downstream_requests:
            unmet.append(
                f"the fault layer disagrees with itself: dispatch_count="
                f"{layer_requests}, crossings={measurements.counters.downstream_requests}"
            )
        return unmet

    # -- per-configuration verdicts --------------------------------------

    def _direct_naive_result(
        self,
        target: Target,
        log: EventLog,
        attempts: list[AttemptOutcome],
        measurements: Measurements,
        extra: dict[str, Any],
        risks: list[str],
        ledger_count: int,
        layer_requests: int,
        preconditions: list[str],
    ) -> ConfigurationResult:
        duplicates = max(0, ledger_count - 1)
        extra["duplicate_executions"] = duplicates
        extra["amount_minor_units_per_execution"] = AMOUNT_MINOR_UNITS
        extra["duplicated_value_minor_units"] = duplicates * AMOUNT_MINOR_UNITS
        observation = (
            f"{len(attempts)} identical concurrent requests for one business "
            f"operation. The independent effect ledger recorded {ledger_count} "
            f"downstream execution(s) and the independent fault layer counted "
            f"{layer_requests} request(s) reaching the tool."
        )
        if ledger_count > 1:
            observation += (
                f" {duplicates} refund(s) of {AMOUNT_MINOR_UNITS} minor units were "
                f"paid out beyond the one the agent intended "
                f"({duplicates * AMOUNT_MINOR_UNITS} minor units of duplicated value)."
            )
        elif ledger_count == 1:
            observation += (
                " One execution was recorded for the whole storm, which is not what an "
                "integration without replay defence is expected to do; the number is "
                "reported as measured rather than explained."
            )
        else:
            observation += (
                " No execution was recorded at all, so this run measured nothing about "
                "duplication and its numbers should not be quoted as a baseline."
            )
        if ledger_count == layer_requests:
            observation += (
                " Nothing here deduplicates: the count of effects tracked the count of "
                "requests one for one."
            )
        else:
            observation += (
                f" Effects ({ledger_count}) and requests ({layer_requests}) do not match, "
                "so something other than this configuration's tool dropped or collapsed "
                "requests on this run; the gap is reported, not explained away."
            )
        if extra["unexpected_statuses"]:
            observation += (
                f" Attempt statuses outside the healthy set for a direct caller: "
                f"{extra['unexpected_statuses']}."
            )
        if preconditions:
            observation += (
                " Conditions the claim is about were not established on this run: "
                + "; ".join(preconditions)
                + "."
            )
        observation += (
            " Descriptive baseline: there is no guarantee here to pass or fail, and the "
            "numbers are the point."
        )
        risks.append(
            "A direct naive integration has no replay defence at all: a retry "
            "storm multiplies the business effect one for one."
        )
        log.emit(
            "verdict",
            f"{target.configuration.value} -> OBSERVED ({ledger_count} executions)",
            scenario=self.test_id,
            configuration=target.configuration.value,
            downstream_executions=ledger_count,
            duplicate_executions=duplicates,
        )
        return self.result(
            target,
            verdict=Verdict.OBSERVED,
            observation=observation,
            measurements=measurements,
            attempts=attempts,
            remaining_risks=risks,
            extra=extra,
        )

    def _direct_native_result(
        self,
        target: Target,
        log: EventLog,
        attempts: list[AttemptOutcome],
        measurements: Measurements,
        extra: dict[str, Any],
        risks: list[str],
        ledger_count: int,
        layer_requests: int,
        preconditions: list[str],
    ) -> ConfigurationResult:
        fresh = sum(1 for a in attempts if a.status == "succeeded")
        replayed = sum(1 for a in attempts if a.status == "replayed")
        extra["fresh_responses"] = fresh
        extra["replayed_responses"] = replayed
        failures: list[str] = list(preconditions)
        if ledger_count != 1:
            failures.append(
                f"downstream executions (independent effect ledger) = {ledger_count}, want 1"
            )
        verdict = Verdict.PASS if not failures else Verdict.FAIL
        extra["verdict_failures"] = failures
        observation = (
            f"{len(attempts)} identical concurrent requests. The independent "
            f"effect ledger recorded {ledger_count} downstream execution(s) for "
            f"this operation while the independent fault layer counted "
            f"{layer_requests} request(s) reaching the tool. The tool answered "
            f"{fresh} caller(s) with a fresh execution and {replayed} from its "
            f"stored result."
        )
        if layer_requests == len(attempts):
            observation += (
                " Every request was admitted to the tool, so whatever collapsing "
                "happened, happened inside it."
            )
        else:
            observation += (
                f" The fault layer counted {layer_requests} request(s) for "
                f"{len(attempts)} attempt(s), so the storm did not reach the tool one "
                "for one and this run does not show the tool collapsing all of it."
            )
        if extra["unexpected_statuses"]:
            observation += (
                f" Attempt statuses outside the healthy set for a direct caller: "
                f"{extra['unexpected_statuses']}."
            )
        if failures:
            observation += " Guarantee not met: " + "; ".join(failures) + "."
        else:
            observation += (
                " A correctly built downstream held this line under concurrency with no "
                "gateway in front of it."
            )
        risks.append(
            "The native guarantee here is one SQLite transaction in one process; "
            "it says nothing about the same tool sharded across nodes or "
            "databases."
        )
        risks.append(
            "The deduplication is invisible from outside the tool: the caller "
            "gets no signed artifact, and no third party can check after the "
            "fact how many times the operation ran."
        )
        log.emit(
            "verdict",
            f"{target.configuration.value} -> {verdict.value} ({ledger_count} executions)",
            scenario=self.test_id,
            configuration=target.configuration.value,
            downstream_executions=ledger_count,
            fresh_responses=fresh,
            replayed_responses=replayed,
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

    def _gateway_result(
        self,
        target: Target,
        log: EventLog,
        attempts: list[AttemptOutcome],
        measurements: Measurements,
        extra: dict[str, Any],
        risks: list[str],
        ledger_count: int,
        layer_requests: int,
        preconditions: list[str],
    ) -> ConfigurationResult:
        counters = measurements.counters
        dispatches = counters.gateway_dispatches
        debits = counters.gateway_debits
        receipts = counters.receipts
        sent = counters.gateway_sent_attempts

        answered = [a for a in attempts if a.status == "success"]
        in_progress = [a for a in attempts if a.status == "in_progress"]
        receipt_ids = sorted(
            {a.receipt_id for a in answered if a.receipt_id is not None}
        )
        without_receipt = sum(1 for a in answered if a.receipt_id is None)
        outcomes = measurements.receipt_outcomes()

        extra.update(
            {
                "answered_attempts": len(answered),
                "in_progress_attempts": len(in_progress),
                "distinct_receipt_ids_on_answered": len(receipt_ids),
                "receipt_ids_on_answered": receipt_ids,
                "answered_without_receipt": without_receipt,
                "gateway_dispatches": dispatches,
                "gateway_debits": debits,
                "gateway_refunds": counters.gateway_refunds,
                "gateway_net_debits": counters.gateway_net_debits,
                "gateway_sent_attempts": sent,
                "gateway_receipts": receipts,
                "receipt_outcomes": outcomes,
                "wallet_balance": (
                    measurements.snapshot.wallet_balance
                    if measurements.snapshot is not None
                    else None
                ),
            }
        )

        failures: list[str] = list(preconditions)
        if ledger_count != 1:
            failures.append(
                f"downstream executions (independent effect ledger) = {ledger_count}, want 1"
            )
        if dispatches != 1:
            failures.append(
                f"gateway dispatches (independent fault layer) = {dispatches}, want 1"
            )
        if debits != 1:
            failures.append(f"wallet debits (gateway-reported) = {debits}, want 1")
        if receipts != 1:
            failures.append(f"receipts (gateway-reported) = {receipts}, want 1")
        if sent is not None and sent != dispatches:
            failures.append(
                f"the gateway's own attempt table reports {sent} send(s) past the claim "
                f"where the independent fault layer observed {dispatches} crossing(s); "
                "the two instruments do not agree about what was dispatched"
            )
        if not answered:
            failures.append(
                "no caller was answered with a completed result, so whatever the "
                "gateway's own books say, nobody learned the outcome of the operation"
            )
        elif without_receipt or len(receipt_ids) != 1:
            failures.append(
                f"answered callers did not all cite one receipt id "
                f"(distinct ids={len(receipt_ids)}, answered={len(answered)}, "
                f"answered without a receipt={without_receipt})"
            )
        if extra["product_anomalies"]:
            failures.append(
                "caller(s) received neither a completed result nor an in-flight "
                f"answer: {extra['product_anomalies']}"
            )

        verdict = Verdict.PASS if not failures else Verdict.FAIL
        extra["verdict_failures"] = failures

        observation = (
            f"{len(attempts)} identical concurrent requests carrying one "
            f"idempotency key ({extra['idempotency_key']}) and "
            f"{extra['distinct_request_ids']} distinct request ids. Independently "
            f"observed: the effect ledger recorded {ledger_count} downstream "
            f"execution(s) and the fault layer counted {layer_requests} request(s) "
            f"crossing into the tool. Gateway-reported: {debits} wallet debit(s), "
            f"{sent} attempt(s) past the send boundary, and {receipts} receipt(s) "
            f"[{', '.join(outcomes) if outcomes else 'none'}]. "
            f"{len(answered)} caller(s) were answered with a completed result "
            f"citing {len(receipt_ids)} distinct receipt id(s); {len(in_progress)} "
            f"were told the key was still in flight. The cost of collapsing the "
            f"storm shows in latency: p50 {counters.latency_p50_ms} ms, p95 "
            f"{counters.latency_p95_ms} ms per caller, against a client patience "
            f"budget of {extra['client_timeout_seconds']} s."
        )
        if extra["unexpected_statuses"]:
            observation += (
                f" Attempt statuses outside the healthy set for a gateway caller: "
                f"{extra['unexpected_statuses']}."
            )
        if failures:
            observation += " Guarantee not met: " + "; ".join(failures) + "."
        log.emit(
            "verdict",
            f"{target.configuration.value} -> {verdict.value}",
            scenario=self.test_id,
            configuration=target.configuration.value,
            downstream_executions=ledger_count,
            gateway_dispatches=dispatches,
            gateway_sent_attempts=sent,
            gateway_debits=debits,
            receipts=receipts,
            distinct_receipt_ids=len(receipt_ids),
            answered=len(answered),
            in_progress=len(in_progress),
            failures=failures,
        )

        risks.append(
            "Dispatches and debits were counted for one operation under "
            "in-process concurrency against one gateway database; a partitioned "
            "or multi-node gateway deployment is not exercised here."
        )
        patience = float(extra["client_timeout_seconds"])
        if patience >= DEFAULT_TIMEOUT_SECONDS:
            risks.append(
                f"Client patience for this run was {patience} s, set above the gateway's "
                "own bounded in-progress wait (its upstream connect and call timeouts "
                "plus five seconds) so that what is recorded is how the gateway "
                "classifies the storm rather than when the harness gave up. A caller "
                "with less patience learns correspondingly less; the framework's "
                f"default is {FRAMEWORK_DEFAULT_TIMEOUT_SECONDS} s."
            )
        else:
            risks.append(
                f"Client patience for this run was {patience} s, below this scenario's "
                f"default of {DEFAULT_TIMEOUT_SECONDS} s. A patience that expires inside "
                "the gateway's own bounded in-progress wait records when the harness "
                "gave up rather than how the gateway classified the storm, so the "
                "client-side counts here describe this client, not the product."
            )
        if len(answered) > 1 and ledger_count == 1:
            risks.append(
                f"{len(answered)} callers each received a completed result, but "
                f"only {ledger_count} execution reached the tool. The stored "
                "envelope is served back verbatim, so a caller cannot tell from "
                "its own payload whether it drove the call or was served a copy; "
                "the shared receipt id is the only signal that distinguishes them."
            )
        if in_progress:
            risks.append(
                f"{len(in_progress)} caller(s) got an in-flight answer rather than "
                "a final one. A real agent must then poll or retry to learn the "
                "outcome, and this test does not measure that follow-up."
            )
        p95 = counters.latency_p95_ms
        if p95 is not None and p95 >= 1000:
            risks.append(
                f"Collapsing the storm is a blocking wait paid by every caller: "
                f"p95 was {p95} ms at concurrency "
                f"{extra['concurrency']}. A client whose patience is shorter than "
                "that gets no information even though the gateway's own state is "
                "consistent."
            )
        if p95 is not None and p95 >= FRAMEWORK_DEFAULT_TIMEOUT_SECONDS * 1000:
            risks.append(
                f"At concurrency {extra['concurrency']} the 95th-percentile caller "
                f"waited {p95} ms, longer than the framework's default client patience "
                f"of {FRAMEWORK_DEFAULT_TIMEOUT_SECONDS} s. A caller holding to that "
                "default would have been left with no information about an operation "
                "the gateway had dispatched, debited and receipted. The guarantee "
                "measured here is about the gateway's dispatch and debit, not about "
                "any caller learning the outcome."
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
