"""Turning measurements into a report that can argue against its own product.

Two rules shape every function here, both from the PRD:

* **No risk score.** A single invented number would let a reader skip the
  measurements, and there is no defensible way to weigh "one duplicate refund"
  against "one unresolved operation" on a common scale. Every figure rendered
  here is something an instrument actually counted.
* **The conclusion is descriptive.** It is computed from the counts, and it is
  allowed to say the gateway added nothing. A diagnostic that cannot reach
  that conclusion is marketing with a progress bar.

The comparison is always at least three-way: the existing integration, the
same integration with correct native controls, and native controls plus the
gateway. That is what makes a positive result mean anything -- and what makes
the "you may not need us" result reachable rather than theoretical.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from failure_lab.configurations import Configuration
from failure_lab.scenarios.base import ConfigurationResult, ScenarioResult, Verdict

#: How many downstream executions one business operation is supposed to cause.
INTENDED_EXECUTIONS = 1


class ConclusionKind(str, Enum):
    #: The naive/existing integration already handled the tested failure.
    EXISTING_INTEGRATION_SUFFICIENT = "existing_integration_sufficient"
    #: Correct native controls handled it, with no gateway involved.
    NATIVE_CONTROLS_SUFFICIENT = "native_controls_sufficient"
    #: The gateway prevented duplicate business effects the baseline allowed.
    GATEWAY_PREVENTED_DUPLICATES = "gateway_prevented_duplicates"
    #: No additional duplicate was prevented, but the gateway changed what the
    #: caller can know or prove. Reported as exactly that, never as prevention.
    GATEWAY_ADDED_EVIDENCE_ONLY = "gateway_added_evidence_only"
    #: The gateway did not hold its own documented guarantee here.
    GATEWAY_DID_NOT_HOLD = "gateway_did_not_hold"
    #: The test exercises a component only the gateway has, so no baseline
    #: column ran and no comparison was possible. The gateway's own verdict
    #: still stands; what cannot be said is whether the caller needs it.
    #: Distinct from :attr:`INCONCLUSIVE`, where the gateway did not run
    #: either and the report knows nothing at all.
    NO_BASELINE_COMPARISON = "no_baseline_comparison"
    #: Not enough configurations ran to say anything.
    INCONCLUSIVE = "inconclusive"


#: One line per conclusion kind, for the summary block. A reader should never
#: have to infer what a kind means from its name -- least of all the two that
#: say the product was not needed or was not compared.
CONCLUSION_GLOSS: dict[str, str] = {
    ConclusionKind.EXISTING_INTEGRATION_SUFFICIENT.value: (
        "the existing integration handled it; the gateway prevented no "
        "additional duplicate effect"
    ),
    ConclusionKind.NATIVE_CONTROLS_SUFFICIENT.value: (
        "correct native controls handled it on their own; the gateway "
        "prevented no additional duplicate effect"
    ),
    ConclusionKind.GATEWAY_PREVENTED_DUPLICATES.value: (
        "duplicate business effects occurred in a measured baseline and did "
        "not occur behind the gateway"
    ),
    ConclusionKind.GATEWAY_ADDED_EVIDENCE_ONLY.value: (
        "no additional duplicate effect was prevented; what changed is what "
        "the caller can know and prove"
    ),
    ConclusionKind.GATEWAY_DID_NOT_HOLD.value: (
        "the gateway did not hold the property this test checks"
    ),
    ConclusionKind.NO_BASELINE_COMPARISON.value: (
        "only the gateway has the component under test, so no baseline ran "
        "and no comparison was made either way"
    ),
    ConclusionKind.INCONCLUSIVE.value: (
        "too few configurations ran for this test to say anything"
    ),
}


@dataclass
class ConfigurationView:
    """One column of the comparison, in the PRD's vocabulary."""

    configuration: str
    label: str
    verdict: str
    requests: int
    #: Requests that crossed the fault layer into the tool. Independently
    #: observed for every configuration, including the direct ones.
    downstream_requests: int
    gateway_dispatches: int | None
    downstream_effects: int
    duplicate_effects: int
    known_final_outcome: int
    explicit_uncertain: int
    unresolved_operations: int
    gateway_debits: int | None
    gateway_refunds: int | None
    receipts: int | None
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    observation: str
    ran: bool  # completed with usable measurements, not a setup/execution error

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Conclusion:
    kind: ConclusionKind
    text: str
    #: Duplicate downstream effects the gateway prevented relative to the
    #: correct native baseline. Zero is a legitimate, reportable answer.
    duplicates_prevented_vs_native: int
    #: The same relative to the existing (naive) integration.
    duplicates_prevented_vs_existing: int
    #: What the gateway changed that is not duplicate prevention. Listed
    #: separately so it can never be presented as prevention.
    non_prevention_differences: list[str] = field(default_factory=list)
    #: What the baseline did BETTER. A report that only lists a product's
    #: advantages is an advertisement; this field is what stops that.
    gateway_disadvantages: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "text": self.text,
            "duplicates_prevented_vs_native": self.duplicates_prevented_vs_native,
            "duplicates_prevented_vs_existing": self.duplicates_prevented_vs_existing,
            "non_prevention_differences": self.non_prevention_differences,
            "gateway_disadvantages": self.gateway_disadvantages,
        }


