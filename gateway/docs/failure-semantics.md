# Failure Semantics — when a metered call dies mid-flight

A governed tool call can fail before the debit, after the debit, during the
upstream dispatch, after the upstream executed, or while the result is being
committed — and a process crash can interrupt any of those. This document
defines the terminal outcome for every one of those cases: what the caller is
told, whether the wallet ends up charged, whether the tool may have executed,
and which test proves it.

Like [PROOF_MATRIX.md](PROOF_MATRIX.md), this is descriptive, not aspirational.
Each row names its proving tests, and the last section lists what is deliberately
**not** claimed. For the claims the project never makes, see
[SECURITY_LIMITATIONS.md](../SECURITY_LIMITATIONS.md).

## The invariant

Every governed invocation that terminalizes or is reconcilable ends in
**exactly one signed terminal accounting**: an Ed25519-signed receipt (plus a
chain-hashed audit event) stating the outcome and the net charge. A local
post-effect crash is the explicit exception: it remains in manual review with
no receipt and no automatic redispatch.
Money only moves next to a durable record that a reconciler can later
compensate:

1. Budget reservation and the durable dispatch-attempt row are written in
   **one transaction** (`authorize_reserve_and_prepare`,
   `app/services/mcp_dispatch_attempts.py`) — a failed transaction leaves
   neither.
2. The **debit strictly precedes dispatch**, is keyed to the idempotency
   record (`operation_key`), and is linked to the attempt before the upstream
   call starts (`_charge_and_checkpoint`, `app/routers/mcp.py`).
3. A durable, one-shot **dispatch claim** (`claim_dispatch`) is committed
   immediately before the network send. The transition stores
   `state = dispatch_claimed` plus one nullable `dispatch_claim_hash`; the raw
   process-local claim is not persisted. Only the still-live activation that
   created that hash may recover a lost commit acknowledgement. Every later
   activation must fail closed without sending.

The dispatch attempt is a small state machine — `prepared → dispatch_claimed →
{succeeded, returned_error, delivery_uncertain, response_rejected}`, with
`prepared → returned_error` as the only pre-dispatch terminal. Terminal states
are absorbing. Historical `dispatched` rows have a null claim hash and remain
classified as already sent; they are never claimable again.

## Terminal outcomes

| Outcome | Trigger | Wallet | Gateway observation | HTTP / JSON-RPC | Proven by |
|---|---|---|---|---|---|
| `success` | Tool returned a valid result; receipt committed | **Charged** | A valid response reached the gateway; the downstream effect is not independently proven | 200 | `test_governed_mcp_invoke_returns_receipt_and_replay_does_not_double_charge`, `test_governed_upstream_success_replays_without_second_debit_or_dispatch` |
| `denied` | Permit, wallet-policy, recipient-domain, or human-approval denial — all evaluated **before money moves** | Never debited; reservation released | No gateway dispatch | 403 / `-32003` | `test_frozen_wallet_denial_returns_receipt_and_replays_without_charge`, `test_out_of_scope_governed_mcp_denial_returns_receipt`, `test_rejected_approval_is_terminal_and_replays`, `test_upstream_permit_denials_never_charge_or_dispatch` |
| `insufficient_funds` | The charge itself was refused (balance, child-wallet cap, daily limit) | Never debited; reservation released | No gateway dispatch | 402 / `-32004` | `test_insufficient_funds_returns_receipt_and_replays_without_charge` |
| `failed_refunded` | A **confirmed** failure: local tool raised; upstream returned `isError: true`; DNS/TLS/`initialize` failed before the remote dispatch claim; charge refused on the upstream path; or a crash before dispatch, finalized by the reconciler | Debit refunded (or never taken). The receipt signs `credits_charged = 0`; the ledger separately proves the correlated refund | An `isError` response may have reached the gateway; pre-claim cases were not dispatched; no downstream effect is independently proven | 500 or 502 / `-32006` (402/403 for refused charges) | Pre-claim failure, guarded completion, and reconciliation tests in `tests/test_mcp_upstream_governed.py`, `tests/test_mcp_dispatch_router_claim.py`, and `tests/test_mcp_dispatch_reconciliation.py` |
| `delivery_uncertain` | The one-shot remote dispatch claim was committed, then a timeout, transport failure, or process death left the outcome unknowable | **Stays charged.** Never redispatched | **Unknown** — that is the definition | 504 / `-32005` | Stale-claim reconciliation and PostgreSQL process-kill cases |
| `response_rejected` | The upstream **did** respond, but the response is unusable: invalid shape, reflected bearer token, non-serializable, or over the byte cap (wire-level or at persistence) | **Stays charged** | A response reached the gateway; the downstream effect is not independently proven | 502 / `-32006` | `test_confirmed_result_rejected_by_persistence_is_terminal_and_replayable`, `test_terminal_response_rejected_retains_charge_and_is_replayable` |
| `failed_unrefunded` | A refund was owed and the refund **itself** failed | Charged; a durable operator work item is created atomically with the receipt | Depends on the underlying failure | 500 / `-32603` | `test_governed_refund_failure_keeps_permit_budget_reserved`, `test_refund_reconciliation_retries_exactly_once_and_preserves_agent_replay` |

