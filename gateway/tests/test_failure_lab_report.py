"""The report must be able to argue against the product it reports on.

Two failure modes matter here and neither is cosmetic. A report that cannot
reach the conclusion "the gateway added nothing" is an advertisement with a
table in it. A report that lists only what the gateway added, and never what
it cost, is the same thing with better manners.

These build comparisons from synthetic results rather than running the
gateway, so the rendering contract is pinned cheaply and independently of any
scenario's behaviour.
"""

from __future__ import annotations

import re

import pytest

from failure_lab.configurations import CONFIGURATION_LABELS, Configuration
from failure_lab.report import (
    ConclusionKind,
    build_comparison,
    render_comparison_text,
    render_run_html,
    render_run_json,
)
from failure_lab.scenarios.base import (
    ConfigurationResult,
    Counters,
    ScenarioResult,
    Verdict,
)

ENVIRONMENT = {
    "run_id": "test-run",
    "started_at": "2026-09-19T00:00:00Z",
    "test_definition_version": "2026.09.1",
    "gateway_commit": "0" * 40,
    "python_version": "3.12",
    "seed": "1",
    "traffic_source": "internal_test",
    "reproduction_command": "make failure-lab",
}


def _entry(
    configuration: Configuration,
    *,
    verdict: Verdict,
    executions: int,
    dispatches: int | None = None,
    downstream_requests: int | None = None,
    known: int = 0,
    uncertain: int = 0,
    unresolved: int = 0,
    receipts: int | None = None,
    debits: int | None = None,
    p50: float | None = None,
) -> ConfigurationResult:
    return ConfigurationResult(
        configuration=configuration.value,
        label=CONFIGURATION_LABELS[configuration],
        verdict=verdict,
        expectation=verdict.value,
        observation=f"{configuration.value} observed {executions} execution(s)",
        counters=Counters(
            incoming_requests=2,
            accepted_requests=2,
            downstream_requests=(
                downstream_requests if downstream_requests is not None else executions
            ),
            downstream_executions=executions,
            gateway_dispatches=dispatches,
            gateway_debits=debits,
            gateway_refunds=0 if debits is not None else None,
            receipts=receipts,
            known_final_outcome=known,
            explicit_uncertain=uncertain,
            unresolved=unresolved,
            latency_p50_ms=p50,
            latency_p95_ms=p50,
        ),
    )


def _result(
    entries: list[ConfigurationResult], *, verdict: Verdict = Verdict.PASS
) -> ScenarioResult:
    return ScenarioResult(
        test_id="TXX",
        title="Synthetic",
        claim="A synthetic claim long enough to read like a sentence about behaviour.",
        definition_version="2026.09.1",
        definition_hash="f" * 64,
        started_at="2026-09-19T00:00:00Z",
        finished_at="2026-09-19T00:00:01Z",
        configurations=entries,
        limitations=["a stated limitation"],
        events=[],
        expected={entry.configuration: entry.verdict.value for entry in entries},
    )


def test_the_gateway_adding_nothing_is_a_reachable_conclusion():
    """The result the PRD requires to be implementable, implemented."""
    result = _result(
        [
            _entry(
                Configuration.DIRECT_NAIVE,
                verdict=Verdict.OBSERVED,
                executions=2,
                known=1,
                unresolved=1,
            ),
            _entry(
                Configuration.DIRECT_NATIVE,
                verdict=Verdict.PASS,
                executions=1,
                known=1,
                unresolved=1,
            ),
            _entry(
                Configuration.GATEWAY_NATIVE,
                verdict=Verdict.PASS,
                executions=1,
                dispatches=1,
                downstream_requests=1,
                uncertain=2,
                receipts=1,
                debits=1,
            ),
        ]
    )
    comparison = build_comparison(result)

    assert comparison.conclusion.kind is ConclusionKind.NATIVE_CONTROLS_SUFFICIENT
    assert comparison.conclusion.duplicates_prevented_vs_native == 0
    assert (
        "did not observe an additional duplicate effect" in comparison.conclusion.text
    )


