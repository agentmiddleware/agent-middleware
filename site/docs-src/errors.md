---
title: Errors
section: Reference
order: 2
description: Read governed Call errors without mistaking an uncertain result for a safe retry.
---

# Errors

Read the JSON-RPC `error.code`, `error.message`, and any `error.data.receipt` from `POST /mcp/messages`. An HTTP route instead returns its status and a `detail` body. The [Python SDK](/docs/python-sdk/) maps common responses to typed exceptions.

| JSON-RPC code | Meaning |
| --- | --- |
| `-32602` | Invalid parameters, including an invalid Request key. |
| `-32003` | Permit, wallet, or policy denial. |
| `-32004` | Insufficient funds. |
| `-32005` | A Call may still be in progress, or a Receipt may report `delivery_uncertain`. Inspect the message and Receipt before retrying. |
| `-32007` | Effects were committed but a terminal record could not be written. Manual review is required. |
| `-32009` | The Request key was reused for a different request. |

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

For a finalized Call, a retry with the same key returns the original receipt instead of dispatching again. Remember that a new key is a new request. `delivery_uncertain` is charged and needs reconciliation. Do not infer that an upstream action failed from a timeout. See the [governed error handling](https://github.com/agentmiddleware/agent-middleware/blob/main/gateway/app/routers/mcp.py) and [failure semantics](https://github.com/agentmiddleware/agent-middleware/blob/main/gateway/docs/failure-semantics.md).
