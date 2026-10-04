"""Scenario framework: workloads, measurements, verdicts.

A :class:`Scenario` runs one workload with one injected failure against each
of its configurations and returns a :class:`ScenarioResult`. Verdicts are
computed against the **documented** expectation for that configuration:

* ``PASS``  -- the documented guarantee held.
* ``FAIL``  -- the documented guarantee did not hold, or a protection the
  configuration is sold as providing was not observed. A FAIL is reported,
  never softened, and the PRD names scenarios that are expected to FAIL today.
* ``OBSERVED`` -- descriptive only. The naive baseline has no guarantee to
  pass or fail; its numbers are the point.
* ``NOT_APPLICABLE`` -- the configuration has no component the scenario
  exercises (there is no gateway budget in a direct integration).
* ``NOT_RUN`` -- with a stated reason.

Every scenario also declares ``expected`` verdicts. The pytest tier asserts
observed == expected, so a regression *and* an unexpected improvement both
surface: an improvement means the documentation and the expectation must be
updated together, not silently.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import statistics
import time
import traceback
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from failure_lab import TEST_DEFINITION_VERSION
from failure_lab.configurations import (
    ALL_CONFIGURATIONS,
    CONFIGURATION_LABELS,
    AttemptOutcome,
    Configuration,
    LabEnvironment,
    Target,
    configured_target,
)
from failure_lab.identity import OperationIdentity, new_business_operation_id
from failure_lab.refund_tool import RefundRequest


class Verdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    OBSERVED = "OBSERVED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NOT_RUN = "NOT_RUN"
    ERROR = "ERROR"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class EventLog:
    """Append-only, ordered record of what the harness did and saw."""

    events: list[dict[str, Any]] = field(default_factory=list)
    _sequence: int = 0

    def emit(
        self,
        step: str,
        message: str,
        *,
        scenario: str = "",
        configuration: str = "",
        **data: Any,
    ) -> dict[str, Any]:
        self._sequence += 1
        event = {
            "sequence": self._sequence,
            "at": _now(),
            "scenario": scenario,
            "configuration": configuration,
            "step": step,
            "message": message,
            "data": _jsonable(data),
        }
        self.events.append(event)
        return event


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "as_dict"):
        return _jsonable(value.as_dict())
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    rank = fraction * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return round(ordered[low] * (1 - weight) + ordered[high] * weight, 3)


#: Statuses that mean the request was refused rather than admitted.
REFUSED_STATUSES = frozenset(
    {
        "rejected",
        "conflict",
        "key_conflict",
        "denied",
        "invalid_params",
        "insufficient_funds",
    }
)


@dataclass
class Counters:
    """The measurement set the PRD asks for, per configuration.

    Independently observed: ``downstream_executions`` (effect ledger),
    ``downstream_requests`` and ``gateway_dispatches`` (fault layer).
    Gateway-reported: ``gateway_sent_attempts``, ``gateway_debits``,
    ``gateway_refunds``, ``receipts``.
    """

    incoming_requests: int = 0
    accepted_requests: int = 0
    refused_requests: int = 0
    downstream_requests: int = 0
    downstream_executions: int = 0
    gateway_dispatches: int | None = None
    gateway_sent_attempts: int | None = None
    gateway_debits: int | None = None
    gateway_refunds: int | None = None
    gateway_net_debits: int | None = None
    receipts: int | None = None
    known_final_outcome: int = 0
    explicit_uncertain: int = 0
    unresolved: int = 0
    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Measurements:
    """Everything one configuration's run observed, from every instrument."""

    counters: Counters
    gateway: dict[str, Any] | None
    snapshot: Any
    effects: list[dict[str, Any]]
    crossings: list[dict[str, Any]]
    receipts: list[dict[str, Any]]

    @property
    def downstream_executions(self) -> int:
        return self.counters.downstream_executions

    @property
    def dispatches(self) -> int:
        return self.counters.downstream_requests

    def receipt_outcomes(self) -> list[str]:
        return [r["outcome"] for r in self.receipts]


@dataclass
class ConfigurationResult:
    configuration: str
    label: str
    verdict: Verdict
    expectation: str
    observation: str
    counters: Counters
    attempts: list[dict[str, Any]] = field(default_factory=list)
    downstream_effects: list[dict[str, Any]] = field(default_factory=list)
    crossings: list[dict[str, Any]] = field(default_factory=list)
    gateway: dict[str, Any] | None = None
    receipts: list[dict[str, Any]] = field(default_factory=list)
    remaining_risks: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "configuration": self.configuration,
            "label": self.label,
            "verdict": self.verdict.value,
            "expectation": self.expectation,
            "observation": self.observation,
            "counters": self.counters.as_dict(),
            "attempts": self.attempts,
            "downstream_effects": self.downstream_effects,
            "crossings": self.crossings,
            "gateway": self.gateway,
            "receipts": self.receipts,
            "remaining_risks": self.remaining_risks,
            "extra": _jsonable(self.extra),
            "error": self.error,
        }