def test_an_already_correct_existing_integration_is_told_so():
    result = _result(
        [
            _entry(
                Configuration.DIRECT_NAIVE, verdict=Verdict.PASS, executions=1, known=2
            ),
            _entry(
                Configuration.DIRECT_NATIVE, verdict=Verdict.PASS, executions=1, known=2
            ),
            _entry(
                Configuration.GATEWAY_NATIVE,
                verdict=Verdict.PASS,
                executions=1,
                dispatches=1,
                known=2,
            ),
        ]
    )
    conclusion = build_comparison(result).conclusion
    assert conclusion.kind is ConclusionKind.EXISTING_INTEGRATION_SUFFICIENT
    assert "handled this test correctly" in conclusion.text


def test_a_real_prevention_is_reported_as_one():
    result = _result(
        [
            _entry(Configuration.DIRECT_NAIVE, verdict=Verdict.OBSERVED, executions=2),
            _entry(Configuration.DIRECT_NATIVE, verdict=Verdict.OBSERVED, executions=2),
            _entry(
                Configuration.GATEWAY_NATIVE,
                verdict=Verdict.PASS,
                executions=1,
                dispatches=1,
                receipts=1,
                debits=1,
            ),
        ]
    )
    conclusion = build_comparison(result).conclusion
    assert conclusion.kind is ConclusionKind.GATEWAY_PREVENTED_DUPLICATES
    assert conclusion.duplicates_prevented_vs_native == 1


def test_a_gateway_that_did_not_hold_is_not_dressed_up():
    result = _result(
        [
            _entry(Configuration.DIRECT_NAIVE, verdict=Verdict.OBSERVED, executions=2),
            _entry(Configuration.DIRECT_NATIVE, verdict=Verdict.PASS, executions=1),
            _entry(
                Configuration.GATEWAY_NATIVE,
                verdict=Verdict.FAIL,
                executions=2,
                dispatches=2,
            ),
        ],
        verdict=Verdict.FAIL,
    )
    conclusion = build_comparison(result).conclusion
    assert conclusion.kind is ConclusionKind.GATEWAY_DID_NOT_HOLD


def test_the_cost_side_is_reported_not_only_the_benefit():
    """The baseline resolving an outcome the gateway could not is a cost."""
    result = _result(
        [
            _entry(
                Configuration.DIRECT_NAIVE,
                verdict=Verdict.OBSERVED,
                executions=2,
                known=1,
                unresolved=1,
            ),
            _entry(
                Configuration.DIRECT_NATIVE,
                verdict=Verdict.PASS,
                executions=1,
                known=2,
                p50=10.0,
            ),
            _entry(
                Configuration.GATEWAY_NATIVE,
                verdict=Verdict.PASS,
                executions=1,
                dispatches=1,
                downstream_requests=1,
                known=0,
                uncertain=2,
                receipts=1,
                debits=1,
                p50=120.0,
            ),
        ]
    )
    conclusion = build_comparison(result).conclusion

    assert conclusion.gateway_disadvantages, "the report listed no cost at all"
    joined = " ".join(conclusion.gateway_disadvantages)
    assert "confirmed outcome" in joined
    assert "retained" in joined
    assert "latency" in joined


def test_load_shielding_is_credited_when_it_happens():
    result = _result(
        [
            _entry(
                Configuration.DIRECT_NAIVE,
                verdict=Verdict.OBSERVED,
                executions=100,
                downstream_requests=100,
            ),
            _entry(
                Configuration.DIRECT_NATIVE,
                verdict=Verdict.PASS,
                executions=1,
                downstream_requests=100,
                known=100,
            ),
            _entry(
                Configuration.GATEWAY_NATIVE,
                verdict=Verdict.PASS,
                executions=1,
                dispatches=1,
                downstream_requests=1,
                known=100,
                receipts=1,
                debits=1,
            ),
        ]
    )
    added = " ".join(build_comparison(result).conclusion.non_prevention_differences)
    assert "absorbed before reaching the tool" in added


@pytest.fixture
def rendered() -> tuple[str, str]:
    result = _result(
        [
            _entry(
                Configuration.DIRECT_NAIVE,
                verdict=Verdict.OBSERVED,
                executions=2,
                known=1,
                unresolved=1,
            ),
            _entry(
                Configuration.DIRECT_NATIVE, verdict=Verdict.PASS, executions=1, known=2
            ),
            _entry(
                Configuration.GATEWAY_NATIVE,
                verdict=Verdict.PASS,
                executions=1,
                dispatches=1,
                downstream_requests=1,
                uncertain=2,
                receipts=1,
                debits=1,
            ),
        ]
    )
    comparison = build_comparison(result)
    return render_comparison_text(comparison), render_run_html(
        [comparison], environment=ENVIRONMENT
    )