@dataclass
class Comparison:
    test_id: str
    title: str
    claim: str
    verdict: str
    matches_expectation: bool
    expectation_mismatches: list[dict[str, str]]
    columns: list[ConfigurationView]
    conclusion: Conclusion
    additional_latency: dict[str, float | None]
    remaining_risks: list[str]
    limitations: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "test_id": self.test_id,
            "title": self.title,
            "claim": self.claim,
            "verdict": self.verdict,
            "matches_expectation": self.matches_expectation,
            "expectation_mismatches": self.expectation_mismatches,
            "columns": [column.as_dict() for column in self.columns],
            "conclusion": self.conclusion.as_dict(),
            "additional_latency": self.additional_latency,
            "remaining_risks": self.remaining_risks,
            "limitations": self.limitations,
        }


def _verdict_text(value: Any) -> str:
    """Normalise a verdict to its bare name.

    ``Verdict`` subclasses ``str``, so a raw enum compares equal to its own
    value but formats as ``Verdict.PASS``. Rendering that into a report would
    leak Python into an artifact meant for a reader who does not have the
    source, and would produce CSS class names like ``v-Verdict.PASS``.
    """
    return value.value if isinstance(value, Verdict) else str(value)


def _view(entry: ConfigurationResult) -> ConfigurationView:
    counters = entry.counters
    duplicates = max(0, counters.downstream_executions - INTENDED_EXECUTIONS)
    return ConfigurationView(
        configuration=entry.configuration,
        label=entry.label,
        verdict=_verdict_text(entry.verdict),
        requests=counters.incoming_requests,
        downstream_requests=counters.downstream_requests,
        gateway_dispatches=counters.gateway_dispatches,
        downstream_effects=counters.downstream_executions,
        duplicate_effects=duplicates,
        known_final_outcome=counters.known_final_outcome,
        explicit_uncertain=counters.explicit_uncertain,
        unresolved_operations=counters.unresolved,
        gateway_debits=counters.gateway_debits,
        gateway_refunds=counters.gateway_refunds,
        receipts=counters.receipts,
        latency_p50_ms=counters.latency_p50_ms,
        latency_p95_ms=counters.latency_p95_ms,
        observation=entry.observation,
        ran=entry.verdict
        not in (
            Verdict.NOT_APPLICABLE.value,
            Verdict.NOT_RUN.value,
            Verdict.ERROR.value,
        ),
    )


def _by_configuration(result: ScenarioResult) -> dict[str, ConfigurationResult]:
    return {entry.configuration: entry for entry in result.configurations}


