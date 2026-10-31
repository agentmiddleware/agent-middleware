# Run Agent Middleware locally

If you just want to try Agent Middleware (AMW), use the
[3 minute hosted quickstart](https://github.com/agentmiddleware/amw-demo).
This page is for running the gateway on your own machine.

## Prerequisites

Python 3.11+, `git`, `make`, `curl`, and
[`uv`](https://docs.astral.sh/uv/). The first run needs network access to
clone the repository and resolve Python dependencies. No AMW account or hosted
gateway is required.

## Run locally

```bash
git clone https://github.com/agentmiddleware/agent-middleware
cd agent-middleware/gateway
make quickstart
```

## What the command does

`make quickstart` starts the gateway on `127.0.0.1:8000` with strict trust
mode, SQLite, a locally generated signing key, and one available demo tool.
Wait for `[quickstart] Trust plane is up: http://127.0.0.1:8000`. The banner
also lists the discovery URL, the standard MCP endpoint (`POST /mcp`), and the
local key provisioning route.

The tool, `partner.notes.write`, appends a line to
`data/dogfood_partner_notes.jsonl` when called. The command starts the server
but does not provision a key, call the tool, retry, or verify a receipt.
Complete those steps below in a second terminal while the server stays
running.

The server binds to loopback because anyone who can reach its local key
provisioning route can mint a key with synthetic credits. Do not expose this
setup on a shared host. A production configuration refuses to boot with self
provisioning enabled. State and the signing key persist under
`data/quickstart/`; press Ctrl-C to stop the server without clearing them.

## Step by step

### 1. Provision a local key

Open a second terminal in the parent directory that contains
`agent-middleware`:

```bash
cd agent-middleware/gateway
export API_URL=http://127.0.0.1:8000
PROVISION_JSON=$(curl -s -X POST "$API_URL/v1/dev-keys/self-provision" \
  -H "Content-Type: application/json" -d '{"agent_id": "quickstart"}')
export AGENT_API_KEY=$(echo "$PROVISION_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["api_key"])')
```

### 2. Call the tool and check the retry

Any Streamable HTTP MCP client can connect to `http://127.0.0.1:8000/mcp`
with an `X-API-Key` header. An example client configuration is in
[`examples/mcp-client/mcp.json`](examples/mcp-client/mcp.json).

To use the hosted endpoint instead of the local server, [get an API key](https://www.agentmiddleware.dev/demo-key/)
and point the rest of this walkthrough at the hosted gateway:

```bash
export API_URL=https://api.agentmiddleware.dev
export AMW_MCP_URL=https://api.agentmiddleware.dev/mcp
export AGENT_API_KEY=<your API key>
```

The included client reads `AMW_MCP_URL` and `AGENT_API_KEY` and sends the key
as the `X-API-Key` header. Set `AMW_TOOL` if your key is allowed a different
tool than `partner.notes.write`. The `data/dogfood_partner_notes.jsonl` check
below applies only to the local server.

Standard MCP clients cannot send permit context, so the gateway mints a
bounded permit for one tool with a short expiry from your wallet. Send an
`Idempotency-Key` header or `io.agentmiddleware/idempotency_key` in
`params._meta` for a safe retry. **A call with no key is a new charged
operation every time.**

Run the included client using the official MCP Python SDK:

```bash
uv run --with "mcp>=1.29,<2" python ../examples/mcp-client/call_with_retry.py
```

It prints `first receipt: rcpt-...` and `retry receipt: rcpt-...` with the
same receipt ID. For an unchanged request,
a retry with the same key returns the original receipt instead of dispatching again.
Check `wc -l data/dogfood_partner_notes.jsonl`: the note count increases by
one for the pair of calls.

### 3. Verify the receipt offline

Replace `rcpt-...` with the receipt ID printed by the client:

```bash
export RECEIPT_ID=rcpt-...
mkdir -p data/quickstart
curl -s "$API_URL/v1/receipts/$RECEIPT_ID/portable" -H "X-API-Key: $AGENT_API_KEY" \
  -o data/quickstart/receipt-bundle.json
curl -s "$API_URL/.well-known/trust-keys.json" -o data/quickstart/trust-keys.json

PYTHONPATH=../sdk/python/src uv run --with "cryptography>=50" --with "httpx>=0.27,<1" \
  python -m b2a_sdk.verify_cli \
  --bundle data/quickstart/receipt-bundle.json --keys data/quickstart/trust-keys.json
```

The public key request needs no credential, and the verifier does not import
the gateway. Edit one character inside `signing_input` in the saved bundle and
run the verifier again. It reports the receipt as tampered and exits with
status 1. A TypeScript verifier is in [`sdk/typescript/`](sdk/typescript/).

### Troubleshooting and more detail

If the startup banner does not appear, inspect `data/quickstart/server.log`
and check whether port 8000 is in use. The
[full local walkthrough](gateway/docs/quickstart.md) covers explicit permits,
budget caps, a denied overspend, and the audit chain; start at step 2 after
launching the server here. The [failure lab](gateway/docs/failure-lab.md)
covers lost responses, and the
[tool runbook](gateway/docs/partner-first-tool-runbook.md) shows how to put
your own MCP tool behind the gateway. Read the
[security limitations](gateway/SECURITY_LIMITATIONS.md) before relying on
this pattern for real actions.
