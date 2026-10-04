# What happens when an agent's tool executes but its response disappears?

Run this failure against your own agent. The harness, the failure injection,
and the measurements are all in this repository.

```bash
make failure-lab-suite      # the fast tier
make failure-lab-all        # every scenario, including the crash and concurrency tests
```

> **There are two failure labs in this repository and they are not the
> same thing.** [`failure-lab.md`](failure-lab.md) documents
> `scripts/failure_lab.py`, which measures one workflow under one injected
> fault and is what `make failure-lab` runs. This document describes the
> `failure_lab/` package: fourteen scenarios, an evidence bundle, a claims
> manifest, an independent verifier and a self-serve diagnostic. The two
> overlap on the lost-response scenario and reach the same conclusion
> there, which is the only cross-check either of them gets. Consolidating
> them is an open decision, not an oversight.

This document describes the **Agent Gateway Failure Lab** (`failure_lab/`): a
harness that drives the Agent Middleware API under realistic failure
conditions and measures what actually happened. It is written to be useful
even if you never deploy this gateway, because the failure mode it tests is
one every agent that calls a consequential tool will eventually hit.

---

## 1. The failure mode

An agent issues a refund. The tool executes it. The response never comes back.

```text
agent                gateway              refund API              customer
  │                     │                     │                      │
  │──── refund $50 ────▶│                     │                      │
  │                     │──── refund $50 ────▶│                      │
  │                     │                     │──── $50 refunded ───▶│
  │                     │                     │                      │
  │                     │          ✕ response lost                   │
  │                     │                     │                      │
  │◀─── timeout ────────│                     │                      │
  │                     │                     │                      │
  │  the agent now knows nothing: paid? not paid? charged?           │
```

The agent did nothing wrong. A timeout is genuinely ambiguous: the request may
never have arrived, or it may have completed perfectly and only the answer was
lost. An agent that retries risks refunding twice. An agent that does not retry
risks never refunding at all. There is no local reasoning that resolves this —
the information simply is not at the agent.

This is not an exotic case. It is the ordinary behaviour of networks under
load, of proxies with their own timeouts, of processes that get rescheduled,
and of every deployment that has ever had a bad afternoon. Once an agent's
tools cause external effects — refunds, credits, provisioning, messages,
database mutations — the ambiguity becomes a business problem rather than a
reliability statistic.

---

## 2. What the lab measures, and why you can believe it

Three design choices are what make the numbers evidence rather than assertion.

**The effect ledger is independent.** The simulated refund tool writes every
execution to its own SQLite file (`failure_lab/effect_ledger.py`). The gateway
has no handle to it and no API that reaches it. When a report says "downstream
effects: 2", that is a `COUNT(*)` over a table the thing under test cannot
touch. The table is deliberately **duplicate-tolerant**: a second execution of
the same operation is a second row. A uniqueness constraint there would hide
exactly what the lab exists to detect.

**Dispatches are counted from outside.** A fault-injection layer
(`failure_lab/faults.py`) sits between the gateway and the tool, as an ASGI
middleware wrapped around it. It injects one failure at a time at a named
point, and it counts every request that crosses it. "Gateway dispatches" is
that count — not a number read back from the gateway's own tables. Figures
that *are* gateway-reported (debits, receipts, attempt states) are labelled
`gateway-reported` everywhere they are rendered.

**Every scenario runs against a correct native baseline.** This is the part
that makes a positive result mean anything. Each workload runs against:

| | Configuration | What it is |
|---|---|---|
| A | `A_direct_naive` | The agent calls the tool directly. The tool executes every request. What an integration looks like before anyone thought about retries. |
| B | `B_direct_native_idempotency` | The agent calls the tool directly, and the tool honours the business `operation_id` as a durable idempotency key — the way payment processors do. **The correct native baseline.** |
| C | `C_gateway_with_native_idempotency` | The same correctly-built tool, with the gateway in front of it. Measures what the gateway adds *on top of* native controls. |
| D | `D_gateway_naive_downstream` | The naive tool behind the gateway. Isolates the gateway's own guarantee from the downstream's. |

Building a deliberately weak baseline and beating it would prove nothing. B is
built the way a competent engineer would build it, and when B handles a
scenario the report says so.

---

## 3. The result

The headline scenario (`T03`, "Execute then lose response") issues one refund,
withholds the response after the tool has committed, and retries with the same
idempotency key. Measured on this repository:

| | A: naive direct | B: native direct | C: native + gateway | D: gateway only |
|---|---|---|---|---|
| Requests | 2 | 2 | 2 | 2 |
| Gateway dispatches | — | — | 1 | 1 |
| **Downstream effects** | **2** | **1** | **1** | **1** |
| Duplicate effects | 1 | 0 | 0 | 0 |
| Confirmed final outcome | 1 | 1 | 0 | 0 |
| Explicitly uncertain | 0 | 0 | 2 | 2 |
| Left with no information | 1 | 1 | 0 | 0 |
| Gateway debits | — | — | 1 | 1 |
| Receipts | — | — | 1 | 1 |