def _differences(
    native: ConfigurationView | None, governed: ConfigurationView | None
) -> tuple[list[str], list[str]]:
    """What the gateway added, and what it cost, relative to the baseline.

    Both halves are computed the same way and returned together. Reporting
    only the first half would turn a measurement into a pitch -- and for the
    lost-response scenario the second half is the interesting number: a
    downstream that dedupes natively can answer "it already happened", while a
    gateway that never saw the response can only answer "I do not know".
    """
    if native is None or governed is None or not native.ran or not governed.ran:
        return [], []

    added: list[str] = []
    lost: list[str] = []

    if governed.unresolved_operations < native.unresolved_operations:
        added.append(
            f"attempts left with no information at all fell from "
            f"{native.unresolved_operations} to {governed.unresolved_operations}; "
            f"{governed.explicit_uncertain} ended in an explicit uncertain state "
            "the caller can route on instead of an unexplained timeout"
        )
    elif governed.explicit_uncertain > 0:
        added.append(
            f"{governed.explicit_uncertain} attempt(s) ended in an explicit "
            "uncertain state rather than an unexplained timeout"
        )

    if governed.downstream_requests < native.downstream_requests:
        absorbed = native.downstream_requests - governed.downstream_requests
        added.append(
            f"{absorbed} request(s) were absorbed before reaching the tool at "
            f"all: {native.downstream_requests} crossed into the downstream in "
            f"the baseline against {governed.downstream_requests} behind the "
            "gateway. The baseline reaches the same effect count by letting "
            "every request in and collapsing them inside the tool's own "
            "transaction, which is load the downstream has to absorb"
        )

    if (governed.receipts or 0) > 0:
        added.append(
            f"{governed.receipts} signed receipt(s) were issued. A signature "
            "establishes what the gateway recorded, not that the business "
            "action occurred, and the key that verifies it is published by the "
            "same origin that issued it"
        )

    # The cost side.
    if governed.known_final_outcome < native.known_final_outcome:
        lost.append(
            f"the baseline resolved {native.known_final_outcome} attempt(s) to a "
            f"confirmed outcome; behind the gateway {governed.known_final_outcome} "
            "did. A downstream that deduplicates natively can replay the stored "
            "result and tell the caller the action already happened; a gateway "
            "that never received the response cannot, and reports uncertainty "
            "instead"
        )
    if (governed.gateway_debits or 0) > (governed.gateway_refunds or 0) and (
        governed.explicit_uncertain > 0
    ):
        net = (governed.gateway_debits or 0) - (governed.gateway_refunds or 0)
        lost.append(
            f"{net} charge(s) are retained against an operation whose outcome "
            "the gateway does not know. That is the documented conservative "
            "choice, and it is a cost the baseline does not impose"
        )
    if governed.latency_p50_ms is not None and native.latency_p50_ms is not None:
        delta = governed.latency_p50_ms - native.latency_p50_ms
        if delta > 0:
            lost.append(f"p50 latency rose by {delta:.1f} ms")

    return added, lost


