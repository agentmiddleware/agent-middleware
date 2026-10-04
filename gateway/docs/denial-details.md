# Denial reason codes and details (a refusal an agent can act on)

**Audience:** partner integrators and agent authors.

A governed denial used to be a bare reason string. `permit_budget_exceeded`
tells an agent that it failed; it does not tell it whether to ask for two more
credits or two hundred, so the only available move is to retry and fail again.

Permit-validation denials carry a `details` object naming the constraint that
was evaluated and the numbers behind it. When a governed MCP call with a bound
permit reaches a terminal non-success state, new receipts also carry a nullable
`reason_code`.

`reason_code` is part of the receipt's signed payload. It survives portable
export and is available in the verified `claims` returned by
`b2a_sdk.receipt_verifier`; the human-readable `b2a-verify-receipt` output
prints it as `reason`. This makes the denial category independently verifiable
without treating the richer diagnostic context as a signed claim.

Legacy receipts may omit `reason_code` and still verify. Successful receipts
omit it. Consumers must therefore treat the field as optional.

## Where it appears

| Surface | Shape |
|---------|-------|
| `POST /mcp/tools/{id}/invoke` | `403 {"detail": {"error": ..., "receipt": {...}, "details": {...}}}` |
| `POST /mcp/messages` (JSON-RPC) | `error.data.details`, beside `error.data.receipt` |
| `POST /v1/permits/verify` | `details` alongside `valid` and `reason` (absent when valid) |
| Governed AWI actions | `detail.details` |
| Signed audit metadata | `denial_details` on the denial's audit event |
| Portable receipt | signed `reason_code` inside `signing_input` when present |

The portable receipt signature authenticates `reason_code`, not the adjacent
`details` object. Details remain API and audit context beside the receipt,
never instead of it.

### `verify` needs the action, not just the permit

`POST /v1/permits/verify` decides an *action*: it answers "would this wallet,
calling this tool, at this price, be admitted under this permit?" Send
`wallet_id` and `tool` (and `estimated_credits` when the price matters) or the
verdict is not about the call you meant. A request omitting either gets
`valid: false` with reason `permit_verify_context_missing` and
`details.missing` naming the absent fields — not a binding reason, which would
read as "this permit is not yours" to the permit's own subject. Reasons that do
not depend on the missing context (`permit_not_found`, `permit_expired`,
`permit_revoked`) are still reported as themselves.

## Reason-code and remediation catalog

This is the authoritative catalog for the `reason_code` values emitted by the
current governed MCP receipt paths. It excludes errors that happen before a
receipt can be created, such as a missing permit, quote failure, or idempotency
conflict. The signed reason names the failure class; use the signed `outcome`
and credit fields to determine whether a debit was retained, refunded, or never
made. API details and audit metadata remain adjacent context, not receipt-signed
claims.

### Permit and recipient binding

A non-active permit uses `permit_<status>`; `permit_revoked` is the normal
revocation case.

| Reason code | Details | Remediation |
|-------------|---------|-------------|
| `permit_budget_exceeded` | `required_credits`, `remaining_credits`, `spent_credits`, `max_credits` | Request a permit with enough remaining credits, then retry with a new idempotency key. |
| `permit_aggregate_value_cap_exceeded` | `required_credits`, `reserved_credits`, `aggregate_value_cap`; `charged_to_date` when the denial came from the read-time check | Request a higher aggregate cap or a replacement permit. `reserved_credits` includes in-flight reservations that have not yet produced a receipt, floored to the permit's receipt total so it is never below `charged_to_date`. |
| `permit_max_calls_exceeded` | `tool`, `limit`, `calls_made` | Request a replacement permit with a higher per-tool call limit, then retry with a new idempotency key. Replaying the denied key returns the stored denial receipt. |
| `permit_constraint_unsupported_for_upstream` | `execution_backend`, `unsupported_constraints` | Returned only for `aggregate_value_cap` on remote tools. Request a replacement permit that omits the aggregate cap constraint and uses `max_credits` as the atomic ceiling, then invoke with a new idempotency key. Replaying the denied key returns the stored denial. |
| `duplicate_request_new_key` | `prior_attempt_id`, `prior_state`, `request_hash` | A new idempotency key carried arguments identical to a prior effectful call on the same permit and tool within the duplicate window (global `MCP_UPSTREAM_DUPLICATE_WINDOW_SECONDS` or permit-specific `repeat_window_seconds`). Returned only when `MCP_UPSTREAM_DUPLICATE_GUARD=enforce` and the permit does not set `allow_identical_repeats`; in the default `log` mode the call is logged and dispatched. Repeating the identical request under another new key is denied again while the prior attempt is in the window, and replaying this denied key returns the stored denial. To get the prior call's result, replay the prior call's own idempotency key; to run the call again on purpose, change the arguments or use a permit with `allow_identical_repeats`. |
| `permit_tool_not_allowed` | `requested_tool`, `allowed_tools` | Use an allowed tool or request a permit that names the requested tool. |
| `permit_scope_missing` | `required_scopes`, `missing_scopes` | Request a permit containing every listed missing scope. |
| `permit_expired` | `expired_at`, `checked_at` | Request a new, unexpired permit; do not retry the expired one. |
| `permit_<status>` (normally `permit_revoked`) | `status`, `revoked_at` | Request a new active permit from the issuer. |
| `permit_forbidden_field` | `field`, `forbidden_fields` | Remove the forbidden field and its value, or request a permit that allows it. |
| `permit_signature_invalid` | `key_id` | Obtain a freshly issued permit signed by an active trusted key. |
| `permit_wallet_mismatch` / `permit_key_mismatch` | `bound_to` only — no values | Authenticate as the permit subject or request a permit bound to the current wallet and key. |
| `permit_recipient_domain_mismatch` | No diagnostic values in the portable receipt | Use the permit's bound upstream domain or request a permit bound to the intended recipient. |
| `permit_denied` | No stable diagnostic details | Inspect the adjacent audit event and replace the permit; this is the fail-closed fallback when validation supplies no narrower reason. |

