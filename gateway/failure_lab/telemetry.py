"""Product analytics that cannot flatter the product.

This module measures whether the diagnostic converts. It is written on the
assumption that most of the traffic through the Failure Lab will be the
authors, their CI, and agents testing the thing -- so the interesting risk is
not privacy theatre, it is a conversion number inflated by its own authors.

Four properties, each enforced by the code rather than by a policy document:

* **Traffic source is a required field with no default.** There is no way to
  emit an event without saying where it came from, and
  :func:`detect_traffic_source` can return ``ci_run``, ``ai_test_agent`` or
  ``unknown`` but never ``human_customer``. A process cannot accidentally
  label itself a customer; a human has to type it.
* **The funnel only ever sees human events.** :func:`funnel` filters before it
  counts, and reports what it dropped and why. Synthetic traffic is not
  "excluded by convention" -- it never reaches the numerator or the
  denominator.
* **Property names are an allowlist.** A denylist is a promise to have thought
  of every field name a future engineer will invent. An allowlist fails closed
  on the one that was not thought of, which is exactly the field that leaks.
  Values are checked for credential, payment and payload shapes on top of
  that.
* **A local JSONL file under the run directory is the only default sink.**
  Sending anything off the machine takes two explicit, non-default arguments,
  one on the sink and one on the client.

:func:`disclosure_text` is generated from the same constants the collector
enforces, so the sentence shown to a person and the behaviour of the code
cannot drift apart.

This module deliberately imports nothing from the rest of the lab: analytics
about the diagnostic must not be able to break the diagnostic, and it must
stay importable while the scenario suite is being edited.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

TELEMETRY_SCHEMA_VERSION = "failure-lab-telemetry/1"

#: The default sink's filename inside the run directory.
DEFAULT_SINK_FILENAME = "telemetry.jsonl"


class TrafficSource(str, Enum):
    """Who generated the event. Required on every event, never defaulted."""

    #: A person evaluating the product for themselves. The only source that
    #: can appear in a conversion number.
    HUMAN_CUSTOMER = "human_customer"
    #: The authors exercising their own product.
    INTERNAL_TEST = "internal_test"
    #: An agent driving the surface, including agents doing customer-shaped
    #: work. Plausible traffic, but not a customer decision.
    AI_TEST_AGENT = "ai_test_agent"
    #: An automated pipeline.
    CI_RUN = "ci_run"
    #: Genuinely not known. Counted as non-human, because guessing in the
    #: flattering direction is how these numbers rot.
    UNKNOWN = "unknown"


#: The single source a conversion number is allowed to be computed from.
CONVERSION_SOURCES: frozenset[TrafficSource] = frozenset({TrafficSource.HUMAN_CUSTOMER})


def counts_toward_conversion(source: TrafficSource | str) -> bool:
    """True only for ``human_customer``.

    Accepts the enum or its value. An unrecognised string is not a customer --
    a typo must never be read as a sale.
    """
    try:
        resolved = TrafficSource(source)
    except ValueError:
        return False
    return resolved in CONVERSION_SOURCES


def detect_traffic_source(environ: Mapping[str, str] | None = None) -> TrafficSource:
    """Classify the current process, with ``human_customer`` unreachable.

    A CI runner and an agent harness both announce themselves in the
    environment. Nothing in an environment can establish that a person is
    sitting there, so the best this returns is ``unknown`` -- and the product
    surface has to pass ``human_customer`` explicitly for it to appear.
    """
    env = os.environ if environ is None else environ
    if any(
        env.get(name) for name in ("CI", "GITHUB_ACTIONS", "BUILDKITE", "JENKINS_URL")
    ):
        return TrafficSource.CI_RUN
    if any(
        env.get(name)
        for name in (
            "CLAUDE_CODE_SESSION",
            "CLAUDECODE",
            "ANTHROPIC_AGENT",
            "OPENAI_AGENT",
            "AGENT_HARNESS",
        )
    ):
        return TrafficSource.AI_TEST_AGENT
    if env.get("FAILURE_LAB_INTERNAL_TEST"):
        return TrafficSource.INTERNAL_TEST
    return TrafficSource.UNKNOWN


class EventName(str, Enum):
    """Every event the product is allowed to record."""

    DIAGNOSTIC_STARTED = "diagnostic_started"
    DIAGNOSTIC_COMPLETED = "diagnostic_completed"
    BASELINE_FAILED = "baseline_failed"
    BASELINE_PASSED = "baseline_passed"
    GATEWAY_COMPARISON_STARTED = "gateway_comparison_started"
    GATEWAY_COMPARISON_COMPLETED = "gateway_comparison_completed"
    REPORT_VIEWED = "report_viewed"
    INTEGRATION_STARTED = "integration_started"
    INTEGRATION_COMPLETED = "integration_completed"
    PRICING_VIEWED = "pricing_viewed"
    PAID_ACTIVATION_STARTED = "paid_activation_started"
    PAID_ACTIVATION_COMPLETED = "paid_activation_completed"
    #: Not in the PRD's event list, but the PRD's funnel ends at "continued
    #: usage" and a funnel stage with no event behind it is a stage that can
    #: never be measured.
    CONTINUED_USAGE = "continued_usage"


EVENT_DESCRIPTIONS: dict[EventName, str] = {
    EventName.DIAGNOSTIC_STARTED: "A diagnostic run was started.",
    EventName.DIAGNOSTIC_COMPLETED: "A diagnostic run finished and produced a report.",
    EventName.BASELINE_FAILED: "The caller's own integration did not survive the injected failure.",
    EventName.BASELINE_PASSED: "The caller's own integration survived the injected failure.",
    EventName.GATEWAY_COMPARISON_STARTED: "The same workload was started against the gateway.",
    EventName.GATEWAY_COMPARISON_COMPLETED: "The gateway comparison finished.",
    EventName.REPORT_VIEWED: "A run report was opened.",
    EventName.INTEGRATION_STARTED: "The caller began integrating the gateway.",
    EventName.INTEGRATION_COMPLETED: "The caller finished integrating the gateway.",
    EventName.PRICING_VIEWED: "Pricing was opened.",
    EventName.PAID_ACTIVATION_STARTED: "A paid activation was begun.",
    EventName.PAID_ACTIVATION_COMPLETED: "A paid activation completed.",
    EventName.CONTINUED_USAGE: "A governed call was made after the activation day.",
}

#: Property names that may be attached to an event, each with the one line
#: :func:`disclosure_text` shows for it. An allowlist, not a denylist: a name
#: that is not here is refused, which is the correct behaviour for the field
#: nobody anticipated.
ALLOWED_PROPERTIES: dict[str, str] = {
    "test_id": "the scenario identifier, e.g. T03",
    "scenario_count": "how many scenarios ran",
    "tier": "fast or slow",
    "configuration": "which of the four configurations",
    "verdict": "PASS / FAIL / OBSERVED / NOT_APPLICABLE / NOT_RUN / ERROR",
    "conclusion_kind": "which comparison conclusion the run reached",
    "matches_expectation": "whether the run matched its documented expectation",
    "duplicate_effects": "count of duplicate downstream effects observed",
    "downstream_executions": "count of downstream executions observed",
    "unresolved_operations": "count of operations left with no information",
    "duration_ms": "how long the step took",
    "stage": "which funnel stage the surface believes it is in",
    "step": "a named step inside a multi-step surface",
    "surface": "cli, web or sdk",
    "page": "a page name, from a fixed set the surface defines",
    "report_format": "html, text or json",
    "referrer_kind": "a coarse bucket: search, social, direct, docs, referral",
    "outcome": "success, failure or abandoned",
    "error_kind": "a short error classifier, never an error message",
    "integration_kind": "mcp, rest or sdk",
    "transport": "jsonrpc or rest",
    "definition_version": "the scenario definition version the run used",
    "gateway_version": "the gateway version under test",
    "lab_version": "the failure lab version",
    "plan": "the plan name a pricing page showed",
    "billing_interval": "monthly or annual",
}

#: Values longer than this are treated as a payload rather than a dimension.
MAX_PROPERTY_VALUE_LENGTH = 120
#: Subject identifiers are opaque and short. Anything longer is not an id.
MAX_SUBJECT_LENGTH = 64

_SENSITIVE_NAME_PATTERN = re.compile(
    r"api[_-]?key|apikey|token|secret|seed|password|authorization|bearer"
    r"|private[_-]?key|email|user_agent|payload",
    re.IGNORECASE,
)
#: Short, ambiguous words that are only sensitive as a whole word in a name --
#: ``ip`` must not condemn ``recipient_kind``. Checked against the name split
#: on non-alphanumerics rather than as a substring.
_SENSITIVE_NAME_TOKENS = frozenset(
    {
        "ip",
        "name",
        "address",
        "phone",
        "body",
        "card",
        "cvv",
        "iban",
        "account",
        "user",
        "customer",
        "mail",
        "wallet",
        "pan",
    }
)
_EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")
_CREDENTIAL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:amw|b2a|sk|pk|rk|lab-admin)[_-][A-Za-z0-9_-]{12,}"
)
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+\S{8,}")
_LONG_DIGIT_RUN = re.compile(r"(?:\d[ -]?){13,}")
_PAYMENT_WORD_PATTERN = re.compile(
    r"(?i)\b(?:cvv|cvc|iban|sort[_ -]?code|routing|pan)\b"
)
_SUBJECT_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class TelemetryRejected(ValueError):
    """An event was refused. Refusing is the feature; do not catch this."""


class PropertyNotAllowed(TelemetryRejected):
    """A property name is not on the allowlist."""


class SensitiveValueRejected(TelemetryRejected):
    """A property value looks like a credential, a payment detail or a payload."""


class RemoteSinkNotOptedIn(TelemetryRejected):
    """A remote sink was constructed or attached without the explicit opt-in."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def pseudonymous_subject(raw: str, *, namespace: str = "failure-lab") -> str:
    """Fold an identifier into an opaque, stable subject id.

    The surface calls this on whatever it uses to recognise a returning
    visitor -- a session cookie, a CLI install id -- before telemetry ever
    sees it. Hashing an email address would still be an identifier for a
    person, so do not: pass the session, not the human.
    """
    digest = hashlib.sha256(f"{namespace}\x1f{raw}".encode("utf-8")).hexdigest()
    return f"sub_{digest[:24]}"