@dataclass
class ScenarioResult:
    test_id: str
    title: str
    claim: str
    definition_version: str
    definition_hash: str
    started_at: str
    finished_at: str
    configurations: list[ConfigurationResult]
    limitations: list[str]
    events: list[dict[str, Any]]
    expected: dict[str, str]

    @property
    def verdict(self) -> Verdict:
        """The scenario-level verdict: the worst gateway-side verdict."""
        verdicts = [c.verdict for c in self.configurations]
        if Verdict.ERROR in verdicts:
            return Verdict.ERROR
        if Verdict.FAIL in verdicts:
            return Verdict.FAIL
        if Verdict.PASS in verdicts:
            return Verdict.PASS
        if Verdict.OBSERVED in verdicts:
            return Verdict.OBSERVED
        if Verdict.NOT_RUN in verdicts:
            return Verdict.NOT_RUN
        return Verdict.NOT_APPLICABLE

    @property
    def matches_expectation(self) -> bool:
        observed = {c.configuration: c.verdict.value for c in self.configurations}
        return all(
            observed.get(cfg) == verdict for cfg, verdict in self.expected.items()
        )

    def mismatches(self) -> list[tuple[str, str, str]]:
        observed = {c.configuration: c.verdict.value for c in self.configurations}
        return [
            (cfg, verdict, observed.get(cfg, "missing"))
            for cfg, verdict in self.expected.items()
            if observed.get(cfg) != verdict
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "test_id": self.test_id,
            "title": self.title,
            "claim": self.claim,
            "definition_version": self.definition_version,
            "definition_hash": self.definition_hash,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "verdict": self.verdict.value,
            "expected": self.expected,
            "matches_expectation": self.matches_expectation,
            "configurations": [c.as_dict() for c in self.configurations],
            "limitations": self.limitations,
            "events": self.events,
        }


