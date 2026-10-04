"""Test 3 -- Execute, then lose the response."""

from __future__ import annotations

from typing import Any

from failure_lab.configurations import (
    ALL_CONFIGURATIONS,
    AttemptOutcome,
    Configuration,
    Target,
)
from failure_lab.faults import FaultMode, FaultPlan
from failure_lab.identity import OperationIdentity
from failure_lab.scenarios.base import (
    ConfigurationResult,
    EventLog,
    Measurements,
    Scenario,
    Verdict,
)

#: How long the fault layer withholds the executed call's response before it
#: severs the connection. It only bounds a leaked handler: every caller in this
#: scenario is meant to give up first, so this must exceed every client budget
#: below.
DEFAULT_HOLD_SECONDS = 30.0

#: Client patience on the gateway path. Comfortably above the gateway's own 2s
#: upstream call timeout, so what this test records is the gateway's
#: classification of the lost response rather than the harness's impatience.
DEFAULT_GATEWAY_TIMEOUT_SECONDS = 8.0

#: Client patience on the direct path. There is no gateway to classify
#: anything here, so the client's own timeout *is* the mechanism under
#: measurement; it only has to be shorter than the hold above.
DEFAULT_DIRECT_TIMEOUT_SECONDS = 4.0

#: Minor units per refund, mirroring :meth:`Scenario.refund`'s default.
AMOUNT_MINOR_UNITS = 5000

#: The only receipt outcome that is honest about a response that never came
#: back after the dispatch claim was committed.
UNCERTAIN_OUTCOME = "delivery_uncertain"

#: A receipt outcome that would assert the gateway knows the call succeeded.
#: It cannot know that here, so recording it is a failure of the claim.
OVERCLAIMED_OUTCOME = "success"

#: The scope every configuration's result carries. The lost response here is
#: produced by an in-process fault layer holding a response inside one event
#: loop, against SQLite -- not a network partition, a load balancer dropping a
#: connection, or a PostgreSQL deployment. The shape of the failure the caller
#: sees is the same; the environment it is observed in is not production's.
HARNESS_SCOPE_RISK = (
    "The lost response is produced by an in-process fault layer that holds "
    "the executed call's answer inside a single event loop, against SQLite. "
    "That reproduces the shape a caller sees -- the tool ran, the answer "
    "never came -- but not a real network partition, a proxy dropping a "
    "connection mid-body, or the PostgreSQL deployment the product runs on."
)


