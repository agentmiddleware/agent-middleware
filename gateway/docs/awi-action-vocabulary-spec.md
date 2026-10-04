# AWI Semantic Action Vocabulary Draft

Status: Draft, not a published standard
Version: 0.1.0
Date: 2026-05-20
Source implementation: `app/schemas/awi.py` and `app/services/awi_action_vocab.py`

This document specifies the current semantic action vocabulary implemented by
Agent Middleware API for Agentic Web Interface (AWI) sessions. It is intended as
a discussion draft for implementers and researchers, not as a claim of external
standardization or endorsement by the authors of the AWI paper.

## Scope

The vocabulary defines higher-level web actions that an agent can request
without controlling raw browser coordinates or low-level DOM events. A conforming
implementation may expose these actions over HTTP, MCP, a local SDK, or another
transport, as long as the action contract and safety semantics are preserved.

This draft covers:

- 13 action names across semantic and compatibility tiers
- Action categories
- Action tier, maturity status, risk level, and sensitive parameter metadata
- Required and optional parameters
- Preconditions and postconditions
- Representation requests
- Safety and versioning requirements

This draft does not define:

- A universal AWI wire protocol
- Browser automation internals
- A benchmark methodology for representation quality
- Website-specific business rules

## Design Goals

1. Prefer semantic actions over DOM mechanics.
2. Keep action contracts stable enough for cross-site agent code.
3. Let sites enforce local policy before side effects occur.
4. Return compact, task-relevant representations when possible.
5. Make high-risk actions auditable, bounded, and interruptible.
6. Allow extension without fragmenting the core vocabulary.

## Implementation Status

The action names, categories, parameters, preconditions, and postconditions in
this draft mirror the current reference implementation.

The safety language is a draft requirement for implementations, not a guarantee
that `app/services/awi_action_vocab.py` enforces the behavior by itself. In this
repo, enforcement lives in surrounding services where implemented, including
wallet-scoped authorization, passkey checks, signed permits, receipts, and audit
chains.

Important unresolved design questions before externalizing this draft:

- Should `click_button` and `scroll` remain core actions, or should they be
  compatibility primitives outside the semantic vocabulary?
- Should `login` require secret handles only, or keep transitional support for
  direct `username` and `password` parameters?
- Should `checkout` be split into reversible and irreversible steps?
- Which benchmark should determine whether progressive representations improve
  token cost or task completion versus DOM or screenshot baselines?

## Action Request Shape

The reference implementation accepts action requests through
`POST /v1/awi/execute`.

```json
{
  "session_id": "awi-session-id",
  "action": "search_and_sort",
  "parameters": {
    "query": "laptops",
    "sort_by": "price"
  },
  "representation_request": "summary",
  "dry_run": false
}
```

Fields:

| Field | Required | Meaning |
| --- | --- | --- |
| `session_id` | yes | Stateful AWI session identifier. |
| `action` | yes | One of the defined action names or a supported extension action. |
| `parameters` | no | Action-specific JSON object. Defaults to `{}`. |
| `representation_request` | no | Optional representation to return after execution. |
| `dry_run` | no | If true, validate and preview without intended side effects. |

## Action Response Shape

The reference implementation returns a structured response from
`POST /v1/awi/execute`.

Successful execution:

```json
{
  "execution_id": "awi-exec-id",
  "session_id": "awi-session-id",
  "action": "search_and_sort",
  "status": "success",
  "parameters": {
    "query": "laptops"
  },
  "result": {
    "success": true,
    "results_count": 12
  },
  "representation": null,
  "duration_ms": 14
}
```

Validation or execution failure:

```json
{
  "execution_id": "awi-exec-id",
  "session_id": "awi-session-id",
  "action": "login",
  "status": "error",
  "parameters": {
    "username": "[REDACTED]",
    "password": "[REDACTED]"
  },
  "error": "Missing required login parameters: credential_handle or username+password"
}
```

Response fields:

| Field | Meaning |
| --- | --- |
| `execution_id` | Unique execution identifier. |
| `session_id` | Stateful AWI session identifier. |
| `action` | Action attempted. |
| `status` | Execution status such as `success`, `error`, `paused`, `passkey_required`, or `max_steps_reached`. |
| `parameters` | Request parameters after sensitive-parameter redaction. |
| `result` | Action-specific result payload when execution runs. |
| `error` | Clear failure reason when `status` is not successful. |
| `new_state` | Optional state snapshot after execution. |
| `representation` | Optional representation generated when `representation_request` is set. |
| `duration_ms` | Runtime duration in milliseconds when execution runs. |
| `cost_estimate` | Present for dry-run previews when supported. |

Implementations should return a clear error status when validation fails or a
precondition is unmet. HTTP status codes may still be `200` when the request was
syntactically valid and the action-level status carries the execution outcome.

## Representation Types

The current representation vocabulary is:

