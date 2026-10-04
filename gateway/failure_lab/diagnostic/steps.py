"""Turning the harness's own event log into the line a visitor reads.

The PRD gives six lines for the lost-response experiment -- "Sending business
operation...", "Downstream executed operation.", "Response intentionally
removed.", "Client observed timeout.", "Retrying...", "Measuring downstream
effects..." -- and the obvious way to produce them is to print them on a timer
while a run happens somewhere else. That would be a screensaver. Every line
this module emits is derived from one
:class:`~failure_lab.scenarios.base.EventLog` entry that the scenario actually
wrote, and carries the instrument reading that entry recorded.

Three consequences of that choice, all of them constraints rather than
features:

**A line only appears when its evidence does.** "Downstream executed
operation." is emitted when the event carries a non-zero execution count read
from the effect ledger -- the file the gateway cannot reach. If the count is
absent the line is not printed, because the alternative is narrating an
execution nobody counted.

**"Response intentionally removed." needs the fault to have fired, not to have
been armed.** Arming is a separate line. The removal is claimed only when the
fault layer recorded a request crossing into the tool, the ledger recorded the
effect, and the caller still came away without a confirmed outcome. Those three
facts together are what "executed, then the answer vanished" means.

**An unrecognised step is printed, not dropped.** The scenario modules belong
to other people and their step vocabulary will change. A step this module does
not know renders the event's own message, so a new step degrades to a plain
line instead of a silent gap in the middle of somebody's experiment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from failure_lab.configurations import Configuration
from failure_lab.evidence import redact

#: The PRD's vocabulary. Referenced by name everywhere below so the wording is
#: changed in one place and never drifts between two branches.
SENDING = "Sending business operation..."
EXECUTED = "Downstream executed operation."
RESPONSE_REMOVED = "Response intentionally removed."
CLIENT_TIMEOUT = "Client observed timeout."
RETRYING = "Retrying..."
MEASURING = "Measuring downstream effects..."

#: Phases the page groups steps under. The baseline runs first because
#: :meth:`Scenario.run` iterates the configurations in declaration order, which
#: puts the two direct configurations ahead of the two governed ones.
PHASE_SETUP = "setup"
PHASE_BASELINE = "baseline"
PHASE_PROTECTED = "protected"

PHASE_LABELS: dict[str, str] = {
    PHASE_SETUP: "Setting up",
    PHASE_BASELINE: "Baseline — no gateway",
    PHASE_PROTECTED: "Protected — same workload, same injected failure",
}

#: Fields of an event's ``data`` a step may carry to the page. An allowlist,
#: because ``data`` is whatever a scenario author put there and this surface is
#: served to a browser. Everything here is a count, a short status word or a
#: boolean; nothing here is free text from a downstream.
EVIDENCE_FIELDS: tuple[str, ...] = (
    "status",
    "client_visible_state",
    "http_status",
    "executions_so_far",
    "downstream_requests_so_far",
    "downstream_executions",
    "downstream_requests",
    # The fault layer counts two different things and they are not the same
    # number. See :meth:`StepStream._measurement`.
    "downstream_requests_reaching_tool",
    "gateway_dispatches",
    "gateway_debits",
    "gateway_refunds",
    "gateway_net_debits",
    "gateway_receipts",
    "gateway_sent_attempts",
    "same_key_as_first",
    "latency_ms",
    "hold_seconds",
    "client_timeout_seconds",
    "verdict",
)

#: Client-visible states in which the caller does not know what happened.
_NO_CONFIRMED_OUTCOME = frozenset({"no_information", "explicit_uncertain"})


@dataclass(frozen=True)
class Step:
    """One line of the live stream, and what it was derived from."""

    index: int
    at: str
    phase: str
    configuration: str
    label: str
    scenario: str
    #: The visitor-facing sentence.
    text: str
    #: The harness's own message for the event this line came from. Shown
    #: underneath, so a reader can always see the raw record behind the prose.
    detail: str
    #: The event's step name, unchanged. This is how somebody finds the line in
    #: the evidence bundle's event log.
    source_step: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def phase_for(configuration: str) -> str:
    """Which half of the experiment a configuration belongs to."""
    if not configuration:
        return PHASE_SETUP
    try:
        return (
            PHASE_PROTECTED
            if Configuration(configuration).uses_gateway
            else PHASE_BASELINE
        )
    except ValueError:
        return PHASE_SETUP


def label_for(configuration: str) -> str:
    try:
        return Configuration(configuration).label
    except ValueError:
        return configuration or "harness"


def _evidence(data: dict[str, Any]) -> dict[str, Any]:
    return {
        name: data[name]
        for name in EVIDENCE_FIELDS
        if name in data and data[name] is not None
    }


def _count(data: dict[str, Any], name: str) -> int:
    value = data.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


@dataclass
class _ConfigurationState:
    """What this translator knows about one scenario's configuration, so far.

    Only two facts are kept, and both are needed to decide whether a line is
    entitled to claim the injected failure fired: whether a fault was armed for
    this configuration at all, and how many effects the ledger had already
    recorded when the previous line was written.

    Scoped to one *scenario's* run of a configuration, not to the
    configuration across a whole run. Every scenario gets its own effect
    ledger, so ``executions_so_far`` starts again from zero for each one:
    keyed by configuration alone, the second scenario's executions never
    exceeded the first scenario's high-water mark and its "Downstream executed
    operation." line vanished. ``fault_armed`` leaked the same way and in the
    worse direction, letting a scenario that armed nothing claim the response
    had been removed on purpose.
    """

    fault_armed: bool = False
    executions_seen: int = 0


class StepStream:
    """Stateful translator from event log entries to visitor-facing steps.

    Stateful because two of the six lines are conditional on something an
    earlier event established -- a fault was armed for this configuration --
    and a translator that re-derived that from each event in isolation would
    have to guess.
    """

    def __init__(self) -> None:
        self._next_index = 0
        self._configurations: dict[tuple[str, str], _ConfigurationState] = {}

    def _state(self, scenario: str, configuration: str) -> _ConfigurationState:
        """State for one scenario's run of one configuration.

        The scenario is part of the key. See :class:`_ConfigurationState` for
        what went wrong when it was not.
        """
        return self._configurations.setdefault(
            (scenario, configuration), _ConfigurationState()
        )

    def push(self, event: dict[str, Any]) -> list[Step]:
        """Translate one event. Returns zero or more lines, in order."""
        safe = redact(event)
        step_name = str(safe.get("step", ""))
        message = str(safe.get("message", ""))
        configuration = str(safe.get("configuration", ""))
        scenario = str(safe.get("scenario", ""))
        at = str(safe.get("at", ""))
        data = safe.get("data")
        data = data if isinstance(data, dict) else {}
        state = self._state(scenario, configuration)

        texts = self._texts(step_name, message, data, state)
        evidence = _evidence(data)
        phase = phase_for(configuration)
        label = label_for(configuration)
        steps: list[Step] = []
        for text in texts:
            self._next_index += 1
            steps.append(
                Step(
                    index=self._next_index,
                    at=at,
                    phase=phase,
                    configuration=configuration,
                    label=label,
                    scenario=scenario,
                    text=text,
                    detail=message,
                    source_step=step_name,
                    evidence=evidence,
                )
            )
        return steps

    # -- the translation table -------------------------------------------

    def _texts(
        self,
        step_name: str,
        message: str,
        data: dict[str, Any],
        state: _ConfigurationState,
    ) -> list[str]:
        if step_name == "configuration.start":
            return ["Starting this configuration."]
        if step_name == "configuration.finish":
            return [message or "Configuration finished."]
        if step_name.startswith("fault.") or step_name.endswith(".arm"):
            state.fault_armed = True
            return [
                "Arming the injected failure: the tool will execute and commit, "
                "and its answer will be discarded on the way back."
            ]
        if step_name.startswith("attempt.first") or step_name in (
            "t09.submit",
            "t12.call",
        ):
            return [SENDING, *self._observed(data, state)]
        if step_name.startswith("attempt.retry") or step_name.endswith(".retry"):
            return [RETRYING, *self._observed(data, state)]
        if step_name.startswith("measure."):
            return [MEASURING, *self._measurement(data)]
        if step_name == "verdict" or step_name.endswith(".verdict"):
            return [message or "Verdict recorded."]
        return [message] if message else []

    def _observed(self, data: dict[str, Any], state: _ConfigurationState) -> list[str]:
        """What the instruments recorded for one attempt, in the PRD's order.

        The caller supplies the verb -- "Sending..." or "Retrying..." -- and
        this supplies only what was measured, so the two attempt kinds cannot
        drift apart in what they are allowed to claim.
        """
        lines: list[str] = []

        executions = _count(data, "executions_so_far")
        crossings = _count(data, "downstream_requests_so_far")
        status = str(data.get("status", ""))
        visible = str(data.get("client_visible_state", ""))
        confirmed = visible not in _NO_CONFIRMED_OUTCOME and status != "timeout"

        if executions > state.executions_seen:
            new = executions - state.executions_seen
            state.executions_seen = executions
            lines.append(
                f"{EXECUTED} The downstream tool's own ledger — which the "
                f"gateway cannot read or write — recorded {new} execution(s), "
                f"{executions} in total for this operation."
            )

        # The removal is claimed only when all three instruments agree: a
        # request crossed the fault layer, the effect landed in the ledger, and
        # the caller still has no confirmed outcome.
        if state.fault_armed and executions >= 1 and crossings >= 1 and not confirmed:
            lines.append(
                RESPONSE_REMOVED
                + " The request reached the tool and the effect committed; the "
                "response was dropped on the way back, by this harness, on purpose."
            )

        if status == "timeout" or visible == "no_information":
            lines.append(
                CLIENT_TIMEOUT
                + " The caller came away with no information about whether the "
                "operation happened."
            )
        elif visible == "explicit_uncertain":
            lines.append(
                "Client observed an explicit uncertain state — not a confirmed "
                "outcome, but a state it can route on."
            )
        elif status:
            lines.append(f"Client observed: {status}.")

        return lines

    def _measurement(self, data: dict[str, Any]) -> list[str]:
        """What the two independent instruments counted, named as themselves.

        ``downstream_requests`` is
        :meth:`~failure_lab.faults.FaultInjector.dispatch_count`, which is
        every execution request that reached the *fault layer* -- including
        the ones the layer refused before the tool ever saw them, which is
        precisely what ``initialize_failure``,
        ``http_error_before_execution``, ``connection_failure_before_execution``
        and a crashed downstream all record. Printing it as requests that
        "crossed into the tool" overstates it in the direction that makes the
        injected failure look more thorough than it was. The layer records
        ``reached_tool`` per crossing for exactly this distinction, so both
        numbers are shown when the scenario reports both.
        """
        executions = data.get("downstream_executions")
        requests = data.get("downstream_requests")
        reached = data.get("downstream_requests_reaching_tool")
        if executions is None and requests is None:
            return []
        parts: list[str] = []
        if executions is not None:
            parts.append(f"{executions} downstream execution(s) in the effect ledger")
        if requests is not None:
            observed = f"{requests} execution request(s) observed at the fault layer"
            if reached is not None:
                observed += f", {reached} of which reached the tool"
            parts.append(observed)
        return ["Measured: " + "; ".join(parts) + "."]


def translate_all(events: list[dict[str, Any]]) -> list[Step]:
    """Translate a whole event log at once. Used for replay, not streaming."""
    stream = StepStream()
    steps: list[Step] = []
    for event in events:
        steps.extend(stream.push(event))
    return steps


__all__ = [
    "CLIENT_TIMEOUT",
    "EVIDENCE_FIELDS",
    "EXECUTED",
    "MEASURING",
    "PHASE_BASELINE",
    "PHASE_LABELS",
    "PHASE_PROTECTED",
    "PHASE_SETUP",
    "RESPONSE_REMOVED",
    "RETRYING",
    "SENDING",
    "Step",
    "StepStream",
    "label_for",
    "phase_for",
    "translate_all",
]
