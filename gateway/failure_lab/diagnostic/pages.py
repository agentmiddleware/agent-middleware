"""The developer-facing pages, server-rendered, with nothing fetched from anywhere.

One module-level template, one ``<style>`` block, one ``<script>`` block, no
CDN, no framework, no build step. The reason is not minimalism: this page
exists to be trusted about what it measured, and a page that pulls a megabyte
of somebody else's JavaScript before it will tell you what happened is asking
for a kind of trust it has not earned.

Four rules that the markup enforces rather than describes:

**The result that says the product was not needed looks exactly like the result
that says it was.** :func:`_answer_card` picks its accent from a table, and the
two headline outcomes -- "you may not need us" and "duplicates were prevented"
-- are given the same one. The only answer rendered in the warning colour is
the one where the product failed its own guarantee, which is the reader's
problem and not a styling decision.

**The offer is downstream of the answer, not of the page.**
:func:`render_result` asks :attr:`DiagnosticAnswer.recommends_the_product`, and
that property has exactly one true case. There is no branch here that can put
an offer under a result which did not point at one. What follows the "you may
not need us" card is the PRD's own sentence -- try another failure mode, or
inspect the detailed trace -- and two links that do exactly those two things.
No "but", no "however", nothing to buy.

**Every number shown comes with the instrument that took it.** The comparison
table is not re-implemented here at all: :func:`render_report` serves
:func:`failure_lab.report.render_run_html`'s own document, and the result page
frames it. One renderer, one presentation, no second version to drift.

**The stream is a live region.** The steps land in ``role="log"`` with
``aria-live="polite"``, so a screen reader hears the experiment happen instead
of being told at the end that something did.
"""

from __future__ import annotations

import html
import json
from typing import Any

from failure_lab.diagnostic.answer import Answer, DiagnosticAnswer, ScenarioRow
from failure_lab.diagnostic.offer import OFFER, Offer
from failure_lab.diagnostic.steps import PHASE_LABELS, Step

#: The one line this page leads with. Specific, testable, and not a claim about
#: anybody's safety: the PRD forbids "see if your AI is safe" and this is the
#: sentence it gives instead.
HEADLINE = "Agent Action Safety Check"
SUBHEADLINE = (
    "Test what happens when your agent's tool executes but its response disappears."
)
CALL_TO_ACTION = "Run the failure test"
REASSURANCE = "No production credentials required. Runs against a disposable sandbox."

#: Which accent an answer is drawn in. The two that matter most share one, on
#: purpose -- see the module docstring.
_ANSWER_ACCENT: dict[Answer, str] = {
    Answer.GATEWAY_DID_NOT_HOLD: "bad",
    Answer.GATEWAY_PREVENTED_DUPLICATES: "accent",
    Answer.GATEWAY_CHANGED_EVIDENCE_ONLY: "accent",
    Answer.YOU_MAY_NOT_NEED_US: "accent",
    Answer.NO_COMPARISON_WAS_MADE: "muted",
    Answer.NOTHING_ESTABLISHED: "muted",
}

#: The short label above the headline, so a reader knows what kind of answer
#: they are looking at before they read it.
_ANSWER_KICKER: dict[Answer, str] = {
    Answer.GATEWAY_DID_NOT_HOLD: "Finding against this product",
    Answer.GATEWAY_PREVENTED_DUPLICATES: "Measured difference",
    Answer.GATEWAY_CHANGED_EVIDENCE_ONLY: "Measured difference",
    Answer.YOU_MAY_NOT_NEED_US: "Result",
    Answer.NO_COMPARISON_WAS_MADE: "No comparison",
    Answer.NOTHING_ESTABLISHED: "No result",
}