Requests rejected before a valid permit and an executable tool are established
produce no receipt — there is no authority to bill against. The README states
this limit. What they leave behind differs by reason:

- An unknown tool (`Tool not found`) or `permit_not_found` on a governed call
  that carries an idempotency key completes a terminal *idempotency record*;
  a same-key replay returns the original error envelope.
- `permit_required` on a resolved tool, and a resolved tool that is not
  executable, record nothing: the governed idempotency record is only begun
  after the permit check, so the completion call in `app/routers/mcp.py` is a
  no-op. A same-key retry returns the same error while nothing has changed,
  and after minting a permit it executes normally
  (`test_strict_mode_missing_permit_denial_leaves_no_idempotency_record`).
  Whether that stays the contract is the open D2 decision in
  [authority-required-flow.md](authority-required-flow.md).

A failure the pipeline did not classify — an exception that is none of the
typed denials, tool failures, or reason-coded errors above — is reported as
`-32603` / `internal_error` with a `correlation_id` in `data`. The exception
text is logged server-side under that id and never returned.

## When no terminal accounting can be written

One state produces neither a receipt nor an unclassified failure: the audit
event or the receipt itself is lost to write contention **after** the call has
already run or the wallet has already moved. The receipt is built from the
audit event's id, so a lost audit event means no receipt is possible; a lost
receipt insert means the same thing one step later. Either way the invocation
has effects and no terminal accounting, and `reconcile_stuck_records` counts
it for manual review rather than completing it — this is the "local post-effect
crash" the invariant above carves out.

It answers `-32007` (HTTP 500) with `reason_code` `audit_chain_contended_after_effects`
or `receipt_write_contended_after_effects`, and a `remediation` of
`reconcile_out_of_band`. It is **not** `-32005`: a retry would invite a second
execution of a call the caller has paid for. It is **not** `-32603` either,
which is reserved above for failures the pipeline *did not* classify — these
two sites classify deliberately, checking whether effects were committed before
choosing non-retryability, and reporting that decision through the unclassified
channel hid a known state among genuine bugs.

What the code claims is the situation, not the outcome. Whether the tool's
effect landed is exactly as unknowable as before; what is now stated is that
effects are committed, no receipt exists, the record is held for manual review,
and the caller must not mint a fresh idempotency key. A governed record, where
one was opened, stays held by the invocation that ran and answers
`idempotency_in_progress` on the same key until an operator resolves it.
Proven by `tests/test_audit_chain_contention_surface.py` and
`tests/test_receipt_write_contention_surface.py`, which also pin that an
ordinary bug in the same window still returns `-32603`.

## Why `delivery_uncertain` cannot be "fixed"

The durable dispatch claim is written immediately before the network
send (`before_dispatch` in `app/services/upstream_mcp.py`). Every failure
**before** that claim is provably non-delivered, so it is safe to refund
and sign `failed_refunded`. Every failure **after** it is inherently
ambiguous: the upstream may or may not have executed, and no amount of gateway
logic can find out. The design response is to *preserve the ambiguity
honestly* rather than guess:

- the debit is retained and the receipt says so;
- the server **never redispatches** — the HTTP transport is built with
  `retries=0`, the adapter invokes the tool at most once, the claim transition
  permits only one activation, terminal states are absorbing, and the
  reconciler imports no executor at all;
