"""Test 6 -- Agent restart with a new idempotency key."""

from __future__ import annotations

from typing import Any

from failure_lab.configurations import (
    ALL_CONFIGURATIONS,
    AttemptOutcome,
    Configuration,
    Target,
)
from failure_lab.faults import FaultMode, FaultPlan
from failure_lab.identity import KeyPolicy, OperationIdentity
from failure_lab.scenarios.base import (
    ConfigurationResult,
    EventLog,
    Measurements,
    Scenario,
    Verdict,
)

#: How long the fault layer withholds the executed call's response before it
#: severs the connection. Every caller here is meant to give up first, so this
#: must exceed every client budget below; it only bounds a leaked handler.
DEFAULT_HOLD_SECONDS = 30.0

#: Client patience on the gateway path. Comfortably above the gateway's own 2s
#: upstream call timeout, so the first attempt records the gateway's
#: classification of the lost response rather than the harness's impatience.
DEFAULT_GATEWAY_TIMEOUT_SECONDS = 8.0

#: Client patience on the direct path. There is no gateway to classify
#: anything, so the client's own timeout is the mechanism; it only has to be
#: shorter than the hold above.
DEFAULT_DIRECT_TIMEOUT_SECONDS = 4.0

#: Minor units per refund, mirroring :meth:`Scenario.refund`'s default.
AMOUNT_MINOR_UNITS = 5000

#: Which layer, if any, stopped the replanned attempt from becoming a second
#: downstream effect. Recorded verbatim in ``extra["deduplicated_by"]``.
DEDUPLICATED_BY_GATEWAY = "gateway"
DEDUPLICATED_BY_DOWNSTREAM = "downstream"
DEDUPLICATED_BY_NOTHING = "nothing"

#: The scope statement this test exists to make explicit. The gateway's
#: documented replay guarantee is keyed on the idempotency key the caller
#: presents; two distinct keys are two distinct operations by design. The
#: business-level property a reader may assume ("one intended refund") is a
#: strictly larger claim, and the gap between the two is what is measured here.
GATEWAY_GUARANTEE_SCOPE = (
    "The gateway's documented guarantee is keyed on the idempotency key the "
    "caller presents, so two distinct keys are two distinct operations by "
    "design -- the gateway never sees the business_operation_id as an identity. "
    "This test measures the gap between that key-scoped guarantee and the "
    "business-level property a reader might assume it covers: one intended "
    "refund, executed at most once and paid for at most once. A FAIL here is a "
    "real gap against the business-level property and is simultaneously outside "
    "the guarantee the product states; both facts belong in any report of it."
)