_CSS = """
:root {
  color-scheme: light dark;
  --bg: #ffffff;
  --fg: #16181d;
  --muted: #5b6270;
  --line: #d9dde5;
  --card: #f7f8fa;
  --card-2: #eef1f6;
  --accent: #1f5fd0;
  --accent-fg: #ffffff;
  --warn: #9a5b00;
  --bad: #b42318;
  --good: #05603a;
  --focus: #1f5fd0;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #101216;
    --fg: #e8eaee;
    --muted: #9aa2b1;
    --line: #2a2f38;
    --card: #171a20;
    --card-2: #1e222a;
    --accent: #7fa8f0;
    --accent-fg: #0b0d11;
    --warn: #e0a34a;
    --bad: #f08a80;
    --good: #67d7a5;
    --focus: #7fa8f0;
  }
}
:root[data-theme="dark"] {
  --bg: #101216;
  --fg: #e8eaee;
  --muted: #9aa2b1;
  --line: #2a2f38;
  --card: #171a20;
  --card-2: #1e222a;
  --accent: #7fa8f0;
  --accent-fg: #0b0d11;
  --warn: #e0a34a;
  --bad: #f08a80;
  --good: #67d7a5;
  --focus: #7fa8f0;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--fg);
  font: 16px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  overflow-x: hidden;
}
.wrap { max-width: 900px; margin: 0 auto; padding: 32px 16px 96px; }
h1 { font-size: 1.75rem; line-height: 1.2; margin: 0 0 8px; letter-spacing: -0.015em; }
h2 { font-size: 1.15rem; margin: 40px 0 8px; }
h3 { font-size: 0.95rem; margin: 20px 0 6px; }
p { margin: 0 0 12px; }
a { color: var(--accent); }
a:focus-visible, button:focus-visible, input:focus-visible, summary:focus-visible {
  outline: 3px solid var(--focus);
  outline-offset: 2px;
}
.lede { font-size: 1.1rem; color: var(--fg); margin: 0 0 8px; max-width: 62ch; }
.sub { color: var(--muted); margin: 0 0 20px; max-width: 68ch; }
.kicker {
  font-size: 0.74rem; text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--muted); margin: 0 0 6px;
}
button.primary {
  font: inherit; font-weight: 600;
  background: var(--accent); color: var(--accent-fg);
  border: 1px solid var(--accent); border-radius: 8px;
  padding: 12px 20px; cursor: pointer; min-height: 44px;
}
button.primary[disabled] { opacity: 0.55; cursor: not-allowed; }
button.secondary {
  font: inherit; background: transparent; color: var(--fg);
  border: 1px solid var(--line); border-radius: 8px;
  padding: 10px 16px; cursor: pointer; min-height: 44px;
}
.actions { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin: 20px 0 8px; }
fieldset { border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; margin: 24px 0; }
legend { padding: 0 6px; font-weight: 600; font-size: 0.9rem; }
.choice { display: flex; gap: 10px; align-items: flex-start; padding: 8px 0; }
.choice input { margin-top: 4px; width: 18px; height: 18px; flex: none; }
.choice label { cursor: pointer; }
.choice .hint { display: block; color: var(--muted); font-size: 0.88rem; }
.card {
  background: var(--card); border: 1px solid var(--line);
  border-radius: 10px; padding: 16px 18px; margin: 16px 0;
}
.card.accent { border-left: 4px solid var(--accent); }
.card.bad { border-left: 4px solid var(--bad); }
.card.muted { border-left: 4px solid var(--muted); }
.card h2, .card h3 { margin-top: 0; }
.note { border-left: 3px solid var(--warn); padding-left: 12px; color: var(--muted); margin: 12px 0; }
ul, ol { margin: 6px 0 12px; padding-left: 22px; }
li { margin: 4px 0; }
code, pre {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.85em;
}
pre {
  background: var(--card-2); border: 1px solid var(--line); border-radius: 8px;
  padding: 12px 14px; overflow-x: auto; margin: 8px 0 14px;
}
code.inline { background: var(--card-2); border-radius: 4px; padding: 1px 5px; }
#steps { margin: 8px 0 0; padding: 0; list-style: none; }
#steps li {
  border-left: 2px solid var(--line); padding: 6px 0 6px 14px; margin: 0;
}
#steps li .text { display: block; }
#steps li .meta {
  display: block; color: var(--muted); font-size: 0.8rem;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  word-break: break-word;
}
#steps li.phase-protected { border-left-color: var(--accent); }
.phase-heading {
  font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.07em;
  color: var(--muted); margin: 18px 0 2px; font-weight: 600;
}
table { width: 100%; border-collapse: collapse; margin: 10px 0 4px; font-size: 0.9rem; }
th, td { text-align: left; padding: 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--muted); font-weight: 600; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.04em; }
.wrapscroll { overflow-x: auto; }
.v { font-weight: 600; font-size: 0.8rem; letter-spacing: 0.03em; }
.v-PASS { color: var(--good); }
.v-FAIL, .v-ERROR { color: var(--bad); }
.v-OBSERVED { color: var(--accent); }
.v-NOT_APPLICABLE, .v-NOT_RUN { color: var(--muted); }
.offer li { margin: 8px 0; }
.offer .review { color: var(--warn); font-size: 0.85rem; display: block; }
.price { font-size: 1.05rem; font-weight: 600; }
iframe.report {
  width: 100%; height: 70vh; min-height: 420px;
  border: 1px solid var(--line); border-radius: 10px; background: var(--bg);
}
footer {
  margin-top: 56px; color: var(--muted); font-size: 0.85rem;
  border-top: 1px solid var(--line); padding-top: 16px;
}
.visually-hidden {
  position: absolute; width: 1px; height: 1px; overflow: hidden;
  clip: rect(0 0 0 0); white-space: nowrap;
}
@media (max-width: 640px) {
  .wrap { padding: 24px 16px 72px; }
  h1 { font-size: 1.45rem; }
  button.primary, button.secondary { width: 100%; }
  iframe.report { height: 60vh; }
}
"""


