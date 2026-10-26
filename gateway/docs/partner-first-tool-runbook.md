# Partner First-Tool Runbook

One-pager for replacing the local demo tool with one real Streamable HTTP MCP
tool.

## Goal

Prove this loop for **one** partner tool — nothing else:

```text
permit → governed MCP invoke → wallet charge → signed receipt
→ audit → replay (no double charge) → out-of-scope deny
```

Reference demo tool: `trust-plane-echo` (`make prove-trust-plane`).

## Placeholders

Fill these before the walkthrough:

| Placeholder | Example | Partner value |
|-------------|---------|---------------|
| `YOUR_TOOL_ID` | `internal.crm.search` | |
| `UPSTREAM_TOOL_NAME` | `crm_search` | |
| `UPSTREAM_MCP_URL` | `https://mcp.partner.example/mcp` | |
| `CREDITS_PER_CALL` | `3.0` | |
| `MAX_CREDITS` | `50` | |

## Environment

```bash
export TRUST_MODE_ENABLED=true
export ALLOW_LEGACY_UNPERMITTED_MCP=false
export ENABLE_PROOF_SURFACES=false
export TRUST_SIGNING_PRIVATE_KEY_B64=...   # from secret manager
export VALID_API_KEYS=...                  # bootstrap admin only
export DATABASE_URL=...
export MCP_UPSTREAM_ENABLED=true
export MCP_UPSTREAM_URL="UPSTREAM_MCP_URL"
export MCP_UPSTREAM_TOOL_NAME="UPSTREAM_TOOL_NAME"
export MCP_UPSTREAM_PUBLIC_TOOL_ID="YOUR_TOOL_ID"
export MCP_UPSTREAM_BEARER_TOKEN=...       # from secret manager
export MCP_UPSTREAM_CREDITS_PER_CALL="CREDITS_PER_CALL"
```

Do not put the bearer token in the URL, logs, manifests, or partner artifacts.
Do not mount AWI/media/oracle for this session.

## Rolling deployment safety

Apply Alembic through the current head (`033_drop_optimizer_telemetry`)
before current workers take traffic. Confirm with `alembic heads` rather than
trusting this line — a stale head here under-migrates the deployment.

Revision `027_governed_mcp_identity` adds a rolling-compatible unique index
that serializes pre-026 physical MCP endpoint keys with the current
`/mcp/invoke` identity. Revision 028 revokes historical refresh tokens that
cannot be safely bound to their originating API key. Revisions 029-032 add
permit v2 constraints, permit requests, signed quotes, and the machine-readable
receipt reason code; a worker running current code against a database stopped
at 028 fails on those columns. Revision 033 drops `optimizer_telemetry`, a
table no code reads, so it adds no column requirement — but a database stopped
at 032 still fails startup, because `app/db/database.py` refuses to boot on any
revision behind the packaged head, not only on missing columns.

Revision 027 stops with only an aggregate conflict count if historical rows
already reuse one wallet/key across MCP endpoint generations. Do not pick or
delete one automatically; adjudicate those operations from their receipts,
ledger entries, and audit evidence, then rerun the migration.

For an emergency code rollback, keep migration 027 in place. Old workers remain
compatible and fail closed if they collide with a canonical row. Dropping the
index after canonical rows exist reopens the duplicate debit/dispatch race.

## Configure the partner tool

The gateway performs `initialize` and `tools/list` during startup, selects the
exact `MCP_UPSTREAM_TOOL_NAME`, and exposes it as
`MCP_UPSTREAM_PUBLIC_TOOL_ID`. Configuration fails closed if the endpoint is
unreachable or that tool is absent. No database service registration is
required.

Keep a second tool id out of the permit (e.g. `YOUR_TOOL_ID.admin`) for the
out-of-scope denial step.

### Operator-controlled live smoke server

Before a partner endpoint is available, deploy the repository's isolated
stateless smoke server as a separate service. It proves real TLS, bearer auth,
MCP discovery, metadata forwarding, and gateway dispatch; it is not a partner
integration and performs no persistent side effect.

```bash
export APP_MODULE=app.partner_mcp:app
export RUN_MIGRATIONS_ON_START=false
export PARTNER_MCP_BEARER_TOKEN=...       # generated secret, 32+ characters
export PARTNER_MCP_ALLOWED_HOSTS=exact-service-host.example
```

The service exposes public `GET /health` and authenticated Streamable HTTP at
`/mcp`, with one tool named `partner.echo`. Configure the gateway's
`MCP_UPSTREAM_URL`, tool names, and bearer token from this service, complete the
live checklist below, then replace it with the real partner endpoint. Never
describe the smoke server as evidence of partner adoption or remote
exactly-once side effects.

