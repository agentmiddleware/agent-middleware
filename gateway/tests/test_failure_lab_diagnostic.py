"""The diagnostic must be able to argue against the thing it is selling.

Two classes of defect are load-bearing here and neither is cosmetic.

A diagnostic that cannot print "you may not need us" is a sales page with a
progress bar on it. The PRD calls that result a feature, and a feature that is
only promised in a docstring is not implemented, so these tests drive the
branch, assert the page it produces carries no offer, and assert that the one
answer which does carry an offer is the only one that can.

A diagnostic that will send failure traffic wherever it is pointed is a
denial-of-service tool with a friendly name. These tests assert that both
authorizations are required, that neither substitutes for the other, that a
caller who follows the endpoint's own instructions reaches its real answer, and
that a credential is refused before anything parses it.

Most cases drive the decision and refusal layers from synthetic comparisons,
because those are the layers that can silently start lying. The three that
drive real runs do so in a subprocess, for a reason that is itself part of the
design: see :data:`_TWO_RUNS_DRIVER`.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from starlette.requests import Request

from failure_lab.configurations import CONFIGURATION_LABELS, Configuration
from failure_lab.diagnostic import (
    OFFER,
    Answer,
    DiagnosticSettings,
    build_answer,
    create_app,
    headline_for,
    pages,
)
from failure_lab.report import build_comparison
from failure_lab.scenarios.base import (
    ConfigurationResult,
    Counters,
    ScenarioResult,
    Verdict,
)

#: The repository root, for the out-of-process driver below.
ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# Synthetic comparisons                                                         #
# --------------------------------------------------------------------------- #


def _entry(
    configuration: Configuration,
    *,
    verdict: Verdict,
    executions: int,
    dispatches: int | None = None,
    known: int = 0,
    uncertain: int = 0,
    unresolved: int = 0,
    receipts: int | None = None,
    debits: int | None = None,
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
            downstream_requests=executions,
            downstream_executions=executions,
            gateway_dispatches=dispatches,
            gateway_debits=debits,
            gateway_refunds=0 if debits is not None else None,
            receipts=receipts,
            known_final_outcome=known,
            explicit_uncertain=uncertain,
            unresolved=unresolved,
        ),
    )


def _not_applicable(configuration: Configuration) -> ConfigurationResult:
    return ConfigurationResult(
        configuration=configuration.value,
        label=CONFIGURATION_LABELS[configuration],
        verdict=Verdict.NOT_APPLICABLE,
        expectation=Verdict.NOT_APPLICABLE.value,
        observation="a direct integration has no such component",
        counters=Counters(),
    )


def _comparison(entries: list[ConfigurationResult], *, test_id: str = "TXX"):
    result = ScenarioResult(
        test_id=test_id,
        title="Synthetic scenario",
        claim="A synthetic claim, long enough to read like a sentence.",
        definition_version="2026.09.1",
        definition_hash="f" * 64,
        started_at="2026-09-19T00:00:00Z",
        finished_at="2026-09-19T00:00:01Z",
        configurations=entries,
        limitations=["a stated limitation"],
        events=[],
        expected={entry.configuration: entry.verdict.value for entry in entries},
    )
    return build_comparison(result)


def _baseline_wins():
    """A run where the correct native baseline handled everything."""
    return [
        _comparison(
            [
                _entry(
                    Configuration.DIRECT_NAIVE, verdict=Verdict.OBSERVED, executions=2
                ),
                _entry(
                    Configuration.DIRECT_NATIVE,
                    verdict=Verdict.PASS,
                    executions=1,
                    known=2,
                ),
                _entry(
                    Configuration.GATEWAY_NATIVE,
                    verdict=Verdict.PASS,
                    executions=1,
                    dispatches=1,
                    known=2,
                    receipts=1,
                    debits=1,
                ),
            ]
        )
    ]


def _gateway_prevents():
    """A run where the baseline duplicated and the gateway did not."""
    return [
        _comparison(
            [
                _entry(
                    Configuration.DIRECT_NAIVE, verdict=Verdict.OBSERVED, executions=3
                ),
                _entry(
                    Configuration.DIRECT_NATIVE, verdict=Verdict.OBSERVED, executions=2
                ),
                _entry(
                    Configuration.GATEWAY_NATIVE,
                    verdict=Verdict.PASS,
                    executions=1,
                    dispatches=1,
                    known=2,
                    receipts=1,
                    debits=1,
                ),
            ]
        )
    ]


def _gateway_fails():
    return [
        _comparison(
            [
                _entry(
                    Configuration.DIRECT_NAIVE, verdict=Verdict.OBSERVED, executions=2
                ),
                _entry(Configuration.DIRECT_NATIVE, verdict=Verdict.PASS, executions=1),
                _entry(
                    Configuration.GATEWAY_NATIVE,
                    verdict=Verdict.FAIL,
                    executions=2,
                    dispatches=2,
                    receipts=2,
                    debits=2,
                ),
            ]
        )
    ]


def _no_baseline():
    return [
        _comparison(
            [
                _not_applicable(Configuration.DIRECT_NAIVE),
                _not_applicable(Configuration.DIRECT_NATIVE),
                _entry(
                    Configuration.GATEWAY_NATIVE,
                    verdict=Verdict.PASS,
                    executions=1,
                    dispatches=1,
                    known=2,
                    receipts=1,
                    debits=1,
                ),
            ]
        )
    ]


class _Record:
    """The shape :func:`pages.render_result` reads, without a real run.

    ``render_result`` takes a ``RunRecord`` and is typed loosely so the
    renderer imports no run machinery. Building only the parts it reads keeps
    these cases at the decision layer instead of booting a gateway to assert
    what a template does.
    """

    def __init__(
        self, comparisons, *, state: str = "complete", error: str | None = None
    ):
        self.state = state
        self.error = error
        self.run_id = "synthetic-run"
        self.answer = build_answer(comparisons)
        self.comparisons = comparisons
        self.archive = None
        self.archive_note = "no archive was retained for this synthetic record"
        self.report_html = "<p>report</p>"
        self.environment = {
            "reproduction_command": "python -m failure_lab run --test T03",
            "seed_note": "seeded for reproducibility",
        }


def _html(comparisons) -> str:
    return pages.render_result(_Record(comparisons))


# --------------------------------------------------------------------------- #
# The answer the PRD requires to be reachable                                   #
# --------------------------------------------------------------------------- #


def test_you_may_not_need_us_is_reachable_and_says_so_plainly():
    answer = build_answer(_baseline_wins())

    assert answer.answer is Answer.YOU_MAY_NOT_NEED_US
    assert "may not need us" in answer.headline.lower()
    assert not answer.recommends_the_product


def test_the_page_for_that_answer_carries_no_offer():
    """The branch exists; the rendered page must not undo it."""
    html = _html(_baseline_wins())

    assert "may not need us" in html.lower()
    assert OFFER.headline not in html
    for item in OFFER.items:
        assert item.detail not in html


def test_a_prevention_result_is_the_only_one_that_carries_an_offer():
    prevented = build_answer(_gateway_prevents())
    assert prevented.answer is Answer.GATEWAY_PREVENTED_DUPLICATES
    assert prevented.recommends_the_product
    assert OFFER.headline in _html(_gateway_prevents())

    for comparisons in (_baseline_wins(), _gateway_fails(), _no_baseline()):
        answer = build_answer(comparisons)
        assert not answer.recommends_the_product, answer.answer
        assert OFFER.headline not in _html(comparisons)


def test_exactly_one_answer_recommends_the_product():
    """A second true case would be a second place to put a pitch."""
    recommending = [member for member in Answer if member.recommends_the_product]
    assert recommending == [Answer.GATEWAY_PREVENTED_DUPLICATES]


def test_a_failed_guarantee_outranks_a_prevention_in_the_same_run():
    """The product's own broken promise is the headline, not a footnote."""
    comparisons = _gateway_prevents() + _gateway_fails()
    assert headline_for(comparisons) is Answer.GATEWAY_DID_NOT_HOLD

    answer = build_answer(comparisons)
    html = _html(comparisons)
    # The failure block is physically above the answer card.
    assert html.index("Read this first") < html.index(answer.headline)


