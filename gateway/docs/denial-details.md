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
`verify_bundle` from the Agent Middleware (AMW) `amw` package; the `amw verify` output
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

This catalog lists stable governed denials and the permit-issuance and
idempotency admission reasons below. Generic API errors such as
`invalid_api_key`, `rate_limited`, and `internal_error` use their own envelope
codes. A terminal permit-bound denial can carry a signed receipt; an API-only
error has no receipt. The signed reason names the failure class; use the
signed `outcome` and credit fields to determine whether a debit was
retained, refunded, or never made. API details and audit metadata remain
adjacent context, not receipt-signed claims.

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

### Permit issuance

These refusals happen before a permit is issued or an invocation can produce
a signed denial receipt. They are API-only.

| Reason code | Meaning | Remediation |
|-------------|---------|-------------|
| `action_permit_requires_trusted_issuance` | A caller tried to mint an action permit through ordinary permit issuance. | Use the trusted action-permit flow. |
| `aggregate_value_cap_not_storable` | The aggregate cap cannot be represented in the permit store. | Supply a valid finite cap within the supported range. |
| `demo_permit_out_of_bounds` | A demo key requested authority outside its wallet, tool, credit, or lifetime limits. | Issue a permit within the demo key's limits. |
| `issuer_wallet_not_found` | The requested issuer wallet does not exist. | Use an existing authorized issuer wallet. |
| `max_credits_must_be_positive` | The requested permit budget is zero or negative. | Supply a positive budget. |
| `max_credits_not_storable` | The requested budget cannot be represented in the permit store. | Supply a valid finite budget within the supported range. |
| `permit_budget_exceeds_wallet_balance` | The subject wallet cannot cover the requested permit budget. | Fund the wallet, lower the budget, or request additional authority. |
| `permit_expired_at_creation` | The requested permit has already expired. | Set a future expiry and issue a new permit. |
| `repeat_window_issuance_disabled` | Repeat-window permit issuance is disabled. | Omit the repeat window until issuance is enabled. |
| `subject_wallet_access_denied` | The caller cannot issue a permit against the subject wallet. | Use a wallet the key may access. |
| `subject_wallet_not_found` | The requested subject wallet does not exist. | Use an existing authorized subject wallet. |

### Admission, credentials, and action binding

These refusals cover admission and action validation before dispatch. Rows
marked API-only occur before a signed denial receipt can be created.

| Reason code | Meaning | Remediation |
|-------------|---------|-------------|
| `permit_not_found` | API-only: the referenced permit does not exist; no signed receipt is created. | Check the permit ID or mint a new permit. |
| `permit_credits_invalid` | The call's estimated credit amount cannot be stored or compared against the permit budget. | Fix the tool's pricing or request amount, then retry with a new idempotency key. |
| `wallet_access_denied` | The authenticated key may not act on the named wallet. | Authenticate with a key scoped to that wallet. |
| `wallet_scoped_key_required` | The standard `/mcp` surface mints permits from the caller's wallet, so it needs a wallet-scoped key. The message may carry a `: <explanation>` suffix. | Authenticate with a wallet-scoped API key. |
| `key_tool_not_allowed` | The API key's own tool allowlist excludes the tool. | Use a key that allows the tool. |
| `demo_tool_not_allowed` | Demo-tenant wallets may call only the demo allowlist. | Use an allowed demo tool or a non-demo wallet. |
| `demo_auto_permit_budget_exceeded` | The tool costs more than the demo auto-permit credit cap. | Use a cheaper tool or a non-demo wallet. |
| `idempotency_key_required` | An unkeyed MCP `tools/call` got no derived retry key (a `read_only` tool, `MCP_UNKEYED_RETRY_WINDOW_SECONDS=0`, or an enterprise bearer that fails verification). Refused before any permit, dispatch, or charge instead of minting a random key. | Send an `Idempotency-Key` and reuse it when retrying the same call. |
| `idempotency_key_required_for_human_approval` | The wallet policy requires approval, and without a client idempotency key each retry would page a human again. | Send an `Idempotency-Key` and retry the same call with the same key to poll the decision. |
| `idempotency_key_not_a_string` | API-only: a supplied request key is not a string. | Send a string key. |
| `idempotency_key_blank` | API-only: the key is empty or only whitespace. | Send a non-blank key. |
| `idempotency_key_too_long` | API-only: the key exceeds the allowed length. | Send a shorter key. |
| `idempotency_key_control_characters` | API-only: the key contains a control character. | Remove control characters and resend. |
| `idempotency_key_not_utf8` | API-only: an HTTP header key is not valid UTF-8. | Send a UTF-8 string key. |
| `idempotency_key_conflict` | API-only: header and body supply different keys. | Send the same key in both places or use one source. |
| `idempotency_key_reused` | API-only: the idempotency key was already used for a different request; no signed receipt is created. | Use a new idempotency key. |
| `action_permit_required` | The tool has an action binding and needs an explicit action permit. | Mint an action permit for the tool and pass its ID. |
| `action_tool_binding_required` | The permit is an action permit but the tool has no action binding. | Use a tool with an action binding or a standard permit. |
| `action_binding_mismatch` | The tool's current upstream binding differs from the one the action permit was minted for. | Mint a new action permit against the current binding. |
| `action_quote_unsupported` | Quotes cannot be combined with action permits. | Invoke without a quote. |
| `action_permit_denied` | Action permit validation failed without a narrower reason. | Inspect the adjacent audit event and replace the permit. |
| `unsupported_action_contract_version` | The permit uses an action contract other than v1. | Mint a v1 action permit. |
| `invalid_action_binding` | The action permit or tool binding is missing a required identity field. | Repair the registered tool binding or mint a new permit. |
| `invalid_action_digest` | A signed action or upstream binding digest is malformed. | Mint a new action permit against the current binding. |
| `invalid_action_permit_scope` | The action permit's tool list or one-call cap is malformed. | Mint a new action permit with the required tool and cap. |
| `unsupported_action_constraints` | The permit carries options the action path cannot enforce. | Mint an action permit without those options. |
| `action_payload_mismatch` | The call arguments do not match the payload signed in the action permit. | Send the exact bound arguments or mint a new permit. |
| `action_arguments_must_be_object` | The arguments or registered input schema is not an object. | Send object arguments or repair the tool schema. |
| `action_arguments_must_be_strict_json` | The arguments or schema contain a value outside strict JSON. | Remove unsupported values and retry with a new permit. |
| `invalid_action_schema` | The registered action input schema is malformed. | Repair the tool schema and mint a new permit. |
| `unsupported_action_schema` | The registered action input schema uses a feature outside the supported v1 profile. | Use a supported schema and mint a new permit. |
| `action_argument_type_mismatch` | An argument has the wrong JSON type. | Send the type required by the tool schema. |
| `action_argument_unknown_property` | An argument names a property the schema does not allow. | Remove that property or update the registered schema. |
| `action_argument_required` | A required argument is missing. | Supply the required property. |
| `action_argument_constraint` | An argument violates a length, count, or numeric bound. | Send a value within the schema bound. |
| `action_argument_const` | An argument differs from the schema's fixed value. | Send the required fixed value. |
| `action_argument_enum` | An argument is outside the schema's allowed choices. | Send one of the allowed values. |

