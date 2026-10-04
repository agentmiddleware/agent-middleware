# Golden Path: Wallet-Scoped Agent Tool Call

This is the canonical production-beta flow. It proves that a developer can
provision money, issue a scoped key, let an agent act, and inspect the result.

The flow uses a bootstrap/admin key only for provisioning. The agent uses a
DB-created key scoped to its own wallet.

> **First time here?** This page is the *operator* flow: bootstrap keys,
> sponsor wallets, and policies. If you just want to drive the governed loop
> yourself with no operator-issued key, start with
> [docs/quickstart.md](quickstart.md) (`make quickstart`) instead.
>
> **About the tool name:** the examples below invoke `golden-path-echo`,
> which exists only where an operator (or the test/battery harness) has
> registered it. On a stock local server, set `ENABLE_DOGFOOD_TOOL=true`
> and substitute `partner.notes.write` (2 credits/call) everywhere
> `golden-path-echo` appears. Also replace the echo arguments
> `{"message": "hello"}` with `{"text": "hello"}` in both the first invoke and its replay.
> Keep the same permit and invocation idempotency keys when replaying.

## Prerequisites

Strict trust mode is on by default, so the API refuses to start without an
Ed25519 signing seed. Generate one **once** and save it — this flow uses a
persistent `./test.db`, and rebinding the same `TRUST_SIGNING_KEY_ID` to new key
material is rejected with `signing_key_id_public_key_mismatch` to preserve
historical verification. Reuse the same value on every restart:

```bash
python3 -c 'import base64, secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())'
```

Start the API with a local bootstrap key and that saved seed:

```bash
export VALID_API_KEYS=dev-bootstrap-key
export DATABASE_URL=sqlite+aiosqlite:///./test.db
export TRUST_SIGNING_KEY_ID=local-dev-ed25519
export TRUST_SIGNING_PRIVATE_KEY_B64='<paste-the-saved-seed>'
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Keep the seed in the gitignored `.env` or another local secret store. If you
lose it, delete `./test.db` and start from a fresh database. Do not reuse local
secrets in a shared environment.

Set shell helpers:

```bash
export API_URL=http://localhost:8000
export BOOTSTRAP_KEY=dev-bootstrap-key
```

## 1. Confirm Discovery

```bash
curl "$API_URL/.well-known/agent.json"
curl "$API_URL/llm.txt"
curl "$API_URL/mcp/tools.json"
```

## 2. Create A Sponsor Wallet

```bash
SPONSOR_JSON=$(
  curl -s -X POST "$API_URL/v1/billing/wallets/sponsor" \
    -H "X-API-Key: $BOOTSTRAP_KEY" \
    -H "Content-Type: application/json" \
    -d '{
      "sponsor_name": "Acme Beta",
      "email": "billing@example.com",
      "initial_credits": 10000,
      "require_kyc": false
    }'
)

export SPONSOR_WALLET_ID=$(echo "$SPONSOR_JSON" | jq -r '.wallet_id')
echo "$SPONSOR_WALLET_ID"
```

## 3. Provision An Agent Wallet

```bash
AGENT_JSON=$(
  curl -s -X POST "$API_URL/v1/billing/wallets/agent" \
    -H "X-API-Key: $BOOTSTRAP_KEY" \
    -H "Content-Type: application/json" \
    -d "{
      \"sponsor_wallet_id\": \"$SPONSOR_WALLET_ID\",
      \"agent_id\": \"research-agent-001\",
      \"budget_credits\": 1000,
      \"daily_limit\": 250
    }"
)

export AGENT_WALLET_ID=$(echo "$AGENT_JSON" | jq -r '.wallet_id')
echo "$AGENT_WALLET_ID"
```

## 4. Issue A Wallet-Scoped Agent API Key

```bash
AGENT_KEY_JSON=$(
  curl -s -X POST "$API_URL/v1/api-keys" \
    -H "X-API-Key: $BOOTSTRAP_KEY" \
    -H "Content-Type: application/json" \
    -d "{
      \"wallet_id\": \"$AGENT_WALLET_ID\",
      \"key_name\": \"research-agent-runtime\",
      \"expires_in_days\": 30
    }"
)

