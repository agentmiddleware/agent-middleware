# Trust Model

The trust model is intentionally narrow: governed MCP tool calls are the first
hard trust boundary.

## Signed Permits

`POST /v1/permits` creates Ed25519-signed authority for a wallet-scoped agent.
A permit binds:

- issuer wallet
- subject wallet
- optional subject API key
- allowed tools
- scopes such as `tool:{tool_name}:invoke` and `billing:charge`
- maximum credits
- expiry
- nonce
- revocation state

Permit creation requires `Idempotency-Key`. Reusing the same key with the same
request returns the original permit. Reusing it with a different request fails
with `409 Conflict`.

## Governed MCP Calls

A governed MCP call supplies `permit_id` and `idempotency_key` in `mcpContext`
or the `Idempotency-Key` header. The server validates wallet binding, key
binding, tool scope, budget, expiry, revocation, and signature before charging.

Legacy wallet-only MCP calls remain available while `TRUST_MODE_ENABLED=false`.
Production trust mode should run with `ALLOW_LEGACY_UNPERMITTED_MCP=false`.

## At-Most-Once Debit, Bound To The Idempotency Record

This is the primitive the rest of the model exists to protect, and the one that
distinguishes this system — signed receipts do not (see
[`WEDGE.md`](WEDGE.md) §"Signed receipts are table stakes now").

One accepted idempotency key produces **at most one gateway dispatch to the
configured upstream MCP tool, and at most one ledger debit**, linked by a single
persisted chain — and **exactly one receipt on every path that finalizes or
reconciles**. The dispatch half of that guarantee is enforced by a durable
dispatch state machine that exists **only on the upstream path**; local governed
tools have no attempt row and fail closed into manual review instead (see
below), so for them the at-most-one debit guarantee still applies, but a receipt
exists only on a finalized or reconciled path — a post-effect crash can leave
the debit recorded with no receipt, pending manual review (see below). The ledger entry carries
the idempotency record's identity as its `operation_key` under a uniqueness
constraint, so a duplicate debit cannot be written even if two processes race.
Replaying the same request under the same accepted key returns the original
result and receipt without a second dispatch or debit **once that record has
reached a terminal state**; replaying a *changed* request under that key fails
closed with `409 Conflict`. A record still in progress replays as in-progress,
and a local attempt stranded by the crash below stays that way pending manual
review rather than ever returning a receipt it does not have.

**Why "at most one" and not "exactly one."** Two real paths produce neither a
dispatch nor a net debit, and one produces no receipt at all:

- A crash *before* dispatch is reconciled to a refund, so the call never
  dispatched and the wallet nets zero
  (`test_kill_between_debit_and_dispatch_refunds_without_dispatching`).
- A **local** governed tool that crashes *after* its side effect leaves one
  execution and one debit with **no receipt**, permanently `needs_manual_review`;
  reconciliation deliberately does not finalize it, and replay stays
  in-progress rather than redispatching
  (`test_post_side_effect_crash_requires_review_without_redispatch` asserts
  `receipt_ids == ()`).

The guarantee is therefore never a duplicate charge, not always a charge — and
the receipt guarantee holds for the upstream path and every reconcilable
outcome, not for the local post-effect crash. Overstating this as "exactly one
receipt" would contradict the crash behaviour documented below.

Budgets are enforced before money moves: a permit's cap is checked in the same
atomic conditional update that consumes it, so concurrent invocations cannot
overspend it.

**Ambiguity is a first-class durable state.** For the configured upstream MCP
tool, a dispatch attempt is persisted through
`prepared → dispatch_claimed → {succeeded, returned_error,
delivery_uncertain, response_rejected}`. Immediately before the network send,
the activation commits `dispatch_claimed` with one nullable
`dispatch_claim_hash`. The raw process-local claim is not persisted; only that
still-live activation may match its stored hash after a lost commit
acknowledgement. A fresh activation cannot reacquire the claim. Historical
`dispatched` rows remain classified as already sent even though their claim hash
is null.

If the process dies after the claim becomes durable but before a trustworthy
result is committed, recovery classifies the attempt as
`delivery_uncertain`: **the charge stands and the call is never redispatched**,
because non-delivery can no longer be proven. Reconciliation waits a fixed,
globally conservative 11,430-second window before treating the claim as stale.
That window covers the maximum supported 600-second connection timeout, three
maximum 3,600-second call phases, and cleanup margin; it does not shrink when a
new worker is deployed with smaller local settings.

By default, reconciling a `delivery_uncertain` attempt is an operator task. A
deployment can optionally register a per-upstream-origin status lookup. The
gateway ships with none registered, so the feature is inert until a deployment
wires one. There is no environment setting. A deployment calls
`register_upstream_status_lookup(origin, lookup)` from
`app.services.mcp_dispatch_reconciliation` in its own startup code. The
production cleanup singleton reads that registry on every sweep, so
registering before or after the singleton exists both work. Tests and custom
callers can pass `status_lookups` to the constructor instead.

For a stale `dispatch_claimed` attempt, the reconciler asks the lookup before it
writes the terminal state. The lookup gets an immutable query: attempt id,
forwarded invocation id, stored idempotency key, forwarded idempotency key,
origin, tool, and request hash. It does not get the executor, so it cannot
redispatch. Each lookup is bounded by a 5-second timeout
(`STATUS_LOOKUP_TIMEOUT_SECONDS`). A hang counts as no answer, and sweeps are
serial, so one sweep spends at most `limit` × 5 seconds on lookups.