def test_a_run_with_no_baseline_does_not_claim_the_baseline_would_have_coped():
    answer = build_answer(_no_baseline())

    assert answer.answer is Answer.NO_COMPARISON_WAS_MADE
    assert "comparison" in answer.headline.lower()
    assert any("your own integration" in note for note in answer.not_tested)


def test_an_empty_run_establishes_nothing_rather_than_something_flattering():
    answer = build_answer([])
    assert answer.answer is Answer.NOTHING_ESTABLISHED
    assert not answer.recommends_the_product


def test_a_failed_run_renders_no_result_at_all():
    """A partial run is not a result, and rendering one anyway is worse."""
    html = pages.render_result(
        _Record(_baseline_wins(), state="failed", error="the sandbox never booted")
    )
    assert "the sandbox never booted" in html
    assert "may not need us" not in html.lower()


def test_every_answer_has_a_headline_and_a_detail():
    from failure_lab.diagnostic.answer import ANSWER_DETAIL, ANSWER_HEADLINES

    for member in Answer:
        assert ANSWER_HEADLINES[member]
        assert ANSWER_DETAIL[member]


# --------------------------------------------------------------------------- #
# No score, anywhere                                                            #
# --------------------------------------------------------------------------- #


#: A number that looks like a grade: "risk score: 73", "82/100".
_SCORE_WITH_A_VALUE = re.compile(
    r"(?i)\b(?:risk|safety|readiness|trust|health)\s*(?:score|rating|grade|index)\b"
    r"\s*[:=]?\s*\d"
    r"|\b\d{1,3}\s*/\s*100\b"
)