### Enterprise identity and group policy

Enterprise bearer verification and IGA policy checks can refuse a governed
call before dispatch. A token rejected by authentication has no receipt; an
IGA denial after a permit-bound call reaches the governed gate can have a
signed denial receipt.

| Reason code | Meaning | Remediation |
|-------------|---------|-------------|
| `iga_config_invalid` | The pinned issuer or group-policy configuration is invalid. | Have an operator repair the IGA configuration. |
| `iga_token_malformed` | The enterprise bearer cannot be parsed or lacks a required claim. | Obtain a well-formed token from the configured IdP. |
| `iga_token_invalid` | Token verification failed without a narrower reason. | Obtain a fresh valid token from the configured IdP. |
| `iga_token_expired` | The bearer has expired. | Obtain a fresh token. |
| `iga_token_not_yet_valid` | The bearer's validity window has not begun. | Wait until it is valid or correct the IdP clock. |
| `iga_issuer_not_trusted` | The token issuer is not configured as trusted. | Authenticate with a configured issuer. |
| `iga_algorithm_not_allowed` | The token uses a signing algorithm outside the allowlist. | Use the configured IdP and signing algorithm. |
| `iga_signing_key_not_found` | No pinned signing key matches the token. | Refresh the configured signing keys or obtain a token signed by a pinned key. |
| `iga_signature_invalid` | The token signature does not verify. | Obtain a correctly signed token. |
| `iga_audience_mismatch` | The token is not intended for this gateway. | Request a token with the configured audience. |
| `iga_no_matching_role` | The principal has no group mapped to a gateway policy. | Have an administrator grant an authorized mapped group. |
| `iga_policy_not_found` | A mapped policy bundle no longer exists. | Have an administrator repair the group-policy mapping. |
| `iga_policy_inactive` | A mapped policy bundle is inactive. | Have an administrator activate an appropriate policy. |
| `iga_tool_not_allowed` | A mapped policy does not allow the requested tool. | Use an allowed tool or obtain an authorized policy change. |
| `iga_max_uses_exceeded` | The grant's lifetime use cap is exhausted. | Obtain a new grant or have an administrator revise the cap. |
| `iga_velocity_exceeded` | The grant's short-window call cap is exhausted. | Wait for the window to clear or have an administrator revise the cap. |

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
| `policy_cost_invalid` | The action's cost could not be evaluated against the policy (non-finite or unstorable). Fail-closed denial. | Fix the tool's pricing, then retry with a new idempotency key. |
| `max_cost_per_action_exceeded` | The quoted action cost exceeds the wallet policy's per-action limit. | Lower the action cost or raise the policy limit. |
| `daily_spend_limit_exceeded` | Current daily spend plus this action exceeds the wallet policy limit. | Wait for the policy window to reset or have an administrator change the limit. |
| `real_effects_required` | The policy forbids a simulated execution path. | Use a real-effects tool configuration or revise the policy. |
| `risk_tier_not_allowed` | The action's risk tier exceeds the wallet policy bundle's tier ceiling (tiers run low < medium < high; the bundle's `risk_tier` is the highest tier it permits). | Lower the action's risk tier or have an administrator raise the policy ceiling. |
| `daily_spend_unknown` | The policy sets a daily spend limit but past spending cannot be determined, so the cap cannot be shown to hold. Fail-closed denial. | Retry once spend accounting is healthy; if it persists, have an administrator investigate the ledger before spending resumes. |
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
$ amw verify --bundle receipt.json --keys trust-keys.json
VERIFIED  rcpt-example
  signed by   key-example
  permit      permit-example
  tool        partner.echo
  outcome     denied
  reason      permit_budget_exceeded
  credits     0 charged of 5 authorized
  at          2026-08-11T00:00:00Z
```

## Stable registry

Every code in the tables above is registered in `app/core/reason_codes.py`
(`REFUSAL_REASON_CODES`). These values are a public contract; new refusals add
a new code. `tests/test_reason_code_registry.py` pins the set, checks it
against this page, and scans known refusal producers for unregistered literals.