Credit amounts are exact decimal strings, not floats. Timestamps are explicit
UTC (`...Z`).

For forbidden-field denials, the API response reason may be
`permit_forbidden_field:<name>`. The portable receipt normalizes that dynamic
value to the signed, stable `permit_forbidden_field` reason code because field
names are not constrained to the receipt's safe machine-code grammar. The
adjacent `details.field` value identifies the rejected field.

### Wallet and standing policy

| Reason code | Meaning | Remediation |
|-------------|---------|-------------|
| `wallet_frozen` | The wallet is frozen and cannot be debited. | Resolve the freeze with an administrator; retry with a new idempotency key only after the wallet is spendable. |
| `wallet_expired` | The child wallet's spending lifetime has ended. | Use a non-expired wallet and a permit bound to it. |
| `insufficient_funds` | Balance or a wallet spending cap cannot cover the action. | Top up the wallet, reduce the action cost, or change the applicable cap, then use a new idempotency key. |
| `human_approval_required` | An active wallet policy demands a human decision and the invoke's permit has no approval gate to provide one. | Invoke under a permit minted with `requires_human_approval` (the standard `/mcp` surface mints one automatically when policy demands it), or have an administrator revise the policy. A gated permit satisfies only this constraint — the policy's other limits still apply. |
| `tool_not_allowed` | A wallet policy excludes the requested tool. | Use a policy-allowed tool or update the policy allowlist. |
| `service_category_not_allowed` | A wallet policy excludes the tool's service category. | Use an allowed category or update the category allowlist. |
| `policy_constraint_corrupt` | A wallet policy's allow-list column is not a JSON array of strings, so the policy cannot be applied. | Have an administrator repair or recreate the policy bundle; the action stays denied until then. |
| `max_cost_per_action_exceeded` | The quoted action cost exceeds the wallet policy's per-action limit. | Lower the action cost or raise the policy limit. |
| `daily_spend_limit_exceeded` | Current daily spend plus this action exceeds the wallet policy limit. | Wait for the policy window to reset or have an administrator change the limit. |
| `real_effects_required` | The policy forbids a simulated execution path. | Use a real-effects tool configuration or revise the policy. |
| `policy_denied` | Policy evaluation denied without a narrower stable reason. | Inspect the adjacent audit policy ID and evaluated constraints, then correct the blocking policy. |

### Terminal human approval

| Reason code | Meaning | Remediation |
|-------------|---------|-------------|
| `human_approval_not_configured` | The permit requires approval, but the production approval service is not safely configured. | Configure Sentinel and approvers, then start a new invocation; do not enable simulated approval in production. |
| `human_approval_request_mismatch` | The stored approval is bound to different tool arguments or cost. | Start a new invocation and approval with a new idempotency key for the exact request. |
| `human_approval_request_rejected` | Sentinel rejected approval creation as a terminal client/configuration error. | Correct Sentinel credentials, approvers, or request configuration, then start a new invocation. |
| `human_approval_rejected` | A human rejected the action. | Do not retry automatically; change the action or obtain a new explicit approval. |
| `human_approval_expired` | The approval window elapsed before use. | Start a new invocation and obtain a fresh approval. |
| `human_approval_consumed` | The single-use approval already authorized another invocation. | Start a new invocation with a new idempotency key and approval. |

`human_approval_pending` and `human_approval_unavailable` are deliberately not
receipt reason codes. They are retryable API states: no receipt is created,
nothing is charged, and the in-progress idempotency record is abandoned so the
same request body and idempotency key can be retried when approval is decided
or Sentinel recovers.

### Dispatch, tool execution, and refund