## Live checklist

1. `GET /.well-known/agent.json` and `GET /mcp/tools.json` — confirm `YOUR_TOOL_ID` and `requirePermit`.
2. Create sponsor wallet → agent wallet → agent API key.
3. Create permit:
   - `allowed_tools: ["YOUR_TOOL_ID"]`
   - `scopes: ["tool:YOUR_TOOL_ID:invoke", "billing:charge"]`
   - `max_credits: MAX_CREDITS`
   - `Idempotency-Key` on permit create
4. Governed invoke via `POST /mcp/messages`. `mcpContext` carrying `wallet_id`,
   `permit_id`, and `idempotency_key` goes directly in `params` rather than
   `_meta`.
5. Show ledger debit + `POST /v1/receipts/verify` with the receipt ID and the
   authorized agent credential:

   ```bash
   curl -s -X POST "$API_URL/v1/receipts/verify" \
     -H "X-API-Key: $AGENT_API_KEY" \
     -H "Content-Type: application/json" \
     -d "{\"receipt_id\": \"$RECEIPT_ID\"}"
   ```
6. Verify that a retry with the same key returns the original receipt instead of dispatching again. Confirm that the ledger shows exactly one debit for that request key after the retry.
7. Call the out-of-scope tool under the same permit → deny (`permit_tool_not_allowed`).
8. Call `YOUR_TOOL_ID` with no permit → deny (`permit_required`).
9. Force a post-dispatch timeout → signed `delivery_uncertain`, charge retained,
   and no automatic retry.
10. **Partner verifies the receipt offline, in their own environment.** Export
    the portable bundle and the public key set, then verify with no gateway
    access and no credentials:

    ```bash
    curl -s "$API_URL/v1/receipts/$RECEIPT_ID/portable" \
      -H "X-API-Key: $AGENT_API_KEY" > receipt-bundle.json
    curl -s "$API_URL/.well-known/trust-keys.json" > trust-keys.json

    # Install the Agent Middleware (AMW) Python SDK from this checkout
    # or a built SDK wheel:
    pip install "../sdk/python[verify]"  # or: pip install "./amw_sdk-<version>-py3-none-any.whl[verify]"
    amw verify --bundle receipt-bundle.json --keys trust-keys.json
    ```

    Expect `VERIFIED` with the permit, tool, outcome, and credits. Exit code 0
    verified, 1 invalid, 2 undetermined. Tampering with any field in
    `signing_input` must produce `INVALID`. This step must run on the partner's
    machine, by the partner's engineer — it is the one that demonstrates the
    evidence does not depend on trusting the issuer at read time.

Request bodies for steps 1-8 are in
[`docs/golden-path.md`](golden-path.md); the offline verification path is also
covered in [`docs/quickstart.md`](quickstart.md).

## Pass / fail

**Technical pass:** the partner-owned agent and staging tool complete the loop,
the partner engineer verifies the receipt in the partner environment, and the
measured integration burden is recorded.

**Commercial signal:** the partner says what unacceptable risk would return if
the boundary were removed and commits an owner, next action, and decision date.
A follow-up meeting or "cool demo" response alone is not validation. The
pass and fail rules in this section are the gate. There is no separate
validation document in this repository.

**Fail / stop:** partner asks for settlement, KMS, multi-framework policy, or
broad migration before the single-tool loop is trusted. Point to
`SECURITY_LIMITATIONS.md` and freeze proof surfaces.

## Talk track

- Permit = bounded authority (wallet, tool, budget, expiry, signature).
- MCP invoke is authorized, not only authenticated.
- Receipt is the durable proof of charge + outcome.
- Replay and out-of-scope deny are the product, not edge cases.
- The gateway permits at most one dispatch and debit per accepted idempotency
  key. The remote side effect is deduplicated only if the partner tool honors
  the forwarded idempotency metadata.

## Commands

```bash
make dogfood-trust-plane        # partner.notes.write playground (real side effect)
make prove-trust-plane          # local echo proof
make agent-ops-war-room         # narrated operator timeline
make red-team-trust-plane       # adversarial deny battery
```

See also: [`docs/golden-path.md`](golden-path.md) (copy-pasteable requests for
every step above), [`docs/quickstart.md`](quickstart.md) (offline receipt
verification), [`DESIGN_PARTNER_GUIDE.md`](../DESIGN_PARTNER_GUIDE.md),
[`DEMO_SCRIPT.md`](../DEMO_SCRIPT.md), [`WEDGE.md`](../WEDGE.md).