_SCRIPT = """
(function () {
  var form = document.getElementById('run-form');
  if (!form) { return; }
  var button = document.getElementById('run-button');
  var status = document.getElementById('run-status');
  var list = document.getElementById('steps');
  var result = document.getElementById('result');
  var source = null;
  var lastPhase = null;

  function setStatus(text) { status.textContent = text; }

  function addPhase(phase, label) {
    if (phase === lastPhase) { return; }
    lastPhase = phase;
    var heading = document.createElement('li');
    heading.className = 'phase-heading';
    heading.textContent = label;
    list.appendChild(heading);
  }

  function addStep(step) {
    addPhase(step.phase, step.phase_label);
    var item = document.createElement('li');
    item.className = 'phase-' + step.phase;
    var text = document.createElement('span');
    text.className = 'text';
    text.textContent = step.text;
    item.appendChild(text);
    var meta = document.createElement('span');
    meta.className = 'meta';
    meta.textContent = step.source_step + (step.evidence_text ? ' — ' + step.evidence_text : '');
    item.appendChild(meta);
    list.appendChild(item);
  }

  function selectedScenarios() {
    var boxes = form.querySelectorAll('input[name="scenario"]:checked');
    var ids = [];
    for (var i = 0; i < boxes.length; i++) { ids.push(boxes[i].value); }
    return ids;
  }

  function finish(message) {
    if (source) { source.close(); source = null; }
    button.disabled = false;
    setStatus(message);
  }

  function showResult(runId) {
    fetch('/diagnostic/run/' + runId + '?fragment=1', { headers: { 'Accept': 'text/html' } })
      .then(function (response) { return response.text(); })
      .then(function (markup) {
        result.innerHTML = markup;
        result.focus();
      })
      .catch(function (error) {
        result.textContent = 'The result could not be loaded: ' + error;
      });
  }

  form.addEventListener('submit', function (event) {
    event.preventDefault();
    var mode = form.querySelector('input[name="mode"]:checked');
    if (mode && mode.value !== 'sample') { return; }
    button.disabled = true;
    list.innerHTML = '';
    result.innerHTML = '';
    lastPhase = null;
    setStatus('Starting the run...');

    fetch('/diagnostic/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify({ scenarios: selectedScenarios() })
    })
      .then(function (response) {
        return response.json().then(function (body) { return { status: response.status, body: body }; });
      })
      .then(function (answer) {
        if (answer.status !== 202) {
          finish(answer.body && answer.body.reason ? answer.body.reason : 'The run was refused.');
          return;
        }
        var runId = answer.body.run_id;
        setStatus('Running. Every line below is one entry from the run log.');
        source = new EventSource('/diagnostic/run/' + runId + '/events');
        source.addEventListener('step', function (message) { addStep(JSON.parse(message.data)); });
        source.addEventListener('failed', function (message) {
          finish(JSON.parse(message.data).error || 'The run failed.');
        });
        source.addEventListener('complete', function () {
          finish('Run complete.');
          showResult(runId);
        });
        source.onerror = function () {
          if (source && source.readyState === 2) { finish('The stream closed before the run finished.'); }
        };
      })
      .catch(function (error) { finish('The run could not be started: ' + error); });
  });

  var modes = form.querySelectorAll('input[name="mode"]');
  var local = document.getElementById('local-instructions');
  function syncMode() {
    var mode = form.querySelector('input[name="mode"]:checked');
    var isSample = !mode || mode.value === 'sample';
    button.disabled = !isSample;
    local.hidden = isSample;
    setStatus(isSample ? '' : 'Testing your own integration runs from the command line. The commands are below.');
  }
  for (var i = 0; i < modes.length; i++) { modes[i].addEventListener('change', syncMode); }
  syncMode();
})();
"""


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{style}</style>
</head>
<body>
<div class="wrap">
{body}
</div>
<script>{script}</script>
</body>
</html>
"""


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _page(title: str, body: str, *, script: bool = False) -> str:
    return (
        _PAGE.replace("{style}", _CSS)
        .replace("{script}", _SCRIPT if script else "")
        .replace("{title}", _esc(title))
        .replace("{body}", body)
    )


def _list(items: Any, *, empty: str = "") -> str:
    entries = [str(item) for item in items]
    if not entries:
        return f"<p class='sub'>{_esc(empty)}</p>" if empty else ""
    return "<ul>" + "".join(f"<li>{_esc(item)}</li>" for item in entries) + "</ul>"


# --------------------------------------------------------------------------- #
# The index                                                                     #
# --------------------------------------------------------------------------- #


def render_index(
    *,
    scenarios: list[str],
    defaults: list[str],
    scenario_titles: dict[str, str],
    external_targets_enabled: bool,
) -> str:
    """The page a visitor lands on. Nothing is behind a registration wall."""
    choices = "".join(
        f"""
        <div class="choice">
          <input type="checkbox" id="scenario-{_esc(test_id)}" name="scenario"
                 value="{_esc(test_id)}"{" checked" if test_id in defaults else ""}>
          <label for="scenario-{_esc(test_id)}">{_esc(test_id)}
            <span class="hint">{_esc(scenario_titles.get(test_id, ""))}</span>
          </label>
        </div>"""
        for test_id in scenarios
    )

    external_note = (
        "<div class='note'>This instance was started with external targets "
        "permitted. Every individual request still has to carry its own "
        "authorization, and no external adapter is compiled into this build, so "
        "nothing here will send traffic at a system you name.</div>"
        if external_targets_enabled
        else ""
    )

    body = f"""