class AgentRestartNewKey(Scenario):
    """The same business intent, a fresh agent, a brand-new key. Who notices?

    This is the test the PRD says must exist even though it is expected to
    fail today. DO NOT weaken it and DO NOT edit ``expected`` to make a run
    look better.

    WHAT TO IMPLEMENT
    -----------------
    1. Create the identity with ``OperationIdentity.first_attempt(op,
       key_policy=KeyPolicy.ATTEMPT)`` -- an agent that mints its key per
       attempt rather than deriving it from the business operation.
    2. Arm ``RESPONSE_LOST_AFTER_EXECUTION`` for the operation. Submit. The
       first attempt ends ambiguous.
    3. Destroy the agent's context: call ``identity.after_restart()``, which
       under ``KeyPolicy.ATTEMPT`` mints a NEW idempotency key and a new
       request id while keeping the SAME ``business_operation_id``. For the
       gateway configurations, also rebuild the caller
       (``target.gateway_agent(permit_id)``) so nothing in-process carries
       over from the first incarnation.
    4. Submit the replanned attempt and measure.
    5. Verdict rules -- the property under test is *business-level*: one
       intended refund must not execute twice and must not be paid for twice.
       * ``DIRECT_NAIVE``  -> ``OBSERVED``; two executions.
       * ``DIRECT_NATIVE`` -> ``PASS`` iff downstream_executions == 1. The
         downstream deduplicated on ``operation_id``, with no gateway present.
       * ``GATEWAY_NATIVE`` -> ``FAIL`` unless BOTH downstream_executions == 1
         AND gateway_debits == 1. Expect 1 execution (the downstream caught
         it) but 2 debits (the gateway did not), so expect FAIL.
       * ``GATEWAY_NAIVE``  -> ``FAIL`` unless downstream_executions == 1.
         Expect 2 executions and 2 debits.
    6. The observation MUST state, for each gateway configuration, which layer
       prevented the duplicate -- and must say explicitly when the answer is
       "the downstream, not the gateway". Record in
       ``extra["deduplicated_by"]`` one of ``"gateway"``, ``"downstream"``,
       ``"nothing"``.
    7. Record in ``extra["gateway_guarantee_scope"]`` that the gateway's
       documented guarantee is keyed on the idempotency key, so two distinct
       keys are two distinct operations by design; this test measures the gap
       between that guarantee and the business-level property a reader might
       assume it covers.
    """

    test_id = "T06"
    title = "Agent restart with a new idempotency key"
    claim = (
        "One intended business operation executes at most once and is paid "
        "for at most once, even when a restarted agent presents a new "
        "idempotency key for it."
    )
    tier = "fast"
    configurations = ALL_CONFIGURATIONS
    expected = {
        Configuration.DIRECT_NAIVE.value: Verdict.OBSERVED.value,
        Configuration.DIRECT_NATIVE.value: Verdict.PASS.value,
        Configuration.GATEWAY_NATIVE.value: Verdict.FAIL.value,
        Configuration.GATEWAY_NAIVE.value: Verdict.FAIL.value,
    }
    limitations = (
        "The gateway's documented guarantee is per idempotency key, not per "
        "business operation. The FAIL recorded here is a real gap against the "
        "business-level property, and it is outside the guarantee the product "
        "states. Both facts belong in any report of this result.",
        "An agent that derives its key from the business operation "
        "(KeyPolicy.BUSINESS) does not hit this; the other scenarios use that "
        "policy. This one exists because real agents replan.",
    )

    async def run_configuration(
        self, target: Target, log: EventLog
    ) -> ConfigurationResult:
        hold_seconds = float(self.options.get("hold_seconds", DEFAULT_HOLD_SECONDS))
        default_timeout = (
            DEFAULT_GATEWAY_TIMEOUT_SECONDS
            if target.uses_gateway
            else DEFAULT_DIRECT_TIMEOUT_SECONDS
        )
        timeout_seconds = float(self.options.get("timeout_seconds", default_timeout))

        operation_id, refund = self.refund("pay_t06")
        identity = OperationIdentity.first_attempt(
            operation_id, key_policy=KeyPolicy.ATTEMPT
        )

        target.injector.arm(
            FaultPlan(
                mode=FaultMode.RESPONSE_LOST_AFTER_EXECUTION,
                operation_id=operation_id,
                remaining=1,
                hold_seconds=hold_seconds,
                label="T06: execute, then withhold the response so the agent dies unsure",
            )
        )
        log.emit(
            "fault.arm",
            (
                "armed response_lost_after_execution for one request on this "
                "operation; the agent mints its idempotency key per attempt "
                "(KeyPolicy.ATTEMPT), so a restart will present a new key for "
                "the same business operation"
            ),
            scenario=self.test_id,
            configuration=target.configuration.value,
            operation_id=operation_id,
            key_policy=identity.key_policy.value,
            first_idempotency_key=identity.idempotency_key,
            hold_seconds=hold_seconds,
            client_timeout_seconds=timeout_seconds,
        )

        first = await target.agent.submit(
            identity, refund, timeout_seconds=timeout_seconds
        )

        # Read the independent instruments BEFORE the replan. Everything the
        # narrative below says about the first attempt -- that it executed,
        # that the armed fault is what withheld its response -- is measured
        # here rather than assumed from the fact that a plan was armed.
        executions_before_restart = target.ledger.execution_count(operation_id)
        crossings_before_restart = target.injector.crossings(operation_id=operation_id)
        requests_before_restart = len(crossings_before_restart)
        fault_applied = any(
            crossing.fault == FaultMode.RESPONSE_LOST_AFTER_EXECUTION.value
            and crossing.reached_tool
            and not crossing.delivered
            for crossing in crossings_before_restart
        )
        log.emit(
            "attempt.first",
            (
                f"first attempt (generation {identity.agent_generation}) -> "
                f"status={first.status}, "
                f"client_visible_state={first.client_visible_state}"
            ),
            scenario=self.test_id,
            configuration=target.configuration.value,
            request_id=identity.request_id,
            idempotency_key=identity.idempotency_key,
            agent_generation=identity.agent_generation,
            status=first.status,
            client_visible_state=first.client_visible_state,
            http_status=first.http_status,
            reason=first.reason,
            receipt_id=first.receipt_id,
            receipt_outcome=first.receipt.get("outcome") if first.receipt else None,
            latency_ms=round(first.latency_ms, 3),
            executions_so_far=executions_before_restart,
            downstream_requests_so_far=requests_before_restart,
            first_attempt_fault_applied=fault_applied,
        )

        # -- the restart: a fresh agent incarnation replans the same intent --
        restarted = identity.after_restart()
        caller = target.agent
        rebuilt_caller = False
        if target.uses_gateway:
            _, _, permit = target.require_gateway()
            caller = target.gateway_agent(permit["permit_id"])
            rebuilt_caller = True
        log.emit(
            "agent.restart",
            (
                "the agent process died without learning the outcome; a fresh "
                f"incarnation (generation {restarted.agent_generation}) replans "
                "the SAME business operation and, having no memory of the old "
                "key, mints a new one"
            ),
            scenario=self.test_id,
            configuration=target.configuration.value,
            business_operation_id=restarted.business_operation_id,
            business_operation_id_unchanged=(
                restarted.business_operation_id == identity.business_operation_id
            ),
            previous_idempotency_key=identity.idempotency_key,
            new_idempotency_key=restarted.idempotency_key,
            key_changed=(restarted.idempotency_key != identity.idempotency_key),
            agent_generation=restarted.agent_generation,
            caller_rebuilt=rebuilt_caller,
        )

        second = await caller.submit(restarted, refund, timeout_seconds=timeout_seconds)
        log.emit(
            "attempt.after_restart",
            (
                f"replanned attempt (new key, new request id, same business "
                f"operation) -> status={second.status}, "
                f"client_visible_state={second.client_visible_state}"
            ),
            scenario=self.test_id,
            configuration=target.configuration.value,
            request_id=restarted.request_id,
            idempotency_key=restarted.idempotency_key,
            agent_generation=restarted.agent_generation,
            status=second.status,
            client_visible_state=second.client_visible_state,
            http_status=second.http_status,
            reason=second.reason,
            receipt_id=second.receipt_id,
            receipt_outcome=second.receipt.get("outcome") if second.receipt else None,
            latency_ms=round(second.latency_ms, 3),
        )

        attempts = [first, second]
        measurements = await self.measure(
            target, attempts, operation_ids=[operation_id]
        )
        ledger_count = target.ledger.execution_count(operation_id)
        layer_requests = target.injector.dispatch_count(operation_id)
        replan_requests = max(0, layer_requests - requests_before_restart)
        deduplicated_by = self._deduplicated_by(
            target, ledger_count, executions_before_restart, replan_requests
        )
        premise = self._first_attempt_premise(executions_before_restart, fault_applied)

        log.emit(
            "measure.instruments",
            (
                f"effect ledger (independent): {ledger_count} execution(s), "
                f"{executions_before_restart} of them committed before the "
                f"restart; fault layer (independent): {layer_requests} "
                f"request(s) into the tool, {replan_requests} of them after the "
                f"restart; deduplicated_by={deduplicated_by}"
            ),
            scenario=self.test_id,
            configuration=target.configuration.value,
            downstream_executions=ledger_count,
            downstream_requests=layer_requests,
            executions_before_restart=executions_before_restart,
            replan_downstream_requests=replan_requests,
            first_attempt_fault_applied=fault_applied,
            deduplicated_by=deduplicated_by,
            gateway_debits=measurements.counters.gateway_debits,
            gateway_refunds=measurements.counters.gateway_refunds,
            gateway_net_debits=measurements.counters.gateway_net_debits,
            gateway_receipts=measurements.counters.receipts,
            gateway_sent_attempts=measurements.counters.gateway_sent_attempts,
            receipt_outcomes=measurements.receipt_outcomes(),
        )

        duplicate_executions = max(0, ledger_count - 1)
        extra: dict[str, Any] = {
            "operation_id": operation_id,
            "key_policy": identity.key_policy.value,
            "first_idempotency_key": identity.idempotency_key,
            "restart_idempotency_key": restarted.idempotency_key,
            "idempotency_key_changed": (
                restarted.idempotency_key != identity.idempotency_key
            ),
            "business_operation_id_unchanged": (
                restarted.business_operation_id == identity.business_operation_id
            ),
            "agent_generations": [
                identity.agent_generation,
                restarted.agent_generation,
            ],
            "caller_rebuilt_after_restart": rebuilt_caller,
            "fault": FaultMode.RESPONSE_LOST_AFTER_EXECUTION.value,
            "first_attempt_fault_applied": fault_applied,
            # Named so failure_lab.evidence._FAULT_KEYS_IN_EXTRA finds the
            # armed failure even on a run where it never fired.
            "fault_plan": {
                "mode": FaultMode.RESPONSE_LOST_AFTER_EXECUTION.value,
                "operation_id": operation_id,
                "hold_seconds": hold_seconds,
                "applied_to_first_attempt": fault_applied,
            },
            "hold_seconds": hold_seconds,
            "client_timeout_seconds": timeout_seconds,
            "first_attempt_status": first.status,
            "first_attempt_client_visible_state": first.client_visible_state,
            "first_attempt_receipt_id": first.receipt_id,
            "first_attempt_receipt_outcome": (
                first.receipt.get("outcome") if first.receipt else None
            ),
            "restart_attempt_status": second.status,
            "restart_attempt_client_visible_state": second.client_visible_state,
            "restart_attempt_receipt_id": second.receipt_id,
            "restart_attempt_receipt_outcome": (
                second.receipt.get("outcome") if second.receipt else None
            ),
            "restart_served_from_stored_result": second.status == "replayed"
            or bool(second.refund and second.refund.get("replayed")),
            "effect_ledger_execution_count": ledger_count,
            "effect_ledger_executions_before_restart": executions_before_restart,
            "fault_layer_downstream_requests": layer_requests,
            "fault_layer_requests_before_restart": requests_before_restart,
            "fault_layer_requests_after_restart": replan_requests,
            "executed_once": ledger_count == 1,
            "duplicate_executions": duplicate_executions,
            "value_at_risk_minor_units": duplicate_executions * AMOUNT_MINOR_UNITS,
            "amount_minor_units_per_execution": AMOUNT_MINOR_UNITS,
            "deduplicated_by": deduplicated_by,
            "gateway_guarantee_scope": GATEWAY_GUARANTEE_SCOPE,
        }

        if target.uses_gateway:
            return self._gateway_result(
                target,
                log,
                attempts,
                measurements,
                extra,
                premise,
                ledger_count,
                layer_requests,
            )
        if target.configuration is Configuration.DIRECT_NATIVE:
            return self._direct_native_result(
                target,
                log,
                attempts,
                measurements,
                extra,
                premise,
                ledger_count,
                layer_requests,
            )
        return self._direct_naive_result(
            target,
            log,
            attempts,
            measurements,
            extra,
            premise,
            ledger_count,
            layer_requests,
        )

    # -- what the first attempt actually did ------------------------------

    def _first_attempt_premise(
        self, executions_before_restart: int, fault_applied: bool
    ) -> str:
        """State the first attempt's outcome from the instruments, not the plan.

        Arming a fault is not evidence that it fired, and this scenario's whole
        point rests on the first attempt having *executed* before its response
        went missing. Every observation opens with whichever of these three
        sentences the effect ledger and the fault layer actually support.
        """
        if executions_before_restart >= 1 and fault_applied:
            return (
                "The tool executed and committed (effect ledger, independent: "
                f"{executions_before_restart} execution(s) before the restart) "
                "and the fault layer then withheld the response it had already "
                "produced"
            )
        if executions_before_restart >= 1:
            return (
                "The tool executed and committed (effect ledger, independent: "
                f"{executions_before_restart} execution(s) before the restart), "
                "but the armed response-loss fault was NOT recorded on the "
                "first attempt's crossing, so whatever the caller saw was not "
                "the injected failure"
            )
        return (
            "PREMISE NOT ESTABLISHED: the first attempt committed no downstream "
            "effect at all (effect ledger, independent: 0 executions before the "
            "restart), so this run did not reproduce the executed-but-"
            "unacknowledged call the scenario is about, and nothing below should "
            "be read as measuring it"
        )

    # -- which layer, if any, absorbed the replanned attempt --------------

    def _deduplicated_by(
        self,
        target: Target,
        ledger_count: int,
        executions_before_restart: int,
        replan_requests: int,
    ) -> str:
        """Name the layer that stopped the replan from becoming a second effect.

        A layer only earns the credit if there was a duplicate to stop: the
        first attempt must already have committed an effect, and the run must
        have ended with exactly that one effect. Otherwise -- two effects, or a
        single effect that the *replan* rather than the first attempt produced,
        or no effect at all -- nothing deduplicated anything, whatever the
        request counts look like.

        Given a real duplicate to stop, the fault layer says who stopped it: it
        sits between the caller (gateway or agent) and the tool, so a replan
        that produced no crossing never reached the tool and was absorbed in
        front of it, while a replan that did cross was absorbed inside it.
        """
        if ledger_count != 1 or executions_before_restart != 1:
            return DEDUPLICATED_BY_NOTHING
        if replan_requests == 0:
            # In front of the tool. With a gateway there, that is the gateway;
            # on the direct path there is no layer in front, so the replan
            # simply never left the client and nothing deduplicated it.
            return (
                DEDUPLICATED_BY_GATEWAY
                if target.uses_gateway
                else DEDUPLICATED_BY_NOTHING
            )
        return DEDUPLICATED_BY_DOWNSTREAM

    # -- per-configuration verdicts --------------------------------------

    def _direct_naive_result(
        self,
        target: Target,
        log: EventLog,
        attempts: list[AttemptOutcome],
        measurements: Measurements,
        extra: dict[str, Any],
        premise: str,
        ledger_count: int,
        layer_requests: int,
    ) -> ConfigurationResult:
        first, second = attempts
        duplicates = extra["duplicate_executions"]

        observation = (
            f"{premise}; the agent saw '{first.status}' "
            f"({first.client_visible_state}) and "
            f"died. A fresh incarnation replanned the same business operation "
            f"({extra['operation_id']}) and, having no memory of the old key, "
            f"presented a new one ({extra['first_idempotency_key']} -> "
            f"{extra['restart_idempotency_key']}); that attempt returned "
            f"'{second.status}' ({second.client_visible_state}). The independent "
            f"effect ledger recorded {ledger_count} downstream execution(s) for "
            f"this operation -- {duplicates} beyond the one the agent intended, "
            f"{extra['value_at_risk_minor_units']} minor units of unintended "
            f"refund -- and the fault layer counted {layer_requests} request(s) "
            f"reaching the tool ({extra['fault_layer_requests_after_restart']} "
            f"of them after the restart). This tool deduplicates on nothing: "
            f"not on the key, which changed, and not on the business operation "
            f"id, which did not. Deduplicated by: {extra['deduplicated_by']}. "
            f"Descriptive baseline, no guarantee to test."
        )
        risks = [
            "The agent had no way to distinguish a lost response from a lost "
            "request, and no memory across the restart that could have told it "
            "either way. Retrying duplicated a refund; abandoning might have "
            "left the customer unpaid.",
            "The duplicate is invisible until someone reads the tool's own "
            "ledger. There is no signed artifact here and no third party who "
            "could establish how many refunds this operation caused.",
        ]
        log.emit(
            "verdict",
            f"{target.configuration.value} -> OBSERVED ({ledger_count} executions)",
            scenario=self.test_id,
            configuration=target.configuration.value,
            downstream_executions=ledger_count,
            duplicate_executions=duplicates,
            deduplicated_by=extra["deduplicated_by"],
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
        premise: str,
        ledger_count: int,
        layer_requests: int,
    ) -> ConfigurationResult:
        first, second = attempts

        failures: list[str] = []
        if ledger_count > 1:
            failures.append(
                f"downstream executions (effect ledger) = {ledger_count}, want 1 "
                "-- the restarted agent's replan executed the refund again"
            )
        elif ledger_count != 1:
            failures.append(
                f"downstream executions (effect ledger) = {ledger_count}, want 1 "
                "-- the intended refund never executed, so this run does not "
                "establish the native baseline either way"
            )
        extra["verdict_failures"] = failures
        verdict = Verdict.PASS if not failures else Verdict.FAIL

        if extra["deduplicated_by"] == DEDUPLICATED_BY_DOWNSTREAM:
            absorption = (
                "The replanned request did reach the tool and was absorbed "
                "inside it: the new key was irrelevant, because this downstream "
                "keys its idempotency on the business operation_id, which "
                "survived the restart. A correctly built downstream handles an "
                "agent restart on its own, with no gateway present"
            )
        else:
            absorption = (
                "No layer absorbed a duplicate here -- the run did not end with "
                "the first attempt's single effect, so the native control was "
                "not what this run exercised"
            )

        observation = (
            f"{premise}; the agent saw '{first.status}' "
            f"({first.client_visible_state}) and "
            f"died. A fresh incarnation replanned the same business operation "
            f"({extra['operation_id']}) under a brand-new idempotency key "
            f"({extra['first_idempotency_key']} -> "
            f"{extra['restart_idempotency_key']}) and got '{second.status}' "
            f"({second.client_visible_state}). The independent effect ledger "
            f"recorded {ledger_count} downstream execution(s) and the fault "
            f"layer counted {layer_requests} request(s) reaching the tool, "
            f"{extra['fault_layer_requests_after_restart']} of them after the "
            f"restart. {absorption}. Deduplicated by: "
            f"{extra['deduplicated_by']}."
        )
        if failures:
            observation += (
                " The correct native baseline did NOT hold: "
                + "; ".join(failures)
                + "."
            )
        risks = [
            "The protection lives entirely in the tool and is keyed on a field "
            "the agent chose to send. An agent that regenerated its "
            "operation_id along with its key would duplicate here too; nothing "
            "outside the tool checks that.",
            "The deduplication is invisible from outside the tool: no signed, "
            "portable artifact records that a restart happened or that the "
            "second request was absorbed.",
            "The agent still never learned the outcome of its first call; it "
            "recovered only because it was willing to re-issue a consequential "
            "operation blind.",
        ]
        log.emit(
            "verdict",
            f"{target.configuration.value} -> {verdict.value} ({ledger_count} executions)",
            scenario=self.test_id,
            configuration=target.configuration.value,
            downstream_executions=ledger_count,
            restart_status=second.status,
            deduplicated_by=extra["deduplicated_by"],
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
        premise: str,
        ledger_count: int,
        layer_requests: int,
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
        attempt_states = (
            [a["state"] for a in snapshot.attempts] if snapshot is not None else []
        )
        records = snapshot.idempotency_records if snapshot is not None else []
        duplicate_debits = max(0, debits - 1) if debits is not None else None

        extra.update(
            {
                "gateway_dispatches": dispatches,
                "gateway_debits": debits,
                "gateway_refunds": refunds,
                "gateway_net_debits": net_debits,
                "gateway_sent_attempts": counters.gateway_sent_attempts,
                "gateway_receipts": receipt_count,
                "receipt_outcomes": outcomes,
                "dispatch_attempt_states": attempt_states,
                "duplicate_debits": duplicate_debits,
                "idempotency_record_count": len(records),
                "idempotency_records": records,
                "wallet_balance": (
                    snapshot.wallet_balance if snapshot is not None else None
                ),
                "distinct_receipt_ids": (
                    first.receipt_id is not None
                    and second.receipt_id is not None
                    and first.receipt_id != second.receipt_id
                ),
                "paid_twice": (debits > 1) if debits is not None else None,
                "executed_once": ledger_count == 1,
                "paid_once": (debits == 1) if debits is not None else None,
            }
        )

        failures: list[str] = []
        if ledger_count > 1:
            failures.append(
                f"downstream executions (effect ledger) = {ledger_count}, want 1 "
                "-- one intended refund executed more than once"
            )
        elif ledger_count != 1:
            failures.append(
                f"downstream executions (effect ledger) = {ledger_count}, want 1 "
                "-- the intended refund never executed, so this run does not "
                "establish the property either way"
            )
        if target.configuration is Configuration.GATEWAY_NATIVE:
            # The documented rule for this configuration gates on the debit as
            # well as the execution. An unreadable debit count is a failure to
            # establish that half, never a silently skipped assertion.
            if debits is None:
                failures.append(
                    "gateway debits (gateway-reported) could not be read, so "
                    "'paid for at most once' was not established"
                )
            elif debits != 1:
                failures.append(
                    f"gateway debits (gateway-reported) = {debits}, want 1 -- one "
                    "intended refund was paid for more than once"
                )
        extra["verdict_failures"] = failures
        verdict = Verdict.PASS if not failures else Verdict.FAIL

        # The property the CLAIM states, measured directly and independently of
        # whichever half this configuration's verdict rule happens to gate on.
        executed_once = ledger_count == 1
        paid_once = debits == 1 if debits is not None else None
        # True, False, or None. A run that duplicated either the effect or the
        # charge violated the property. A run that recorded one effect and one
        # debit upheld it. Anything else -- the refund never executed, so the
        # premise was not reproduced, or an instrument could not be read --
        # leaves the property UNESTABLISHED, which is neither "held" nor
        # "violated" and is never rendered as either.
        duplicated = ledger_count > 1 or (debits is not None and debits > 1)
        property_held: bool | None
        if duplicated:
            property_held = False
        elif executed_once and paid_once:
            property_held = True
        else:
            property_held = None
        extra["business_property_held"] = property_held

        dedup = extra["deduplicated_by"]
        replan_requests = extra["fault_layer_requests_after_restart"]
        downstream_clause = (
            "the downstream, whose native operation_id idempotency did not "
            "absorb it either"
            if target.configuration.native_idempotency
            else "the downstream, which has no idempotency of its own"
        )
        if dedup == DEDUPLICATED_BY_GATEWAY:
            layer_sentence = (
                "The gateway is what stopped the replanned attempt: it never "
                "reached the tool (no crossing after the restart), and the "
                f"caller got '{second.status}' "
                f"({second.client_visible_state}) back. Read that status before "
                "crediting replay protection -- a refusal also produces no "
                "crossing."
            )
        elif dedup == DEDUPLICATED_BY_DOWNSTREAM:
            layer_sentence = (
                "The layer that prevented the duplicate was THE DOWNSTREAM, NOT "
                "THE GATEWAY: the gateway treated the new key as a new operation "
                f"and dispatched again ({replan_requests} request(s) crossed "
                "into the tool after the restart), and the tool's own "
                "operation_id idempotency absorbed it. Remove the native control "
                "and the duplicate lands."
            )
        else:
            layer_sentence = (
                "NOTHING prevented a duplicate here: neither the gateway, which "
                f"saw a new key and therefore a new operation, nor "
                f"{downstream_clause}. {layer_requests} request(s) crossed into "
                f"the tool ({replan_requests} after the restart) and "
                f"{ledger_count} execution(s) committed "
                f"({extra['effect_ledger_executions_before_restart']} of them "
                "before the restart)."
            )

        property_word = (
            "held"
            if property_held is True
            else "NOT held"
            if property_held is False
            else "NOT ESTABLISHED"
        )
        if debits is None:
            charge_sentence = (
                "The gateway's own debit count could not be read from its "
                "snapshot, so the 'paid for at most once' half of the property "
                "is unestablished in this run."
            )
        else:
            charge_sentence = (
                "The gateway's replay guarantee is keyed on the idempotency key, "
                "and the key changed, so from the gateway's point of view these "
                f"are two operations: it recorded {debits} debit(s) for the one "
                f"refund the agent intended, {duplicate_debits} beyond it."
            )

        observation = (
            f"{premise}; "
            f"the first attempt came back '{first.status}' "
            f"({first.client_visible_state}) with receipt {first.receipt_id}. "
            f"The agent then died and a fresh incarnation replanned the SAME "
            f"business operation ({extra['operation_id']}) with a NEW "
            f"idempotency key ({extra['first_idempotency_key']} -> "
            f"{extra['restart_idempotency_key']}) through a rebuilt caller; "
            f"that attempt returned '{second.status}' "
            f"({second.client_visible_state}) with receipt "
            f"{second.receipt_id}. Independent instruments: the effect ledger "
            f"recorded {ledger_count} downstream execution(s) and the fault "
            f"layer counted {layer_requests} request(s) crossing into the tool. "
            f"Gateway-reported: {counters.gateway_sent_attempts} attempt(s) past "
            f"the send boundary in state(s) {attempt_states or ['none']}, "
            f"{debits} debit(s) and {refunds} refund(s) for a net {net_debits}, "
            f"{len(records)} idempotency record(s), and {receipt_count} "
            f"receipt(s) recording {outcomes or ['none']}. Those last figures "
            f"are the gateway's account of itself, not an independent one. "
            f"{layer_sentence} Deduplicated by: {dedup}. {charge_sentence} "
            f"The business-level property under test -- one intended refund, "
            f"executed at most once and paid for at most once -- is therefore "
            f"{property_word} here "
            f"(executed once: {executed_once}; paid once: {paid_once})."
        )
        if failures:
            observation += " Verdict rule not met: " + "; ".join(failures) + "."
        if verdict is Verdict.PASS and property_held is not True:
            observation += (
                " NOTE: this configuration's documented verdict rule gates only "
                "on downstream executions, so the verdict reads PASS while the "
                f"business-level property above was {property_word}. The verdict "
                "is the narrower statement; the claim is the wider one."
            )
        if property_held is True:
            observation += (
                " Scope note: no business-level gap was observed in this run. "
                "The guarantee the product states is per idempotency key, not "
                "per business operation, so a run that holds here holds by more "
                "than that guarantee promises."
            )
        elif property_held is False:
            observation += (
                " Scope note: this gap is outside the guarantee the product "
                "states, which is per idempotency key, not per business "
                "operation. It is reported as a real business-level gap and as "
                "an out-of-scope one at the same time; neither half should be "
                "dropped."
            )
        else:
            observation += (
                " Scope note: this run says nothing either way about the "
                "business-level property -- the refund did not execute once and "
                "get charged once, so there was no duplicate to prevent and no "
                "clean pass to record. The guarantee the product states is in "
                "any case per idempotency key, not per business operation."
            )

        risks = [
            "The gateway has no notion of the business operation. Nothing in "
            "the permit, the receipt or the idempotency record ties the two "
            "attempts together, so no gateway-side report would show this as "
            "one intended refund.",
            "Budget and rate limits are consumed per key as well. A restart "
            "loop spends the permit's credits on repeats of a single intent, "
            "and each repeat is individually within policy.",
            "Each attempt produced its own signed receipt. The receipts are "
            "individually truthful about the gateway's own dispatch and debit "
            "and collectively silent about the fact that they describe the same "
            "business action.",
        ]
        if dedup == DEDUPLICATED_BY_DOWNSTREAM:
            risks.append(
                "The single execution recorded here is a property of the "
                "downstream tool, not of the gateway. A buyer reading this row "
                "as evidence that the gateway prevents duplicate refunds would "
                "be reading it wrong."
            )
        if debits is not None and debits > 1:
            risks.append(
                "The charge for the second attempt is retained: the first "
                "attempt's delivery was uncertain, so the gateway does not "
                "refund it, and the second was a fully successful governed call "
                "in its own right."
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
            duplicate_debits=duplicate_debits,
            receipts=receipt_count,
            receipt_outcomes=outcomes,
            deduplicated_by=dedup,
            business_property_held=property_held,
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