| Type | Purpose |
| --- | --- |
| `summary` | Concise state summary for low-token planning. |
| `embedding` | Semantic vector or embedding-like representation. |
| `low_res_screenshot` | Visual state at reduced resolution. |
| `accessibility_tree` | Accessibility-first structure. |
| `json_structure` | Structured state object. |
| `text_extraction` | Extracted visible or relevant text. |
| `full_dom` | Complete DOM representation when compact forms are insufficient. |

Implementations should prefer the smallest representation that supports the
agent's next decision. `full_dom` should be treated as a fallback, not the
default.

## Categories

| Category | Actions |
| --- | --- |
| `navigation` | `navigate_to`, `scroll` |
| `search` | `search_and_sort` |
| `interaction` | `fill_form`, `click_button`, `select_option`, `upload_file` |
| `extraction` | `extract_data`, `get_representation` |
| `transaction` | `add_to_cart`, `checkout` |
| `auth` | `login`, `logout` |

## Action Metadata

The reference implementation exposes these metadata fields for each action:

| Field | Values | Meaning |
| --- | --- | --- |
| `tier` | `semantic`, `compatibility` | Whether the action expresses intent directly or exists as a practical escape hatch. |
| `status` | `stable`, `provisional`, `deprecated` | Maturity of the action contract. |
| `risk_level` | `low`, `medium`, `high` | Minimum policy attention expected before execution. |
| `sensitive_parameters` | list of parameter names | Parameters that must be redacted before durable storage, logs, receipts, or audit payloads. |

Default actions are `semantic`, `stable`, and `low` risk unless stated
otherwise below.

## Preconditions and Postconditions

Preconditions and postconditions are declarative predicates over session state.
They may be represented as boolean session flags, computed predicates, or
site-provided capability metadata, but they are not caller-controlled bypasses.

The executor is responsible for checking preconditions before side effects. If a
precondition is unmet, the executor should reject the action with a clear
diagnostic or request a remedy such as navigation, login, or human approval.
Callers may use preconditions for planning, but caller assertions do not satisfy
the contract by themselves.

Postconditions describe the expected state after successful execution. If a
postcondition is not achieved, the executor should return a failure status and
diagnostic information rather than silently treating the action as successful.
For high-risk actions such as `checkout`, and runtime-sensitive `fill_form`
requests, authorization and safety decisions should be surfaced in the response.

## Action Definitions

### `search_and_sort`

Search for items or records and optionally sort or filter the result set.

Category: `search`

Required parameters:

- `query`: string

Optional parameters:

- `sort_by`: string
- `sort_order`: string
- `filters`: object

Preconditions:

- `page_loaded`

Postconditions:

- `results_displayed`

### `add_to_cart`

Add an item to a shopping cart or equivalent pending transaction container.

Category: `transaction`

Risk level: `medium`

Required parameters:

- `item_id`: string

Optional parameters:

- `quantity`: integer
- `variant`: string

Preconditions:

- `item_visible`
- `cart_accessible`

Postconditions:

- `item_in_cart`

### `checkout`

Complete or advance a checkout flow.

Category: `transaction`

Risk level: `high`

Sensitive parameters:

- `payment_method`

Required parameters:

- `payment_method`: string

Optional parameters:

- `shipping_address`: object
- `billing_address`: object

Preconditions:

- `cart_not_empty`
- `user_authenticated`

Postconditions:

- `order_placed`
- `payment_processed`

Safety requirement: `checkout` is high risk. Implementations should require an
explicit authorization boundary such as a signed permit, human approval,
passkey/WebAuthn verification, or equivalent local policy before execution.

### `fill_form`

Fill a form with provided field values, optionally submitting it.

Category: `interaction`

Risk level: `medium`

Required parameters:

- `fields`: object

Optional parameters:

- `form_id`: string
- `submit`: boolean

Preconditions:

- `form_visible`

Postconditions:

- `form_filled`
- `form_submitted`

Safety requirement: if fields contain regulated, personal, credential, or
payment data, implementations should treat the request as high risk even though
the action name is general. `risk_level: medium` is baseline metadata only.
Executors should inspect `fields`, site/form metadata, and caller-provided field
sensitivity hints, then elevate runtime risk to `high` for sensitive field names
or values such as `password`, `pass`, `pwd`, `ssn`, `social_security`,
`credit_card`, `cc_num`, `card_number`, `cvv`, `cvc`, `routing`, `account`,
`dob`, `date_of_birth`, or financial-context email fields.

### `login`

Authenticate with the target website.

Category: `auth`

Status: `provisional`

Risk level: `high`

Sensitive parameters:

- `username`: string
- `password`: string

Accepted credential shapes:

- Preferred: `credential_handle`: string
- Transitional v0.1 compatibility: `username`: string plus `password`: string

Optional parameters:

- `remember_me`: boolean

Preconditions:

- `login_page_visible`

Postconditions:

- `user_authenticated`
- `session_created`