<h1>{_esc(HEADLINE)}</h1>
<p class="lede">{_esc(SUBHEADLINE)}</p>
<p class="sub">Your agent calls a tool. The tool executes and commits. The
response never comes back. Your agent retries. This runs that exact sequence
against four configurations — your kind of integration, a correct native one,
and both of those behind Agent Middleware — and counts what the downstream
system actually did, from the downstream system's own ledger.</p>

{external_note}

<form id="run-form">
  <fieldset>
    <legend>What to test</legend>
    <div class="choice">
      <input type="radio" id="mode-sample" name="mode" value="sample" checked>
      <label for="mode-sample">Use the provided sample agent
        <span class="hint">A simulated refund tool and a sample agent, both
        disposable. This is the only option that runs from this page.</span>
      </label>
    </div>
    <div class="choice">
      <input type="radio" id="mode-local" name="mode" value="local">
      <label for="mode-local">Test my local integration
        <span class="hint">Runs from the command line, not from here. You write
        one adapter class against your own client and the harness drives it.
        There is no one-click version of this and this page will not pretend
        otherwise.</span>
      </label>
    </div>
  </fieldset>

  <fieldset>
    <legend>Failure modes</legend>
    {choices}
    <p class="sub">The full suite of fourteen runs from the command line. This
    page offers the fast ones so that a browser tab cannot be used to occupy the
    machine.</p>
  </fieldset>

  <div class="actions">
    <button type="submit" class="primary" id="run-button">{_esc(CALL_TO_ACTION)}</button>
    <span class="sub" style="margin:0">{_esc(REASSURANCE)}</span>
  </div>
</form>