- resolution belongs to the caller and the upstream provider, using the
  forwarded idempotency metadata (below) and the signed evidence.

Retaining the charge is a deliberate incentive choice: an ambiguous call that
auto-refunded would pay callers to induce timeouts against providers whose
side effects completed.

## Crash windows

A worker process can die at any point in the sequence. The reconciler
(`app/services/mcp_dispatch_reconciliation.py`) finalizes what the crash left
behind — "without ever redispatching", per its own docstring. Faults are
injected at each durable boundary in the tests named below.

| Window — process dies… | Detected by | Resolution | Money |
|---|---|---|---|
| **A. after debit, before dispatch** (attempt `prepared`) | Stale-active sweep; the debit is found by its `operation_key` even if the crash preceded `attach_charge` | `failed_refunded` (`reconciled_stale_prepared`), signed receipt + audit, replay returns 502 | **Refunded**, budget released |
| **A′. A, proved by an actual process kill** (worker killed the instant the debit commits, before `attach_charge`) | Stale-active sweep, after a real `SIGKILL` across two OS processes | `failed_refunded`; the orphaned debit is adopted by operation identity and the upstream is never contacted | **Refunded** exactly once, budget released |
| **B. after the claim, before a trustworthy response** (attempt `dispatch_claimed`) | Stale-active sweep after the fixed maximum-lifetime idle window | `delivery_uncertain`, signed receipt + audit, replay returns 504; no gateway redispatch | **Stays charged** |
| **B′. B, with a landed remote effect** (attempt still `dispatch_claimed`) | Same stale-active sweep | `delivery_uncertain`; the gateway does not send again, but this record does not prove whether or how many downstream effects occurred | **Stays charged** |
| **C. after the response, before the receipt commit** (attempt terminal, no receipt) | Terminal-without-receipt sweep; the stored canonical result is **re-hashed byte-exact** before reuse (`dispatch_stored_result_hash_mismatch` on any tamper) | The recorded terminal state is adopted and signed; a recorded `returned_error` completes its refund | Per the recorded state |
| **D1. lost COMMIT ack on reserve+prepare** | Recovery re-read adopts only an invariant-identical `prepared` row | Same attempt id returned; no second reservation | Unchanged (`test_lost_commit_ack_recovery_no_double_charge`) |
| **D2. lost COMMIT ack on the dispatch claim** | Only the still-live activation whose generated hash matches the stored `dispatch_claim_hash` may adopt the committed claim | That activation may continue to one send; a fresh caller cannot acquire the claim | **Stays charged** if the claim later becomes ambiguous |
| **D3. lost ack on the final completion** (receipt exists, idempotency record empty) | Receipt-present / response-`NULL` sweep, which runs **before** the generic sweep | The replay record is rebuilt from the signed receipt; an existing `failed_refunded` receipt **blocks** a second budget release | Unchanged |
| **E. before any effect** (no attempt, no debit, no receipt) | Effect-free idempotency sweep | The record is deleted so the same key can genuinely retry. **Governed remote dispatch only** — the sweep is scoped to `operation_kind == "upstream_mcp"`, because a local tool's side-effect ordering is not observable to the reconciler. A local-path crash in the same window is held in progress rather than released, and the key cannot be reused until an operator intervenes. | Nothing ever moved |
| **F. reserved, then died before charging** (budget reserved, no debit) | Permit budget reconciliation, but only once the permit can no longer admit a charge | The reservation is left in place on a still-active permit: a long-running call is indistinguishable from a dead one, so reclaiming early could let a concurrent request over-spend. It is reclaimed when the permit is revoked or expires. | **Nothing charged**; the agent can spend less than authorized, never more |

The established pre-claim and terminal recovery behavior is covered by
`tests/test_mcp_dispatch_reconciliation.py` (including
`test_crash_between_debit_and_dispatch_reconciles_refund`,
`test_kill_between_dispatch_and_response_becomes_delivery_uncertain`,
`test_terminal_success_is_reconstructed_from_bounded_result`,
`test_existing_receipt_missing_idempotency_completion_replays_full_result`,
`test_effect_free_stale_mcp_identity_is_released_for_safe_retry`) and by the
multi-process kill tests in `tests/test_mcp_postgres_multiprocess.py` — of
which `test_kill_after_dispatch_checkpoint_is_charged_delivery_uncertain` and
`test_kill_after_remote_effect_never_redispatches_the_effect` cover window B by
killing a real worker on either side of the remote effect, and
`test_kill_between_debit_and_dispatch_refunds_without_dispatching` covers
window A the same way, rather than by seeding the durable state in process.

