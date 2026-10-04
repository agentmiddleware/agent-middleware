"""Turn a lab run into the one answer a visitor came for.

Everything here is derived from :class:`~failure_lab.report.ConclusionKind`
values that were themselves computed from instrument counts. Nothing in this
module decides what a run *means* -- it decides how to say what the run already
concluded, in a fixed vocabulary, in a fixed order of precedence.

Three rules shape the whole module and each of them is a rule against the
obvious temptation:

1. **A failure of the product outranks anything good the product did.** If any
   scenario reached ``gateway_did_not_hold``, that is the headline, above every
   duplicate the gateway prevented in the same run. A page that leads with the
   win and footnotes the failure is an advertisement.

2. **"You may not need us" is a real answer with its own headline.** It is not
   a softened version of a recommendation and it is not followed by a reason to
   buy anyway. The PRD calls this result a feature; a feature has to be
   reachable, so :func:`headline_for` has a branch that reaches it and a test
   that drives it.

3. **A test with no baseline says nothing about whether you need us.** Nine of
   the fourteen P0 scenarios exercise a component only the gateway has, so no
   baseline column runs. Those scenarios can establish that the gateway held
   its own guarantee and they can establish nothing at all about the visitor's
   own integration. That distinction survives into the headline rather than
   being flattened into either direction.

There is no score. Not a risk score, not a readiness score, not a percentage,
not a grade. The PRD forbids manufacturing one, and a number between 0 and 100
is exactly the kind of thing a reader trusts more than the measurements under
it. :func:`as_dict` emits counts of scenarios, which are facts, and no derived
index over them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from failure_lab.report import CONCLUSION_GLOSS, Comparison, ConclusionKind
from failure_lab.scenarios.base import Verdict


class Answer(str, Enum):
    """The visitor-facing outcomes, in order of precedence.

    Precedence is a property of the *type*, not of the caller: whichever
    branch of :func:`headline_for` fires, the enum member it returns carries
    its own rank, so a renderer cannot reorder them by accident.
    """

    #: A guarantee the product documents did not hold in this run.
    GATEWAY_DID_NOT_HOLD = "gateway_did_not_hold"
    #: Duplicate business effects occurred in a measured baseline and did not
    #: occur behind the gateway, in the same failure, at the same instruments.
    GATEWAY_PREVENTED_DUPLICATES = "gateway_prevented_duplicates"
    #: No additional duplicate was prevented; what changed is what the caller
    #: can know and prove afterwards.
    GATEWAY_CHANGED_EVIDENCE_ONLY = "gateway_changed_evidence_only"
    #: The measured baseline handled every failure this run injected.
    YOU_MAY_NOT_NEED_US = "you_may_not_need_us"
    #: Only gateway-only components were exercised; no baseline ran.
    NO_COMPARISON_WAS_MADE = "no_comparison_was_made"
    #: Nothing ran, or nothing ran far enough to say anything.
    NOTHING_ESTABLISHED = "nothing_established"

    @property
    def rank(self) -> int:
        return _ANSWER_ORDER.index(self)

    @property
    def recommends_the_product(self) -> bool:
        """Whether this answer points the visitor towards buying anything.

        Exposed so a renderer can decide where to put an offer without
        re-deriving the rule, and so a test can assert that the two answers
        which do not recommend the product never carry one.
        """
        return self is Answer.GATEWAY_PREVENTED_DUPLICATES


_ANSWER_ORDER: tuple[Answer, ...] = (
    Answer.GATEWAY_DID_NOT_HOLD,
    Answer.GATEWAY_PREVENTED_DUPLICATES,
    Answer.GATEWAY_CHANGED_EVIDENCE_ONLY,
    Answer.YOU_MAY_NOT_NEED_US,
    Answer.NO_COMPARISON_WAS_MADE,
    Answer.NOTHING_ESTABLISHED,
)

#: The short line at the top of the page. Plain, and in two of the six cases
#: plainly against the product's interest.
ANSWER_HEADLINES: dict[Answer, str] = {
    Answer.GATEWAY_DID_NOT_HOLD: (
        "Agent Middleware did not hold one of its own guarantees in this run."
    ),
    Answer.GATEWAY_PREVENTED_DUPLICATES: (
        "Duplicate business effects happened in the baseline and did not "
        "happen behind Agent Middleware."
    ),
    Answer.GATEWAY_CHANGED_EVIDENCE_ONLY: (
        "No duplicate business effect was prevented. What changed is what the "
        "caller can know and prove afterwards."
    ),
    Answer.YOU_MAY_NOT_NEED_US: (
        "You may not need us. Every failure this run injected was handled by "
        "the baseline on its own."
    ),
    Answer.NO_COMPARISON_WAS_MADE: (
        "No comparison was made. This run exercised only components a direct "
        "integration does not have."
    ),
    Answer.NOTHING_ESTABLISHED: (
        "This run established nothing. No scenario produced a usable result."
    ),
}

#: The paragraph under the headline. Each one says what the run showed and,
#: just as explicitly, what it did not.
ANSWER_DETAIL: dict[Answer, str] = {
    Answer.GATEWAY_DID_NOT_HOLD: (
        "At least one scenario checked a property Agent Middleware documents "
        "and the property did not hold. That result is reported here above "
        "anything else the same run found, including any duplicate the "
        "gateway did prevent. The scenario's own page says which property, "
        "which configuration, and what the instruments counted."
    ),
    Answer.GATEWAY_PREVENTED_DUPLICATES: (
        "The same workload, under the same injected failure, produced more "
        "downstream business effects without the gateway than with it. The "
        "effect counts come from the downstream tool's own ledger, which the "
        "gateway cannot read or write. This says nothing about failures this "
        "run did not inject."
    ),
    Answer.GATEWAY_CHANGED_EVIDENCE_ONLY: (
        "The baseline and the gateway produced the same number of downstream "
        "business effects. What differed is what the caller could know "
        "afterwards and what a third party could check. Whether that is worth "
        "anything to you is a judgement this page will not make for you, and "
        "the cost side of the same comparison is listed with it."
    ),
    Answer.YOU_MAY_NOT_NEED_US: (
        "A correct baseline integration was run against every failure this "
        "check injected, and it handled all of them without a gateway in "
        "front of it. If your downstream honours a durable operation id the "
        "way that baseline does, these failure modes are already covered for "
        "you. This is a real result, not a preamble to a recommendation."
    ),
    Answer.NO_COMPARISON_WAS_MADE: (
        "Every scenario in this run exercises something only the gateway has: "
        "a permit, a spending budget, a durable dispatch claim. There is no "
        "baseline column to compare against, so this run can report whether "
        "the gateway held its own guarantees and cannot tell you whether you "
        "need it. Run a check that includes a baseline scenario for that."
    ),
    Answer.NOTHING_ESTABLISHED: (
        "No scenario ran to a usable result, so there is nothing to report. "
        "This is a failure of the check, not a finding about anything."
    ),
}


@dataclass(frozen=True)
class ScenarioRow:
    """One line of the visitor-facing table."""

    test_id: str
    title: str
    claim: str
    verdict: str
    conclusion_kind: str
    conclusion_gloss: str
    conclusion_text: str
    matches_expectation: bool
    duplicates_prevented_vs_native: int
    duplicates_prevented_vs_existing: int
    #: What the gateway added that is not duplicate prevention.
    added: list[str] = field(default_factory=list)
    #: What it cost. Rendered next to ``added``, never below the fold.
    cost: list[str] = field(default_factory=list)
    remaining_risks: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class DiagnosticAnswer:
    """Everything the result page shows, already decided."""

    answer: Answer
    headline: str
    detail: str
    rows: list[ScenarioRow]
    #: Scenario counts. Facts, not an index over facts.
    counts: dict[str, int]
    #: Failures to state before anything else on the page.
    gateway_failures: list[str]
    #: What this run did not test, in the visitor's own terms.
    not_tested: list[str]
    limitations: list[str]

    @property
    def recommends_the_product(self) -> bool:
        return self.answer.recommends_the_product

    def as_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer.value,
            "headline": self.headline,
            "detail": self.detail,
            "recommends_the_product": self.recommends_the_product,
            "scenarios": [row.as_dict() for row in self.rows],
            "counts": dict(self.counts),
            "gateway_failures": list(self.gateway_failures),
            "not_tested": list(self.not_tested),
            "limitations": list(self.limitations),
            "score": None,
            "score_note": (
                "No score is computed. Every number on this page is a count "
                "taken by a named instrument; a single index over them would "
                "be easier to trust and impossible to check."
            ),
        }


#: What a run of this lab never establishes, whatever it concludes. Shown on
#: every result, including -- especially -- a result that favours the product.
STANDING_LIMITATIONS: tuple[str, ...] = (
    "This check runs against a simulated refund tool inside a sandbox on this "
    "machine. It does not touch your systems, your credentials or any real "
    "money, and it therefore says nothing about your specific downstream.",
    "Only the failures this run injected were tested. A failure mode absent "
    "from the scenario list was not shown to be handled; it was not tested.",
    "The baseline is a correct native integration, built to succeed. It is "
    "not a straw man, and where it wins that is reported as a win.",
    "A signed receipt establishes what the gateway recorded. It is not "
    "evidence that the downstream business action occurred; those are "
    "reported as separate claims wherever receipts appear.",
)


def _rows(comparisons: list[Comparison]) -> list[ScenarioRow]:
    return [
        ScenarioRow(
            test_id=comparison.test_id,
            title=comparison.title,
            claim=comparison.claim,
            verdict=comparison.verdict,
            conclusion_kind=comparison.conclusion.kind.value,
            conclusion_gloss=CONCLUSION_GLOSS.get(comparison.conclusion.kind.value, ""),
            conclusion_text=comparison.conclusion.text,
            matches_expectation=comparison.matches_expectation,
            duplicates_prevented_vs_native=(
                comparison.conclusion.duplicates_prevented_vs_native
            ),
            duplicates_prevented_vs_existing=(
                comparison.conclusion.duplicates_prevented_vs_existing
            ),
            added=list(comparison.conclusion.non_prevention_differences),
            cost=list(comparison.conclusion.gateway_disadvantages),
            remaining_risks=list(comparison.remaining_risks),
            limitations=list(comparison.limitations),
        )
        for comparison in comparisons
    ]


def headline_for(comparisons: list[Comparison]) -> Answer:
    """Pick the one answer, by precedence, from what the scenarios concluded.

    The order is deliberate and is the whole point of the function. A run that
    both prevented a duplicate and failed one of its own guarantees reports the
    failure: a product's own broken promise is more important to the reader
    than a promise it kept.
    """
    if not comparisons:
        return Answer.NOTHING_ESTABLISHED

    kinds = [comparison.conclusion.kind for comparison in comparisons]

    if ConclusionKind.GATEWAY_DID_NOT_HOLD in kinds:
        return Answer.GATEWAY_DID_NOT_HOLD
    if ConclusionKind.GATEWAY_PREVENTED_DUPLICATES in kinds:
        return Answer.GATEWAY_PREVENTED_DUPLICATES

    baseline_sufficient = {
        ConclusionKind.NATIVE_CONTROLS_SUFFICIENT,
        ConclusionKind.EXISTING_INTEGRATION_SUFFICIENT,
    }
    if ConclusionKind.GATEWAY_ADDED_EVIDENCE_ONLY in kinds:
        return Answer.GATEWAY_CHANGED_EVIDENCE_ONLY
    if any(kind in baseline_sufficient for kind in kinds):
        return Answer.YOU_MAY_NOT_NEED_US
    if any(kind is ConclusionKind.NO_BASELINE_COMPARISON for kind in kinds):
        return Answer.NO_COMPARISON_WAS_MADE
    return Answer.NOTHING_ESTABLISHED


def _not_tested(comparisons: list[Comparison]) -> list[str]:
    """Name what this particular run left out, from the run itself.

    Generic caveats are in :data:`STANDING_LIMITATIONS`. These are the ones
    that depend on which scenarios actually ran, so a short check cannot
    silently present itself as a thorough one.
    """
    notes: list[str] = []
    kinds = [comparison.conclusion.kind for comparison in comparisons]
    compared = [
        kind
        for kind in kinds
        if kind
        in (
            ConclusionKind.NATIVE_CONTROLS_SUFFICIENT,
            ConclusionKind.EXISTING_INTEGRATION_SUFFICIENT,
            ConclusionKind.GATEWAY_PREVENTED_DUPLICATES,
            ConclusionKind.GATEWAY_ADDED_EVIDENCE_ONLY,
        )
    ]
    if not compared:
        notes.append(
            "No scenario in this run compared the gateway against a baseline, "
            "so nothing here bears on whether your own integration would have "
            "survived these failures."
        )
    gateway_only = sum(
        1 for kind in kinds if kind is ConclusionKind.NO_BASELINE_COMPARISON
    )
    if gateway_only and compared:
        notes.append(
            f"{gateway_only} of the {len(kinds)} scenarios exercised components "
            "only the gateway has, so they carry no baseline comparison and "
            "were not counted towards the answer above."
        )
    unexpected = [c.test_id for c in comparisons if not c.matches_expectation]
    if unexpected:
        notes.append(
            "These scenarios diverged from their documented expectation and "
            "should be read before anything else on this page: "
            + ", ".join(unexpected)
            + ". A divergence means the documentation and the measurement "
            "disagree; it does not mean the measurement is wrong."
        )
    return notes


def build_answer(comparisons: list[Comparison]) -> DiagnosticAnswer:
    """The whole visitor-facing result, derived and never composed by hand."""
    answer = headline_for(comparisons)
    rows = _rows(comparisons)

    counts: dict[str, int] = {"scenarios": len(comparisons)}
    for comparison in comparisons:
        key = comparison.conclusion.kind.value
        counts[key] = counts.get(key, 0) + 1
    counts["failed_gateway_guarantees"] = sum(
        1
        for c in comparisons
        if c.conclusion.kind is ConclusionKind.GATEWAY_DID_NOT_HOLD
    )
    # Kept as two numbers, never collapsed into one, because they answer two
    # different questions and the collapsed version reads as a contradiction.
    # Against a naive integration the gateway routinely prevents duplicates;
    # so does simply fixing the downstream, which is what the native column
    # measures. Only the second bears on whether the gateway is needed, and a
    # single "duplicates prevented: 1" printed under the headline "you may not
    # need us" invites the reader to believe the page is arguing with itself.
    counts["duplicates_prevented_vs_a_correct_native_integration"] = sum(
        c.conclusion.duplicates_prevented_vs_native for c in comparisons
    )
    counts["duplicates_prevented_vs_an_unprotected_integration"] = sum(
        c.conclusion.duplicates_prevented_vs_existing for c in comparisons
    )
    counts["diverged_from_documentation"] = sum(
        1 for c in comparisons if not c.matches_expectation
    )

    gateway_failures = [
        f"{c.test_id} — {c.title}: {c.conclusion.text}"
        for c in comparisons
        if c.conclusion.kind is ConclusionKind.GATEWAY_DID_NOT_HOLD
        or c.verdict == Verdict.ERROR.value
    ]

    limitations: list[str] = list(STANDING_LIMITATIONS)
    seen = set(limitations)
    for comparison in comparisons:
        for limitation in comparison.limitations:
            if limitation not in seen:
                seen.add(limitation)
                limitations.append(limitation)

    return DiagnosticAnswer(
        answer=answer,
        headline=ANSWER_HEADLINES[answer],
        detail=ANSWER_DETAIL[answer],
        rows=rows,
        counts=counts,
        gateway_failures=gateway_failures,
        not_tested=_not_tested(comparisons),
        limitations=limitations,
    )


__all__ = [
    "ANSWER_DETAIL",
    "ANSWER_HEADLINES",
    "STANDING_LIMITATIONS",
    "Answer",
    "DiagnosticAnswer",
    "ScenarioRow",
    "build_answer",
    "headline_for",
]