@pytest.mark.parametrize(
    "factory",
    [_baseline_wins, _gateway_prevents, _gateway_fails, _no_baseline],
    ids=["baseline_wins", "gateway_prevents", "gateway_fails", "no_baseline"],
)
def test_no_result_page_manufactures_a_score(factory):
    comparisons = factory()
    assert not _SCORE_WITH_A_VALUE.search(_html(comparisons)), "a score appeared"
    assert build_answer(comparisons).as_dict()["score"] is None


def test_duplicate_prevention_is_reported_against_each_baseline_separately():
    """One collapsed number reads as the page arguing with itself.

    In the lost-response scenario the unprotected integration duplicates and a
    correct native one does not, so the gateway prevents a duplicate relative
    to the former and none relative to the latter. A single "duplicates
    prevented: 1" printed under the headline "you may not need us" looks like a
    contradiction, and resolving it requires knowing which baseline the number
    was measured against. So both are reported and never their maximum.
    """
    counts = build_answer(_baseline_wins()).counts

    assert counts["duplicates_prevented_vs_a_correct_native_integration"] == 0
    assert counts["duplicates_prevented_vs_an_unprotected_integration"] == 1
    assert "duplicates_prevented" not in counts, "the collapsed count came back"


def test_every_conclusion_kind_has_a_plain_english_gloss():
    """A summary that names a kind must be able to explain it."""
    from failure_lab.report import CONCLUSION_GLOSS, ConclusionKind

    for kind in ConclusionKind:
        assert CONCLUSION_GLOSS.get(kind.value), f"{kind.value} has no gloss"


def test_a_signature_is_never_presented_as_proof_the_action_happened():
    """The rule the PRD states outright, asserted on a rendered page."""
    html = _html(_gateway_prevents())
    assert "signed receipt establishes what the gateway recorded" in html
    assert "not evidence that the downstream business action occurred" in html


# --------------------------------------------------------------------------- #
# The offer                                                                     #
# --------------------------------------------------------------------------- #


def test_the_offer_never_invents_a_price():
    """An unset price renders as unset, not as a plausible-looking number."""
    from failure_lab.diagnostic.offer import OFFER_PRICE, PRICE_NOT_PUBLISHED

    assert OFFER_PRICE is None, "a price was hardcoded into the repository"
    assert OFFER.price == PRICE_NOT_PUBLISHED

    html = _html(_gateway_prevents())
    assert PRICE_NOT_PUBLISHED in html
    assert not re.search(r"[$£€]\s?\d", html), "a currency amount appeared"


