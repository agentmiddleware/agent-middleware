# Design Partner Guide

Use this guide to qualify design partners for the concrete trust-plane proof:
scoped signed permit, governed MCP invoke, wallet charge, signed receipt,
ledger entry, audit chain, replay safety, and out-of-scope denial.

This guide proves technical fit. It does not by itself prove customer demand.
Use the interview funnel, commercial evidence gate, and partner-owned pilot
acceptance test in
`docs/30-day-customer-validation.md`.

For the concise route through local evaluation, integration, SDK, security, and
pilot material, use the [documentation guide](docs/README.md).

## Best-Fit Partner

An AI platform, infrastructure, or security engineering team that already has
internal agents calling MCP-style tools and needs a practical control point for:

- Tool-level authorization.
- Wallet or budget-backed metering.
- Replay-safe retries.
- Post-hoc receipt verification.
- Audit evidence for who authorized what, what ran, and what it cost.

This is best for teams that can bring one real internal tool call to the demo.
It is not yet a fit for teams seeking production settlement, a full IAM
replacement, or universal governance across every agent framework.

Before a paid partner uses anything beyond the constrained governed MCP path,
conduct a fresh security review of the exact deployed commit. This guide and
the public receipt are product evidence, not a security sign-off.

## Demo Path

Run the focused one-command proof first:

```bash
make demo-trust-plane
```

### API key bootstrap (gated)

There is **no public self-serve key mint**. After discovery, agents get `401`
until an operator provisions a wallet-scoped key with a bootstrap admin key.

Documented flow + script:

- [`docs/partner-api-key-bootstrap.md`](docs/partner-api-key-bootstrap.md)
- `scripts/partner_api_key_bootstrap.py`

Also advertised on `GET /.well-known/agent.json` → `authentication.bootstrap_docs`.

For local development only, dogfood a fake partner tool with a real side
effect (writes `data/dogfood_partner_notes.jsonl`):

```bash
make dogfood-trust-plane
```

Keep `ENABLE_DOGFOOD_TOOL=false` on Railway. The public evidence fixture comes
from the configured upstream `partner.echo`; it is labeled self-issued proof,
not customer traction. `partner.notes.write` must never stand in for a real
partner tool in production.

For the live engagement checklist that replaces `trust-plane-echo` / 
`partner.notes.write` with the partner's real tool id, use
[`docs/partner-first-tool-runbook.md`](docs/partner-first-tool-runbook.md).

### Rate limits