| Reason code | Meaning | Remediation |
|-------------|---------|-------------|
| `upstream_prepare_failed` | Atomic authorization/dispatch preparation failed before an upstream call could start. | Repair the gateway or database preparation failure, then retry with a new idempotency key. |
| `upstream_pre_dispatch_failed` | Charging or checkpointing failed before confirmed upstream dispatch; any discovered debit was refunded. | Fix the gateway or upstream configuration, then retry with a new idempotency key. |
| `upstream_returned_error` | The upstream MCP server returned a tool error and the debit was refunded. | Inspect the adjacent upstream result, fix the request or server, then retry with a new key. |
| `response_rejected` | The upstream response was invalid or too large to retain; the dispatch occurred and the charge is retained. | Inspect dispatch/audit context and fix the response contract or size limit; do not retry automatically. |
| `delivery_uncertain` | Dispatch may have occurred, but the terminal result is unknown; the charge is retained. | Reconcile with the upstream invocation ID before considering any retry. |
| `tool_execution_failed` | A local registered tool raised and the debit was refunded. | Fix the tool or arguments, then retry with a new idempotency key. |
| `refund_failed` | The tool/dispatch failed and its debit could not be refunded; an operator work item is pending. | Do not retry the action; complete and verify refund reconciliation first. |
| `failed_refunded` | Reconciliation finalized a stale prepared or otherwise confirmed failure and compensated any debit. | Inspect adjacent dispatch error/audit context for the underlying cause; use a new key only for an intentional retry. |

For `refund_failed`, the API error names the tool's own error after the
reason (`refund_failed; tool_error:…` for a local tool,
`refund_failed; upstream_error:…` for a configured upstream tool) and may
append `audit_failed`. The refund store's and audit store's own text never
travels: it is kept in the server log and the linked audit event. The
portable receipt signs only the stable `refund_failed` reason; refund
reconciliation state remains adjacent API/audit context.

### Retryable reservation contention

`permit_write_contended` is not a denial reason code because it produces no
receipt. When a permit reservation's compare-and-swap loses to a concurrent
writer on a permit that still has capacity remaining, the gateway raises
`PermitWriteContendedError`, which rolls back the entire reservation
transaction and frees the idempotency record. Nothing is spent, no counter
moves, and no attempt row is created.

The caller receives JSON-RPC error code **`-32005`** (from `POST /mcp/messages`)
or HTTP **409** (from `POST /mcp/tools/{id}/invoke`), with the message
`permit_write_contended`. **Retry with the same idempotency key** — the rollback
freed that record, leaving no persisted reservation or dispatch state, so the
same-key retry runs a fresh compare-and-swap without replaying a denial. A
new-key retry is a separate call: it can dispatch in addition to a retry of the
original key, bounded only by the per-tool call cap when the permit sets
`max_calls_per_tool` (otherwise by the permit's budget and expiry). The cap
bounds the number of calls; it does not deduplicate across keys. The cross-key
duplicate guard (`MCP_UPSTREAM_DUPLICATE_GUARD`, validated at startup to be `off`, `log`, or `enforce`) runs in log-only mode by default
and does not prevent new-key retries. On Postgres, the permit row lock serializes
concurrent reservations, making this contention path practically unreachable; it
remains reachable on SQLite under high concurrency.

## What details deliberately do not say

**Binding mismatches carry no values.** A caller that presents a permit bound
to a different wallet or key has not proved it is the subject, so the denial
names the binding that failed (`{"bound_to": "subject_wallet_id"}`) and nothing
about which wallet or key the permit actually belongs to. Everything else in
the table describes the caller's *own* permit, which it can already read at
`GET /v1/permits/{permit_id}`.

**Forbidden fields echo the name, never the value.** The value is the thing the
permit forbade carrying.

Reading a denial still requires being authorized for the permit: a foreign
wallet key is refused at `POST /v1/permits/verify` (403) before any verdict is
computed. Details enrich a refusal you were already entitled to see; they do
not widen who can ask.

## Example

```json
{
  "detail": {
    "error": "permit_budget_exceeded",
    "receipt": { "outcome": "denied", "...": "..." },
    "details": {
      "required_credits": "5.0",
      "remaining_credits": "3",
      "spent_credits": "0",
      "max_credits": "3"
    }
  }
}
```

An agent reading that can request a permit for the amount it actually needs —
or, if a human must approve the increase,
[ask for one](permit-requests.md) with the number already in hand.

After exporting the receipt, an operator can verify and display the signed
reason without calling the gateway:

```text
$ b2a-verify-receipt --bundle receipt.json --keys trust-keys.json
VERIFIED  rcpt-example
  signed by   key-example
  permit      permit-example
  tool        partner.echo
  outcome     denied
  reason      permit_budget_exceeded
  credits     0 charged of 5 authorized
  at          2026-08-11T00:00:00Z
```
