# Policy Enforcement — MCP Tool-Call Interception

**What this covers:** how the trust plane governs every MCP `tools/call` at
runtime. Enforcement is a **fail-closed, defense-in-depth pipeline**: each gate
runs in a fixed order, denies on the first failure, writes an audit event, and
— on the governed path — emits a **signed denial receipt**. Nothing is charged
and no upstream call is made unless every gate passes.

> **Where enforcement lives.** The single interception point is
> `app/routers/mcp.py::_execute_registered_tool`. It calls three independent
> evaluators (`app/policy/decisions.py`, `app/services/permits.py`,
> `app/services/policies.py`) plus the human-approval and recipient-domain
> gates. Note that `app/services/preflight.py` is a *deploy-time* GO/NO-GO
> readiness checker (keys, domains, manifests) and is **not** part of the live
> per-call path — the two are deliberately decoupled.

Last updated: 2026-08-11.

---

## 1. When a call is "governed"

Most gates below apply only on the **governed path**. A call is governed when
any of the following is true (`_execute_registered_tool`):

- a `permit_id` is supplied, **or**
- `TRUST_MODE_ENABLED` is on and `ALLOW_LEGACY_UNPERMITTED_MCP` is off (the
  shipped default), **or**
- the tool's own registration sets `require_permit` (high-value tools force the
  permit path even when legacy unpermitted MCP is otherwise allowed).

The governed path cannot be silently disabled in production. `app/core/trust_mode.py`
refuses to boot a production-like environment (`prod`, `staging`, `preview`, …)
unless **all** of these hold: `TRUST_MODE_ENABLED=true`, a valid 32-byte Ed25519
signing key is configured, `ALLOW_LEGACY_UNPERMITTED_MCP=false`, `DEBUG=false`,
`WEBAUTHN_ALLOW_MOCK=false`, `ALLOW_PRIVATE_NETWORK_TARGETS=false`,
`ALLOW_UNSAFE_HOST_PYTHON_SANDBOX=false`, no host `BEHAVIORAL_SANDBOX_PYTHON_BACKEND`,
and proof surfaces are off. A permissive posture is
only reachable in local/dev/test, and it logs a loud startup warning.

---

## 2. Enforcement pipeline (in order)

| # | Gate | Source | On failure |
|---|------|--------|------------|
| 1 | **Wallet ownership — unpriced** (before the idempotency store is touched) | `evaluate_tool_invocation` | `wallet_access_denied` |
| 2 | **Tool resolution + executability** | service registry | `Tool not found` / `Tool not executable` |
| 3 | **Wallet ownership — priced** (re-run with real cost, for the audit record) | `evaluate_tool_invocation` | `wallet_access_denied` |
| 4 | **Governed preconditions** | router | `permit_required` / `idempotency_key_required` |
| 5 | **Idempotency begin** (completed prior call replays its stored signed receipt) | `get_idempotency_service()` | replay short-circuit |
| 6 | **Permit validation** (Layer B) | `PermitService.validate_for_action` | `permit_*` |
| 7 | **Wallet policy bundles** (Layer C) | `evaluate_wallet_policy` | policy reason |
| 8 | **Human-approval gate** (Layer D) | `_require_human_approval` | blocks / `human_approval_unavailable` |
| 9 | **Recipient-domain binding** (Layer E) | router | `permit_recipient_domain_mismatch` |
| 10 | **Atomic budget reservation + prepared remote attempt**; after debit, acquire the one-shot remote dispatch claim, send, then meter + sign receipt | permits + dispatch services | reconcilable failure / fail-closed claim contention |