The deployed limit is `RATE_LIMIT_PER_MINUTE` (default and production value:
120). `RateLimitMiddleware` counts it in a 60-second window and returns
`X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `X-RateLimit-Reset`
on every counted response, including `401`s; the 121st request in a window
gets `429`. A burst of a few dozen requests will not `429` — that is the
120/min ceiling working as designed, not a missing limiter. Look at
`X-RateLimit-Remaining` to see the budget without waiting for `429`.

- **Authenticated requests:** one bucket per `X-API-Key` value. Two agents
  sharing one key share the 120; two keys get 120 each.
- **Requests without a key:** one `anonymous` bucket shared by every
  unauthenticated caller of the deployment. `/health/dependencies` is counted
  here.
- **Unaccepted credentials:** a request whose key the deployment does not
  accept — a `401`, the `403 invalid_api_key` an unknown key gets, or any
  request to a route that never checks the key (public discovery, a `404`) —
  is also charged to one shared per-client bucket at ten times the limit,
  because the per-key bucket is chosen from a header before the key is
  verified. Sending a different `X-API-Key` on every request does not hand you
  a fresh 120. A key the deployment accepts on an authenticated route never
  touches that bucket, and neither does a permit denial: being refused on
  scope is normal governed traffic, not a bad credential.
- **Window accounting:** the shared Redis limiter counts fixed 60-second
  buckets; the in-memory fallback (never used in a production-like
  environment, which fails closed) counts a rolling 60 seconds. Budget and
  window length are the contract — `rate_limits.window_accounting` in
  discovery says which backend counts how.
- **Exempt paths:** `/`, `/health`, `/.well-known/agent.json`, `/llms.txt`,
  `/docs`, `/redoc`, `/openapi.json`, and the served markdown docs.
- **`POST /mcp/public` (when enabled):** per client IP at the same limit, plus
  a global cap of ten times the limit.
- **No burst allowance and no per-key override.** Raising the limit for one
  partner today means raising it for the whole deployment.
- **Redis outage:** production-like environments return `503` instead of
  falling back to per-process memory, so the limit is never silently wider
  than declared.

`GET /v1/discover` → `rate_limits` and `GET /` → `rate_limits` report the
same figure the middleware enforces.

### MCP discovery gate (Phase 2)

Live trust mode keeps both `ENABLE_PROOF_SURFACES=false` and
`ENABLE_DOGFOOD_TOOL=false`. Phase9 AWI and
marketplace-style discovery stubs are **not** part of the wedge; with Phase 2
they are gated/removed from `/mcp/tools.json` and MCP registration when proof
surfaces are off. Use one operator-configured upstream tool in production;
`partner.notes.write` remains a local test fixture only. Inventory any
dependency on Phase9 / AWI / marketplace stub tool ids and plan a swap to a
real registered tool.
Details: `docs/tech-debt-remediation-plan.md`
(Phase 2).

If the partner wants the operator narrative instead of the compact proof, run:

```bash
make agent-ops-war-room
```

If the partner is from a security team and wants to see the boundary hold
under attack rather than just on the happy path, run the adversarial battery:

```bash
make red-team-trust-plane
```

It drives one valid permit and attacks it ten ways — no permit, unknown
permit, out-of-scope tool, missing scope, over-budget, stolen permit (wrong
wallet), wrong key, expired, revoked, and tampered signature — and asserts
each is denied with a concrete reason code, that none produces a ledger debit,
and that a final valid call is charged once (one debit for its key).

Then walk the partner through the live flow:

1. Create a sponsor wallet.
2. Create an agent wallet.
3. Issue a DB-backed API key for the agent wallet.
4. Issue a signed permit for one MCP tool with wallet binding, allowed tool,
   `billing:charge`, budget, expiry, nonce, and idempotency.
5. Invoke the tool through governed MCP with `permit_id` and
   `idempotency_key`.
6. Show the wallet charge in the ledger.
7. Verify the signed receipt.
8. Verify the wallet audit chain.
9. Replay the same request and show the same receipt ID with no second debit.
10. Attempt a different tool under the same permit and show out-of-scope
    denial.
11. Attempt the same allowed tool with no permit at all and show the
    `permit_required` denial, proving the trust plane fails closed when
    `ALLOW_LEGACY_UNPERMITTED_MCP=false`.

## Problem Discovery Before The Demo

Start with one question:

> Give me one tool you are currently afraid to let an autonomous agent invoke.

Then establish the consequence, current control, and buyer before showing the
product:

- What happens when that request times out and the agent retries?
- Who authorizes the action and its economic exposure today?
- How does the team prove what happened?
- Which existing IAM, gateway, logging, or approval control is insufficient?
- Would the team accept an inline availability, latency, and security
  dependency?
- Who owns the staging endpoint, engineering work, budget, and decision date?

Do not ask, "Would you use this?" A real signal is a partner committing its own
tool and engineer, not agreeing that the demo is interesting.

## Success Criteria

- The partner can point one internal tool call through the MCP proxy.
- The partner can define a wallet budget and tool scope.
- Retries are safe under the same idempotency key.
- The partner can verify a receipt after the fact.
- The partner can audit who authorized the action, what tool ran, and what it
  cost.
- The partner can see an out-of-scope request denied with an explicit reason.
- The partner's engineer can export and verify the receipt in the partner's own
  environment.
- The partner states what unacceptable risk would return if the boundary were
  removed.
- A commercial owner, next action, and decision date are recorded. Without
  those, the result is a technical pass only.

## Positioning Language

Use:

- "Authorize one agent action. Charge it once. Prove what happened."
- "At most one gateway dispatch and at most one debit per accepted idempotency
  key; a same-key retry returns the original receipt."
- "Signed proof of authorization, execution, and credit debit for one tool."
- "Replay-safe metering: same idempotency key, same receipt, no second gateway
  dispatch or debit."

Avoid:

- Leading with "trust plane" or "MCP gateway" as the product (category is crowded).
- "Production-ready agent payments."
- "Autonomous economic actor infrastructure."
- "Complete policy layer for all agent frameworks."
- "Compliance-grade ledger or audit storage."
- Claiming a marketing-page signature is live cryptographic proof.
- Claiming "exactly-once", or that an arbitrary remote tool's side effect happens once when that
  tool does not honor the forwarded idempotency key.
## Do Not Promise Yet

- Settlement rails.
- Compliance-grade ledger storage.
- Full IAM replacement.
- Production sandbox isolation.
- Cross-protocol governance for every agent framework.
- Key-management hardening beyond the current trust-plane proof.