Read it carefully, because the interesting column is B.

**The naive integration refunded the customer twice.** One retry, one duplicate
$50 refund, and nothing anywhere recorded that it had happened.

**The correct native baseline did not.** A downstream that honours a durable
operation id absorbed the retry and returned the stored result. It produced one
effect *and* told the agent the refund had already succeeded.

**The gateway also produced one effect — and could not tell the agent what
happened.** It dispatched once, charged once, and issued a receipt recording
`delivery_uncertain`. Both attempts came back uncertain. The gateway never
received the response, so it has no basis on which to claim success, and it
does not claim one.

The lab's own conclusion for this scenario is therefore:

> Correct native controls handled this test on their own. We did not observe an
> additional duplicate effect prevented by Agent Middleware in this scenario.
> If your downstream already honours a durable operation id the way this
> baseline does, this failure mode is already covered for you.

and it reports the cost alongside the benefit:

> **What the gateway added:** attempts left with no information at all fell
> from 1 to 0; 2 ended in an explicit uncertain state the caller can route on
> instead of an unexplained timeout. 1 signed receipt was issued.
>
> **What it cost:** the baseline resolved 1 attempt to a confirmed outcome;
> behind the gateway 0 did. 1 charge is retained against an operation whose
> outcome the gateway does not know.

That is the honest shape of this trade. The gateway's value in this scenario is
not that it prevented a duplicate the baseline would have allowed — it did not.
It is that it converts silent ambiguity into a receipted, explicit state, for a
downstream you do not control and cannot modify. Whether that is worth anything
depends entirely on whether you can change your downstream. If you can, change
it; that is cheaper and it gives you a better answer.

### Where the gateway does something the baseline cannot

Two other measured results are worth putting beside that one.

**Under a retry storm the two are not equivalent.** At a hundred identical
concurrent requests for one operation (`T01`), the naive integration paid out
a hundred refunds. The correct native baseline paid one — but all hundred
requests reached the tool and were collapsed inside its own transaction. The
gateway also produced one effect, and let **one** request cross into the tool.
Same effect count, very different load on a downstream you may not control,
and it costs roughly four seconds of p50 latency to the ninety-nine callers
who wait for the winner. Both halves are in the report.

**Budgets and revocation have no baseline at all.** A downstream idempotency
key cannot stop two *distinct* business operations from racing for one
authorisation. `T07` sizes a permit one credit below two calls and races two
concurrent refunds for it: one is authorised, one is refused
`permit_budget_exceeded`, and the downstream executes once. At twenty-way
concurrency against a permit sized for three, three are authorised and
seventeen refused. `T08` revokes a permit while a call is held at each durable
boundary and reports where revocation stops being effective.

Those cases — plus the crash boundaries in `T04` and `T05` — are where the
gateway is doing work nothing downstream of it could do.

---

## 4. What this does not establish

Stated plainly, because a measurement whose limits are hidden is worse than no
measurement.

- **It does not prove exactly-once downstream execution.** The gateway's
  guarantee is about its own dispatch and its own debit. A remote tool's side
  effect is deduplicated only if that tool honours the forwarded idempotency
  key. The lab counts effects; it does not prove a general property.
- **A signature is not evidence that the business action occurred.** The
  independent verifier (`failure_lab/verifier.py`) returns three *separate*
  claims and never collapses them: `SIGNATURE_VALID`,
  `ISSUER_TRUST_ESTABLISHED`, `DOWNSTREAM_EXECUTION_ESTABLISHED`. The third is
  established only by an independent observation of the downstream, never by a
  signature. The second is reported as **not established** whenever the
  verifying key was fetched from the same origin that issued the receipt, which
  is the current state of this product.
- **The crashes are simulated in-process.** A scenario asks the gateway to
  "die" immediately after a named durable commit, and the request handler is
  torn down by an exception that no handler can catch or compensate for.
  Committed state stays committed and uncommitted state rolls back, which
  matches a `SIGKILL`'s footprint, but it is not the same event. The
  repository's two-process PostgreSQL kill proof
  (`tests/test_mcp_postgres_multiprocess.py`) covers that separately.
- **It runs on SQLite.** Production runs PostgreSQL, where the row-lock path,
  failover and replication behave differently.
- **Time is simulated.** Reconciliation only touches attempts older than a long
  idle window, so the lab backdates rows rather than waiting. It does not
  measure the real blind window.
- **Concurrency is in-process asyncio**, not distributed load.
- **It is not a chaos-engineering product and not an observability platform.**
  It tests one gateway's documented guarantees against one consequential
  operation.