<div id="local-instructions" hidden>
  <h2>Testing your own integration</h2>
  <p>The harness needs something it can drive. You implement five methods —
  execute an authorized operation, retry after a lost response, attempt an
  unauthorized one, verify a receipt independently, and recover after a restart
  — and the judge runs the same failures against them.</p>
  <pre>git clone &lt;this repository&gt;
$EDITOR failure_lab/integration_check/PROMPT.md      # what to implement
cp failure_lab/integration_check/reference_candidate.py my_integration.py
python -m failure_lab.integration_check.judge --candidate ./my_integration.py</pre>
  <p class="sub">The judge reports what it observed per assertion, including the
  workarounds it considered unsafe. It does not score you.</p>
</div>

<h2 id="stream-heading">What happened</h2>
<p class="sub" id="run-status" role="status" aria-live="polite"></p>
<ol id="steps" role="log" aria-live="polite" aria-relevant="additions"
    aria-labelledby="stream-heading"></ol>

<div id="result" tabindex="-1"></div>

<footer>
<p>Runs on this machine, against a sandbox this process creates and deletes.
No production credentials are accepted anywhere on this surface and none are
needed. Nothing is sent off the machine.</p>
<p>There is no risk score here, and there will not be one. Every figure is a
count taken by a named instrument, and a run in which the product made no
difference is reported as a run in which the product made no difference.</p>
</footer>
"""
    return _page(HEADLINE, body, script=True)


# --------------------------------------------------------------------------- #
# The result                                                                    #
# --------------------------------------------------------------------------- #


def _answer_card(answer: DiagnosticAnswer) -> str:
    accent = _ANSWER_ACCENT.get(answer.answer, "muted")
    kicker = _ANSWER_KICKER.get(answer.answer, "Result")
    return f"""
<section class="card {accent}" aria-labelledby="answer-headline">
  <p class="kicker">{_esc(kicker)}</p>
  <h2 id="answer-headline" style="margin:0 0 8px">{_esc(answer.headline)}</h2>
  <p style="margin:0">{_esc(answer.detail)}</p>
</section>
"""


def _scenario_table(rows: list[ScenarioRow]) -> str:
    if not rows:
        return "<p class='sub'>No scenario produced a row.</p>"
    body = "".join(
        f"""<tr>
  <td>{_esc(row.test_id)}</td>
  <td>{_esc(row.title)}</td>
  <td><span class="v v-{_esc(row.verdict)}">{_esc(row.verdict)}</span></td>
  <td>{_esc(row.conclusion_gloss or row.conclusion_kind)}</td>
  <td>{_esc("yes" if row.matches_expectation else "NO")}</td>
</tr>"""
        for row in rows
    )
    return f"""
<div class="wrapscroll">
<table>
  <thead><tr>
    <th>Test</th><th>Failure mode</th><th>Verdict</th>
    <th>Conclusion</th><th>Matches the documentation</th>
  </tr></thead>
  <tbody>{body}</tbody>
</table>
</div>
"""


#: Conclusion kinds whose text is the PRD's "you may not need us" wording. The
#: page owes those two the same prominence as any other conclusion, which is
#: why no conclusion text is behind a disclosure triangle.
_BASELINE_SUFFICIENT = frozenset(
    {"existing_integration_sufficient", "native_controls_sufficient"}
)


def _conclusion_details(rows: list[ScenarioRow]) -> str:
    """One card per scenario: the conclusion in the open, the arithmetic behind it.

    The conclusion sentence is never inside the ``<details>``. Two of the seven
    kinds say the product was not needed, and a result a reader has to expand
    is a result somebody decided they would rather not be read.
    """
    sections = []
    for row in rows:
        added = _list(row.added, empty="none measured in this scenario")
        cost = _list(row.cost, empty="none measured in this scenario")
        risks = _list(row.remaining_risks, empty="none recorded by this test")
        accent = "accent" if row.conclusion_kind in _BASELINE_SUFFICIENT else ""
        sections.append(
            f"""