class ExecuteThenLoseResponse(Scenario):
    """The headline failure: the tool ran, the answer vanished, the agent retried.

    WHAT TO IMPLEMENT
    -----------------
    1. Arm ``FaultPlan(mode=FaultMode.RESPONSE_LOST_AFTER_EXECUTION,
       operation_id=op, hold_seconds=...)`` on ``target.injector``. The tool
       executes and commits; the response never comes back.
    2. First attempt: submit and let the client time out (direct configs) or
       let the gateway's call timeout fire (gateway configs). Use a client
       timeout comfortably above the gateway's own 2s call timeout so the
       gateway's classification, not the harness's impatience, is what is
       measured.
    3. Retry with ``identity.retry()`` -- same key, new request id.
    4. Measure and record ALL of: downstream execution count (effect ledger),
       gateway dispatch count (fault layer), gateway attempt state, debit
       state, receipt state, and client-visible state for both attempts.
    5. Verdict rules:
       * ``DIRECT_NAIVE``  -> ``OBSERVED``; expect 2 executions. This is the
         duplicate the whole lab exists to show.
       * ``DIRECT_NATIVE`` -> ``PASS`` iff downstream_executions == 1 and the
         retry was answered from the stored result.
       * gateway configs   -> ``PASS`` iff downstream_executions == 1 AND
         gateway_dispatches == 1 AND the first attempt's client-visible state
         is ``explicit_uncertain`` AND the retry returns the SAME receipt id
         without a second dispatch or debit.
    6. CRITICAL honesty rule: the gateway must not claim knowledge it does not
       have. ``FAIL`` if any gateway receipt for this operation records
       ``success``. The correct outcome is ``delivery_uncertain``. State in the
       observation that the charge is retained by design and that the receipt
       does not establish whether the refund reached the customer.
    """

    test_id = "T03"
    title = "Execute then lose response"
    claim = (
        "When a tool executes but its response is lost, the gateway records a "
        "distinct uncertain outcome, never a second dispatch and never a "
        "claim of success it cannot support."
    )
    tier = "fast"
    configurations = ALL_CONFIGURATIONS
    expected = {
        Configuration.DIRECT_NAIVE.value: Verdict.OBSERVED.value,
        Configuration.DIRECT_NATIVE.value: Verdict.PASS.value,
        Configuration.GATEWAY_NATIVE.value: Verdict.PASS.value,
        Configuration.GATEWAY_NAIVE.value: Verdict.PASS.value,
    }
    limitations = (
        "The gateway retains the charge on an uncertain outcome by design; "
        "this test measures that, it does not argue for it.",
        "A delivery_uncertain receipt is not evidence about the downstream "
        "effect. Only the independent effect ledger speaks to that here.",
    )

    async def run_configuration(
        self, target: Target, log: EventLog
    ) -> ConfigurationResult:
        hold_seconds = float(self.options.get("hold_seconds", DEFAULT_HOLD_SECONDS))
        timeout_seconds = self._client_budget(target)
        call_timeout = self._gateway_call_timeout(target)

        # State the budgets before the run, and say out loud when one of them
        # makes the harness -- rather than the product -- the thing being
        # measured. A verdict produced under an impatient client budget is a
        # statement about this harness; a reader must not have to infer that
        # from a FAIL.
        if call_timeout is not None and timeout_seconds <= call_timeout:
            log.emit(
                "budget.warning",
                (
                    f"client budget {timeout_seconds}s does not exceed the gateway's "
                    f"own {call_timeout}s upstream call timeout: this run measures "
                    f"the harness giving up, not the gateway's classification of a "
                    f"lost response"
                ),
                scenario=self.test_id,
                configuration=target.configuration.value,
                client_timeout_seconds=timeout_seconds,
                gateway_call_timeout_seconds=call_timeout,
            )
        if hold_seconds <= timeout_seconds:
            log.emit(
                "budget.warning",
                (
                    f"the fault layer severs the withheld response after "
                    f"{hold_seconds}s, before the client's {timeout_seconds}s budget "
                    f"expires: the caller will see a severed connection rather than a "
                    f"timeout. Still a lost response, but a different shape of one"
                ),
                scenario=self.test_id,
                configuration=target.configuration.value,
                hold_seconds=hold_seconds,
                client_timeout_seconds=timeout_seconds,
            )

        operation_id, refund = self.refund("pay_t03")
        amount = refund.amount
        identity = OperationIdentity.first_attempt(operation_id)

        target.injector.arm(
            FaultPlan(
                mode=FaultMode.RESPONSE_LOST_AFTER_EXECUTION,
                operation_id=operation_id,
                remaining=1,
                hold_seconds=hold_seconds,
                label="T03: execute, then withhold the response",
            )
        )
        log.emit(
            "fault.arm",
            (
                "armed response_lost_after_execution for one request on this "
                "operation: the tool executes and commits, the answer never returns"
            ),
            scenario=self.test_id,
            configuration=target.configuration.value,
            operation_id=operation_id,
            idempotency_key=identity.idempotency_key,
            hold_seconds=hold_seconds,
            client_timeout_seconds=timeout_seconds,
            gateway_call_timeout_seconds=call_timeout,
            amount_minor_units=amount,
        )

        first = await target.agent.submit(
            identity, refund, timeout_seconds=timeout_seconds
        )
        # Read BEFORE the retry, because every observation below opens by
        # saying the tool executed and its response was withheld. Arming a
        # fault is not evidence that it fired, and a post-retry total cannot
        # say which attempt produced the effect. These two numbers are what
        # licence that sentence.
        executions_after_first = target.ledger.execution_count(operation_id)
        fault_applied = any(
            crossing.fault == FaultMode.RESPONSE_LOST_AFTER_EXECUTION.value
            for crossing in target.injector.crossings(operation_id=operation_id)
        )
        log.emit(
            "attempt.first",
            (
                f"first attempt -> status={first.status}, "
                f"client_visible_state={first.client_visible_state}"
            ),
            scenario=self.test_id,
            configuration=target.configuration.value,
            request_id=identity.request_id,
            status=first.status,
            client_visible_state=first.client_visible_state,
            http_status=first.http_status,
            reason=first.reason,
            receipt_id=first.receipt_id,
            receipt_outcome=first.receipt.get("outcome") if first.receipt else None,
            latency_ms=round(first.latency_ms, 3),
            executions_so_far=target.ledger.execution_count(operation_id),
            downstream_requests_so_far=target.injector.dispatch_count(operation_id),
        )

        retry_identity = identity.retry()
        second = await target.agent.submit(
            retry_identity, refund, timeout_seconds=timeout_seconds
        )
        log.emit(
            "attempt.retry",
            (
                f"retry (same key, new request id) -> status={second.status}, "
                f"client_visible_state={second.client_visible_state}"
            ),
            scenario=self.test_id,
            configuration=target.configuration.value,
            request_id=retry_identity.request_id,
            idempotency_key=retry_identity.idempotency_key,
            same_key_as_first=(
                retry_identity.idempotency_key == identity.idempotency_key
            ),
            status=second.status,
            client_visible_state=second.client_visible_state,
            http_status=second.http_status,
            reason=second.reason,
            receipt_id=second.receipt_id,
            receipt_outcome=second.receipt.get("outcome") if second.receipt else None,
            latency_ms=round(second.latency_ms, 3),
        )

        attempts = [first, second]
        # Measured BEFORE the control runs, so every gateway-reported count
        # below describes the probe alone. The control is a real governed call
        # and adds its own debit and receipt afterwards; those land in
        # ``liveness_control`` instead.
        measurements = await self.measure(
            target, attempts, operation_ids=[operation_id]
        )
        ledger_count = target.ledger.execution_count(operation_id)
        layer_requests = target.injector.dispatch_count(operation_id)
        reached_tool = target.injector.reached_tool_count(operation_id)
        log.emit(
            "measure.instruments",
            (
                f"effect ledger (independent): {ledger_count} execution(s); "
                f"fault layer (independent): {layer_requests} request(s) into the tool, "
                f"{reached_tool} of which reached it"
            ),
            scenario=self.test_id,
            configuration=target.configuration.value,
            downstream_executions=ledger_count,
            downstream_requests=layer_requests,
            downstream_requests_reaching_tool=reached_tool,
            gateway_debits=measurements.counters.gateway_debits,
            gateway_refunds=measurements.counters.gateway_refunds,
            gateway_net_debits=measurements.counters.gateway_net_debits,
            gateway_receipts=measurements.counters.receipts,
            gateway_sent_attempts=measurements.counters.gateway_sent_attempts,
            receipt_outcomes=measurements.receipt_outcomes(),
        )

        control = await self._liveness_control(target, log, timeout_seconds)

        extra: dict[str, Any] = {
            "operation_id": operation_id,
            "idempotency_key": identity.idempotency_key,
            "fault": FaultMode.RESPONSE_LOST_AFTER_EXECUTION.value,
            "hold_seconds": hold_seconds,
            "client_timeout_seconds": timeout_seconds,
            "gateway_call_timeout_seconds": call_timeout,
            "first_attempt_status": first.status,
            "first_attempt_client_visible_state": first.client_visible_state,
            "first_attempt_receipt_id": first.receipt_id,
            "first_attempt_receipt_outcome": (
                first.receipt.get("outcome") if first.receipt else None
            ),
            "retry_status": second.status,
            "retry_client_visible_state": second.client_visible_state,
            "retry_receipt_id": second.receipt_id,
            "retry_receipt_outcome": (
                second.receipt.get("outcome") if second.receipt else None
            ),
            "retry_used_same_key": (
                retry_identity.idempotency_key == identity.idempotency_key
            ),
            "effect_ledger_execution_count": ledger_count,
            "effect_ledger_executions_after_first_attempt": executions_after_first,
            "first_attempt_fault_applied": fault_applied,
            "fault_layer_downstream_requests": layer_requests,
            "fault_layer_requests_reaching_tool": reached_tool,
            # Named so failure_lab.evidence._FAULT_KEYS_IN_EXTRA finds the
            # armed failure even on a run where it never fired.
            "fault_plan": {
                "mode": FaultMode.RESPONSE_LOST_AFTER_EXECUTION.value,
                "operation_id": operation_id,
                "hold_seconds": hold_seconds,
                "applied_to_first_attempt": fault_applied,
            },
            "amount_minor_units_per_execution": amount,
            "liveness_control": control,
            "counters_measured_before_control": True,
        }

        premise = self._premise(executions_after_first, fault_applied)
        if target.uses_gateway:
            return self._gateway_result(
                target,
                log,
                attempts,
                measurements,
                extra,
                premise=premise,
                ledger_count=ledger_count,
                layer_requests=layer_requests,
                reached_tool=reached_tool,
                control=control,
                timeout_seconds=timeout_seconds,
                call_timeout=call_timeout,
            )
        if target.configuration is Configuration.DIRECT_NATIVE:
            return self._direct_native_result(
                target,
                log,
                attempts,
                measurements,
                extra,
                premise=premise,
                ledger_count=ledger_count,
                layer_requests=layer_requests,
                control=control,
            )
        return self._direct_naive_result(
            target,
            log,
            attempts,
            measurements,
            extra,
            premise=premise,
            ledger_count=ledger_count,
            layer_requests=layer_requests,
            amount=amount,
            control=control,
        )

    # -- budgets ----------------------------------------------------------

    def _client_budget(self, target: Target) -> float:
        """How long the harness waits, per path.

        The two paths measure different things, so they get different budgets
        and separate overrides. On the gateway path the client must outlast the
        gateway's own upstream call timeout, or the run records the harness
        giving up instead of the gateway classifying a lost response. On the
        direct path the client timeout *is* the mechanism under measurement.
        A single ``timeout_seconds`` override still works for both, and is kept
        so an operator can shorten the whole scenario deliberately.
        """
        if target.uses_gateway:
            default = DEFAULT_GATEWAY_TIMEOUT_SECONDS
            specific = self.options.get("gateway_timeout_seconds")
        else:
            default = DEFAULT_DIRECT_TIMEOUT_SECONDS
            specific = self.options.get("direct_timeout_seconds")
        if specific is not None:
            return float(specific)
        shared = self.options.get("timeout_seconds")
        return float(shared) if shared is not None else default

    @staticmethod
    def _gateway_call_timeout(target: Target) -> float | None:
        """The gateway's own upstream call timeout, read from the gateway."""
        gateway = target.gateway
        if gateway is None:
            return None
        value = getattr(gateway, "call_timeout_seconds", None)
        return None if value is None else float(value)

    # -- the control ------------------------------------------------------

    async def _liveness_control(
        self, target: Target, log: EventLog, timeout_seconds: float
    ) -> dict[str, Any]:
        """A fresh, unfaulted operation submitted after the probe was measured.

        Without it, "the gateway declined to dispatch a second time for this
        key" and "the gateway stopped dispatching anything" produce identical
        numbers, and so do "the downstream absorbed the retry" and "the
        downstream is returning a stale result to everyone". The control is a
        real governed call: it runs *after* :meth:`measure`, so its debit and
        receipt are outside the headline counters, and the ``*_after_control``
        fields are where its cost is visible.
        """
        operation_id, refund = self.refund("pay_t03_control")
        identity = OperationIdentity.first_attempt(operation_id)
        outcome = await target.agent.submit(
            identity, refund, timeout_seconds=timeout_seconds
        )
        executions = target.ledger.execution_count(operation_id)
        crossings = target.injector.dispatch_count(operation_id)
        control: dict[str, Any] = {
            "operation_id": operation_id,
            "idempotency_key": identity.idempotency_key,
            "status": outcome.status,
            "client_visible_state": outcome.client_visible_state,
            "http_status": outcome.http_status,
            "reason": outcome.reason,
            "receipt_id": outcome.receipt_id,
            "receipt_outcome": (
                outcome.receipt.get("outcome") if outcome.receipt else None
            ),
            "downstream_executions": executions,
            "downstream_requests": crossings,
            "executed": executions >= 1,
        }
        if target.gateway is not None and target.tenant is not None:
            after = await target.gateway.snapshot(target.tenant)
            control["gateway_debits_after_control"] = after.debit_count
            control["gateway_receipts_after_control"] = after.receipt_count
            control["gateway_receipt_outcomes_after_control"] = after.receipt_outcomes()
            control["gateway_sent_attempts_after_control"] = after.sent_attempt_count
            control["wallet_balance_after_control"] = after.wallet_balance
        log.emit(
            "control.fresh_operation",
            (
                f"control (fresh key, no fault armed) -> status={outcome.status}, "
                f"{executions} execution(s), {crossings} crossing(s) into the tool"
            ),
            scenario=self.test_id,
            configuration=target.configuration.value,
            operation_id=operation_id,
            idempotency_key=identity.idempotency_key,
            status=outcome.status,
            client_visible_state=outcome.client_visible_state,
            downstream_executions=executions,
            downstream_requests=crossings,
            executed=control["executed"],
        )
        return control

    @staticmethod
    def _control_sentence(control: dict[str, Any], *, subject: str) -> str:
        if control["executed"]:
            return (
                f"A control operation submitted afterwards under a fresh key, with no "
                f"fault armed, executed normally (status '{control['status']}', "
                f"{control['downstream_executions']} execution(s), "
                f"{control['downstream_requests']} crossing(s) into the tool), so the "
                f"counts above describe {subject}, not a wedged path."
            )
        return (
            f"The control operation submitted afterwards under a fresh key did NOT "
            f"execute (status '{control['status']}', "
            f"{control['downstream_executions']} execution(s), "
            f"{control['downstream_requests']} crossing(s)), so this run cannot "
            f"separate {subject} from a path that had simply stopped working."
        )

    # -- what the first attempt actually did ------------------------------

    @staticmethod
    def _premise(executions_after_first: int, fault_applied: bool) -> str:
        """Open every observation with what the instruments support.

        This scenario is about a call that ran and whose answer vanished. That
        the fault was *armed* is not evidence that it fired, and a total taken
        after the retry cannot say which attempt produced the effect. So the
        opening sentence is chosen by the effect ledger and the fault layer,
        read between the two attempts, rather than asserted from the plan.
        """
        if executions_after_first >= 1 and fault_applied:
            return (
                "The tool executed and committed (effect ledger, independent: "
                f"{executions_after_first} execution(s) before the retry) and "
                "the fault layer then withheld the response it had already "
                "produced."
            )
        if executions_after_first >= 1:
            return (
                "The tool executed and committed (effect ledger, independent: "
                f"{executions_after_first} execution(s) before the retry), but "
                "the armed response-loss fault was NOT recorded on the first "
                "attempt's crossing, so whatever the caller saw was not the "
                "injected failure."
            )
        return (
            "PREMISE NOT ESTABLISHED: the first attempt committed no "
            "downstream effect at all (effect ledger, independent: 0 "
            "executions before the retry), so this run did not reproduce the "
            "executed-but-unacknowledged call the scenario is about, and "
            "nothing below should be read as measuring it."
        )

    # -- per-configuration verdicts --------------------------------------

    def _direct_naive_result(
        self,
        target: Target,
        log: EventLog,
        attempts: list[AttemptOutcome],
        measurements: Measurements,
        extra: dict[str, Any],
        *,
        premise: str,
        ledger_count: int,
        layer_requests: int,
        amount: int,
        control: dict[str, Any],
    ) -> ConfigurationResult:
        first, second = attempts
        duplicates = max(0, ledger_count - 1)
        extra["duplicate_executions"] = duplicates
        extra["value_at_risk_minor_units"] = duplicates * amount

        observation = (
            f"{premise} "
            f"The agent saw '{first.status}' "
            f"({first.client_visible_state}) and retried the same business "
            f"operation with the same idempotency key and a new request id; the "
            f"retry came back '{second.status}' ({second.client_visible_state}). "
            f"The independent effect ledger recorded {ledger_count} downstream "
            f"execution(s) for this operation -- {duplicates} beyond the one the "
            f"agent intended, {duplicates * amount} minor units of "
            f"unintended refund -- and the fault layer counted {layer_requests} "
            f"request(s) reaching the tool. Nothing in this configuration "
            f"deduplicates and nothing told the agent the first call had already "
            f"landed: descriptive baseline, no guarantee to test. "
            + self._control_sentence(
                control, subject="what this integration does with a retry"
            )
        )
        risks = [
            HARNESS_SCOPE_RISK,
            "A lost response is indistinguishable from a lost request to this "
            "agent, so the only safe options are to retry (and duplicate) or to "
            "abandon (and possibly never refund). It has no third option.",
            "There is no signed artifact here; an auditor asking later how many "
            "refunds this operation caused has only the tool's own ledger.",
        ]
        if not control["executed"]:
            risks.append(
                "The control did not execute, so the duplicate count above is a "
                "number from a path whose health was not established. Read it as "
                "provisional."
            )
        log.emit(
            "verdict",
            f"{target.configuration.value} -> OBSERVED ({ledger_count} executions)",
            scenario=self.test_id,
            configuration=target.configuration.value,
            downstream_executions=ledger_count,
            duplicate_executions=duplicates,
            control_executed=control["executed"],
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
        *,
        premise: str,
        ledger_count: int,
        layer_requests: int,
        control: dict[str, Any],
    ) -> ConfigurationResult:
        first, second = attempts
        retry_from_store = second.status == "replayed"
        extra["retry_served_from_stored_result"] = retry_from_store

        failures: list[str] = []
        if ledger_count != 1:
            failures.append(
                f"downstream executions (effect ledger) = {ledger_count}, want 1"
            )
        if not retry_from_store:
            failures.append(
                "the retry was not answered from the tool's stored result "
                f"(status={second.status}, client_visible_state="
                f"{second.client_visible_state})"
            )
        if not control["executed"]:
            failures.append(
                "the liveness control (fresh key, no fault armed) did not execute "
                f"(status={control['status']}, "
                f"{control['downstream_executions']} execution(s)); a tool that "
                "executes nothing suppresses duplicates trivially, so this run "
                "cannot credit its idempotency store"
            )
        extra["verdict_failures"] = failures
        verdict = Verdict.PASS if not failures else Verdict.FAIL

        retry_reached_tool = layer_requests >= 2
        observation = (
            f"{premise} "
            f"The agent saw '{first.status}' "
            f"({first.client_visible_state}) and retried with the same business "
            f"operation id. The retry returned '{second.status}' "
            f"({second.client_visible_state}). The independent effect ledger "
            f"recorded {ledger_count} downstream execution(s) and the fault layer "
            f"counted {layer_requests} request(s) reaching the tool, so the "
            + (
                "retry did reach the tool and was absorbed inside it. "
                if retry_reached_tool
                else "retry never reached the tool at all. "
            )
            + self._control_sentence(
                control, subject="what this tool's idempotency store did with the retry"
            )
        )
        if verdict is Verdict.PASS:
            observation += (
                " A correctly built downstream handles this failure on its own: the "
                "gateway adds nothing to the duplicate-suppression question here."
            )
        else:
            observation += (
                " The correct native baseline did NOT hold: "
                + "; ".join(failures)
                + "."
            )
        risks = [
            HARNESS_SCOPE_RISK,
            "The agent still never learned the outcome of its first call; it "
            "recovered only because it was willing to retry a consequential "
            "operation blind. The tool made that safe, not the agent.",
            "The deduplication is invisible from outside the tool -- there is no "
            "signed, portable artifact a third party could check, and no record "
            "that the first response was lost at all.",
        ]
        log.emit(
            "verdict",
            f"{target.configuration.value} -> {verdict.value} ({ledger_count} executions)",
            scenario=self.test_id,
            configuration=target.configuration.value,
            downstream_executions=ledger_count,
            retry_status=second.status,
            control_executed=control["executed"],
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
        *,
        premise: str,
        ledger_count: int,
        layer_requests: int,
        reached_tool: int,
        control: dict[str, Any],
        timeout_seconds: float,
        call_timeout: float | None,
    ) -> ConfigurationResult:
        first, second = attempts
        counters = measurements.counters
        dispatches = counters.gateway_dispatches
        debits = counters.gateway_debits
        refunds = counters.gateway_refunds
        net_debits = counters.gateway_net_debits
        receipt_count = counters.receipts
        outcomes = measurements.receipt_outcomes()
        snapshot = measurements.snapshot

        overclaimed = [o for o in outcomes if o == OVERCLAIMED_OUTCOME]
        dishonest = sorted({o for o in outcomes if o != UNCERTAIN_OUTCOME})
        uncertain_receipts = [o for o in outcomes if o == UNCERTAIN_OUTCOME]
        attempt_states = (
            [a["state"] for a in snapshot.attempts] if snapshot is not None else []
        )
        sent_attempts = counters.gateway_sent_attempts

        extra.update(
            {
                "gateway_dispatches": dispatches,
                "gateway_debits": debits,
                "gateway_refunds": refunds,
                "gateway_net_debits": net_debits,
                "gateway_sent_attempts": sent_attempts,
                "gateway_receipts": receipt_count,
                "receipt_outcomes": outcomes,
                "dispatch_attempt_states": attempt_states,
                "receipts_claiming_success": len(overclaimed),
                "receipts_recording_uncertainty": len(uncertain_receipts),
                "receipt_outcomes_other_than_uncertain": dishonest,
                "charge_retained": net_debits,
                "wallet_balance": (
                    snapshot.wallet_balance if snapshot is not None else None
                ),
                "idempotency_records": (
                    snapshot.idempotency_records if snapshot is not None else []
                ),
                "same_receipt_id_on_retry": (
                    first.receipt_id is not None
                    and second.receipt_id is not None
                    and first.receipt_id == second.receipt_id
                ),
            }
        )

        failures: list[str] = []

        # A missing instrument is not a pass. If the gateway's own tables could
        # not be read, every gateway-reported check below would otherwise be
        # skipped silently and the configuration would pass on the strength of
        # the checks that happened to survive.
        if snapshot is None:
            failures.append(
                "the gateway snapshot was unavailable, so its attempt, debit and "
                "receipt state could not be read at all"
            )

        # -- independent instruments --
        if ledger_count != 1:
            failures.append(
                f"downstream executions (effect ledger) = {ledger_count}, want 1"
            )
        if dispatches is None:
            failures.append("the fault layer reported no dispatch count")
        elif dispatches != 1:
            failures.append(f"gateway dispatches (fault layer) = {dispatches}, want 1")
        if reached_tool != 1:
            failures.append(
                f"requests that reached the tool (fault layer) = {reached_tool}, want 1"
            )

        # -- what the client was told --
        if first.client_visible_state != "explicit_uncertain":
            detail = (
                "the first attempt's client-visible state was "
                f"'{first.client_visible_state}' (status={first.status}), want "
                "'explicit_uncertain'"
            )
            if first.status == "timeout":
                detail += (
                    f" -- the client budget ({timeout_seconds}s) expired before the "
                    f"gateway's own {call_timeout}s upstream call timeout could "
                    f"classify the lost response, so this run measured the harness's "
                    f"patience, not the gateway's behaviour"
                )
            failures.append(detail)
        if first.receipt_id is None:
            failures.append("the first attempt returned no receipt id")
        elif second.receipt_id != first.receipt_id:
            failures.append(
                f"the retry cited receipt id {second.receipt_id!r}, not the "
                f"first attempt's {first.receipt_id!r}"
            )

        # -- gateway-reported: the charge --
        if debits is None:
            failures.append("gateway debits were not reported")
        elif debits != 1:
            failures.append(f"gateway debits (gateway-reported) = {debits}, want 1")
        if net_debits is None:
            failures.append("the gateway ledger was not reported")
        elif net_debits != 1:
            failures.append(
                f"the charge was not retained: {debits} debit(s) and {refunds} "
                f"refund(s), net {net_debits}, want a retained net 1 "
                f"(docs/failure-semantics.md: delivery_uncertain stays charged)"
            )

        # -- gateway-reported: what the receipt claims --
        if receipt_count is None:
            failures.append("gateway receipts were not reported")
        elif not outcomes:
            failures.append(
                "no receipt was written for a dispatch that crossed the send "
                "boundary, so there is no signed record of the uncertain outcome"
            )
        elif dishonest:
            failures.append(
                f"receipt outcome(s) {dishonest} recorded for a call whose response "
                f"never returned; the honest outcome is '{UNCERTAIN_OUTCOME}'"
            )
        if overclaimed:
            failures.append(
                f"{len(overclaimed)} receipt(s) record outcome "
                f"'{OVERCLAIMED_OUTCOME}' for a call whose response never "
                "returned -- the gateway claimed knowledge it does not have"
            )

        # -- the control --
        if not control["executed"]:
            failures.append(
                "the liveness control (fresh key, no fault armed) did not execute "
                f"(status={control['status']}, "
                f"{control['downstream_executions']} execution(s), "
                f"{control['downstream_requests']} crossing(s)); without a working "
                "control, 'the gateway declined to dispatch again' and 'the gateway "
                "stopped dispatching at all' are the same measurement"
            )

        verdict = Verdict.PASS if not failures else Verdict.FAIL
        extra["verdict_failures"] = failures

        if not outcomes:
            receipt_sentence = (
                "No receipt was written at all, so there is no signed record of this "
                "dispatch."
            )
        elif dishonest:
            receipt_sentence = (
                f"The receipt(s) written here record {outcomes}; "
                f"'{UNCERTAIN_OUTCOME}' is the only outcome the gateway can support "
                f"for a response it never saw."
            )
        else:
            receipt_sentence = (
                f"Every receipt written here records '{UNCERTAIN_OUTCOME}' and none "
                f"records '{OVERCLAIMED_OUTCOME}'. A receipt is a signed statement "
                f"about the gateway's own dispatch and debit, so it does not "
                f"establish whether the refund reached the customer."
            )
        if net_debits == 1 and refunds == 0:
            charge_sentence = (
                "The charge was retained, as documented: the dispatch claim was "
                "committed before the send, so the gateway cannot prove the call did "
                "not land, and it neither redispatches nor returns the credits."
            )
        else:
            charge_sentence = (
                f"The charge was NOT retained the way delivery_uncertain is "
                f"documented to behave: {debits} debit(s) and {refunds} refund(s), "
                f"net {net_debits}."
            )

        observation = (
            f"{premise} "
            f"Independent instruments: the effect ledger recorded {ledger_count} "
            f"downstream execution(s) for this operation and the fault layer counted "
            f"{layer_requests} request(s) crossing into the tool across both "
            f"attempts, {reached_tool} of which reached it. The first attempt came "
            f"back '{first.status}' ({first.client_visible_state}) with receipt "
            f"{first.receipt_id}; the retry, carrying the same idempotency key and a "
            f"new request id, came back '{second.status}' "
            f"({second.client_visible_state}) citing receipt {second.receipt_id}. "
            f"Gateway-reported: {sent_attempts} attempt(s) past the send boundary in "
            f"state(s) {attempt_states or ['none']}, {debits} debit(s) and {refunds} "
            f"refund(s) for a net {net_debits}, and {receipt_count} receipt(s) "
            f"recording {outcomes or ['none']}. "
            f"{charge_sentence} {receipt_sentence} "
            f"Only the effect ledger, which the gateway cannot reach, speaks to the "
            f"downstream effect, and here it counted {ledger_count} committed "
            f"execution(s) for this operation. "
            + self._control_sentence(
                control, subject="what the gateway did with this key"
            )
        )
        if overclaimed:
            observation += (
                f" HONESTY FAILURE: {len(overclaimed)} receipt(s) assert "
                f"'{OVERCLAIMED_OUTCOME}' for a response the gateway never saw."
            )
        if failures:
            observation += " Guarantee not met: " + "; ".join(failures) + "."

        risks = [
            HARNESS_SCOPE_RISK,
            "The charge is retained on an uncertain outcome. A caller whose "
            "downstream silently dropped the call pays for a refund that may "
            "never have happened; reconciling that is the caller's job, not the "
            "gateway's.",
            "A delivery_uncertain receipt is a signed statement about the "
            "gateway's own dispatch and debit, not evidence about the downstream "
            "effect. Nothing the gateway can sign here says how many refunds the "
            "customer received.",
            "The agent is told the outcome is unknown but is given no resolution "
            "path inside the gateway; it must reconcile with the tool provider "
            "using the forwarded idempotency metadata.",
        ]
        if ledger_count == 1 and layer_requests == 1:
            risks.append(
                "The downstream here honours a forwarded idempotency key only in "
                "the native configuration; this scenario never needed it, because "
                "the gateway sent the call once. That is a statement about this "
                "run's counts, not a general guarantee about the tool."
            )

        log.emit(
            "verdict",
            f"{target.configuration.value} -> {verdict.value}",
            scenario=self.test_id,
            configuration=target.configuration.value,
            downstream_executions=ledger_count,
            gateway_dispatches=dispatches,
            gateway_debits=debits,
            gateway_net_debits=net_debits,
            receipts=receipt_count,
            receipt_outcomes=outcomes,
            receipts_claiming_success=len(overclaimed),
            first_client_visible_state=first.client_visible_state,
            retry_receipt_matches_first=extra["same_receipt_id_on_retry"],
            control_executed=control["executed"],
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
