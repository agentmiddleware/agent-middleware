# Failure lab: the action completed, but the response was lost

```bash
make failure-lab          # transcript + report, artifacts in data/failure-lab/<run>/
make failure-lab-check    # the same run, JSON summary only (what CI asserts)
```

> A second, larger harness now lives alongside this one:
> [`failure-lab-suite.md`](failure-lab-suite.md) documents the
> `failure_lab/` package, which runs fourteen scenarios and produces a
> signed evidence bundle. It covers this document's scenario too, and
> reaches the same conclusion on it. This document describes the single
> focused run that `make failure-lab` still executes.

One consequential workflow (a vendor payout), one injected fault (the
downstream executes, then its response is lost on the wire), measured across
three integrations of the same simulated payment rail. The lab exists because
"the retry paid twice" is the problem this boundary is for, and that claim is
only worth something when it is measured against a correctly built
integration rather than a deliberately naive one. Payment APIs such as Stripe
already document idempotency keys, parameter-conflict detection, and key
retention; the lab includes that baseline and is required to report when it
already handles the fault. It does.

Everything runs in one process with no network and no credentials: the real
FastAPI application against a throwaway SQLite database, the real
`UpstreamMcpAdapter` (`app/services/upstream_mcp.py`) registered through
`register_configured_upstream_mcp`, and a real in-process Streamable HTTP MCP
server standing in for the payment rail. Nothing in `app/` changes for the
lab; it is a proof, not a capability.

## The three integrations

| Configuration | Who the agent calls | Idempotency key | What it stands for |
|---|---|---|---|
| **Existing integration** | the rail, directly over MCP | none | the integration most agents ship with |
| **Native baseline** | the rail, directly over MCP | persisted by the agent across the retry, honored by the rail, conflicts refused | a correctly built integration using the downstream's own control |
| **Gateway** | `POST /mcp/messages` under a scoped permit | the agent's key in `mcpContext`; the rail's own key argument left unused | the boundary protecting a downstream that has no such control |
| **Native baseline + gateway** | `POST /mcp/messages` | both: the gateway key, and the same key passed as the tool argument | the boundary in front of a downstream that has the control |

The rail is one FastMCP tool, `payout.send`, registered with the gateway as
`vendor.payout.send`. It implements the native control the way a payment API
documents it: an optional `idempotency_key` whose stored response is replayed,
and whose reuse with different parameters is refused. Whether a caller uses
that control is the caller's choice; the rail does not change between
configurations.

## The fault

A `LossyTransport` sits below the caller at each hop. When armed, it forwards
the `tools/call` request, waits for the server to finish, discards the
response, and raises a read timeout. The server has already returned, so any
effect it had is already durable; the caller sees what it would see from a
dead socket. The fault is armed for one tool call and then disarms, so the
retry goes through.

There are three hops, and the fault is injected at each one that exists in a
configuration:

```text
agent ──(1)──▶ rail                          existing integration, native baseline
agent ──(2)──▶ gateway ──(3)──▶ rail          gateway configurations
```

- **Hop 1 or 2, the agent loses the response.** The downstream (or the
  gateway) completed; the agent holds nothing and, correctly, retries.
- **Hop 3, the gateway loses the response.** The rail completed; the adapter's
  durable dispatch claim was already committed, so the adapter classifies the
  loss as its own `UpstreamMcpDeliveryUncertainError`, exactly as it would a
  socket timeout after the claim (`_call_tool_once` in
  `app/services/upstream_mcp.py`). The gateway receipts `delivery_uncertain`,
  retains the charge, and must not send again.

Why an application-level failpoint rather than a TCP proxy such as Toxiproxy:
a proxy cannot know when the downstream's effect has landed, so a timeout
toxic drops the request at an arbitrary point relative to the effect. This
fault is defined by ordering (effect first, then loss), and only a transport
that has watched the server return can place it there. The lab's transport is
the same kind of object the adapter is built on (`httpx.AsyncBaseTransport`),
below the MCP client, so no adapter code is bypassed.

## Two rules that keep the numbers honest

1. **Downstream effects are counted by a record the gateway cannot reach.**
   The rail appends one line to `effects.jsonl` every time the tool body
   executes. That count is what every "paid once" or "paid twice" line means.
   Gateway receipts are never used to prove what the gateway prevented.
2. **Gateway dispatches are counted at the downstream's HTTP layer**, by the
   transport that sees every `tools/call` reach the rail, never from the
   gateway's own attempt rows.

Every expectation is asserted. A broken one is recorded in `config.json` and
`report.json`, the remaining scenarios still run, all artifacts are written,
and the process exits non-zero. `tests/test_failure_lab.py` runs the lab in
CI and pins the same numbers, so the report cannot drift from the code.

## What one run shows

Recorded 2026-09-19 on the commit that added the lab. The numbers are
asserted, so a later run either prints the same ones or fails.

| Scenario | Attempts | Downstream effects | Gateway dispatches | Debits | Unresolved | Final known outcome |
|---|---|---|---|---|---|---|
| existing, response lost | lost, success | **2** | n/a | n/a | 0 | second payout settled; the first is unknown to the agent |
| native, response lost | lost, success (replayed) | **1** | n/a | n/a | 0 | the original confirmation, replayed by the rail |
| gateway, lost at agent hop | lost, success | 1 | 1 | 1 | 0 | the original confirmation and the same receipt the lost response carried, verified offline |
| gateway, lost at downstream hop | uncertain, uncertain | 1 | **1** | 1 (retained) | **1** | `delivery_uncertain`; the retry returned the same receipt and dispatched nothing |
| native + gateway, agent hop | lost, success | 1 | 1 | 1 | 0 | as above |
| native + gateway, downstream hop | uncertain, uncertain | 1 | 1 | 1 (retained) | 1 | as above; the rail also holds the stored response under the forwarded key |
| native, agent restart, new key | lost, success | **2** | n/a | n/a | 0 | paid twice |
| gateway, agent restart, new key | lost, success | **2** | 2 | 2 | 0 | paid twice, both correctly receipted |
| native, key reused with a changed amount | success, refused | 1 | n/a | n/a | 0 | refused by the rail |
| gateway, key reused with a changed amount | success, refused | 1 | 1 | 1 | 0 | `idempotency_key_reused`, refused before dispatch |
| controls, no fault (all three) | success | 1 | 1 (gateway) | 1 (gateway) | 0 | ordinary successful workflow |