def test_the_offer_marks_what_a_human_has_to_size_first():
    """A trust plane that implies one-click production is lying about itself."""
    assert OFFER.needs_manual_review
    assert len([item for item in OFFER.items if item.manual_review]) >= 2
    assert OFFER.manual_review_note in _html(_gateway_prevents())


def test_the_offer_has_exactly_one_definition():
    """The renderer must not carry a second copy of the offer text."""
    body = Path(pages.__file__).read_text(encoding="utf-8")
    for item in OFFER.items:
        assert item.detail not in body, (
            f"{item.title!r} is spelled out in pages.py as well as offer.py"
        )


# --------------------------------------------------------------------------- #
# What the surface refuses to do                                                #
# --------------------------------------------------------------------------- #


def _client(**kwargs) -> TestClient:
    return TestClient(create_app(DiagnosticSettings(telemetry=False, **kwargs)))


def test_the_landing_page_refuses_a_score_and_says_nothing_leaves_the_machine():
    with _client() as client:
        body = client.get("/diagnostic").text.lower()
    assert "risk score" in body
    assert "nothing is sent off the machine" in body


def test_a_target_is_refused_on_the_sandbox_endpoint():
    with _client() as client:
        response = client.post(
            "/diagnostic/run", json={"target": "https://payments.example.com"}
        )
    assert response.status_code == 403
    assert "will not send a request" in response.json()["reason"]


def test_the_external_endpoint_does_not_exist_unless_it_was_asked_for():
    """Absent, not disabled. That is the difference from a feature flag."""
    with _client() as client:
        assert client.post("/diagnostic/external", json={}).status_code == 404


def test_the_server_flag_alone_does_not_authorize_an_individual_request():
    """Two authorizations, and neither stands in for the other."""
    with _client(allow_external_targets=True) as client:
        response = client.post(
            "/diagnostic/external", json={"target": "https://payments.example.com"}
        )
    assert response.status_code == 403
    assert "external_target_acknowledged" in response.json()["reason"]


def test_following_the_external_endpoints_own_instructions_reaches_its_real_answer():
    """The acknowledgement field must not be one the credential guard refuses.

    This surface rejects any submission whose text matches the redaction
    pattern, and that pattern matches "authorization" -- correctly, since an
    ``Authorization:`` header in a request body is exactly what it is for.
    While the field was called ``authorization_acknowledged`` the only value
    that satisfied the handler was one the guard refused first, so a caller who
    did what the error message said got a contradiction and the 501 below was
    unreachable. The guard was right; the name was wrong.
    """
    with _client(allow_external_targets=True) as client:
        response = client.post(
            "/diagnostic/external",
            json={
                "target": "https://payments.example.com",
                "external_target_acknowledged": True,
            },
        )
    assert response.status_code == 501, response.json()
    assert "nothing was sent" in response.json()["reason"]


@pytest.mark.parametrize(
    "body",
    [
        {"scenarios": ["T03"], "note": "api_key=sk-abcdefghijklmnopqrstuvwx"},
        {
            "scenarios": ["T03"],
            "note": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
        },
        {"scenarios": ["T03"], "pem": "-----BEGIN PRIVATE KEY-----\\nMIIB"},
        {"scenarios": ["T03"], "client_secret": "hunter2hunter2hunter2"},
    ],
    ids=["api_key", "bearer", "private_key", "client_secret"],
)
def test_a_credential_shaped_submission_is_refused_before_it_is_parsed(body):
    with _client() as client:
        response = client.post("/diagnostic/run", json=body)
    assert response.status_code == 400
    assert "credential" in response.json()["reason"]


