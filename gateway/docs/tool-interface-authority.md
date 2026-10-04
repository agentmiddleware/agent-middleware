# The Tool Interface Is the Product Surface

Design goal: an agent discovers a normal MCP tool, calls it normally, and
governance happens automatically because that is the only executable path.
The agent should not have to understand "Agent Middleware" as a product
category, learn `/v1/permits`, or construct `mcpContext`.

The permit remains the cryptographically verifiable authorization object —
it is just an **implementation primitive, not a UX primitive**. On the
standard `/mcp` surface the middleware materializes the appropriate permit
from wallet policy at call time.

The design for generalizing this — one `authority_required` envelope for
every insufficient-authority outcome, an explicit resume contract in the
remediation payload, and remediation that carries a live approval handle —
lives in [`authority-required-flow.md`](authority-required-flow.md).

## The decision an invoke gets

Every `tools/call` on the standard surface resolves to one of three
outcomes, decided server-side:

```text
ALLOW             policy satisfied -> auto-permit -> execute -> signed receipt
REQUIRE_APPROVAL  policy demands a human -> approval-gated auto-permit ->
                  retryable pending_human_approval -> approve -> same call,
                  same Idempotency-Key -> execute -> signed receipt
DENY              constraint violated -> machine-actionable denial
                  (reason code + details + remediation), receipted where a
                  permit exists
```

### ALLOW

The server mints a bounded, signed, single-tool, short-lived permit from the
caller's wallet, meters the call, and writes a signed receipt when the outcome
finalizes. For the configured upstream MCP tool, identical retries under the
same accepted idempotency key allow at most one gateway dispatch and at most
one debit. This does not guarantee delivery or a downstream effect.
Local governed tools have no dispatch state machine and interrupted calls fail
closed into manual review.

The receipt arrows above describe completed paths. Effects or a debit may
commit before an audit or receipt write fails, returning
`manual_review_required` with no receipt. Do not retry with a new idempotency key:
that can execute and charge the call again. Reconcile from the ledger and audit
chain; see [failure semantics](failure-semantics.md). Upstream ambiguity is
receipted as `delivery_uncertain` only when finalization or reconciliation
succeeds.

### REQUIRE_APPROVAL

When any active policy bundle for the caller's wallet sets
`human_approval_required`, the auto-minted permit carries
`requires_human_approval=true`, so the existing invoke-time approval gate
(Sentinel-backed, fail-closed) pauses the call:

```json
{
  "code": -32005,
  "message": "human_approval_pending",
  "data": {
    "error": "authority_required",
    "status": "pending_human_approval",
    "approval_id": "appr-…",
    "expires_at": "…",
    "remediation": {
      "type": "await_human_decision",
      "detail": "Nothing was charged. Retry the identical tools/call with the same Idempotency-Key once the approval is decided; the same permit and approval are reused."
    }
  }
}
```

Nothing is charged, no receipt exists, and the idempotency key is released —
the retry that polls the decision resolves to the *same* permit and the
*same* approval instead of paging a human again. Because that retry loop
depends on the key, approval-gated calls **require** a client
`Idempotency-Key` (or `params._meta["io.agentmiddleware/idempotency_key"]`);
without one the call is refused with
`idempotency_key_required_for_human_approval` and a remediation block.

A key the client *does* supply — here, on the legacy MCP transports, or on
the governed AWI HTTP routes — must be usable: the contract (at most 128
characters, no control characters, one key per request, never trimmed or
replaced by a generated one) and the `invalid_idempotency_key` refusal are
specified in [`failure-semantics.md`](failure-semantics.md) under "What
counts as a key".

An approval-gated auto-permit lives for the whole approval window
(`SENTINEL_APPROVAL_TIMEOUT_SECONDS`) plus the standard TTL as execution
margin, so a decision made late in the window still executes instead of
stranding as `permit_expired`.

The gate satisfies only the policy's `human_approval_required` constraint.
Every other constraint in the bundle — tool allowlists, category allowlists,
`max_cost_per_action`, `daily_spend_limit`, `require_real_effects` — is
still enforced (`evaluate_wallet_policy(approval_gate_active=True)`).

### DENY

Denials are machine-actionable, not bare strings:

- Governed denials carry the same `details` object on `/mcp` as on
  `/mcp/messages` (which limit was hit, by how much) plus the signed denial
  receipt where one exists. See `docs/denial-details.md` for the reason-code
  catalog.
- An auto-mint the wallet cannot cover returns `-32004` with:

```json
{
  "error": "authority_required",
  "reason_code": "permit_budget_exceeds_wallet_balance",
  "requested": {"tool": "…", "estimated_credits": "…"},
  "remediation": {
    "type": "fund_wallet_or_request_authority",
    "check_authority": "GET /v1/me/authority",
    "request_authority": "POST /v1/permit-requests"
  }
}
```

## Discovery describes the contract

Each tool in `tools/list` / `/mcp/tools.json` now advertises the governance
contract alongside pricing:

```json
{
  "governed": true,
  "receiptProvided": true,
  "supportsIdempotency": true,
  "economicAction": true,
  "approvalMayBeRequired": true
}
```

`economicAction` is true when the tool's per-call cost is non-zero.
`approvalMayBeRequired` signals that wallet policy or the backing permit can
pause any call on a human decision without that being an error. Sensitive
policy internals (whose policy, what thresholds) are not exposed here.

The governance annotations reflect the **active trust configuration**: a
permissive local/demo deployment (`ALLOW_LEGACY_UNPERMITTED_MCP=true`,
refused at boot in production-like environments) accepts ungoverned
permit-less calls, so it advertises `governed`, `receiptProvided`,
`supportsIdempotency`, and `approvalMayBeRequired` as `false` rather than
promising guarantees that path does not provide. The exception is a
`require_permit` tool: the router forces the governed path for it even in
permissive mode, so it advertises the contract as `true` regardless.

## "What authority do I currently have?"

`GET /v1/me/authority` (wallet-scoped keys only) answers the planning
question in one read:

```json
{
  "wallet_id": "…",
  "balance": "…",
  "daily_spend_used": "…",
  "human_approval_required": true,
  "policies": [ … active policy bundles … ],
  "active_permits": [ … unexpired status=active permits for this key … ],
  "active_permits_total": 3,
  "pending_permit_requests": [ … awaiting a human … ],
  "pending_permit_requests_total": 1
}
```

It is read-only and composes reads the caller could already make one at a
time; listing never advances a decision or pages a human. Permits past their
`expires_at` are excluded even while their stored status is still `active`.
The two lists are previews capped at 50 rows; the `*_total` fields say
whether a list is complete, and `/v1/me/permits` / `/v1/me/permit-requests`
page through the remainder.

## What stays the same

`/v1/permits`, `/v1/permit-requests`, signed receipts, the ledger, and the
governed `/mcp/messages` transport are unchanged and remain the right
surface for operators and sophisticated integrations. The standard surface
just stops requiring the agent to orchestrate those pieces manually in the
common case.