<section class="card {accent}">
  <h3 style="margin:0 0 4px">{_esc(row.test_id)} — {_esc(row.title)}
    <span class="v v-{_esc(row.verdict)}">{_esc(row.verdict)}</span></h3>
  <p class="sub" style="margin:0 0 10px">{_esc(row.claim)}</p>
  <p>{_esc(row.conclusion_text)}</p>
  <details>
    <summary>What the instruments measured for this scenario</summary>
    <h3>What the gateway added, other than duplicate prevention</h3>
    {added}
    <h3>What it cost, against the same baseline</h3>
    {cost}
    <h3>Remaining risks this test did not close</h3>
    {risks}
  </details>
</section>"""
        )
    return "".join(sections)


def _next_steps(answer: DiagnosticAnswer, run_id: str) -> str:
    """The PRD's third sentence for the "you may not need us" result.

    It is an affordance, not a pitch: another failure mode, or the trace. There
    is deliberately nothing else in this block, and it is rendered for exactly
    the answer the PRD attaches it to.
    """
    if answer.answer is not Answer.YOU_MAY_NOT_NEED_US:
        return ""
    return f"""
<p>Try another failure mode or inspect the detailed trace:
<a href="/diagnostic">run a different failure</a> ·
<a href="/diagnostic/run/{_esc(run_id)}/report" target="_blank" rel="noopener">open the
full trace</a>.</p>
"""


def _offer_block(offer: Offer) -> str:
    items = "".join(
        f"""<li><strong>{_esc(item.title)}</strong> — {_esc(item.detail)}
        {'<span class="review">Needs a technical review of your downstream before it can be turned on.</span>' if item.manual_review else ""}
        </li>"""
        for item in offer.items
    )
    return f"""
<section class="card offer" aria-labelledby="offer-headline">
  <p class="kicker">What a deployment includes</p>
  <h2 id="offer-headline" style="margin:0 0 8px">{_esc(offer.headline)}</h2>
  <p>{_esc(offer.summary)}</p>
  <ul>{items}</ul>
  <p class="price">Price: {_esc(offer.price)}</p>
  <p class="sub">{_esc(offer.price_note)}</p>
  <div class="note">{_esc(offer.manual_review_note)}</div>
  <p><a class="primary" href="{_esc(offer.action_href)}"
        style="display:inline-block;text-decoration:none;padding:12px 20px;border-radius:8px">
     {_esc(offer.action_label)}</a></p>
</section>
"""


def render_result(
    record: Any,
    *,
    fragment: bool = False,
) -> str:
    """The result of one run: the answer, the comparison, the evidence.

    ``record`` is a :class:`~failure_lab.diagnostic.runs.RunRecord`. It is typed
    loosely here so this module stays a renderer and imports no run machinery.
    """
    if record.state == "failed":
        body = f"""
<section class="card bad">
  <p class="kicker">The check did not complete</p>
  <h2 style="margin:0 0 8px">This run produced no result.</h2>
  <p>{_esc(record.error or "The run ended without a reportable outcome.")}</p>
  <p class="sub">Nothing is shown below, because a partial run is not a result
  and a diagnostic that renders one anyway is worse than one that fails.</p>
</section>
"""
        return body if fragment else _page("Run failed", body)

    answer: DiagnosticAnswer = record.answer
    run_id = _esc(record.run_id)
    failures = (
        f"""
<section class="card bad">
  <p class="kicker">Read this first</p>
  <h3 style="margin:0 0 8px">Guarantees this product did not hold in this run</h3>
  {_list(answer.gateway_failures)}
</section>"""
        if answer.gateway_failures
        else ""
    )
    offer = _offer_block(OFFER) if answer.recommends_the_product else ""
    not_tested = (
        f"<h2>What this run did not test</h2>{_list(answer.not_tested)}"
        if answer.not_tested
        else ""
    )
    archive = (
        f"""<p><a href="/diagnostic/run/{run_id}/evidence.zip">Download the evidence bundle</a>
        — every measurement, every event, the scenario definitions and their hashes,
        and a manifest with a sha256 per file. Redacted with the same pass this page uses.</p>"""
        if record.archive is not None
        else f"<p class='sub'>{_esc(record.archive_note)}</p>"
    )
    command = _esc(record.environment.get("reproduction_command", "not recorded"))
    # NOT ``seed_note``. :func:`failure_lab.runner.collect_environment` calls
    # this field ``reproducibility_note`` on purpose -- a key containing
    # ``seed`` is replaced wholesale by ``REDACTED_KEY_PATTERN`` -- and reading
    # the old name here rendered an empty paragraph under the reproduction
    # command on every single run. The caveat that a seeded run is not a
    # byte-identical run has exactly one place it has to appear, and that is
    # beside the command that claims to reproduce it. Missing, it reads as a
    # replay; the fallback below says so rather than staying silent.
    reproducibility = record.environment.get("reproducibility_note") or (
        "this run recorded no reproducibility note. Treat the command above as "
        "a fresh run of the same scenarios, not a replay of this one."
    )

    body = f"""
{failures}
{_answer_card(answer)}
{_next_steps(answer, record.run_id)}