def test_an_oversized_submission_is_refused():
    from failure_lab.diagnostic.server import MAX_REQUEST_BYTES

    with _client() as client:
        response = client.post(
            "/diagnostic/run",
            content=json.dumps({"pad": "x" * (MAX_REQUEST_BYTES + 1024)}),
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 413


@pytest.mark.parametrize("declared_length", [None, b"1"])
@pytest.mark.parametrize("first_chunk_overflows", [False, True])
def test_body_reader_stops_at_first_oversized_chunk(
    declared_length, first_chunk_overflows
):
    from failure_lab.diagnostic.server import (
        MAX_REQUEST_BYTES,
        SubmissionRefused,
        _read_json,
    )

    reads = []

    async def receive():
        reads.append(1)
        return {
            "type": "http.request",
            "body": b"x" * (MAX_REQUEST_BYTES + int(first_chunk_overflows)),
            "more_body": len(reads) < 4,
        }

    headers = [] if declared_length is None else [(b"content-length", declared_length)]
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/diagnostic/run",
            "headers": headers,
        },
        receive,
    )
    with pytest.raises(SubmissionRefused) as exc:
        asyncio.run(_read_json(request))
    assert exc.value.status_code == 413
    assert len(reads) == (1 if first_chunk_overflows else 2)


@pytest.mark.parametrize("body", [b"", b"{}", b'{"ok": true}', None])
def test_body_reader_accepts_bounded_and_exact_limit_json(body):
    from failure_lab.diagnostic.server import MAX_REQUEST_BYTES, _read_json

    if body is None:
        body = b"{}" + b" " * (MAX_REQUEST_BYTES - 2)
    chunks = iter([body[:1], body[1:]])
    reads = []

    async def receive():
        reads.append(1)
        return {
            "type": "http.request",
            "body": next(chunks),
            "more_body": len(reads) < 2,
        }

    request = Request(
        {"type": "http", "method": "POST", "path": "/diagnostic/run", "headers": []},
        receive,
    )
    assert asyncio.run(_read_json(request)) == (json.loads(body) if body else {})


@pytest.mark.parametrize("body", [b"not JSON", b"[]", b"null"])
def test_body_reader_preserves_bounded_json_refusals(body):
    from failure_lab.diagnostic.server import SubmissionRefused, _read_json

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {"type": "http", "method": "POST", "path": "/diagnostic/run", "headers": []},
        receive,
    )
    with pytest.raises(SubmissionRefused) as exc:
        asyncio.run(_read_json(request))
    assert exc.value.status_code == 400


def test_a_scenario_outside_the_allowlist_is_refused():
    with _client() as client:
        response = client.post("/diagnostic/run", json={"scenarios": ["T11"]})
    assert response.status_code == 400
    assert "not offered on this surface" in response.json()["reason"]


def test_the_public_allowlist_cannot_include_a_slow_scenario():
    """A curated list goes stale the day somebody re-tiers a scenario."""
    from failure_lab.diagnostic.runs import PUBLIC_SCENARIOS
    from failure_lab.scenarios import FAST_SCENARIO_IDS

    assert PUBLIC_SCENARIOS
    assert set(PUBLIC_SCENARIOS) <= set(FAST_SCENARIO_IDS)


def test_the_surface_never_labels_its_own_traffic_as_a_customer():
    """A visitor cannot make themselves count towards conversion."""
    from failure_lab.telemetry import TrafficSource, counts_toward_conversion

    settings = DiagnosticSettings()
    assert not counts_toward_conversion(settings.traffic_source)
    assert settings.traffic_source is not TrafficSource.HUMAN_CUSTOMER


def test_a_production_like_environment_refuses_to_boot_a_sandbox():
    """The guard reads the environment this process STARTED with.

    ``boot_standalone_environment`` sets ``ENVIRONMENT=local`` unconditionally,
    so a guard that read the variable after the first run would always find a
    development posture and always pass.
    """
    from failure_lab.diagnostic.runs import (
        ProductionRefused,
        assert_not_production_like,
    )

    with pytest.raises(ProductionRefused):
        assert_not_production_like("production")
    assert_not_production_like("local")


def test_health_reports_whether_external_targets_are_enabled():
    with _client() as client:
        body = client.get("/healthz").json()
    assert body["external_targets_enabled"] is False
    assert body["scenarios"]


def test_an_unknown_run_is_not_found_rather_than_invented():
    with _client() as client:
        assert client.get("/diagnostic/run/does-not-exist").status_code == 404
        assert client.get("/diagnostic/run/does-not-exist.json").status_code == 404