def _check_subject(subject: str) -> str:
    value = str(subject)
    if not _SUBJECT_PATTERN.match(value):
        raise SensitiveValueRejected(
            "subject must be an opaque id of at most "
            f"{MAX_SUBJECT_LENGTH} characters from [A-Za-z0-9_.:-]; "
            "use telemetry.pseudonymous_subject() on whatever the surface has"
        )
    if _EMAIL_PATTERN.search(value):
        raise SensitiveValueRejected("subject looks like an email address")
    return value


def check_property(name: str, value: Any) -> Any:
    """Validate one event property, raising rather than dropping it silently.

    Silent dropping turns a leak into a mystery: the field is gone from the
    warehouse and nobody learns that the surface tried to send it. Raising
    means the attempt shows up the first time it is made, in the surface's own
    tests.
    """
    key = str(name)
    if key not in ALLOWED_PROPERTIES:
        raise PropertyNotAllowed(
            f"property {key!r} is not on the telemetry allowlist. Allowed: "
            + ", ".join(sorted(ALLOWED_PROPERTIES))
        )
    tokens = {part for part in re.split(r"[^A-Za-z0-9]+", key.lower()) if part}
    if _SENSITIVE_NAME_PATTERN.search(key) or (tokens & _SENSITIVE_NAME_TOKENS):
        # Unreachable while the allowlist stays clean; here so that widening
        # the allowlist with a sensitive name fails loudly instead of quietly.
        raise PropertyNotAllowed(f"property {key!r} has a sensitive name")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if not isinstance(value, str):
        raise SensitiveValueRejected(
            f"property {key!r} must be a string, number, bool or None; "
            f"got {type(value).__name__}. Structured values are customer payload."
        )
    if len(value) > MAX_PROPERTY_VALUE_LENGTH:
        raise SensitiveValueRejected(
            f"property {key!r} is {len(value)} characters; anything over "
            f"{MAX_PROPERTY_VALUE_LENGTH} is a payload, not a dimension"
        )
    if "\n" in value or ("{" in value and "}" in value):
        raise SensitiveValueRejected(
            f"property {key!r} looks like a serialised payload rather than a dimension"
        )
    if _EMAIL_PATTERN.search(value):
        raise SensitiveValueRejected(f"property {key!r} contains an email address")
    if _CREDENTIAL_PATTERN.search(value) or _BEARER_PATTERN.search(value):
        raise SensitiveValueRejected(f"property {key!r} contains credential material")
    if _LONG_DIGIT_RUN.search(value) or _PAYMENT_WORD_PATTERN.search(value):
        raise SensitiveValueRejected(f"property {key!r} looks like a payment detail")
    return value