Focused state-machine and reconciliation tests cover the `dispatch_claimed`
transition, hash ownership after a lost commit acknowledgement, claim
contention, and the fixed stale window. The PostgreSQL process-kill harness
covers the durable claim boundaries across independent workers.

A claim becomes eligible after the fixed 11,430-second idle threshold: enough
for the maximum supported 600-second connection timeout, three maximum
3,600-second call phases, and cleanup margin. It is processed by the next
five-minute sweep, so nominal pickup can take about 11,730 seconds, plus any
bounded-batch backlog. Using the global ceiling instead of each worker's
current settings prevents a rollout with shorter timeouts from declaring an
older worker's valid long-running call stale.

Reconciliation itself is idempotent by construction: audit event ids are
derived deterministically from the attempt id, refunds are keyed
`refund-{ledger_entry_id}`, budget release happens at most once, and receipts are
unique per idempotency record — so two reconcilers racing produce one receipt
and one audit event
(`test_concurrent_dispatch_reconcilers_create_one_signed_audit_in_postgres`),
and a repeated sweep repairs zero rows. Crash-recovered receipts sign the
same permit-constraint snapshot bytes a live receipt would
(`test_reconciler_constraints_snapshot_matches_live_path`).

## The replay contract

A completed or terminalized invocation replays its stored envelope without
re-execution or a second charge, including the original receipt where one
exists. An unfinished local manual-review record instead remains
`idempotency_in_progress` and has no receipt. Replay access re-validates the
permit↔wallet↔key binding first, so a revoked permit or a different key cannot
read the stored receipt.

| Stored terminal state | Replay returns |
|---|---|
| `success` | 200 with the stored result and the same `receipt_id` |
| `denied` | 403 / `-32003`, original reason and receipt |
| `insufficient_funds` | 402 / `-32004` |
| `failed_refunded` | 500/502 with the upstream error detail where recorded |
| `delivery_uncertain` | 504 / `-32005`, with the dispatch attempt reference |
| `response_rejected` | 502 / `-32006` |
| `failed_unrefunded` | 500 with the reconciliation status — a stored claim of `resolved` is **re-validated against the ledger on every replay** and rejected if unsubstantiated |

Two non-terminal answers matter to clients:

- **`idempotency_in_progress`** — another request holds this key with no
  terminal result yet. Upstream calls wait a bounded window for the winner;
  local calls answer immediately. Retry the **same** key; do not mint a new
  one, because the original may already have had a side effect.
  The claim fence also uses this response for an uncertain
  preparation commit and for a losing dispatch claimant. That is an observable
  correction from the earlier generic upstream-prepare failure on the rare
  uncertain-commit path. REST preserves the existing `400` response and
  JSON-RPC preserves the existing `-32003` code.
- **`idempotency_key_reused`** — the same key arrived with a *different*
  logical payload. Fail-closed; nothing is dispatched or charged
  (`test_governed_upstream_conflicting_payload_reuse_never_redispatches`).

The idempotency identity is the *logical* call — transport-independent, so a
JSON-RPC call and its REST retry share one identity
(`test_governed_upstream_replays_across_jsonrpc_and_rest_transport`).

## What counts as a key

A client-supplied key is honored exactly as sent, and only when it is usable:
a JSON string with at least one non-whitespace character, at most 128
characters (the width of the durable `idempotency_records.idempotency_key`
column; the Python SDK enforces the same bound before sending), and no control
characters. The gateway never strips, truncates, stringifies, or regenerates a
key on the caller's behalf, so two distinct valid keys stay two distinct keys.

