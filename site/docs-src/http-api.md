---
title: HTTP API
section: Reference
order: 1
description: Find the live API contract and invoke a governed MCP tool.
---

# HTTP API

Read `GET /openapi.json` on your gateway for its current HTTP contract. Use `GET /v1/discover/tools` to find a tool and its argument schema. Create a Permit with `POST /v1/permits`. The standard MCP path invokes at `POST /mcp` when enabled. The example below uses `POST /mcp/messages`, a deprecated JSON-RPC route retained for existing clients and local proof scripts.

Send your wallet key in `X-API-Key`. Permit creation requires an `Idempotency-Key` header. A Call includes its Permit id, wallet id, and Request key in `mcpContext`; the same key can also be sent in the header. Replace the tool name and arguments below with values from discovery.

```bash
API_URL='YOUR_API_URL'
curl -sS -X POST "$API_URL/mcp/messages" \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: YOUR_DEMO_KEY' \
  -H 'Idempotency-Key: YOUR_REQUEST_KEY' \
  -d '{"jsonrpc":"2.0","id":"YOUR_REQUEST_KEY","method":"tools/call","params":{"name":"YOUR_TOOL_NAME","arguments":{},"mcpContext":{"wallet_id":"YOUR_WALLET_ID","permit_id":"YOUR_PERMIT_ID","idempotency_key":"YOUR_REQUEST_KEY"}}}'
```

For a finalized Call, a retry with the same key returns the original receipt instead of dispatching again. Remember that a new key is a new request. Keep the same request body when retrying; a changed request under the same key is a conflict.

Use `GET /v1/receipts/{receipt_id}/portable` to export an authorized Receipt. Its public verification keys are at `GET /.well-known/trust-keys.json` without a credential. See the [gateway routes](https://github.com/agentmiddleware/agent-middleware/tree/main/gateway/app/routers), the [Python SDK](/docs/python-sdk/), and the [AMW site](https://www.agentmiddleware.dev/).