# --------------------------------------------------------------------------- #
# Narration                                                                     #
# --------------------------------------------------------------------------- #


def test_the_narration_cannot_claim_an_execution_nobody_counted():
    """The line only appears where the instrument reading does.

    The obvious way to produce the PRD's six lines is to print them on a timer
    while a run happens elsewhere. That is a screensaver, and it would keep
    saying "Downstream executed operation." on a run where nothing executed.
    """
    from failure_lab.diagnostic.steps import EXECUTED, translate_all

    base = {
        "sequence": 1,
        "at": "2026-09-19T00:00:00Z",
        "scenario": "T03",
        "configuration": Configuration.DIRECT_NAIVE.value,
        "step": "attempt.first",
        "message": "first attempt timed out",
    }
    without = [
        {
            **base,
            "data": {"status": "timeout", "client_visible_state": "no_information"},
        }
    ]
    with_evidence = [
        {
            **base,
            "data": {
                "status": "timeout",
                "client_visible_state": "no_information",
                "executions_so_far": 1,
                "downstream_requests_so_far": 1,
            },
        }
    ]

    assert not any(step.text.startswith(EXECUTED) for step in translate_all(without))
    assert any(step.text.startswith(EXECUTED) for step in translate_all(with_evidence))


def test_an_unknown_step_is_printed_rather_than_dropped():
    """A new scenario step must degrade to a plain line, not a silent gap."""
    from failure_lab.diagnostic.steps import translate_all

    steps = translate_all(
        [
            {
                "sequence": 1,
                "at": "2026-09-19T00:00:00Z",
                "scenario": "T99",
                "configuration": Configuration.GATEWAY_NATIVE.value,
                "step": "some.step.invented.later",
                "message": "a step this module has never heard of",
                "data": {},
            }
        ]
    )
    assert len(steps) == 1
    assert "never heard of" in (steps[0].text + steps[0].detail)
    assert steps[0].source_step == "some.step.invented.later"


# --------------------------------------------------------------------------- #
# Driving real runs, out of process                                             #
# --------------------------------------------------------------------------- #

#: The sandbox boots the gateway with ``boot_standalone_environment``, which
#: refuses to run once ``app.main`` is in ``sys.modules`` -- the application
#: caches its settings at import time. pytest's own conftest imports the
#: application, so these cases cannot run in this interpreter and are driven in
#: a fresh one. That is not a workaround: it is the same ordering constraint
#: the server lives under, exercised honestly rather than mocked away.
_TWO_RUNS_DRIVER = """
import json, sys
from starlette.testclient import TestClient
from failure_lab.diagnostic import create_app, DiagnosticSettings

out = {}
with TestClient(create_app(DiagnosticSettings(telemetry=False))) as client:
    ids = []
    for _ in range(2):
        started = client.post("/diagnostic/run", json={"scenarios": ["T03"]})
        if started.status_code != 202:
            out["error"] = [started.status_code, started.text[:400]]
            break
        run_id = started.json()["run_id"]
        ids.append(run_id)
        with client.stream("GET", "/diagnostic/run/" + run_id + "/events") as stream:
            steps = 0
            for line in stream.iter_lines():
                if line.startswith("event: step"):
                    steps += 1
                if line.startswith("event: complete") or line.startswith("event: failed"):
                    break
        out.setdefault("streamed", []).append(steps)
    out["ids"] = ids
    if len(ids) == 2:
        out["documents"] = [
            client.get("/diagnostic/run/" + run_id + ".json").json() for run_id in ids
        ]
        out["statuses"] = [
            client.get("/diagnostic/run/" + run_id).status_code for run_id in ids
        ]
sys.stdout.write("RESULT" + json.dumps(out))
"""