A key that is **present but unusable** — the wrong JSON type, blank, too long,
carrying control characters, or supplied in two sources (`Idempotency-Key`
header and body) that disagree — is refused with invalid params (JSON-RPC
`-32602`, REST `400`) and a machine-readable
`reason_code` (`idempotency_key_not_a_string`, `idempotency_key_blank`,
`idempotency_key_too_long`, `idempotency_key_control_characters`,
`idempotency_key_not_utf8`, `idempotency_key_conflict`). The refusal happens before a permit is minted,
anything is reserved or charged, or anything is dispatched. Substituting a
generated key there would silently defeat the retry protection the caller
asked for: every retry carrying the same malformed key would become a new
charged operation (`test_mcp_idempotency_key_validation`).

Only a genuinely **absent** key — no header, no body entry, or a JSON `null`
entry — is treated as an un-keyed call. On the standard MCP endpoint (and the
`/mcp/messages` demo auto-permit path) an un-keyed call to a mutating tool
gets a deterministic `mcp-retry-` key from the caller's wallet and API key,
the verified enterprise principal (provider, issuer, subject) when an IGA
bearer is present, the tool, and the canonical JSON of the arguments. The
JSON-RPC id is not part of it, because retries carry fresh ids.

- **Sliding window.** Slot selection and the auto-permit mint run under a
  per-identity lock row. A slot first minted less than
  `MCP_UNKEYED_RETRY_WINDOW_SECONDS` (default 300) ago is joined even if the
  clock bucket has rolled over, so a retry or concurrent twin straddling the
  edge resolves to one dispatch. A concurrent duplicate either replays the
  same receipt or gets the retryable `idempotency_in_progress`. A lock older
  than 120 s is treated as abandoned and can be taken over. The auto-permit
  mint row is written only if the writer still owns the lock (checked in the
  same transaction as the insert), so a stalled former holder never mints or
  dispatches, and it releases only its own lock row.
- **IGA before replay.** A replay of a stored governed result re-runs the IGA
  check (without spending a use) first, and the principal is part of the key,
  so one human never receives another's stored result.
- **Mutating tools only.** Tools registered `read_only` (for example
  `partner.notes.count`) keep a fresh key per call. Undeclared tools are
  treated as mutating.
- **No-effect failures retry.** If the stored result is a refunded
  pre-dispatch failure, the retry moves to the next attempt slot and runs
  again (up to three attempts per window) instead of replaying the failure.

Two deliberately identical un-keyed calls inside one window still collapse
into one; clients that need repeats must send their own key. Setting the
window to `0` restores a random key per call. A
governed `/mcp/messages` call without a key is still refused with
`idempotency_key_required`, and a wallet that requires human approval still
needs an explicit key.

The governed AWI HTTP routes — `POST /v1/awi/execute`,
`/v1/awi/passkey/challenge`, `/v1/awi/passkey/verify`, `/v1/awi/dom/sync`,
`/v1/awi/rag/index`, and `/v1/awi/rag/query` — hold their `Idempotency-Key`
header to this same contract. There the key is mandatory: no header at all is
HTTP 400 `idempotency_key_required`, and a header that is present but unusable
(blank, over 128 characters, control characters, or repeated lines that name
different keys) is HTTP 400 `invalid_idempotency_key` with the same
`reason_code`, `source`, and `remediation` fields plus the governed `tool`,
refused before the permit is validated and before any idempotency record
exists. The stored identity is the header value exactly as received — it is
not trimmed, so `' k'` and `'k'` are two different keys
(`test_awi_http_governance`).

Two details of *where* a key is read: on `POST /mcp` the body sources are
`params._meta["io.agentmiddleware/idempotency_key"]` and, for a client written
against the legacy transport, `params.mcpContext.idempotency_key` (the rest of
that object is ignored there; the endpoint mints its own permit), and a
`mcpContext` that is not an object is `-32602`. An `Idempotency-Key` header,
on every surface that reads one, is decoded as UTF-8 and nothing else: a
non-ASCII key sent in the header equals the same key sent in the body instead
of being read as latin-1 mojibake, and bytes that are not valid UTF-8 are
refused (`idempotency_key_not_utf8`) rather than read as latin-1, so two
different wire values can never alias to one stored key
(`test_mcp_transport_hardening`).