def test_no_python_leaks_into_the_rendered_artifacts(rendered):
    """A reader of a report does not have the source. Do not show them enums."""
    text, html = rendered
    assert "Verdict." not in text
    assert "Verdict." not in html
    assert "Configuration." not in html


def test_the_report_assigns_no_risk_score(rendered):
    """A single invented number would let a reader skip the measurements.

    The renderings *say* there is no risk score, so a bare substring check
    would fail on the disclaimer. What must not exist is a score with a value
    attached to it.
    """
    text, html = rendered
    for rendering in (text.lower(), html.lower()):
        # Every mention of a score must be a denial that one exists.
        for match in re.finditer(r"(risk|safety)\s+score", rendering):
            preceding = rendering[max(0, match.start() - 12) : match.start()]
            assert "no " in preceding, f"an actual score appears: ...{preceding}"
        # And no score may carry a value.
        assert not re.search(r"(risk|safety)\s+score\s*[:=]\s*\d", rendering)
        assert not re.search(r"\bscore\b\s*[:=]\s*[\d.]+", rendering)
        assert not re.search(r"\bgrade\b\s*[:=]\s*[a-f]\b", rendering)


def test_the_html_stands_alone_and_works_in_both_themes(rendered):
    _, html = rendered
    assert 'name="viewport"' in html
    assert "@media (prefers-color-scheme: dark)" in html
    assert ':root:not([data-theme="light"])' in html
    assert re.search(r"body\s*\{[^}]*background:", html)
    # No external assets: everything must render from the file itself.
    head = html.split("<style>")[0]
    assert "http://" not in head and "https://" not in head
    assert "cdn" not in html.lower()


def test_the_json_rendering_round_trips(rendered):
    import json

    result = _result(
        [_entry(Configuration.DIRECT_NAIVE, verdict=Verdict.OBSERVED, executions=2)]
    )
    document = json.loads(
        render_run_json([build_comparison(result)], environment=ENVIRONMENT)
    )
    assert document["environment"]["run_id"] == "test-run"
    assert document["comparisons"][0]["conclusion"]["kind"]
    assert "gateway_disadvantages" in document["comparisons"][0]["conclusion"]


def test_a_baseline_that_never_ran_is_never_reported_as_one_that_succeeded():
    """The mirror image of an advertisement, and just as false.

    Nine of the fourteen P0 scenarios exercise a component only the gateway
    has -- a permit, a budget, a durable dispatch claim -- so both direct
    configurations are NOT_APPLICABLE and no baseline column runs. Reporting
    those as ``native_controls_sufficient`` told a reader that their own
    controls already cover crash recovery and permit revocation, on the
    strength of a comparison that never happened. Zero duplicates prevented
    against a baseline that did not run is an absence of data, not a finding.
    """
    result = _result(
        [
            ConfigurationResult(
                configuration=Configuration.DIRECT_NAIVE.value,
                label=CONFIGURATION_LABELS[Configuration.DIRECT_NAIVE],
                verdict=Verdict.NOT_APPLICABLE,
                expectation=Verdict.NOT_APPLICABLE.value,
                observation="a direct integration has no gateway to crash",
                counters=Counters(),
            ),
            ConfigurationResult(
                configuration=Configuration.DIRECT_NATIVE.value,
                label=CONFIGURATION_LABELS[Configuration.DIRECT_NATIVE],
                verdict=Verdict.NOT_APPLICABLE,
                expectation=Verdict.NOT_APPLICABLE.value,
                observation="a direct integration has no gateway to crash",
                counters=Counters(),
            ),
            _entry(
                Configuration.GATEWAY_NATIVE,
                verdict=Verdict.PASS,
                executions=1,
                dispatches=1,
                downstream_requests=1,
                known=2,
                receipts=1,
                debits=1,
            ),
        ]
    )
    conclusion = build_comparison(result).conclusion

    assert conclusion.kind is ConclusionKind.NO_BASELINE_COMPARISON
    assert conclusion.kind is not ConclusionKind.NATIVE_CONTROLS_SUFFICIENT
    assert conclusion.kind is not ConclusionKind.EXISTING_INTEGRATION_SUFFICIENT
    # It must say what it does not establish, in both directions.
    assert "no comparison was made" in conclusion.text
    assert "would have handled this failure" in conclusion.text
    assert "would not" in conclusion.text
    # And it must not smuggle in a prevention count from unmeasured columns.
    assert conclusion.duplicates_prevented_vs_native == 0
    assert conclusion.duplicates_prevented_vs_existing == 0
    assert conclusion.non_prevention_differences == []


