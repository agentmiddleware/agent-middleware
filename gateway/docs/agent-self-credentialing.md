# Credentialing an agent to test this platform

An agent asked to validate the trust plane hits a wall at the economic
invariants: `discover → authenticate` are open, but `authorize → invoke →
meter → receipt` all require a wallet-scoped API key, and production
deliberately has no anonymous signup, no public key minting, and no free
credit grant. That gate is correct and must stay. This document is the
supported way around it: the agent credentials itself against a **local**
instance running the same code, and proves the invariants there.

What that does and does not establish is stated plainly at the bottom.

## 1. Start a local instance with self-provisioning on

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
mkdir -p data
cp .env.example .env
```

Set these literal values in `.env`:

```bash
ENVIRONMENT=local
DATABASE_URL=sqlite+aiosqlite:///./data/local_api.db
ENABLE_DEV_KEY_SELF_PROVISION=true   # mints wallet-scoped keys, no shared secret
ENABLE_DOGFOOD_TOOL=true             # registers partner.notes.write
ENABLE_DOGFOOD_SECOND_TOOL=true      # registers partner.notes.count (scope-denial target)
TRUST_SIGNING_KEY_ID=local-dev-ed25519
```

Generate a fresh local signing seed **in the shell**, export it, and start the
server in that same shell. `.env` parsing does not execute `$(...)`; do not put
the generation command in that file. Never reuse a production signing key.

```bash
export TRUST_SIGNING_PRIVATE_KEY_B64="$(.venv/bin/python -c 'import base64,secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())')"
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Startup must log `trust_signing_key_ready`. Without a seed the app refuses
to boot — trust mode has no unsigned fallback.

## 2. Let the agent mint its own credential

```bash
curl -X POST http://localhost:8000/v1/dev-keys/self-provision \
  -H 'Content-Type: application/json' -d '{"agent_id": "my-agent"}'
```

This returns a sponsor wallet, an agent wallet funded with synthetic dev
credits, and a **wallet-scoped** key (shown once) — the same credential
class `scripts/partner_api_key_bootstrap.py` produces for a partner, so the
agent exercises the real permit/metering/receipt path rather than a special
one. It is never bootstrap-admin: it cannot read the audit plane or reach
another tenant's wallet. Call it from a CLI or SDK — a cross-origin `Origin`
header is refused, because the route is unauthenticated and returns a live
secret.

## 3. Run the invariant proof

```bash
.venv/bin/python scripts/agent_self_credential_proof.py
```

The harness self-provisions, issues a permit scoped to one tool, and checks
the claims that carry economic weight, exiting non-zero on any failure:

| # | Invariant | Expected |
|---|---|---|
| 1 | charge-once | one governed invoke → exactly one debit |
| 2 | replay | same idempotency key → cached receipt, no new debit, no second side effect |
| 3 | concurrency | 5 identical in-flight calls → one success, one debit, one side effect |
| 4 | reuse-conflict | same key, different body → 400, no debit |
| 5 | scope-denial | tool outside the permit → 403 `permit_tool_not_allowed`, signed denial receipt, zero charged |
| 6 | no-permit | governed tool without a permit → 403, no debit |
| 7 | signature | every receipt verifies under the published Ed25519 key; tampered copies do not |

Check 7 is a genuinely independent verification: it fetches the public key
from `/.well-known/trust-keys.json`, verifies the portable receipt's
`signing_input` with its own Ed25519 code, and then re-signs nothing —
it mutates `credits_charged`, `outcome`, `tool`, and `wallet_id` in turn and
requires each forgery to fail. A field already equal to the mutation value
is skipped, so no check can pass vacuously.

Two behaviors worth knowing before you read the output as a bug:

- Under true concurrency the losing callers get **400**, not a replayed 200.
  The gateway fails closed while the first call is in flight; the cached
  receipt is served on retry once the winner settles.
- A denial still mints a **signed receipt** with `credits_charged` 0. Denials
  are evidence, not silence.

## 4. What this proves — and what it does not

A passing run verifies this local scenario: one debit and one tool execution
for the accepted identity under replay and concurrency, no debit for the
exercised conflicts and denials, and tamper-evident receipts on the paths
that finalize. It does not establish receipt completion after every failure.

Not proven by this harness, and not claimed:

- **Production behavior.** Same code, different infrastructure (Postgres,
  Redis, a real upstream MCP server). A local pass is strong evidence about
  the logic, not a measurement of the production deployment.
- **Remote effects.** For the configured upstream MCP tool, identical retries
  under the same accepted idempotency key allow at most one gateway dispatch
  and at most one debit. This does not guarantee delivery or a downstream
  effect; downstream replay safety also requires the upstream to honor the
  forwarded key. This local harness does not exercise upstream crash recovery.
- **Local and upstream execution differ.** `partner.notes.write` appends to a
  local JSONL file and is labeled `simulation: true` in discovery.
  `partner.echo` uses the configured upstream MCP adapter and durable dispatch
  state machine. Local governed tools have no dispatch state machine and
  interrupted calls fail closed into manual review.
- **Receipt completion.** A call can commit effects or a debit but fail to
  write its audit event or receipt, returning `manual_review_required` with
  no receipt. Do not retry with a new idempotency key: that can execute and
  charge the call again. Reconcile from the ledger and audit chain as described
  in [failure semantics](failure-semantics.md). An ambiguous upstream outcome
  is receipted as `delivery_uncertain` only when finalization or reconciliation
  succeeds.

To reproduce the invariants against production, an operator must issue a
wallet-scoped key and funded permit out of band. No agent should manufacture
production credentials, and this surface cannot: production-like
environments refuse to boot with `ENABLE_DEV_KEY_SELF_PROVISION` set, and
the handler independently returns 403 there.
