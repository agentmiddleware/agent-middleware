# Quickstart

Goal: run the gateway locally, put a tool behind it, call it from an MCP client,
retry with the same key, and verify the receipt offline. About ten minutes. No
account, no network service other than your own machine.

Prerequisites: Python 3.11+, [`uv`](https://docs.astral.sh/uv/), `make`, `curl`.

## 1. Boot a local gateway

```bash
git clone https://github.com/agentmiddleware/agent-middleware
cd agent-middleware/gateway
make quickstart        # strict trust mode, SQLite, a locally generated signing key
```

Wait for `Trust plane is up: http://127.0.0.1:8000`. The quickstart enables the
standard MCP endpoint (`POST /mcp`) and local-only key self-provisioning. A
production-like configuration refuses to boot with self-provisioning on.

The demo tool behind the gateway is `partner.notes.write`: it appends a line to
`data/dogfood_partner_notes.jsonl`, so you can count real executions.

## 2. Get a key (second terminal, in `gateway/`)

```bash
export API_URL=http://127.0.0.1:8000
PROVISION_JSON=$(curl -s -X POST "$API_URL/v1/dev-keys/self-provision" \
  -H "Content-Type: application/json" -d '{"agent_id": "quickstart"}')
export AGENT_API_KEY=$(echo "$PROVISION_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["api_key"])')
```

## 3. Call the tool from an MCP client, then retry

Any Streamable HTTP MCP client works. Point it at `http://127.0.0.1:8000/mcp`
with an `X-API-Key` header; a config file for clients that read `mcp.json` is in
[`examples/mcp-client/mcp.json`](examples/mcp-client/mcp.json).

Standard MCP clients cannot send permit context, so for these calls the gateway
mints a bounded, single-tool, short-lived permit from your wallet. Send an
`Idempotency-Key` header (or `io.agentmiddleware/idempotency_key` in
`params._meta`) to make retries safe. **A call with no key is a new charged
operation every time.**

The scripted version, using the official MCP Python SDK:

```bash
uv run --with "mcp>=1.29,<2" python ../examples/mcp-client/call_with_retry.py
```

```text
first receipt: rcpt-...
retry receipt: rcpt-...      <- the same id
```

For this key, a retry with the same key returns the original receipt instead of dispatching again.

Check the effect count yourself: `wc -l data/dogfood_partner_notes.jsonl` grew
by one, not two.

## 4. Verify a receipt offline

```bash
export RECEIPT_ID=rcpt-...   # from step 3
mkdir -p data/quickstart
curl -s "$API_URL/v1/receipts/$RECEIPT_ID/portable" -H "X-API-Key: $AGENT_API_KEY" \
  -o data/quickstart/receipt-bundle.json
curl -s "$API_URL/.well-known/trust-keys.json" -o data/quickstart/trust-keys.json   # no credential needed

PYTHONPATH=../sdk/python/src uv run --with "cryptography>=50" --with "httpx>=0.27,<1" \
  python -m b2a_sdk.verify_cli \
  --bundle data/quickstart/receipt-bundle.json --keys data/quickstart/trust-keys.json
```

The verifier never imports the gateway. Edit one character inside
`signing_input` and run it again: it reports the receipt as tampered (exit 1).
A TypeScript verifier with the same behavior is in [`sdk/typescript/`](sdk/typescript/).

## 5. Go further

- The full walkthrough with explicit permits, budget caps, an overspend that is
  denied before any debit, and an audit-chain check:
  [`gateway/docs/quickstart.md`](gateway/docs/quickstart.md).
- The lost-response fault against a native-idempotency baseline:
  `make failure-lab` and [`gateway/docs/failure-lab.md`](gateway/docs/failure-lab.md).
- Put your own MCP tool behind the gateway:
  [`gateway/docs/partner-first-tool-runbook.md`](gateway/docs/partner-first-tool-runbook.md).
- What can still go wrong: [`gateway/SECURITY_LIMITATIONS.md`](gateway/SECURITY_LIMITATIONS.md).
