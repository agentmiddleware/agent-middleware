---
title: Errors
section: Reference
order: 2
description: Read governed Call errors without mistaking an uncertain result for a safe retry.
---

# Errors

Applies to gateway commit 75278a8 (also deployed b039e0bf).

Read the JSON-RPC `error.code`, `error.message`, and any `error.data.receipt` from `POST /mcp/messages`. An HTTP route instead returns its status and a `detail` body. The [Python SDK](/docs/python-sdk/) maps common responses to typed exceptions.

| JSON-RPC code | HTTP status | Meaning |
| --- | --- | --- |
| `-32602` | Varies by route | Invalid parameters, including an invalid Request key. |
| `-32003` | Varies by route | Permit, wallet, or policy denial. |
| `-32004` | Varies by route | Insufficient funds. |
| `-32005` | 504 for `delivery_uncertain` | Also used for `idempotency_in_progress`. Read the message and Receipt outcome. |
| `-32006` | 502 | Upstream `response_rejected` (charged) or upstream `failed_refunded` (refunded). Read the Receipt outcome. |
| `-32603` | 500 | `failed_unrefunded`: execution failed and the refund failed. Reconcile the Receipt before any retry. A failing local tool also uses this code and status, but carries a `failed_refunded` Receipt at this commit. |
| `-32007` | 500 | Effects were committed but a terminal record could not be written. Manual review is required. |

A changed payload under the same Request key is a terminal `idempotency_key_reused` error. `POST /mcp/messages` returns JSON-RPC `-32603` with message `idempotency_key_reused`; match the message, not the code. `POST /mcp` returns JSON-RPC `-32003`. REST `POST /mcp/tools/{id}/invoke` returns HTTP 409 with `{"detail":{"error":"idempotency_key_reused"}}`. Never retry with a new key; fix the caller.

`delivery_uncertain` is charged and never redispatched. On `POST /mcp/messages` it is JSON-RPC `-32005`; the REST invoke route returns HTTP 504. On `POST /mcp`, it is an `isError` tool result carrying the Receipt, not a JSON-RPC error. Reconcile with the upstream; do not retry with a new key.

Save a JSON-RPC response as `response.json` and inspect the fields that are present:

```bash
python3 - <<'PY'
import json

with open('response.json', encoding='utf-8') as file:
    error = json.load(file).get('error', {})
print(error.get('code'), error.get('message'))
print((error.get('data') or {}).get('receipt'))
PY
```

For a finalized Call, a retry with the same key returns the original receipt instead of dispatching again. Remember that a new key is a new request. `delivery_uncertain` is charged and needs reconciliation. Do not infer that an upstream action failed from a timeout. See the [governed error handling](https://github.com/agentmiddleware/agent-middleware/blob/75278a8f97bcdfe042f1f9bee59b01412612aaa7/gateway/app/routers/mcp.py), [standard MCP handling](https://github.com/agentmiddleware/agent-middleware/blob/75278a8f97bcdfe042f1f9bee59b01412612aaa7/gateway/app/routers/mcp_standard.py), and [failure semantics](https://github.com/agentmiddleware/agent-middleware/blob/75278a8f97bcdfe042f1f9bee59b01412612aaa7/gateway/docs/failure-semantics.md).