@pytest.fixture(scope="module")
def two_runs() -> dict:
    """Two real runs in one fresh interpreter. Each takes real seconds."""
    import os
    import subprocess

    completed = subprocess.run(
        [sys.executable, "-c", _TWO_RUNS_DRIVER],
        capture_output=True,
        text=True,
        timeout=900,
        cwd=str(ROOT),
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    assert "RESULT" in completed.stdout, (
        f"driver produced no result (exit {completed.returncode}):\n"
        f"{completed.stdout[-2000:]}\n{completed.stderr[-2000:]}"
    )
    return json.loads(completed.stdout.split("RESULT", 1)[1])


def test_a_second_run_in_the_same_process_still_works(two_runs):
    """A server answers more than one request. This one nearly did not.

    ``boot_standalone_environment`` refuses to run once ``app.main`` is in
    ``sys.modules``. Driving each check through ``run_lab`` -- which boots on
    every call -- answered the first request and raised on the second, and a
    probe that ran a single check could not see it. The sandbox is booted once
    per process and reused instead.
    """
    assert "error" not in two_runs, two_runs.get("error")
    assert len(two_runs["ids"]) == 2
    assert two_runs["ids"][0] != two_runs["ids"][1]
    assert two_runs["statuses"] == [200, 200]
    # Both runs streamed their log; a run that narrates nothing is not a run
    # anybody watched.
    assert all(count > 0 for count in two_runs["streamed"]), two_runs["streamed"]


def test_two_runs_do_not_share_an_effect_ledger(two_runs):
    """Each run counts its own executions and nobody else's.

    The effect ledgers are named after the configuration and the test id. Two
    runs sharing a directory would share those files, and the second run would
    report the first run's executions as its own: a measurement error that is
    silent, plausible, and fatal to every number on the page.
    """
    first, second = (document["answer"]["counts"] for document in two_runs["documents"])
    assert first == second, (
        "the same scenario run twice produced different counts, so one run saw "
        f"the other's ledger: {first} then {second}"
    )


def test_a_real_run_serves_nothing_with_a_credential_in_it(two_runs):
    """The redaction pass, asserted against a run that really minted one."""
    for document in two_runs["documents"]:
        body = json.dumps(document)
        assert "lab-admin-" not in body, "the sandbox admin key was served"
        assert not re.search(
            r"(?<![A-Za-z0-9])(?:amw|b2a|sk)[_-][A-Za-z0-9_-]{12,}", body
        )


def test_one_scenarios_narration_does_not_silence_the_next():
    """Per-configuration state must not leak across scenarios.

    Every scenario gets its own effect ledger, so ``executions_so_far`` starts
    again from zero for each one. The translator kept its high-water mark under
    the configuration name alone, so on a multi-scenario run the second
    scenario's executions never exceeded the first scenario's mark and its
    "Downstream executed operation." line vanished -- from the module whose one
    job is to not misreport what executed.

    ``fault_armed`` leaked the same way and in the more dangerous direction: a
    scenario that armed no fault inherited the previous scenario's flag and
    could claim the response was removed on purpose.
    """
    from failure_lab.diagnostic.steps import EXECUTED, RESPONSE_REMOVED, translate_all

    def event(scenario, step, **data):
        return {
            "sequence": 1,
            "at": "2026-09-19T00:00:00Z",
            "scenario": scenario,
            "configuration": Configuration.DIRECT_NAIVE.value,
            "step": step,
            "message": f"{scenario} {step}",
            "data": data,
        }

    steps = translate_all(
        [
            # Scenario one arms a fault and reaches two executions.
            event("T02", "fault.arm"),
            event(
                "T02",
                "attempt.first",
                executions_so_far=2,
                downstream_requests_so_far=2,
                status="timeout",
                client_visible_state="no_information",
            ),
            # Scenario two, same configuration, fresh ledger, no fault armed.
            event(
                "T03",
                "attempt.first",
                executions_so_far=1,
                downstream_requests_so_far=1,
                status="succeeded",
                client_visible_state="confirmed_success",
            ),
        ]
    )

    second = [step for step in steps if step.scenario == "T03"]
    assert any(step.text.startswith(EXECUTED) for step in second), (
        "the second scenario's execution went unnarrated because the first "
        "scenario's count was still the high-water mark"
    )
    assert not any(step.text.startswith(RESPONSE_REMOVED) for step in second), (
        "the second scenario armed no fault but inherited the first one's flag"
    )