def check_properties(properties: Mapping[str, Any]) -> dict[str, Any]:
    return {str(k): check_property(k, v) for k, v in properties.items()}


@dataclass(frozen=True)
class Event:
    """One recorded event.

    ``traffic_source`` has no default. That is the whole mechanism: an event
    cannot exist without a source, so there is no path by which unlabelled
    traffic becomes conversion traffic.
    """

    name: EventName
    traffic_source: TrafficSource
    subject: str
    at: str = field(default_factory=_now)
    properties: dict[str, Any] = field(default_factory=dict)
    schema_version: str = TELEMETRY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", EventName(self.name))
        object.__setattr__(self, "traffic_source", TrafficSource(self.traffic_source))
        object.__setattr__(self, "subject", _check_subject(self.subject))
        object.__setattr__(self, "properties", check_properties(self.properties))

    @property
    def counts_toward_conversion(self) -> bool:
        return counts_toward_conversion(self.traffic_source)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event": self.name.value,
            "traffic_source": self.traffic_source.value,
            "counts_toward_conversion": self.counts_toward_conversion,
            "subject": self.subject,
            "at": self.at,
            "properties": dict(self.properties),
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> Event:
        if not document.get("traffic_source"):
            raise TelemetryRejected(
                "an event document without a traffic_source cannot be read back; "
                "there is no default, and guessing one is how synthetic traffic "
                "becomes conversion traffic"
            )
        return cls(
            name=EventName(str(document.get("event") or document.get("name"))),
            traffic_source=TrafficSource(str(document["traffic_source"])),
            subject=str(document.get("subject", "")),
            at=str(document.get("at") or _now()),
            properties=dict(document.get("properties") or {}),
        )