export AGENT_API_KEY=$(echo "$AGENT_KEY_JSON" | jq -r '.api_key')
echo "$AGENT_API_KEY"
```

Store this key securely. It is shown once.

## 5. Issue A Signed Tool Permit

Governed MCP calls use a signed permit plus an idempotency key. The permit
binds the agent wallet, the runtime key, the allowed tool, scope, budget, and
expiry.

```bash
export AGENT_KEY_ID=$(echo "$AGENT_KEY_JSON" | jq -r '.key_id')
export PERMIT_JSON=$(
  curl -s -X POST "$API_URL/v1/permits" \
    -H "X-API-Key: $BOOTSTRAP_KEY" \
    -H "Idempotency-Key: permit-research-agent-001" \
    -H "Content-Type: application/json" \
    -d "{
      \"issuer_wallet_id\": \"$AGENT_WALLET_ID\",
      \"subject_wallet_id\": \"$AGENT_WALLET_ID\",
      \"subject_key_id\": \"$AGENT_KEY_ID\",
      \"allowed_tools\": [\"golden-path-echo\"],
      \"scopes\": [\"tool:golden-path-echo:invoke\", \"billing:charge\"],
      \"max_credits\": 50,
      \"expires_at\": \"$(date -u -d '+30 minutes' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v+30M +%Y-%m-%dT%H:%M:%SZ)\"
    }"
)

export PERMIT_ID=$(echo "$PERMIT_JSON" | jq -r '.permit_id')
echo "$PERMIT_ID"
```

## 6. Verify Agent-Scoped Access

The agent key can read its own wallet:

```bash
curl "$API_URL/v1/billing/wallets/$AGENT_WALLET_ID" \
  -H "X-API-Key: $AGENT_API_KEY"
```

The same key should not read the sponsor wallet:

```bash
curl -i "$API_URL/v1/billing/wallets/$SPONSOR_WALLET_ID" \
  -H "X-API-Key: $AGENT_API_KEY"
```

Expected result: `403 Forbidden`.

## 7. Simulate Cost Before Acting

> **Dormant expansion surface.** The dry-run sandbox (and the velocity
> status read in step 9) mounts only when the local instance runs with
> `ENABLE_PROOF_SURFACES=true`. Production deployments do not mount these
> routes; the wedge path is quote → permit → invoke → receipt.

```bash
DRY_RUN_JSON=$(
  curl -s -X POST "$API_URL/v1/billing/dry-run/session" \
    -H "X-API-Key: $AGENT_API_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"wallet_id\": \"$AGENT_WALLET_ID\"}"
)

export DRY_RUN_SESSION_ID=$(echo "$DRY_RUN_JSON" | jq -r '.session_id')

curl -X POST "$API_URL/v1/billing/dry-run/charge" \
  -H "X-API-Key: $AGENT_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"wallet_id\": \"$AGENT_WALLET_ID\",
    \"service\": \"telemetry_pm\",
    \"units\": 1,
    \"description\": \"Estimate anomaly review cost\",
    \"dry_run_session_id\": \"$DRY_RUN_SESSION_ID\"
  }"
```

## 6a. Optional: Attach A Wallet Policy

Operators can constrain the agent wallet before execution:

```bash
curl -X POST "$API_URL/v1/policies" \
  -H "X-API-Key: $BOOTSTRAP_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"wallet_id\": \"$AGENT_WALLET_ID\",
    \"name\": \"golden-path-policy\",
    \"allowed_service_categories\": [\"agent_comms\"],
    \"max_cost_per_action\": 5
  }"
```

If an MCP invocation, billing charge, or planner action violates the active
wallet policy, it is denied before execution or charge and the audit event
includes the `policy_id` and evaluated constraints.

## 7. Invoke Or Discover Tools

Fetch the MCP manifest:

```bash
curl "$API_URL/mcp/tools.json" \
  -H "X-API-Key: $AGENT_API_KEY"
```

For a registered local or persistent MCP service, invoke through JSON-RPC with
wallet, permit, and replay context:

```bash
INVOKE_JSON=$(
  curl -s -X POST "$API_URL/mcp/messages" \
  -H "X-API-Key: $AGENT_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"id\": \"golden-call-1\",
    \"method\": \"tools/call\",
    \"params\": {
      \"name\": \"golden-path-echo\",
      \"arguments\": {\"message\": \"hello\"},
      \"mcpContext\": {
        \"wallet_id\": \"$AGENT_WALLET_ID\",
        \"permit_id\": \"$PERMIT_ID\",
        \"idempotency_key\": \"golden-path-invoke-001\"
      }
    }
  }"
)