Ownership is checked **twice** on purpose: once unpriced *before* the
idempotency store is touched (so an unauthorized caller can neither be served
another wallet's stored receipt via the replay short-circuit nor plant an
orphaned in-progress record in the victim's `(wallet, key)` namespace), and
again with the real estimated cost to produce the priced audit record.

---

## 3. Layer A — Tenant / wallet ownership

`app/policy/decisions.py::evaluate_tool_invocation`

Binary tenant-isolation check. The call is allowed only when the caller is the
bootstrap admin **or** owns the target wallet (`auth.wallet_id == wallet_id`);
otherwise it is denied `wallet_access_denied`. This layer does not consider
cost, scope, or budget — it exists purely to stop cross-tenant access.

---

## 4. Layer B — Permit validation

`app/services/permits.py::_evaluate` (via `validate_for_action`). Ordered,
first failure wins, every branch fails closed. The permit is an Ed25519-signed
capability bound to a wallet (and optionally an API key).

| Check | Reason code |
|-------|-------------|
| Status must be `active` | `permit_{status}` (e.g. `permit_revoked`) |
| Not past `expires_at` | `permit_expired` |
| Permit's wallet == caller wallet | `permit_wallet_mismatch` |
| Key-bound permit matches the caller's API key | `permit_key_mismatch` |
| Tool is in the permit's `allowed_tools` | `permit_tool_not_allowed` |
| Scopes include both `tool:{tool}:invoke` and `billing:charge` | `permit_scope_missing` |
| `spent_credits + estimated ≤ max_credits` | `permit_budget_exceeded` |
| Per-tool call cap `max_calls_per_tool` (v2) — atomically reserved on local governed tools and remote (`upstream_mcp`) tools via a durable `call_slot_reserved` marker linked to the prepared dispatch attempt; cross-key retries are denied `permit_max_calls_exceeded` once the cap is reached; same-key retries replay the stored receipt | `permit_max_calls_exceeded` |
| Cumulative `aggregate_value_cap` (v2) — enforced on the local MCP and AWI HTTP paths as a predicate of the atomic reservation against `spent_credits` floored to receipt history, so in-flight reservations and already-receipted charges both count; configured upstream calls fail closed | `permit_aggregate_value_cap_exceeded` locally; `permit_constraint_unsupported_for_upstream` remotely |
| `forbidden_fields` (v2): deep scan of tool arguments for banned keys | `permit_forbidden_field:{field}` |
| Ed25519 signature over the permit verifies — checked **last** | `permit_signature_invalid` |

The local per-tool call limit is enforced with a persisted reservation counter,
including an optimistic compare-and-swap when the database does not honor the
requested row lock. The remote per-tool call limit is enforced identically: the
same counter is incremented atomically in the reservation transaction that
creates the prepared remote dispatch attempt, linked by the `call_slot_reserved`
boolean marker. A same-key retry replays the stored receipt; a cross-key retry
is denied at authorization. The aggregate cap is enforced in the `WHERE` clause
of the same guarded reservation `UPDATE`, against `spent_credits` floored to the
permit's receipt total, so concurrent in-flight reservations cannot overshoot it
on any storage engine. The configured upstream path still rejects permits
carrying the aggregate cap before any reservation, attempt, debit, or dispatch.
`max_credits` remains the atomic authorization ceiling on both local and
configured-upstream paths.

---

## 5. Layer C — Wallet policy bundles

`app/services/policies.py::evaluate_wallet_policy`

Evaluates every **active** `PolicyBundle` attached to the wallet. No bundles =
allow. Within each bundle the checks run in order and the first failure wins.
All monetary comparisons are done in `Decimal` end-to-end (thresholds stored as
`Decimal`, incoming cost normalized rather than compared as float).

| Check | Reason code |
|-------|-------------|
| Bundle requires human approval | `human_approval_required` — satisfied instead of denied when the invoke's permit carries `requires_human_approval` (the Layer D gate provides the demanded decision; every other check below still runs) |
| Tool not in the bundle allow-list | `tool_not_allowed` |
| Service category not in the bundle allow-list | `service_category_not_allowed` |
| An allow-list column is present but is not a JSON array of strings | `policy_constraint_corrupt` (denied; never read as unrestricted) |
| `estimated > max_cost_per_action` | `max_cost_per_action_exceeded` |
| `daily_spend_used + estimated > daily_spend_limit` | `daily_spend_limit_exceeded` |
| Bundle requires real effects but the call is in simulation mode | `real_effects_required` |
| `risk_tier` mismatch | *recorded on the decision, does not deny* |
| Non-finite cost input (`NaN`, `Infinity`) for estimated cost or daily spend | `policy_cost_invalid` (denied; a cap that cannot be evaluated must not pass or crash) |

Layer B (permit) and Layer C (wallet policy) are complementary: the permit is a
per-delegation capability issued to one agent for one job; policy bundles are
standing wallet-level guardrails that apply to **every** call regardless of
which permit is presented.

---

## 6. Layer D — Human approval

When the validated permit carries `requires_human_approval`, the call blocks on
the approval service before any dispatch (`_require_human_approval`; see
[`docs/human-approval-gate.md`](./human-approval-gate.md)).

If the approval service is unreachable, the call returns the **retryable**
`human_approval_unavailable` outcome rather than a terminal denial: no receipt
is written, nothing is charged, and the caller's idempotency key is released so
the identical invoke (same body, same key) can be retried once the service is
back.

---

## 7. Layer E — Recipient-domain binding

For `upstream_mcp` tools, if the permit sets `recipient_domain`, the middleware
parses the tool's registered upstream origin and requires the hostname to match
the permit's bound domain, else `permit_recipient_domain_mismatch`. This stops a
valid permit from being redirected to an unauthorized upstream destination — a
gap in naive MCP proxies that forward wherever the tool registration points.

---

## 8. Atomicity, budget reservation, and receipts

Authorization is linearized as late as possible. For a remote (`upstream_mcp`)
call, the permit budget reservation and the recoverable "prepared" dispatch
attempt are created in the **same transaction**, so a durable reservation can
never exist without an attempt the reconciler knows how to compensate.

Immediately before the remote network send, that attempt must transition from
`prepared` to `dispatch_claimed` while setting its one nullable
`dispatch_claim_hash`. Only the activation that created the stored hash may
recover a lost commit acknowledgement; every later activation fails closed and
must not send. A missing trustworthy result after the claim becomes durable is
`delivery_uncertain`, not evidence that the downstream effect occurred. The
reconciler waits a fixed 11,430-second window that covers the maximum supported
600-second connection timeout, three 3,600-second call phases, and cleanup
margin. The window does not shrink across workers with different local timeout
settings.

The claim fence is remote-only. It does not change local-tool reservations,
per-tool call slots, quotes, human approvals, API-key/JWT authentication, or
rate limiting. Focused state-machine, reconciliation, migration, and PostgreSQL
process-kill tests cover this transition; deployment remains a separate
operator action.

- Budget reservation takes a row lock (`SELECT ... FOR UPDATE`) and re-checks
  `spent_credits + amount ≤ max_credits` inside the lock, preventing concurrent
  calls from overspending a permit.
- Crossing 80% / 90% / 100% of a permit's budget emits `info` / `warning` /
  `critical` billing alerts exactly once per threshold.
- On success the call is metered and a **signed receipt** is issued; on a
  governed denial a signed denial receipt is finalized so even rejections are
  provable.

---

## 9. Idempotency and replay semantics

The idempotency key identifies the **logical** invocation, not the transport
framing — JSON-RPC correlation IDs and REST-vs-JSON-RPC routing cannot produce a
second gateway dispatch for the same `(wallet_id, endpoint, key)`.

On replay of a completed call, the stored signed receipt is returned and
**mutable execution constraints are deliberately not re-evaluated** — expiry,
revocation, and remaining budget were already enforced when the receipt was
signed, and a settled outcome must not disappear if they later change. **Stable
identity constraints still apply**: wallet and key must still match. Tool scope
is intentionally not re-checked either, so that a signed *denial* for an
out-of-scope tool itself remains replayable.

### Cross-key duplicate detection (remote tools only)

For upstream (`upstream_mcp`) tools, an optional duplicate guard detects when a
**new** idempotency key carries the same request body as a prior effectful call
on the same permit and tool. Same-key retries are never blocked — they replay
the stored receipt as always. The guard checks `(permit_id, tool,
SHA-256(arguments), created_at ≥ window)` and ignores only pre-dispatch failures
that never reached the upstream (attempts with `dispatched_at IS NULL` and state
`returned_error`). Any older succeeded, delivery-uncertain, or sent attempt
blocks the new key.

The guard runs in **watch-only mode** by default (`MCP_UPSTREAM_DUPLICATE_GUARD=log`):
duplicate new-key retries are **allowed** and dispatched, but logged as
warnings. Operators can enable enforcement (`MCP_UPSTREAM_DUPLICATE_GUARD=enforce`)
to deny duplicates with `duplicate_request_new_key`, or disable detection
entirely (`MCP_UPSTREAM_DUPLICATE_GUARD=off`). Invalid mode values (e.g.,
"enforced") are rejected at startup. Individual permits can opt out with
`allow_identical_repeats=true` regardless of the global setting. Per-permit
windows can be configured via `repeat_window_seconds` to override the global
`MCP_UPSTREAM_DUPLICATE_WINDOW_SECONDS` (default 24 hours).

Observability: `/health/duplicate-guard` reports the current mode, configured
window, and counters for log-mode detections (`log_mode_blocks`) and enforce-mode
denials (`enforce_mode_blocks`). Those two counters are process-local and reset
on restart. `enforce_mode_denials_durable` counts the denied receipts carrying
`duplicate_request_new_key` across the service lifetime; it is `null` when no
database is configured or the count fails or times out, with
`enforce_mode_denials_durable_unavailable` naming why, and the process-local
counters are still served. `metric_scopes` labels each metric's durability so a
monitor never infers it from the values.

### Optional probabilistic risk advice

`JEV_RISK_GUARD` is **off by default**. Operators may opt
in to send redacted, truncated arguments and tool/permit descriptions to
TypeSafe for probabilistic advice. It runs only after deterministic policy
allows the call, never loosens authorization, and fails open on vendor errors.
`log` records advice; `enforce` routes escalations through the existing human
approval gate or denies calls without a governed permit. Advice is labelled
advisory in the signed audit and receipt. It is outside the deterministic
authorization guarantee.

---

## 10. Consolidated reason-code reference

| Reason code | Layer | Meaning |
|-------------|-------|---------|
| `wallet_access_denied` | A | Caller does not own the target wallet |
| `permit_required` | pipeline | Governed call with no permit supplied |
| `idempotency_key_required` | pipeline | Governed call with no idempotency key |
| `permit_not_found` | B | Permit ID does not resolve |
| `permit_{status}` | B | Permit not active (e.g. `permit_revoked`) |
| `permit_expired` | B | Past expiry |
| `permit_wallet_mismatch` | B | Permit bound to a different wallet |
| `permit_key_mismatch` | B | Permit bound to a different API key |
| `permit_tool_not_allowed` | B | Tool outside permit's allow-list |
| `permit_scope_missing` | B | Missing `tool:{tool}:invoke` or `billing:charge` |
| `permit_budget_exceeded` | B | Spend would exceed `max_credits` |
| `permit_max_calls_exceeded` | B | Per-tool call cap hit / malformed cap |
| `permit_aggregate_value_cap_exceeded` | B | Cumulative value cap hit |
| `permit_constraint_unsupported_for_upstream` | B | Configured upstream call carries a usage constraint without an atomic remote enforcement lifecycle |
| `duplicate_request_new_key` | B | Cross-key duplicate detection denied an identical new-key retry (remote tools, enforce mode only) |
| `permit_forbidden_field:{field}` | B | Banned argument key present |
| `permit_signature_invalid` | B | Permit signature failed verification |
| `human_approval_required` | C | Wallet policy demands approval |
| `tool_not_allowed` | C | Tool outside wallet policy allow-list |
| `service_category_not_allowed` | C | Category outside wallet policy allow-list |
| `policy_constraint_corrupt` | C | A wallet policy allow-list column is corrupt; denied rather than read as unrestricted |
| `max_cost_per_action_exceeded` | C | Per-action cost cap hit |
| `daily_spend_limit_exceeded` | C | Daily spend cap hit |
| `real_effects_required` | C | Real-effects policy vs simulation mode |
| `policy_cost_invalid` | C | Non-finite estimated cost or daily spend cannot be compared against caps |
| `human_approval_unavailable` | D | Approval service unreachable (**retryable**) |
| `permit_recipient_domain_mismatch` | E | Upstream origin ≠ permit's bound domain |

---

## 11. Threat → control mapping

Summary view for security review; see [`docs/threat-model.md`](./threat-model.md)
for the full model.

| Threat | Primary control |
|--------|-----------------|
| Cross-tenant access / confused-deputy | Layer A ownership, checked before the idempotency store |
| Stolen or replayed permit | Ed25519 signature verify (B) + wallet/key binding, enforced even on replay |
| Runaway spend by an autonomous agent | Permit budget + aggregate cap (B), wallet per-action/daily caps (C), locked reservation |
| Tool/scope escalation | `allowed_tools` + explicit scope requirement (B), wallet allow-lists (C) |
| Exfiltration via argument injection | `forbidden_fields` deep-scan (B) |
| Permit redirection to a rogue upstream | Recipient-domain binding (E) |
| High-risk action without oversight | Human-approval gate (D) |
| Double-charge / duplicate gateway dispatch | Logical-invocation idempotency + one-shot remote dispatch claim |
| Silent loss of governance in prod | Trust-mode boot guardrails (`core/trust_mode.py`) |

---

## Source files

- `app/routers/mcp.py` — `_execute_registered_tool` (the interceptor)
- `app/policy/decisions.py` — `evaluate_tool_invocation` (Layer A)
- `app/services/permits.py` — `_evaluate` / `validate_for_action` (Layer B)
- `app/services/policies.py` — `evaluate_wallet_policy` (Layer C)
- `app/services/mcp_dispatch_attempts.py` — prepared attempts and the one-shot dispatch claim
- `app/services/mcp_dispatch_reconciliation.py` — fixed maximum-lifetime stale-claim handling without redispatch
- `app/services/upstream_mcp.py` — claim callback immediately before the network send
- `app/core/trust_mode.py` — production trust-posture guardrails