def test_every_conclusion_kind_has_a_plain_english_gloss():
    """A summary table that names a kind must be able to explain it."""
    from failure_lab.report import CONCLUSION_GLOSS

    for kind in ConclusionKind:
        gloss = CONCLUSION_GLOSS.get(kind.value, "")
        assert gloss, f"{kind.value} has no gloss for the summary block"
        assert not gloss.endswith("."), f"{kind.value} gloss should be a fragment"


@pytest.mark.parametrize("broken", ["baseline", "gateway", "both"])
def test_configuration_errors_do_not_support_positive_comparisons(broken):
    entries = [
        _entry(
            Configuration.DIRECT_NAIVE,
            verdict=Verdict.ERROR
            if broken in ("baseline", "both")
            else Verdict.OBSERVED,
            executions=0 if broken in ("baseline", "both") else 2,
        ),
        _entry(
            Configuration.GATEWAY_NATIVE,
            verdict=Verdict.ERROR if broken in ("gateway", "both") else Verdict.PASS,
            executions=0 if broken in ("gateway", "both") else 1,
        ),
    ]
    for entry in entries:
        if entry.verdict == Verdict.ERROR:
            entry.observation = "synthetic setup error"
    comparison = build_comparison(_result(entries))
    assert comparison.verdict == "ERROR"
    assert comparison.conclusion.kind is ConclusionKind.INCONCLUSIVE
    assert comparison.conclusion.duplicates_prevented_vs_existing == 0
    assert comparison.conclusion.duplicates_prevented_vs_native == 0
    assert comparison.conclusion.non_prevention_differences == []
    assert "synthetic setup error" in render_comparison_text(comparison)
    html = render_run_html([comparison], environment=ENVIRONMENT)
    assert "synthetic setup error" in html
    for column in comparison.columns:
        if column.verdict == "ERROR":
            assert not column.ran
            assert column.label in html


@pytest.mark.parametrize(
    "broken", [Configuration.DIRECT_NAIVE, Configuration.DIRECT_NATIVE]
)
def test_only_measured_baselines_support_comparisons(broken):
    entries = [
        _entry(
            configuration,
            verdict=Verdict.ERROR if configuration is broken else Verdict.OBSERVED,
            executions=0 if configuration is broken else 2,
            p50=10.0,
        )
        for configuration in (Configuration.DIRECT_NAIVE, Configuration.DIRECT_NATIVE)
    ]
    entries.append(
        _entry(
            Configuration.GATEWAY_NATIVE, verdict=Verdict.PASS, executions=1, p50=20.0
        )
    )
    comparison = build_comparison(_result(entries))
    assert comparison.conclusion.kind is ConclusionKind.GATEWAY_PREVENTED_DUPLICATES
    assert comparison.conclusion.duplicates_prevented_vs_existing == (
        0 if broken is Configuration.DIRECT_NAIVE else 1
    )
    assert comparison.conclusion.duplicates_prevented_vs_native == (
        0 if broken is Configuration.DIRECT_NATIVE else 1
    )
    assert comparison.additional_latency["p50_ms"] == (
        None if broken is Configuration.DIRECT_NATIVE else 10.0
    )


def test_gateway_error_is_not_hidden_by_another_gateway_configuration():
    comparison = build_comparison(
        _result(
            [
                _entry(
                    Configuration.DIRECT_NATIVE,
                    verdict=Verdict.OBSERVED,
                    executions=2,
                    p50=10.0,
                ),
                _entry(
                    Configuration.GATEWAY_NATIVE,
                    verdict=Verdict.ERROR,
                    executions=0,
                    p50=20.0,
                ),
                _entry(Configuration.GATEWAY_NAIVE, verdict=Verdict.PASS, executions=1),
            ]
        )
    )
    assert comparison.conclusion.kind is ConclusionKind.INCONCLUSIVE
    assert comparison.conclusion.duplicates_prevented_vs_native == 0
    assert comparison.additional_latency["p50_ms"] is None