def _conclude(
    result: ScenarioResult,
    existing: ConfigurationView | None,
    native: ConfigurationView | None,
    governed: ConfigurationView | None,
) -> Conclusion:
    prevented_vs_native = (
        max(0, native.duplicate_effects - governed.duplicate_effects)
        if native is not None and governed is not None and native.ran and governed.ran
        else 0
    )
    prevented_vs_existing = (
        max(0, existing.duplicate_effects - governed.duplicate_effects)
        if existing is not None
        and governed is not None
        and existing.ran
        and governed.ran
        else 0
    )
    differences, disadvantages = _differences(native, governed)

    if governed is None or not governed.ran:
        return Conclusion(
            ConclusionKind.INCONCLUSIVE,
            "The gateway configuration did not produce usable measurements "
            "for this test, so this report cannot establish its behavior.",
            prevented_vs_native,
            prevented_vs_existing,
            differences,
            disadvantages,
        )

    if governed.verdict == Verdict.FAIL.value:
        return Conclusion(
            ConclusionKind.GATEWAY_DID_NOT_HOLD,
            "The gateway did not hold the property this test checks. "
            f"{governed.observation}",
            prevented_vs_native,
            prevented_vs_existing,
            differences,
            disadvantages,
        )

    existing_ran = existing is not None and existing.ran
    native_ran = native is not None and native.ran
    if not existing_ran and not native_ran:
        if any(
            column is not None and column.verdict == Verdict.ERROR.value
            for column in (existing, native)
        ):
            return Conclusion(
                ConclusionKind.INCONCLUSIVE,
                "Baseline configuration errors left no usable baseline measurements. "
                "No comparison or duplicate-prevention claim can be established.",
                0,
                0,
            )
        # Neither baseline column ran, so there is nothing to compare the
        # gateway against. Every "sufficient" conclusion below is a statement
        # about a baseline, and asserting one from a column that never ran
        # would tell a reader their own controls cover a failure mode this run
        # never put them through. The gateway's own verdict is reported; the
        # comparison is not invented.
        return Conclusion(
            ConclusionKind.NO_BASELINE_COMPARISON,
            "This test exercises a component only the gateway has, so neither "
            "baseline configuration ran and no comparison was made. The "
            f"gateway's own verdict for this test is {governed.verdict}, and "
            "that is all this test establishes. It does not show that your "
            "existing integration or correct native controls would have "
            "handled this failure, and it does not show that they would not. "
            "Read a test with a baseline column for that question.",
            prevented_vs_native,
            prevented_vs_existing,
            differences,
            disadvantages,
        )

    if existing is not None and existing.ran and existing.duplicate_effects == 0:
        return Conclusion(
            ConclusionKind.EXISTING_INTEGRATION_SUFFICIENT,
            "Your existing integration handled this test correctly. We did not "
            "observe an additional duplicate effect prevented by Agent "
            "Middleware in this scenario. Try another failure mode or inspect "
            "the detailed trace.",
            prevented_vs_native,
            prevented_vs_existing,
            differences,
            disadvantages,
        )

    if native is not None and native.ran and native.duplicate_effects == 0:
        if prevented_vs_native == 0:
            cost = (
                " On this test the baseline also did something the gateway did "
                "not: see what it cost, below."
                if disadvantages
                else ""
            )
            return Conclusion(
                ConclusionKind.NATIVE_CONTROLS_SUFFICIENT,
                "Correct native controls handled this test on their own. We did "
                "not observe an additional duplicate effect prevented by Agent "
                "Middleware in this scenario. If your downstream already honors "
                "a durable operation id the way this baseline does, this "
                "failure mode is already covered for you." + cost,
                prevented_vs_native,
                prevented_vs_existing,
                differences,
                disadvantages,
            )

    if prevented_vs_native == 0 and prevented_vs_existing == 0:
        if differences:
            return Conclusion(
                ConclusionKind.GATEWAY_ADDED_EVIDENCE_ONLY,
                "No additional duplicate business effect was prevented in this "
                "scenario. What changed is what the caller can know and prove "
                "afterwards, listed below. Judge that on its own terms.",
                prevented_vs_native,
                prevented_vs_existing,
                differences,
                disadvantages,
            )
        return Conclusion(
            ConclusionKind.NATIVE_CONTROLS_SUFFICIENT,
            "No difference was observed between the baseline and the gateway "
            "in this scenario.",
            prevented_vs_native,
            prevented_vs_existing,
            differences,
            disadvantages,
        )

    against = (
        "the correct native baseline"
        if prevented_vs_native
        else "your existing integration"
    )
    count = prevented_vs_native or prevented_vs_existing
    return Conclusion(
        ConclusionKind.GATEWAY_PREVENTED_DUPLICATES,
        f"{count} duplicate downstream business effect(s) occurred in "
        f"{against} and did not occur with Agent Middleware in front of the "
        "same tool under the same injected failure. The duplicate count comes "
        "from the downstream system's own ledger, which the gateway cannot "
        "reach.",
        prevented_vs_native,
        prevented_vs_existing,
        differences,
        disadvantages,
    )


def _additional_latency(
    native: ConfigurationView | None, governed: ConfigurationView | None
) -> dict[str, float | None]:
    def delta(a: float | None, b: float | None) -> float | None:
        if a is None or b is None:
            return None
        return round(b - a, 3)

    if native is None or governed is None or not native.ran or not governed.ran:
        return {"p50_ms": None, "p95_ms": None, "baseline": None}
    return {
        "p50_ms": delta(native.latency_p50_ms, governed.latency_p50_ms),
        "p95_ms": delta(native.latency_p95_ms, governed.latency_p95_ms),
        "baseline": native.configuration,
    }