class Scenario:
    """Base class. Subclasses implement :meth:`run_configuration`."""

    test_id: str = ""
    title: str = ""
    claim: str = ""
    tier: str = "fast"
    configurations: tuple[Configuration, ...] = ALL_CONFIGURATIONS
    #: Documented expectation per configuration value.
    expected: dict[str, str] = {}
    limitations: tuple[str, ...] = ()
    #: Configurations the scenario does not exercise get this verdict.
    inapplicable_reason: str = (
        "no gateway component to exercise in a direct integration"
    )

    def __init__(self, **options: Any) -> None:
        self.options = options

    # -- to implement ----------------------------------------------------

    async def run_configuration(
        self, target: Target, log: EventLog
    ) -> ConfigurationResult:
        raise NotImplementedError

    # -- orchestration ---------------------------------------------------

    def definition(self) -> dict[str, Any]:
        source = inspect.getsource(type(self))
        return {
            "test_id": self.test_id,
            "title": self.title,
            "claim": self.claim,
            "tier": self.tier,
            "definition_version": TEST_DEFINITION_VERSION,
            "configurations": [c.value for c in self.configurations],
            "expected": dict(self.expected),
            "limitations": list(self.limitations),
            "options": _jsonable(self.options),
            "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        }

    def definition_hash(self) -> str:
        canonical = json.dumps(self.definition(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    async def run(
        self,
        env: LabEnvironment,
        *,
        configurations: Iterable[Configuration] | None = None,
        log: EventLog | None = None,
    ) -> ScenarioResult:
        log = log or EventLog()
        started = _now()
        selected = (
            tuple(configurations) if configurations is not None else ALL_CONFIGURATIONS
        )
        results: list[ConfigurationResult] = []
        for configuration in selected:
            if configuration not in self.configurations:
                results.append(
                    self.not_applicable(configuration, self.inapplicable_reason)
                )
                continue
            log.emit(
                "configuration.start",
                f"{self.test_id}: starting {configuration.value}",
                scenario=self.test_id,
                configuration=configuration.value,
            )
            try:
                async with configured_target(
                    env, configuration, ledger_suffix=f"-{self.test_id}"
                ) as target:
                    result = await self.run_configuration(target, log)
            except Exception as exc:  # noqa: BLE001 - a harness error is itself a result
                result = ConfigurationResult(
                    configuration=configuration.value,
                    label=CONFIGURATION_LABELS[configuration],
                    verdict=Verdict.ERROR,
                    expectation=self.expected.get(configuration.value, ""),
                    observation=f"harness error: {type(exc).__name__}: {exc}",
                    counters=Counters(),
                    error="".join(traceback.format_exception(exc))[-4000:],
                )
            log.emit(
                "configuration.finish",
                f"{self.test_id}: {configuration.value} -> {result.verdict.value}",
                scenario=self.test_id,
                configuration=configuration.value,
                observation=result.observation,
            )
            results.append(result)
        return ScenarioResult(
            test_id=self.test_id,
            title=self.title,
            claim=self.claim,
            definition_version=TEST_DEFINITION_VERSION,
            definition_hash=self.definition_hash(),
            started_at=started,
            finished_at=_now(),
            configurations=results,
            limitations=list(self.limitations),
            events=[e for e in log.events if e["scenario"] == self.test_id],
            expected=dict(self.expected),
        )

    # -- helpers for subclasses ------------------------------------------

    def not_applicable(
        self, configuration: Configuration, reason: str
    ) -> ConfigurationResult:
        return ConfigurationResult(
            configuration=configuration.value,
            label=CONFIGURATION_LABELS[configuration],
            verdict=Verdict.NOT_APPLICABLE,
            expectation=self.expected.get(configuration.value, ""),
            observation=reason,
            counters=Counters(),
        )

    def refund(
        self, payment_id: str, *, amount: int = 5000, customer_id: str = "cus_lab"
    ) -> tuple[str, RefundRequest]:
        operation_id = new_business_operation_id(payment_id)
        return operation_id, RefundRequest(
            operation_id=operation_id,
            customer_id=customer_id,
            payment_id=payment_id,
            amount=amount,
        )

    async def measure(
        self,
        target: Target,
        attempts: Sequence[AttemptOutcome],
        *,
        operation_ids: Sequence[str] | None = None,
    ) -> Measurements:
        """Build the PRD counter set from the independent instruments.

        ``downstream_executions`` comes from the effect ledger, which the
        gateway cannot reach. ``downstream_requests``/``gateway_dispatches``
        come from the fault layer, which sits outside the gateway. Only the
        debit, receipt and attempt columns are gateway-reported, and they are
        labelled as such wherever they are rendered.
        """
        wanted = set(operation_ids) if operation_ids is not None else None
        effects = [
            e
            for e in target.ledger.effects()
            if wanted is None or e.operation_id in wanted
        ]
        crossings = [
            c
            for c in target.injector.crossings()
            if wanted is None or c.operation_id in wanted
        ]
        latencies = [a.latency_ms for a in attempts]
        counters = Counters(
            incoming_requests=len(attempts),
            accepted_requests=sum(
                1 for a in attempts if a.status not in REFUSED_STATUSES
            ),
            refused_requests=sum(1 for a in attempts if a.status in REFUSED_STATUSES),
            downstream_requests=len(crossings),
            downstream_executions=len(effects),
            known_final_outcome=sum(1 for a in attempts if a.knows_outcome),
            explicit_uncertain=sum(
                1 for a in attempts if a.client_visible_state == "explicit_uncertain"
            ),
            unresolved=sum(
                1 for a in attempts if a.client_visible_state == "no_information"
            ),
            latency_p50_ms=percentile(latencies, 0.5),
            latency_p95_ms=percentile(latencies, 0.95),
        )
        snapshot = None
        gateway_view: dict[str, Any] | None = None
        receipts: list[dict[str, Any]] = []
        if target.gateway is not None and target.tenant is not None:
            snapshot = await target.gateway.snapshot(target.tenant)
            counters.gateway_dispatches = len(crossings)
            counters.gateway_sent_attempts = snapshot.sent_attempt_count
            counters.gateway_debits = snapshot.debit_count
            counters.gateway_refunds = snapshot.refund_count
            counters.gateway_net_debits = snapshot.net_debit_count
            counters.receipts = snapshot.receipt_count
            gateway_view = snapshot.as_dict()
            receipts = list(snapshot.receipts)
        return Measurements(
            counters=counters,
            gateway=gateway_view,
            snapshot=snapshot,
            effects=[e.as_dict() for e in effects],
            crossings=[c.as_dict() for c in crossings],
            receipts=receipts,
        )

    def result(
        self,
        target: Target,
        *,
        verdict: Verdict,
        observation: str,
        measurements: Measurements,
        attempts: Sequence[AttemptOutcome] = (),
        remaining_risks: Sequence[str] = (),
        extra: dict[str, Any] | None = None,
    ) -> ConfigurationResult:
        return ConfigurationResult(
            configuration=target.configuration.value,
            label=CONFIGURATION_LABELS[target.configuration],
            verdict=verdict,
            expectation=self.expected.get(target.configuration.value, ""),
            observation=observation,
            counters=measurements.counters,
            attempts=[a.as_dict() for a in attempts],
            downstream_effects=measurements.effects,
            crossings=measurements.crossings,
            gateway=measurements.gateway,
            receipts=measurements.receipts,
            remaining_risks=list(remaining_risks),
            extra=extra or {},
        )


@dataclass(frozen=True)
class Timing:
    started: float

    @classmethod
    def start(cls) -> Timing:
        return cls(time.perf_counter())

    @property
    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.started) * 1000


def mean_or_none(values: Sequence[float]) -> float | None:
    return round(statistics.fmean(values), 3) if values else None


__all__ = [
    "ConfigurationResult",
    "Counters",
    "Measurements",
    "REFUSED_STATUSES",
    "EventLog",
    "OperationIdentity",
    "Scenario",
    "ScenarioResult",
    "Timing",
    "Verdict",
    "mean_or_none",
    "percentile",
]