Every scenario carries its own `limitations` list, and they are rendered into
every report and every evidence bundle rather than living only here.

---

## 5. The scenario suite

Fourteen P0 scenarios. `fast` runs on every pull request; `slow` runs on main,
nightly, and release candidates.

| | Scenario | Tier | What it establishes |
|---|---|---|---|
| T01 | Concurrent identical retry | slow | One accepted key admits at most one dispatch, one debit, one receipt under a concurrent storm |
| T02 | Same key, different arguments | fast | A reused key carrying a changed payload is refused before a second effect |
| T03 | Execute then lose response | fast | The ambiguous outcome becomes a distinct receipted state, never a claim of success |
| T04 | Crash before dispatch | slow | A death before the one-shot claim provably sent nothing, so recovery refunds |
| T05 | Crash after dispatch | slow | A death after the claim is receipted uncertain and never redispatched |
| T06 | Agent restart with a new key | fast | **Expected to fail.** See below. |
| T07 | Concurrent budget race | slow | Concurrent consumption never exceeds the authorised limit |
| T08 | Permit revocation race | slow | Authorisation is evaluated at one well-defined point |
| T09 | Forbidden parameter | slow | Out-of-permit operations are refused before anything is dispatched |
| T10 | Receipt tampering | fast | Which fields the signature covers — and which it does not |
| T11 | Database restart | slow | A persistence outage produces no duplicate dispatch and no orphaned charge |
| T12 | Cache failure | fast | Correctness does not depend on the cache, and degradation is loud |
| T13 | Retention expiration | slow | What guarantee remains once the idempotency record ages out |
| T14 | Standard MCP client | fast | The full lifecycle against an independent, standards-compliant client |

### Where authorization is actually decided

The plan asks the product to define the authoritative point at which
authorization is evaluated. `T08` finds it by experiment rather than by
reading: it holds an in-flight call at each durable boundary, revokes the
permit while it is paused, releases it, and records what happens.

| Revocation lands | Outcome |
|---|---|
| before the request | denied, zero dispatches, zero executions |
| after `prepare` | admitted once |
| after `attach_charge` | admitted once |
| after `claim` | admitted once |

**The point is `after_prepare`** — the transaction that authorizes the permit
and reserves its budget. A revocation arriving at or after that boundary does
not stop the call already holding the reservation. That is coherent rather
than alarming: the reservation *is* the authorization decision, and unwinding
it mid-flight would mean a concurrent caller could spend budget this call has
already claimed.

What matters is that it is consistent. No interleaving produced a second
dispatch, a second downstream execution, a charge without a receipt, or a
receipt whose accounting did not match it. And revocation is fully effective
for anything not already in flight: four fresh operations submitted under the
revoked permit were all refused with nothing dispatched.

### What the probe matrix found

`T09` issues a permit per probe and checks, for each, whether the refusal
happened before anything crossed into the tool. All seven permit-scoped probes
were refused pre-dispatch, with zero downstream executions and no net charge.
Two details are worth stating plainly, because a reader would otherwise assume
the opposite.

**One permit constraint is fail-closed on the upstream path rather than
enforced.** A permit carrying `aggregate_value_cap` is refused for a configured
upstream tool as `permit_constraint_unsupported_for_upstream`, not as
`permit_aggregate_value_cap_exceeded`. That is deliberate, and it is the safe
direction: the remote path reserves credits atomically but does not fold
in-flight reservations into an aggregate cap, so a read-time check would let
concurrent calls overshoot. Refusing the permit outright is the right call.
Per-tool call caps (`max_calls_per_tool`) are now enforced atomically on remote
tools via a durable `call_slot_reserved` marker linked to the prepared dispatch
attempt, so cross-key retries are denied `permit_max_calls_exceeded` and
same-key retries replay the stored receipt.

**Argument shape is not part of the pre-dispatch check.** A call with `amount`
missing, or with `amount` sent as the string `"5000"` instead of the integer,
is dispatched to the tool and refused there: one dispatch, zero executions,
charge refunded. No business effect lands, which is what matters for safety,
but the refusal is the downstream's rather than the permit's. "Enforcement
happens before dispatch" is true of authority and false of argument validity.

### What the tampering matrix found

`T10` exports a real receipt, edits one field at a time, and checks each copy
with the independent verifier. Every one of the ten edits inside
`signing_input` broke the signature. Six envelope fields were also edited; two
of them (`kid`, `receipt_id`) are mirrored inside the signed payload and the
verifier caught the disagreement, and four are not:

    issuer   canonicalization   schema_version   keys_url

Those four were edited and the signature still verified. That is a property of
the format, not a defect, but it has to be stated rather than implied: a
holder who trusts the `issuer` line on a bundle is trusting an unauthenticated
string.