def build_comparison(result: ScenarioResult) -> Comparison:
    """Reduce one scenario's measurements to the PRD's comparison report."""
    entries = _by_configuration(result)
    columns = [_view(entry) for entry in result.configurations]
    views = {column.configuration: column for column in columns}
    existing = views.get(Configuration.DIRECT_NAIVE.value)
    native = views.get(Configuration.DIRECT_NATIVE.value)
    governed = views.get(Configuration.GATEWAY_NATIVE.value)
    if governed is None or governed.verdict in (
        Verdict.NOT_APPLICABLE.value,
        Verdict.NOT_RUN.value,
    ):
        fallback = views.get(Configuration.GATEWAY_NAIVE.value)
        if fallback is not None and fallback.ran:
            governed = fallback

    risks: list[str] = []
    for entry in result.configurations:
        for risk in entry.remaining_risks:
            if risk not in risks:
                risks.append(risk)
    del entries

    return Comparison(
        test_id=result.test_id,
        title=result.title,
        claim=result.claim,
        verdict=result.verdict.value,
        matches_expectation=result.matches_expectation,
        expectation_mismatches=[
            {"configuration": cfg, "expected": expected, "observed": observed}
            for cfg, expected, observed in result.mismatches()
        ],
        columns=columns,
        conclusion=_conclude(result, existing, native, governed),
        additional_latency=_additional_latency(native, governed),
        remaining_risks=risks,
        limitations=list(result.limitations),
    )


# --------------------------------------------------------------------------- #
# Text rendering                                                                #
# --------------------------------------------------------------------------- #


def _format(value: Any) -> str:
    if value is None:
        return "n/a"
    return str(value)


def render_comparison_text(comparison: Comparison) -> str:
    lines: list[str] = []
    lines.append("AGENT ACTION FAILURE REPORT")
    lines.append("")
    lines.append(f"Test:       {comparison.test_id} — {comparison.title}")
    lines.append(f"Claim:      {comparison.claim}")
    lines.append(f"Verdict:    {comparison.verdict}")
    if not comparison.matches_expectation:
        lines.append("")
        lines.append("!! Observed verdicts differ from the documented expectation:")
        for mismatch in comparison.expectation_mismatches:
            lines.append(
                f"   {mismatch['configuration']}: documented {mismatch['expected']}, "
                f"observed {mismatch['observed']}"
            )
    lines.append("")

    rows = (
        ("Requests", "requests"),
        ("Reached the tool", "downstream_requests"),
        ("Gateway dispatches", "gateway_dispatches"),
        ("Downstream effects", "downstream_effects"),
        ("Duplicate effects", "duplicate_effects"),
        ("Known final outcome", "known_final_outcome"),
        ("Explicitly uncertain", "explicit_uncertain"),
        ("Unresolved operations", "unresolved_operations"),
        ("Gateway debits", "gateway_debits"),
        ("Gateway refunds", "gateway_refunds"),
        ("Receipts", "receipts"),
    )
    for column in comparison.columns:
        lines.append(column.label)
        lines.append("-" * max(20, len(column.label)))
        if not column.ran:
            lines.append(f"  {column.verdict}: {column.observation}")
            lines.append("  no usable measurements")
            lines.append("")
            continue
        for title, attribute in rows:
            lines.append(f"  {title + ':':<24}{_format(getattr(column, attribute))}")
        lines.append(f"  {'Verdict:':<24}{column.verdict}")
        lines.append(f"  {'Observed:':<24}{column.observation}")
        lines.append("")

    latency = comparison.additional_latency
    lines.append("Additional latency")
    lines.append("------------------")
    if latency["p50_ms"] is None:
        lines.append("  not measured (a baseline column did not run)")
    else:
        lines.append(f"  p50: {latency['p50_ms']:+.3f} ms vs {latency['baseline']}")
        lines.append(f"  p95: {latency['p95_ms']:+.3f} ms vs {latency['baseline']}")
    lines.append("")

    lines.append("Remaining risks")
    lines.append("---------------")
    if comparison.remaining_risks:
        for risk in comparison.remaining_risks:
            lines.append(f"  - {risk}")
    else:
        lines.append("  none recorded by this test")
    lines.append("")

    lines.append("What this test does not establish")
    lines.append("---------------------------------")
    for limitation in comparison.limitations:
        lines.append(f"  - {limitation}")
    lines.append("")

    lines.append("Conclusion")
    lines.append("----------")
    for line in _wrap(comparison.conclusion.text, 74):
        lines.append(f"  {line}")
    if comparison.conclusion.non_prevention_differences:
        lines.append("")
        lines.append("  What the gateway added, other than duplicate prevention:")
        for difference in comparison.conclusion.non_prevention_differences:
            for index, line in enumerate(_wrap(difference, 70)):
                lines.append(f"    {'- ' if index == 0 else '  '}{line}")
    if comparison.conclusion.gateway_disadvantages:
        lines.append("")
        lines.append("  What it cost, measured against the same baseline:")
        for difference in comparison.conclusion.gateway_disadvantages:
            for index, line in enumerate(_wrap(difference, 70)):
                lines.append(f"    {'- ' if index == 0 else '  '}{line}")
    lines.append("")
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current: list[str] = []
    length = 0
    for word in words:
        if current and length + 1 + len(word) > width:
            lines.append(" ".join(current))
            current = [word]
            length = len(word)
        else:
            current.append(word)
            length += (1 if length else 0) + len(word)
    if current:
        lines.append(" ".join(current))
    return lines or [""]