Safety requirement: credentials must not be logged in plaintext, included in
receipts, or exposed in durable state. `credential_handle` is the preferred
shape; direct `username` and `password` parameters are retained only for v0.1
compatibility and must be redacted before storage or response because usernames
are often email addresses or other personally identifying values.

### `logout`

End the current authenticated session.

Category: `auth`

Required parameters: none

Optional parameters: none

Preconditions:

- `user_authenticated`

Postconditions:

- `session_terminated`
- `logged_out`

### `navigate_to`

Navigate to a URL or named page.

Category: `navigation`

Required parameters:

- `url`: string

Optional parameters:

- `wait_for_load`: boolean

Preconditions: none

Postconditions:

- `page_loaded`

Safety requirement: implementations should enforce URL allowlists, same-origin
policy, or permit scopes when navigation can cross trust boundaries.

### `click_button`

Click a button or interactive element.

Category: `interaction`

Tier: `compatibility`

Required parameters: none

Optional parameters:

- `button_id`: string
- `button_text`: string
- `button_selector`: string

Preconditions:

- `button_visible`

Postconditions:

- `button_clicked`

Safety requirement: `click_button` is a compatibility action. Prefer semantic
actions such as `checkout`, `add_to_cart`, or `select_option` when available.
Raw selectors should be constrained and previewable because they can bypass the
intent expressed by a semantic action.

### `scroll`

Scroll the current page or container.

Category: `navigation`

Tier: `compatibility`

Required parameters:

- `direction`: string

Optional parameters:

- `amount`: integer

Preconditions:

- `page_loaded`

Postconditions:

- `scrolled`

### `select_option`

Select an option from a dropdown, radio group, listbox, or similar control.

Category: `interaction`

Required parameters:

- `option_value`: string

Optional parameters:

- `select_id`: string
- `option_text`: string

Preconditions:

- `select_visible`

Postconditions:

- `option_selected`

### `upload_file`

Upload a file to the target page.

Category: `interaction`

Risk level: `medium`

Required parameters:

- `file_path`: string

Optional parameters:

- `input_id`: string

Preconditions:

- `upload_input_visible`

Postconditions:

- `file_uploaded`

Safety requirement: implementations should restrict file paths to approved
workspace or upload directories and should prevent arbitrary host filesystem
access.

### `extract_data`

Extract structured data from the current page or state.

Category: `extraction`

Required parameters:

- `data_type`: string

Optional parameters:

- `selector`: string
- `limit`: integer

Preconditions:

- `data_visible`

Postconditions:

- `data_extracted`

Safety requirement: extraction can leak private or tenant-scoped data.
Implementations should apply the same authorization checks used for viewing the
underlying page or object.

### `get_representation`

Return a specific representation of current state.

Category: `extraction`

Required parameters:

- `representation_type`: string

Optional parameters:

- `options`: object

Preconditions:

- `page_loaded`

Postconditions:

- `representation_returned`

## Safety Model

The action vocabulary is not sufficient by itself. Implementations should pair
the vocabulary with a governance layer that can answer:

- Who is requesting the action?
- Which wallet, tenant, user, or principal owns the session?
- What authority was delegated?
- What budget or rate limit applies?
- Is the action idempotent or replay protected?
- Which evidence proves the action was allowed, denied, charged, or refunded?

The reference implementation uses wallet-scoped API keys, signed permits,
idempotency keys, signed receipts, and tamper-evident audit chains. Other
implementations may use different mechanisms, but high-risk and billable actions
should produce equivalent reviewable evidence.

## Human Intervention

Implementations should expose a human intervention surface with at least:

- `pause`
- `resume`
- `steer`

Human intervention should be available before high-risk side effects and during
long-running task queues.

## Extension Policy

Implementations may add custom actions when the core vocabulary is insufficient.
Custom actions should:

- Avoid changing the meaning of core action names.
- Use a clear namespace such as `x_vendor_action` or `vendor.action`.
- Declare parameters, preconditions, postconditions, action tier, maturity
  status, risk level, and sensitive parameters.
- Provide a fallback or explanation for agents that only understand core
  actions.

Candidate future core actions should demonstrate repeated use across at least
two independent domains before being added to this draft.

## Versioning Policy

This draft uses semantic versioning:

- Patch versions clarify wording without changing behavior.
- Minor versions add optional parameters, new representation types, or new core
  actions.
- Major versions rename actions, remove actions, or change required parameters.

Action names and required parameters should not change in a minor version.
Deprecated actions should remain documented for at least two minor versions.

## Open Questions

1. Should `click_button` and `scroll` remain in the 13-action vocabulary as
   compatibility actions, or move outside the shared vocabulary?
2. Should `login` move from transitional credential support to
   `credential_handle` only in v0.2?
3. Should risk levels be standardized across implementations or left as local
   policy metadata?
4. Should `checkout` be split into `start_checkout`, `review_order`, and
   `confirm_purchase` to better separate reversible and irreversible steps?
5. What benchmark should determine whether progressive representations are
   better than DOM or screenshot baselines?