export RECEIPT_ID=$(echo "$INVOKE_JSON" | jq -r '.result.receipt.receipt_id')
echo "$RECEIPT_ID"
```

Replace `golden-path-echo` with a tool from `/mcp/tools.json`.

Replay the exact same request and confirm the receipt ID is unchanged:

```bash
REPLAY_JSON=$(
  curl -s -X POST "$API_URL/mcp/messages" \
  -H "X-API-Key: $AGENT_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"id\": \"golden-call-1\",
    \"method\": \"tools/call\",
    \"params\": {
      \"name\": \"golden-path-echo\",
      \"arguments\": {\"message\": \"hello\"},
      \"mcpContext\": {
        \"wallet_id\": \"$AGENT_WALLET_ID\",
        \"permit_id\": \"$PERMIT_ID\",
        \"idempotency_key\": \"golden-path-invoke-001\"
      }
    }
  }"
)

echo "$REPLAY_JSON" | jq -r '.result.receipt.receipt_id'
```

Try a different registered tool under the same permit and confirm the response
is denied with a signed denial receipt:

```bash
curl -s -X POST "$API_URL/mcp/messages" \
  -H "X-API-Key: $AGENT_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"id\": \"golden-denial-1\",
    \"method\": \"tools/call\",
    \"params\": {
      \"name\": \"another-registered-tool\",
      \"arguments\": {},
      \"mcpContext\": {
        \"wallet_id\": \"$AGENT_WALLET_ID\",
        \"permit_id\": \"$PERMIT_ID\",
        \"idempotency_key\": \"golden-path-denial-001\"
      }
    }
  }" | jq '.error'
```

## 8. Inspect The Operation Record

After a scoped agent invokes a tool, operators can inspect the control-plane record:

```bash
curl "$API_URL/v1/audit/events?wallet_id=$AGENT_WALLET_ID" \
  -H "X-API-Key: $BOOTSTRAP_KEY"
```

The scoped agent key can also inspect its own wallet's audit stream:

```bash
curl "$API_URL/v1/audit/events?wallet_id=$AGENT_WALLET_ID" \
  -H "X-API-Key: $AGENT_API_KEY"
```

Each audit event should let an operator tie the action back to its wallet,
credential source, tool, endpoint, policy decision, request ID or correlation
ID, success flag, error, and metadata such as transport, estimated cost,
`permit_id`, `idempotency_key`, `request_hash`, and `ledger_entry_id`.
Use the policy decision ID to confirm why the action was allowed or denied.

Verify the signed receipt:

```bash
curl -X POST "$API_URL/v1/receipts/verify" \
  -H "X-API-Key: $AGENT_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"receipt_id\": \"$RECEIPT_ID\"}"
```

Inspect the permit and receipt ledger from the trust surfaces:

```bash
curl "$API_URL/v1/permits/$PERMIT_ID" \
  -H "X-API-Key: $AGENT_API_KEY"

curl "$API_URL/v1/permits/$PERMIT_ID/receipts" \
  -H "X-API-Key: $AGENT_API_KEY"

curl "$API_URL/v1/receipts?permit_id=$PERMIT_ID&wallet_id=$AGENT_WALLET_ID" \
  -H "X-API-Key: $AGENT_API_KEY"
```

Operators can also inspect public signing-key metadata without receiving
private key material:

```bash
curl "$API_URL/v1/signing-keys/active" \
  -H "X-API-Key: $BOOTSTRAP_KEY"
```

Verify the wallet audit chain:

```bash
curl -X POST "$API_URL/v1/audit/verify-chain" \
  -H "X-API-Key: $AGENT_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"wallet_id\": \"$AGENT_WALLET_ID\"}"
```

## 9. Inspect Ledger And Velocity

```bash
curl "$API_URL/v1/billing/ledger/$AGENT_WALLET_ID" \
  -H "X-API-Key: $AGENT_API_KEY"

# Velocity status is a dormant expansion surface: requires a local instance
# running with ENABLE_PROOF_SURFACES=true (never mounted in production).
curl "$API_URL/v1/billing/wallets/$AGENT_WALLET_ID/velocity" \
  -H "X-API-Key: $AGENT_API_KEY"
```

## Success Criteria

- Agent discovery endpoints respond.
- Sponsor and agent wallets are created.
- Agent API key authenticates.
- Agent API key can access only its own wallet.
- Dry-run simulation returns a cost estimate.
- MCP manifest is available.
- Signed permit creation binds wallet, key, tool, budget, and expiry.
- Governed MCP invocation returns a signed receipt.
- Receipt verification and audit-chain verification succeed.
- Replaying the same governed invoke returns the same receipt without a second
  ledger debit.
- Out-of-scope governed invocation is denied with a signed denial receipt.
- Control-plane audit records are inspectable with the bootstrap key.
- Operators can inspect the policy decision, audit event, ledger entry, and
  request/correlation ID for the scoped tool call.
- Ledger and velocity endpoints are inspectable with the agent key.