Before any key is read, both transports refuse a body the reply could not
carry: JSON that only Python's parser accepts (`NaN`, `Infinity`, an
overflowing number, a lone-surrogate escape) would execute, debit, and then
fail to answer, and a body nested deeper than 100 levels would hit the
parser's recursion limit. `POST /mcp/messages` answers every such body, and a
non-UTF-8 one, as HTTP 400 `Invalid JSON`. `POST /mcp` answers an unechoable
body with `-32600` and non-UTF-8 bytes with `-32700` (both HTTP 400), and an
over-nested body with `-32602`; a plain syntax error is the SDK's own
`-32700`. No permit, record, debit, or dispatch exists for any of them
(`test_mcp_legacy_envelope_validation`, `test_standard_mcp_endpoint`).

## Refunds are written at most once

`failed_refunded` means the correlated refund is already durable — the receipt
signs a net charge of zero, and the evidence layer independently requires the
refund ledger row to exist. When the refund itself fails, the system does not
pretend: it signs `failed_unrefunded` **atomically with** a durable
reconciliation work item, and an operator (bootstrap-admin only) retries it
later. That retry cannot double-refund under concurrency: the refund entry id is
the deterministic `refund-{ledger_entry_id}`, so even a refund whose commit
ack was lost is recognized rather than duplicated
(`test_lost_refund_ack_releases_budget_without_double_refund`,
`test_concurrent_refund_reconciliation_is_exactly_once_in_postgres`). A forged
`resolved` claim without the matching ledger row fails closed on every read
and replay (`test_forged_resolved_reconciliation_without_refund_fails_closed`).

## What this deliberately does not claim

1. **Post-dispatch ambiguity is never resolved automatically.** No retry, no
   probabilistic refund. The charge stands, the receipt says
   `delivery_uncertain`, and reconciliation with the upstream is the caller's
   and operator's job.
2. **Remote exactly-once and downstream-effect proof are not claimed.** The gateway forwards
   `io.agentmiddleware/invocation_id` and `io.agentmiddleware/idempotency_key`
   in the MCP call metadata; a remote side effect is deduplicated only if the
   upstream honors them. `dispatch_claimed` proves only that the gateway
   committed its one-shot send authority, not that the effect happened.
3. **Crashed reservations on still-live permits are not reclaimed early.** A
   long-running call and a crashed one are indistinguishable from outside, so
   the reservation is conservatively held until the permit expires.
4. **A local tool's post-side-effect crash goes to operator review, not
   recovery.** With no persisted response there is nothing safe to
   reconstruct; the row is counted for review and the replay stays
   in-progress (`test_post_side_effect_crash_requires_review_without_redispatch`).
5. **There is a blind window after a crash.** A claim is eligible only after
   the fixed 11,430-second idle window and is picked up by the next five-minute
   sweep, in bounded batches; nominal pickup can therefore take about 11,730
   seconds plus backlog. Backlog is surfaced in
   `/health/dependencies` and the operator dispatch summary rather than hidden.
6. **Crash proofs are boundary-instrumented.** Faults are injected at the
   durable commit points listed above — not at arbitrary instructions, not
   database failover, not multi-node HA (see PROOF_MATRIX).
7. **Cross-process correctness is PostgreSQL's row locks.** The SQLite test
   path adds a process-local lock for determinism; it is not the
   correctness boundary.
8. **The durable claim and send-state transitions are remote-only.** The
   supporting operation-keyed billing transaction fence is shared by other
   callers, but this slice does not change local-tool reservations, per-tool
   call slots, quotes, human approvals, API-key/JWT authentication, or rate
   limiting.

## Verify it yourself

```bash
# The failure-path suites named throughout this document:
uv run --with-requirements requirements.txt python -m pytest -q \
  tests/test_mcp_trust.py \
  tests/test_mcp_upstream_governed.py \
  tests/test_mcp_dispatch_reconciliation.py \
  tests/test_refund_reconciliation.py \
  tests/test_mcp_dispatch_evidence.py \
  tests/test_governed_persistence.py

# Multi-process kill tests (PostgreSQL required):
# tests/test_mcp_postgres_multiprocess.py — see PROOF_MATRIX.md

# The whole trust-plane gate, including all of the above:
make trust-coverage-gate

# The lost-response fault, measured against a correctly used native
# idempotency key as well as the naive integration (docs/failure-lab.md):
make failure-lab
```