def render_run_text(
    comparisons: list[Comparison], *, environment: dict[str, Any]
) -> str:
    lines: list[str] = []
    lines.append("=" * 74)
    lines.append("AGENT GATEWAY FAILURE LAB — RUN REPORT")
    lines.append("=" * 74)
    lines.append("")
    lines.append(f"Run id:       {environment.get('run_id', 'unknown')}")
    lines.append(f"Started:      {environment.get('started_at', 'unknown')}")
    lines.append(
        f"Definitions:  {environment.get('test_definition_version', 'unknown')}"
    )
    lines.append(f"Gateway:      {environment.get('gateway_version', 'unknown')}")
    lines.append(f"Source:       {environment.get('traffic_source', 'unknown')}")
    lines.append("")
    lines.append("Scenario verdicts")
    lines.append("-----------------")
    for comparison in comparisons:
        flag = (
            ""
            if comparison.matches_expectation
            else "   (differs from documented expectation)"
        )
        lines.append(
            f"  {comparison.test_id}  {comparison.verdict:<15}{comparison.title}{flag}"
        )
    lines.append("")
    counts: dict[str, int] = {}
    for comparison in comparisons:
        counts[comparison.conclusion.kind.value] = (
            counts.get(comparison.conclusion.kind.value, 0) + 1
        )
    lines.append("Conclusions")
    lines.append("-----------")
    for kind, count in sorted(counts.items()):
        lines.append(f"  {count:>3}  {kind}")
        lines.append(f"       {CONCLUSION_GLOSS.get(kind, '')}")
    lines.append("")
    lines.append("This run reports what its instruments counted. It does not assign a")
    lines.append("risk score, and a scenario in which the gateway added nothing is")
    lines.append("reported as exactly that. A scenario with no baseline column is")
    lines.append("reported as no comparison, never as a baseline that succeeded.")
    lines.append("")
    for comparison in comparisons:
        lines.append("=" * 74)
        lines.append(render_comparison_text(comparison))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# HTML rendering (for the evidence bundle's summary.html)                       #
# --------------------------------------------------------------------------- #