Read across the lost-response rows:

- **The native baseline already handles this fault.** A client that keeps its
  key against a downstream that honors it pays once, with no gateway
  involved. The report prints that verdict, and the lab fails if it ever
  measures the gateway beating a correct baseline on effect count.
- **What the gateway measurably adds** in this run: one payout and one debit
  when the downstream has no idempotency control (the existing integration's
  2 becomes 1); the confirmation and the signed receipt the agent lost,
  recovered by retrying the same key; and, when the gateway itself is the one
  that lost the response, an ambiguity that is receipted, charged once, and
  never repeated.
- **What no configuration handles:** an agent that restarts, loses its key
  store, and mints a new key for the same invoice pays twice everywhere.
  The gateway's two payouts are both correctly authorized, dispatched,
  debited, and receipted. Identity is the caller's key; the boundary keys on
  it and cannot recover an identity the caller lost. This is printed under
  "Remaining limitations" on every run.
- **Added latency** in this run was about 90 ms per call at the median
  (in-process, no sockets, throwaway SQLite). That bounds the gateway's own
  compute and durable-write overhead; it is not a network measurement.

## The report

`report.txt` follows one fixed layout, so two runs can be compared line by
line:

```text
Workflow tested: [name]
Integration version: [versions and commit]
Fault injected: downstream action completed; response lost

Existing integration (no idempotency key):
  Tool call attempts:      [observed]
  Downstream effects:      [observed]
  Unresolved outcomes:     [observed]
  Final known outcome:     [observed]

Native idempotency, correctly used (key persisted across the retry):
  ...

With gateway, response lost between gateway and agent:
  Gateway dispatches:      [observed]
  Downstream effects:      [observed]
  Debits:                  [observed]
  ...

With gateway, response lost between downstream and gateway:
  ...

Agent restart, new key for the same invoice (no configuration recovers this):
  ...

Remaining limitations:
  - [measured in this run, never prose alone]

Reproduce:
  make failure-lab
  saved run: data/failure-lab/<run>/
```

`report.json` carries the same content for machines, plus per-scenario
checks with observed and expected values, `verdicts`, and `comparison`
tables. The run directory also holds `events.jsonl` (every attempt, every
hop crossing, every fault arming, every lost response with what it carried,
every downstream effect, every offline verification, sequence-numbered),
`effects.jsonl` (the independent downstream record), `config.json` (versions,
commit, configuration, the scenario sequence and its checks), and the
throwaway gateway database. No artifact contains an API key, the admin key,
the upstream bearer, or the signing seed; the CI test checks for all four.

One directory holds exactly one run. The event history and the effects log
are append-only while the reports are overwritten, so a second run into the
same directory would leave them disagreeing with the report. The generated
run id is unique; when `--run-id` names a directory that already holds a run,
the lab refuses to start rather than append to or overwrite that evidence.

## What it does not show

- **Crashes.** The fault is a lost response, not a dead process. Kills at
  each durable commit boundary are the PostgreSQL process-kill proof
  (`make prove-crash-recovery`, [PROOF_MATRIX.md](PROOF_MATRIX.md)).
- **Concurrency.** One agent, sequential retries. The 15-way identical
  concurrent admission is in `make trust-conformance-live`; budget races are
  in `tests/test_permits.py` and the `postgres_permit_concurrency` job.
- **Tampering.** Receipts are verified offline here; the edited-copy and
  unknown-key rejections are in `make demo-ambiguous-retry` and
  `make prove-trust-plane`.
- **A real payment API.** The rail moves no money, and its idempotency
  semantics are a model of a documented API, not that API. Retention windows
  and provider-specific conflict codes are not modeled.
- **Resolution of `delivery_uncertain`.** The gateway preserves the
  ambiguity; it does not close it. The lab's independent record shows the
  effect landed once, and the gateway cannot know that. With the key
  forwarded, the rail's stored response is what an operator would use to
  close it out of band; that step is manual and outside the boundary.
- **A network.** The added-latency figure excludes it entirely.

## From lab to diagnostic

The lab is built so that the part worth reusing is separable from the part
that is simulated:

- `LossyTransport` and `FaultInjector` know nothing about payouts. They drop
  the response to any `tools/call` at whichever hop they are placed.
- `PaymentRail` is the only simulated piece. Any Streamable HTTP MCP tool
  with an observable effect can take its place, provided the effect can be
  counted independently of the gateway (a table, a file, a sandbox API's own
  event list).
- The gateway setup is the documented partner configuration
  ([partner-first-tool-runbook.md](partner-first-tool-runbook.md)) driven
  through `register_configured_upstream_mcp` with an injected HTTP client,
  which production refuses; the lab runs with `ENVIRONMENT=local` on purpose.

A diagnostic against a team's own sandbox tool would keep the three
configurations, the report layout, and the two counting rules, and swap the
rail. It must keep the property this lab enforces: when the team's existing
integration already handles the fault, the report says so. That is not built
here; this document records the design so the first version of it starts
from the same numbers.