<h2>Scenarios</h2>
{_scenario_table(answer.rows)}
{_conclusion_details(answer.rows)}

<h2>The comparison, in full</h2>
<p class="sub">Rendered by the lab's own reporter — the same document that goes
into the evidence bundle, not a second version of it.
<a href="/diagnostic/run/{run_id}/report" target="_blank" rel="noopener">Open it
in its own tab.</a></p>
<iframe class="report" src="/diagnostic/run/{run_id}/report"
        title="Full comparison report for run {run_id}" loading="lazy"></iframe>

<h2>Evidence</h2>
{archive}
<p>Reproduce this run from the command line:</p>
<pre>{command}</pre>
<p class="sub">Reproducibility: {_esc(reproducibility)}</p>
<p class="sub">Machine-readable result:
<a href="/diagnostic/run/{run_id}.json">/diagnostic/run/{run_id}.json</a></p>

{offer}

{not_tested}

<h2>What this check does not establish</h2>
{_list(answer.limitations)}
"""
    return body if fragment else _page(f"{HEADLINE} — result", body)


def render_report(record: Any) -> str:
    """The comparison document, exactly as :mod:`failure_lab.report` renders it."""
    return record.report_html


# --------------------------------------------------------------------------- #
# Everything else                                                               #
# --------------------------------------------------------------------------- #


def render_deployment() -> str:
    """What "Start deployment" actually starts. Which is a conversation."""
    body = f"""
<h1>Start deployment</h1>
<p class="lede">There is no self-serve production deployment, and this page is
not going to imply there is one.</p>
<p>Governing a real tool means someone has to look at that tool: what a
duplicate of it costs, what its idempotency semantics already are, which fields
an agent must never be allowed to set, and what your retention obligation is.
Two of the six things in the offer depend on those answers.</p>

<h2>What happens next</h2>
<ol>
  <li>You run the same failures against your own integration from the command
  line, so the comparison is about your downstream and not this sandbox.</li>
  <li>We read that output together and agree which tool is worth governing
  first.</li>
  <li>The permit, the budget and the retention window are configured against
  that tool, in a non-production environment, and the failure runs are repeated
  against it.</li>
  <li>Only then does anything move.</li>
</ol>

<h2>Price</h2>
<p class="price">{_esc(OFFER.price)}</p>
<p class="sub">{_esc(OFFER.price_note)}</p>

<p><a href="/diagnostic">Back to the check</a></p>
"""
    return _page("Start deployment", body)


def render_error(title: str, message: str, *, status: int) -> str:
    body = f"""
<h1>{_esc(title)}</h1>
<p class="lede">{_esc(message)}</p>
<p class="sub">HTTP {status}.</p>
<p><a href="/diagnostic">Back to the check</a></p>
"""
    return _page(title, body)


def step_payload(step: Step) -> str:
    """One SSE ``data:`` line. Values only; the page does the escaping."""
    evidence = ", ".join(f"{name}={value}" for name, value in step.evidence.items())
    return json.dumps(
        {
            "index": step.index,
            "text": step.text,
            "detail": step.detail,
            "source_step": step.source_step,
            "phase": step.phase,
            "phase_label": PHASE_LABELS.get(step.phase, step.phase),
            "configuration": step.configuration,
            "label": step.label,
            "evidence_text": evidence,
        }
    )


__all__ = [
    "CALL_TO_ACTION",
    "HEADLINE",
    "REASSURANCE",
    "SUBHEADLINE",
    "render_deployment",
    "render_error",
    "render_index",
    "render_report",
    "render_result",
    "step_payload",
]