_CSS = """
:root {
  color-scheme: light dark;
  --bg: #ffffff; --fg: #16181d; --muted: #5b6270; --line: #d9dde5;
  --card: #f7f8fa; --accent: #1f5fd0; --warn: #9a5b00; --bad: #b42318;
  --good: #05603a;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #101216; --fg: #e8eaee; --muted: #9aa2b1; --line: #2a2f38;
    --card: #171a20; --accent: #7fa8f0; --warn: #e0a34a; --bad: #f08a80;
    --good: #67d7a5;
  }
}
:root[data-theme="dark"] {
  --bg: #101216; --fg: #e8eaee; --muted: #9aa2b1; --line: #2a2f38;
  --card: #171a20; --accent: #7fa8f0; --warn: #e0a34a; --bad: #f08a80;
  --good: #67d7a5;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
.wrap { max-width: 980px; margin: 0 auto; padding: 32px 16px 96px; }
h1 { font-size: 1.6rem; margin: 0 0 4px; letter-spacing: -0.01em; }
h2 { font-size: 1.15rem; margin: 40px 0 8px; }
h3 { font-size: 0.95rem; margin: 20px 0 6px; }
p, li { color: var(--fg); }
.sub { color: var(--muted); margin: 0 0 24px; }
.meta { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 10px; margin: 20px 0 8px; }
.meta div { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; }
.meta dt { color: var(--muted); font-size: 0.74rem; text-transform: uppercase; letter-spacing: 0.05em; margin: 0 0 2px; }
.meta dd { margin: 0; font-variant-numeric: tabular-nums; word-break: break-word; }
table { width: 100%; border-collapse: collapse; margin: 10px 0 4px; font-size: 0.9rem; }
th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--muted); font-weight: 600; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.04em; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 16px 18px; margin: 14px 0; }
.v { font-weight: 600; font-size: 0.8rem; letter-spacing: 0.03em; }
.v-PASS { color: var(--good); } .v-FAIL { color: var(--bad); }
.v-OBSERVED { color: var(--accent); } .v-NOT_APPLICABLE, .v-NOT_RUN { color: var(--muted); }
.v-ERROR { color: var(--bad); }
.note { border-left: 3px solid var(--warn); padding-left: 12px; color: var(--muted); margin: 10px 0; }
ul { margin: 6px 0 0; padding-left: 20px; }
footer { margin-top: 56px; color: var(--muted); font-size: 0.82rem; border-top: 1px solid var(--line); padding-top: 16px; }
@media (max-width: 640px) { .wrap { padding: 24px 16px 72px; } table { font-size: 0.82rem; } }
"""


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _verdict_span(verdict: str) -> str:
    return f'<span class="v v-{_esc(verdict)}">{_esc(verdict)}</span>'


def _comparison_html(comparison: Comparison) -> str:
    rows = (
        ("Requests", "requests"),
        ("Reached the tool", "downstream_requests"),
        ("Gateway dispatches", "gateway_dispatches"),
        ("Downstream effects", "downstream_effects"),
        ("Duplicate effects", "duplicate_effects"),
        ("Known final outcome", "known_final_outcome"),
        ("Explicitly uncertain", "explicit_uncertain"),
        ("Unresolved operations", "unresolved_operations"),
        ("Gateway debits", "gateway_debits"),
        ("Gateway refunds", "gateway_refunds"),
        ("Receipts", "receipts"),
    )
    ran = [
        column
        for column in comparison.columns
        if column.ran or column.verdict == Verdict.ERROR.value
    ]
    header = "".join(f"<th>{_esc(column.label)}</th>" for column in ran)
    body = []
    for title, attribute in rows:
        cells = "".join(
            f'<td class="num">{_esc(_format(getattr(column, attribute)) if column.ran else "n/a")}</td>'
            for column in ran
        )
        body.append(f"<tr><th>{_esc(title)}</th>{cells}</tr>")
    verdict_cells = "".join(
        f"<td>{_verdict_span(column.verdict)}</td>" for column in ran
    )
    body.append(f"<tr><th>Verdict</th>{verdict_cells}</tr>")

    mismatch_html = ""
    if not comparison.matches_expectation:
        items = "".join(
            f"<li>{_esc(m['configuration'])}: documented "
            f"{_esc(m['expected'])}, observed {_esc(m['observed'])}</li>"
            for m in comparison.expectation_mismatches
        )
        mismatch_html = (
            '<div class="note"><strong>Differs from the documented '
            f"expectation.</strong><ul>{items}</ul></div>"
        )

    latency = comparison.additional_latency
    if latency["p50_ms"] is None:
        latency_html = "<p>Not measured: a baseline column did not run.</p>"
    else:
        latency_html = (
            f"<p>p50 {latency['p50_ms']:+.3f} ms, p95 {latency['p95_ms']:+.3f} ms, "
            f"measured against {_esc(latency['baseline'])}.</p>"
        )

    risks = (
        "".join(f"<li>{_esc(risk)}</li>" for risk in comparison.remaining_risks)
        or "<li>none recorded by this test</li>"
    )
    limitations = "".join(f"<li>{_esc(item)}</li>" for item in comparison.limitations)
    differences = ""
    if comparison.conclusion.non_prevention_differences:
        items = "".join(
            f"<li>{_esc(item)}</li>"
            for item in comparison.conclusion.non_prevention_differences
        )
        differences = (
            "<h3>What the gateway added, other than duplicate prevention</h3>"
            f"<ul>{items}</ul>"
        )
    if comparison.conclusion.gateway_disadvantages:
        items = "".join(
            f"<li>{_esc(item)}</li>"
            for item in comparison.conclusion.gateway_disadvantages
        )
        differences += (
            f"<h3>What it cost, measured against the same baseline</h3><ul>{items}</ul>"
        )
    observations = "".join(
        f"<li><strong>{_esc(column.label)}:</strong> {_esc(column.observation)}</li>"
        for column in ran
    )

    return f"""
<section>
  <h2>{_esc(comparison.test_id)} — {_esc(comparison.title)} {_verdict_span(comparison.verdict)}</h2>
  <p class="sub">{_esc(comparison.claim)}</p>
  {mismatch_html}
  <table><thead><tr><th></th>{header}</tr></thead><tbody>{"".join(body)}</tbody></table>
  <h3>What each configuration observed</h3>
  <ul>{observations}</ul>
  <h3>Additional latency</h3>
  {latency_html}
  <h3>Remaining risks</h3>
  <ul>{risks}</ul>
  <h3>What this test does not establish</h3>
  <ul>{limitations}</ul>
  <div class="card">
    <h3>Conclusion</h3>
    <p>{_esc(comparison.conclusion.text)}</p>
    {differences}
  </div>
</section>
"""