**The gateway's own verify endpoint cannot detect a tampered bundle.**
`POST /v1/receipts/verify` takes a `receipt_id`, so it re-verifies the copy the
gateway holds. Hand it the id from an edited bundle and it answers `valid:
true` for every one of the cases above, because it never looks at the bundle.
That is the endpoint working as designed and it is also the reason the plan
requires an independent verifier: to check the artifact in your hand, verify
its `signing_input` bytes offline, with
[`failure_lab/verifier.py`](../failure_lab/verifier.py) or
[`b2a_sdk.receipt_verifier`](../../sdk/python/README.md). Neither imports the
application.

**An exported bundle does not carry enough to find the key.** `keys_url` is a
relative path and `issuer` is whatever `PUBLIC_URL` was set to, which in a
sandbox is empty. Neither is signed. An offline holder therefore has to know
the issuing origin from somewhere else before the signature means anything —
which is the first-party key distribution limitation, arriving from a
different direction.

### T06 is expected to fail, and it stays

An agent that restarts and replans may mint a **new** idempotency key for the
**same** business operation. The gateway's guarantee is keyed on the
idempotency key, so two distinct keys are two distinct operations by design —
and the duplicate business effect is not prevented.

This test exists because the PRD requires it to, and it must not be softened to
make the product look better. The result is reported in full: which layer
caught the duplicate (in configuration C it is the downstream, not the
gateway), and the fact that the gateway still charged twice. The mitigation is
in the agent: derive your idempotency key from the business operation, not from
the attempt. `failure_lab/identity.py` keeps `request_id`,
`idempotency_key` and `business_operation_id` as three separate things
precisely so this test can be asked.

---

## 6. Evidence

Every run can emit a bundle a third party can check without trusting the run:

```text
manifest.json            every file with its sha256 and size
environment.json         versions, timestamps, configuration, seed
test-definition.json     each scenario's definition and its content hash
event-log.jsonl          the ordered record of what the harness did
downstream-effects.json  the independent effect ledger
gateway-events.json      gateway-reported state, labelled as such
receipts/                each portable receipt as exported
verification-results.json the independent verifier's three claims per receipt
summary.html             the rendered comparison
report.txt               the same, as text
results.json             the raw scenario results
```

Secrets never reach it: the builder redacts credential-shaped keys and values
throughout the tree and **fails the build** if any known secret from the run
appears in the written bytes.

```bash
make failure-lab-evidence                     # produce a bundle
make failure-lab-verify-bundle BUNDLE=...     # re-hash it and re-verify its receipts
```

A machine-readable claims manifest (`python -m failure_lab claims`) ties each
public claim to the test id, definition hash, observed status and limitations
that back it, so a claim with no passing test behind it can be refused
mechanically rather than by review.

---

## 7. Security posture

The lab is sandboxed by default and is not a tool for pointing at things you do
not own.

- Loopback only. The diagnostic server refuses to start in a production-like
  environment.
- No production credentials, ever. Each run mints its own throwaway wallet, key
  and signing seed in a temporary directory.
- No external network requests. Any external-target mode requires an explicit
  opt-in that is off by default.
- Deterministic cleanup: run directories are removed on exit, including on
  failure.
- Secrets are redacted from everything written to disk or served over HTTP.
- Rate limits and a single-in-flight-run cap on the diagnostic server.
- Test definitions are content-addressed, so a published claim cannot silently
  come to refer to a different test.
- Synthetic and agent traffic is structurally incapable of counting toward any
  customer-conversion metric — the traffic source is part of the event model,
  not a convention.

---

## 8. Running it yourself

```bash
make failure-lab-list                        # the suite, with claims and expectations
make failure-lab-suite                       # fast tier
make failure-lab-all                         # everything
make failure-lab-explore SEED=7              # seeded state-machine exploration
make failure-lab-diagnostic                  # the self-serve diagnostic, on loopback
make failure-lab-integration-check           # the clean-room integration judge
python -m failure_lab.dev_run T03 --full     # one scenario, whole result document
```

The suite's exit status is non-zero when any scenario errors, **or** when an
observed verdict differs from the expectation the product documents. An
undocumented improvement fails the build too, because it means the tests and
the documentation disagree and one of them needs changing.

---

## 9. Related

- [`failure-semantics.md`](failure-semantics.md) — the authoritative
  description of what each terminal outcome means and which test proves it.
- [`PROOF_MATRIX.md`](PROOF_MATRIX.md) — which command proves which invariant,
  and what each one does not prove.
- [`../WEDGE.md`](../WEDGE.md) — the product thesis these proofs defend.
- [`../SECURITY_LIMITATIONS.md`](../SECURITY_LIMITATIONS.md) — the claims this
  project deliberately does not make.