# --------------------------------------------------------------------------- #
# Sinks                                                                         #
# --------------------------------------------------------------------------- #


class Sink:
    """Somewhere an event is written."""

    is_remote: bool = False

    def emit(self, event: Event) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def describe(self) -> str:  # pragma: no cover - interface
        raise NotImplementedError


class JsonlSink(Sink):
    """Append one JSON object per line to a file under the run directory.

    The only sink that exists by default. Nothing leaves the machine.
    """

    is_remote = False

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        #: Set when :meth:`read` drops an unterminated final line.
        self.truncated_tail = False

    def emit(self, event: Event) -> None:
        line = json.dumps(event.as_dict(), sort_keys=True, ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def describe(self) -> str:
        return f"a local file at {self.path}"

    def read(self) -> list[Event]:
        """Read the events back, for the funnel or for a test.

        A line that parses but breaks the collection policy still raises: that
        is a surface sending something it should not, and it has to surface.
        An **unterminated final line** does not -- it is a writer that was
        killed mid-append, and refusing to read the file at all would lose
        every event before it to a crash that has nothing to do with policy.
        It is dropped, recorded in :attr:`truncated_tail`, and reported in the
        funnel's notes. Dropping the tail can only ever lower a count.
        """
        self.truncated_tail = False
        if not self.path.exists():
            return []
        text = self.path.read_text(encoding="utf-8")
        lines = text.splitlines()
        events: list[Event] = []
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                document = json.loads(line)
            except ValueError:
                if number == len(lines) and not text.endswith("\n"):
                    self.truncated_tail = True
                    continue
                raise TelemetryRejected(
                    f"{self.path.name} line {number} is not valid JSON and is not "
                    "the unterminated tail of an interrupted write"
                ) from None
            events.append(Event.from_dict(document))
        return events


class RemoteSink(Sink):
    """A sink that sends events off the machine.

    Constructing one requires ``opt_in=True`` typed at the call site, and
    attaching it to a :class:`TelemetryClient` requires
    ``allow_remote_sinks=True`` typed at that call site. Two explicit,
    non-default arguments, because one of them will eventually be set by a
    config file nobody read.
    """

    is_remote = True

    def __init__(
        self,
        name: str,
        deliver: Callable[[dict[str, Any]], None],
        *,
        opt_in: bool = False,
    ) -> None:
        if opt_in is not True:
            raise RemoteSinkNotOptedIn(
                f"remote sink {name!r} requires opt_in=True; the local JSONL "
                "sink under the run directory is the only default"
            )
        self.name = name
        self._deliver = deliver

    def emit(self, event: Event) -> None:
        self._deliver(event.as_dict())

    def describe(self) -> str:
        return f"a remote endpoint named {self.name!r} that you explicitly opted into"


# --------------------------------------------------------------------------- #
# Client                                                                        #
# --------------------------------------------------------------------------- #


class TelemetryClient:
    """Records events for one surface, under one traffic source.

    The traffic source is fixed at construction and cannot be overridden per
    event. A harness that constructs its client with ``ai_test_agent`` cannot
    emit a ``human_customer`` event at all -- not through a keyword, not
    through a property, not by accident.
    """

    def __init__(
        self,
        run_dir: Path | str,
        traffic_source: TrafficSource | str,
        *,
        subject: str | None = None,
        sinks: Sequence[Sink] = (),
        allow_remote_sinks: bool = False,
        filename: str = DEFAULT_SINK_FILENAME,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.traffic_source = TrafficSource(traffic_source)
        self.subject = (
            _check_subject(subject)
            if subject
            else pseudonymous_subject(f"{self.run_dir}")
        )
        self.local_sink = JsonlSink(self.run_dir / filename)
        extra = list(sinks)
        remote = [sink for sink in extra if getattr(sink, "is_remote", False)]
        if remote and allow_remote_sinks is not True:
            names = ", ".join(sink.describe() for sink in remote)
            raise RemoteSinkNotOptedIn(
                "a remote sink was passed without allow_remote_sinks=True: " + names
            )
        self.sinks: list[Sink] = [self.local_sink, *extra]
        self.events: list[Event] = []

    def record(
        self,
        name: EventName | str,
        *,
        subject: str | None = None,
        at: str | None = None,
        **properties: Any,
    ) -> Event:
        """Validate, then write to every sink. Invalid events raise."""
        event = Event(
            name=EventName(name),
            traffic_source=self.traffic_source,
            subject=_check_subject(subject) if subject else self.subject,
            at=at or _now(),
            properties=properties,
        )
        for sink in self.sinks:
            sink.emit(event)
        self.events.append(event)
        return event

    def read(self) -> list[Event]:
        return self.local_sink.read()

    def funnel(self) -> Funnel:
        events = self.read()
        notes = (
            [
                f"{self.local_sink.path.name} ended in an unterminated line, which "
                "was dropped: a writer was interrupted. Counts below are a lower "
                "bound on what was recorded."
            ]
            if self.local_sink.truncated_tail
            else []
        )
        return funnel(events, notes=notes)

    def disclosure_text(self) -> str:
        return disclosure_text(sinks=self.sinks)


# --------------------------------------------------------------------------- #
# Funnel                                                                        #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FunnelStageSpec:
    key: str
    label: str
    #: The events that put a subject in this stage. Empty means "any event",
    #: which is what makes someone a visitor.
    events: frozenset[EventName]


#: The PRD's funnel. ``own_integration_tested`` is the baseline result,
#: because the baseline *is* the caller's existing integration under test;
#: ``gateway_integrated`` is the integration events, not the comparison
#: events, because running a comparison in the lab is not an integration.
FUNNEL_STAGES: tuple[FunnelStageSpec, ...] = (
    FunnelStageSpec("relevant_visitor", "Relevant visitor", frozenset()),
    FunnelStageSpec(
        "diagnostic_started",
        "Diagnostic started",
        frozenset({EventName.DIAGNOSTIC_STARTED}),
    ),
    FunnelStageSpec(
        "diagnostic_completed",
        "Diagnostic completed",
        frozenset({EventName.DIAGNOSTIC_COMPLETED}),
    ),
    FunnelStageSpec(
        "own_integration_tested",
        "Own integration tested",
        frozenset({EventName.BASELINE_PASSED, EventName.BASELINE_FAILED}),
    ),
    FunnelStageSpec(
        "gateway_integrated",
        "Gateway integrated",
        frozenset({EventName.INTEGRATION_COMPLETED}),
    ),
    FunnelStageSpec(
        "paid_activation",
        "Paid activation",
        frozenset({EventName.PAID_ACTIVATION_COMPLETED}),
    ),
    FunnelStageSpec(
        "continued_usage", "Continued usage", frozenset({EventName.CONTINUED_USAGE})
    ),
)


@dataclass(frozen=True)
class FunnelStage:
    key: str
    label: str
    subjects: int
    events: int
    conversion_from_previous: float | None
    conversion_from_top: float | None

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class Funnel:
    stages: list[FunnelStage]
    counted_sources: list[str]
    counted_events: int
    excluded_events_by_source: dict[str, int]
    excluded_subjects_by_source: dict[str, int]
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "counted_sources": self.counted_sources,
            "counted_events": self.counted_events,
            "excluded_events_by_source": self.excluded_events_by_source,
            "excluded_subjects_by_source": self.excluded_subjects_by_source,
            "stages": [stage.as_dict() for stage in self.stages],
            "notes": self.notes,
        }


def _as_event(item: Event | Mapping[str, Any]) -> Event:
    return item if isinstance(item, Event) else Event.from_dict(item)


def funnel(
    events: Iterable[Event | Mapping[str, Any]], *, notes: Sequence[str] = ()
) -> Funnel:
    """Count the PRD's funnel over human traffic only.

    Every non-human event is dropped before any counting happens, and the
    dropped counts are reported so the exclusion is visible rather than
    implied. There is no argument that includes them.

    Stages are counted independently and are **not** forced to be monotone. A
    subject who shows up at ``paid_activation`` with no recorded
    ``diagnostic_completed`` will produce a stage count that rises rather than
    falls, and that is left visible: it means the surface is not instrumented
    where it thinks it is, which is worth knowing and is not worth smoothing
    away. Each rise is named in :attr:`Funnel.notes`, because "visible" and
    "printed as a bare number under a smaller one" are not the same thing.

    ``notes`` are extra lines from the caller -- a reader that dropped a
    truncated line says so there -- kept ahead of the counting notes.
    """
    counted: list[Event] = []
    excluded_events: dict[str, int] = {}
    excluded_subjects: dict[str, set[str]] = {}
    for item in events:
        event = _as_event(item)
        if event.counts_toward_conversion:
            counted.append(event)
            continue
        source = event.traffic_source.value
        excluded_events[source] = excluded_events.get(source, 0) + 1
        excluded_subjects.setdefault(source, set()).add(event.subject)

    stages: list[FunnelStage] = []
    top: int | None = None
    previous: int | None = None
    previous_label = ""
    rises: list[str] = []
    for spec in FUNNEL_STAGES:
        if spec.events:
            matching = [event for event in counted if event.name in spec.events]
        else:
            matching = counted
        subjects = len({event.subject for event in matching})
        if top is None:
            top = subjects
        stages.append(
            FunnelStage(
                key=spec.key,
                label=spec.label,
                subjects=subjects,
                events=len(matching),
                conversion_from_previous=(
                    round(subjects / previous, 4) if previous else None
                ),
                conversion_from_top=round(subjects / top, 4) if top else None,
            )
        )
        if previous is not None and subjects > previous:
            rises.append(
                f"{spec.label} ({subjects}) counts more subjects than "
                f"{previous_label} ({previous})"
            )
        previous = subjects
        previous_label = spec.label

    notes = list(notes)
    notes.append(
        "Only human_customer traffic is counted. Every other source is dropped "
        "before counting, not subtracted afterwards."
    )
    if excluded_events:
        dropped = ", ".join(
            f"{source}={count}" for source, count in sorted(excluded_events.items())
        )
        notes.append(f"Dropped events by source: {dropped}.")
    if rises:
        notes.append(
            "Stages are counted independently and not forced to fall. These rose: "
            + "; ".join(rises)
            + ". A later stage with more subjects than an earlier one means the "
            "earlier step is not instrumented where the surface thinks it is, so "
            "no rate through it is trustworthy."
        )
    if top == 0:
        notes.append(
            "No human traffic was recorded, so every stage is zero. A zero "
            "funnel over synthetic traffic is the correct answer, not a bug."
        )
    return Funnel(
        stages=stages,
        counted_sources=sorted(source.value for source in CONVERSION_SOURCES),
        counted_events=len(counted),
        excluded_events_by_source=dict(sorted(excluded_events.items())),
        excluded_subjects_by_source={
            source: len(subjects)
            for source, subjects in sorted(excluded_subjects.items())
        },
        notes=notes,
    )


def render_funnel_text(result: Funnel) -> str:
    """The funnel as a reader sees it, exclusions on the same page."""
    lines = ["Funnel (human_customer traffic only)", "-" * 42]
    width = max(len(stage.label) for stage in result.stages)
    for stage in result.stages:
        rate = (
            f"{stage.conversion_from_previous:.0%}"
            if stage.conversion_from_previous is not None
            else "  — "
        )
        lines.append(
            f"  {stage.label:<{width}}  {stage.subjects:>6}  {rate:>6} of previous"
        )
    lines.append("")
    if result.excluded_events_by_source:
        lines.append("Excluded, not counted anywhere:")
        for source, count in result.excluded_events_by_source.items():
            subjects = result.excluded_subjects_by_source.get(source, 0)
            lines.append(f"  {source:<16} {count:>6} events from {subjects} subjects")
    else:
        lines.append("No events were excluded.")
    lines.append("")
    lines.extend(result.notes)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Disclosure                                                                    #
# --------------------------------------------------------------------------- #


def disclosure_text(*, sinks: Sequence[Sink] = ()) -> str:
    """The plain-language statement of what this module collects.

    Generated from the same constants the collector enforces -- the event
    enum, the property allowlist, the sinks actually attached -- so a UI that
    renders this cannot describe a collection policy the code does not have.
    """
    destinations = (
        [sink.describe() for sink in sinks]
        if sinks
        else [f"a local file named {DEFAULT_SINK_FILENAME} inside the run directory"]
    )
    remote = [sink for sink in sinks if getattr(sink, "is_remote", False)]
    event_lines = "\n".join(
        f"  - {name.value}: {EVENT_DESCRIPTIONS[name]}" for name in EventName
    )
    property_lines = "\n".join(
        f"  - {name}: {description}"
        for name, description in sorted(ALLOWED_PROPERTIES.items())
    )
    destination_lines = "\n".join(f"  - {destination}" for destination in destinations)
    remote_line = (
        "A remote destination is attached because it was explicitly opted into at "
        "two separate call sites."
        if remote
        else "Nothing is sent off this machine. A remote destination would require "
        "two explicit, non-default arguments that nobody has passed."
    )
    return f"""What this records

Events, and nothing else:
{event_lines}

Each event carries: the event name, a UTC timestamp, an opaque subject id, the
traffic source, and properties drawn from this fixed list:
{property_lines}

Where it goes:
{destination_lines}

{remote_line}

What is never recorded: API keys, bearer tokens, signing seeds and control
tokens; payment details; email addresses, names, IP addresses or user agents;
and any customer payload, request body or error message. Property names are an
allowlist -- a name that is not on the list above is refused outright rather
than dropped quietly, and values that look like credentials, payment details
or payloads are refused the same way.

The subject id is an opaque hash the surface computes before telemetry sees
it. It distinguishes a returning visitor from a new one and identifies nobody.

Traffic source is required on every event and is one of: {", ".join(s.value for s in TrafficSource)}.
Only {TrafficSource.HUMAN_CUSTOMER.value} traffic is counted in any conversion
number; internal, agent and CI traffic is dropped before counting, so it cannot
inflate a funnel.
"""


__all__ = [
    "ALLOWED_PROPERTIES",
    "CONVERSION_SOURCES",
    "DEFAULT_SINK_FILENAME",
    "EVENT_DESCRIPTIONS",
    "Event",
    "EventName",
    "FUNNEL_STAGES",
    "Funnel",
    "FunnelStage",
    "FunnelStageSpec",
    "JsonlSink",
    "MAX_PROPERTY_VALUE_LENGTH",
    "MAX_SUBJECT_LENGTH",
    "PropertyNotAllowed",
    "RemoteSink",
    "RemoteSinkNotOptedIn",
    "SensitiveValueRejected",
    "Sink",
    "TELEMETRY_SCHEMA_VERSION",
    "TelemetryClient",
    "TelemetryRejected",
    "TrafficSource",
    "check_properties",
    "check_property",
    "counts_toward_conversion",
    "detect_traffic_source",
    "disclosure_text",
    "funnel",
    "pseudonymous_subject",
    "render_funnel_text",
]