def render_run_html(
    comparisons: list[Comparison], *, environment: dict[str, Any]
) -> str:
    meta_items = [
        ("Run id", environment.get("run_id", "unknown")),
        ("Started", environment.get("started_at", "unknown")),
        ("Test definitions", environment.get("test_definition_version", "unknown")),
        ("Gateway commit", environment.get("gateway_commit", "unknown")),
        ("Python", environment.get("python_version", "unknown")),
        ("Random seed", environment.get("seed", "not used")),
        ("Traffic source", environment.get("traffic_source", "unknown")),
    ]
    meta_html = "".join(
        f"<div><dt>{_esc(name)}</dt><dd>{_esc(value)}</dd></div>"
        for name, value in meta_items
    )
    index_rows = "".join(
        f"<tr><td>{_esc(c.test_id)}</td><td>{_esc(c.title)}</td>"
        f"<td>{_verdict_span(c.verdict)}</td>"
        f"<td>{_esc(c.conclusion.kind.value)}</td></tr>"
        for c in comparisons
    )
    sections = "".join(_comparison_html(comparison) for comparison in comparisons)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent Gateway Failure Lab</title>
<style>{_CSS}</style></head>
<body><div class="wrap">
<h1>Agent Gateway Failure Lab</h1>
<p class="sub">What happened when an agent's tool executed and its response disappeared —
and what happened in the same situation with correct native controls, and with
the gateway in front of them.</p>
<dl class="meta">{meta_html}</dl>
<h2>Scenarios</h2>
<table><thead><tr><th>Test</th><th>Title</th><th>Verdict</th><th>Conclusion</th></tr></thead>
<tbody>{index_rows}</tbody></table>
<div class="note">Every number on this page was counted by an instrument. Downstream
effects come from the simulated tool's own ledger, which the gateway cannot
reach; dispatch counts come from the fault-injection layer between them. There
is no risk score, a scenario in which the gateway added nothing is reported as
exactly that, and a scenario whose baseline columns did not run is reported as
<em>no comparison</em> rather than as a baseline that succeeded.</div>
{sections}
<footer>Generated by the Agent Gateway Failure Lab. Test definitions
{_esc(environment.get("test_definition_version", "unknown"))}. Reproduce with
<code>{_esc(environment.get("reproduction_command", "make failure-lab"))}</code>.</footer>
</div></body></html>
"""


def render_run_json(
    comparisons: list[Comparison], *, environment: dict[str, Any]
) -> str:
    return json.dumps(
        {
            "environment": environment,
            "comparisons": [comparison.as_dict() for comparison in comparisons],
        },
        indent=2,
        sort_keys=True,
    )


__all__ = [
    "CONCLUSION_GLOSS",
    "Comparison",
    "ConclusionKind",
    "ConfigurationView",
    "INTENDED_EXECUTIONS",
    "build_comparison",
    "render_comparison_text",
    "render_run_html",
    "render_run_json",
    "render_run_text",
]