The only outcome a lookup can force is provider-confirmed delivery.
`delivered` must be a real `bool` (`True`, not `1` or `"yes"`). It must also
come with the raw MCP `CallToolResult` payload. That payload goes through the
live path's own result gate, reached through the registered upstream executor
for the same origin and tool. That gate checks the shape, rejects a reflected
bearer token, enforces the size bound, and reads `isError`. A normal result
finalizes as `succeeded` and keeps the charge. An `isError` result finalizes as
`returned_error` with `upstream_returned_error` and is refunded, exactly as on
the live path. Any other answer leaves the attempt `delivery_uncertain`:

- an exception, a timeout, or `None`
- a malformed answer or a non-`bool` `delivered`
- a delivered answer with no result, or a result that fails the gate
- no matching executor
- a result that cannot be stored

**Provider-confirmed non-delivery does not refund.** The dispatch claim is a
one-shot hash with no lease and no fence. A worker that already holds the claim
can pause (GC stop, VM freeze, SIGSTOP), outlive the stale window, resume, and
still call the upstream. The guarded terminal transition protects the attempt
row, not the network send. A refund issued on the provider's "not delivered"
could therefore pay back a call that then executes. A safe refund needs a
claim lease or fence that the sender re-checks before sending. That requires a
schema change, so until one exists, `delivered=False` is logged
(`mcp_dispatch_status_lookup_not_delivered_unfenced`) and the attempt stays
`delivery_uncertain` for manual reconciliation.

The resolution goes through the same guarded terminal transition as the live
finalizer. If a live owner or another sweep lands first, this sweep adopts that
terminal state, so the attempt resolves exactly once. Because the outcome is
decided before the first receipt is signed, no receipt is ever rewritten.
Attempts that already hold a signed `delivery_uncertain` receipt, and
historical `dispatched` rows, are never offered to the lookup.

Known limitation: action-permit calls forward a derived native idempotency key
that the attempt row does not record. For those calls,
`forwarded_idempotency_key` is `None`, and a lookup must match on
`invocation_id` (`io.agentmiddleware/invocation_id`). A lookup is only as
correct as the provider's status answer.
Local (non-upstream) governed tools have no dispatch state machine; a crash
there fails closed into manual review instead. See
[`docs/failure-semantics.md`](docs/failure-semantics.md).

This is at-most-one *gateway* dispatch plus refusal to redispatch an ambiguous
invocation. It is **not** effect-once inside an arbitrary upstream tool, which
requires that tool to honour the forwarded idempotency key. The claim record is
also not proof that any downstream effect occurred.

The durable claim and send-state transitions apply only to the configured
remote upstream boundary. The supporting operation-keyed billing transaction
fence is shared by other callers, but this slice does not change local-tool
reservations, per-tool call slots, quotes, human approvals, API-key/JWT
authentication, or rate limiting.
Focused state-machine, reconciliation, migration, and PostgreSQL process-kill
tests cover the claim transition. Deployment and downstream-effect truth remain
outside that evidence.

## Signed Receipts

Every successful governed MCP call produces a signed receipt linked to the
permit, ledger entry, audit event, tool, request hash, response hash, and cost.
Denied and failed governed attempts produce signed denial/failure receipts when
a valid permit was present.

For a governed call to the **configured upstream MCP tool**, the receipt's
Ed25519 signature covers the **ledger entry, the idempotency record, and the
dispatch attempt together**, so the issuer's statement about authority, money,
and delivery outcome is one tamper-evident unit rather than three separable
assertions. Local governed tools have no dispatch-attempt row, so their receipts
bind the ledger entry and idempotency record only — the three-way binding is an
upstream-path property, not a universal one. Signed receipts are widely available elsewhere; that
binding is what this signature adds.

**What offline verification does and does not establish.** The portable bundle
(`GET /v1/receipts/{id}/portable`) carries the `signing_input`, the signature,
and a key reference — not the ledger or dispatch records themselves. A holder
with no credential here can therefore verify that *this issuer signed these
identifiers together and nothing has been altered since* — provided they already
hold the trusted key set. The bundle carries a key *reference*, not the key:
`verify_bundle` takes a caller-supplied key set, normally fetched once from the
unauthenticated `/.well-known/trust-keys.json` and pinned. Offline means no call
to the issuer at verification time, not zero setup. They cannot
independently confirm that the named ledger entry carries the matching
`operation_key`, because that record does not travel with the bundle.

That consistency is enforced where the records are written — `attach_charge`
rejects a ledger row whose `operation_key` is not the idempotency record, and
`receipts.idempotency_record_id` / `dispatch_attempt_id` are unique foreign
keys — and it is checkable after the fact through the authenticated evidence
bundle (`GET /v1/receipts/{id}/evidence`). So the correct claim is **signed and
tamper-evident offline; consistency enforced at write time and auditable
online** — never "the linkage is verifiable offline."

## Audit Chain

Control-plane audit events include a payload hash, previous hash, chain hash,
signature, and signing key ID. `/v1/audit/verify-chain` verifies wallet-scoped
audit integrity and detects silent mutation.

## Key Material

The database stores public keys and key metadata only. Production private keys
must come from configured secret material or KMS-backed injection. Local tests
may use process-ephemeral keys; they are not persisted.
