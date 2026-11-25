---
title: Calls and request keys
section: Concepts
order: 2
description: A Call carries a Permit and a stable Request key across retries.
---

# Calls and request keys

A Call invokes one governed MCP tool. It carries the Permit identifier and a Request key. The Request key is sent as `idempotency_key` in `mcpContext` or through the `Idempotency-Key` header.

The caller mints the Request key: the agent or orchestrator chooses one key per logical operation and keeps it across retries. The gateway records a supplied key exactly as sent. A stable operation ID, such as an LLM tool call's `tool_call.id`, is a suitable key when it is stable for that operation, at most 128 characters, and has no control characters.

For a Call to `partner.echo`, the Request key identifies one logical operation. Keep both the key and the payload unchanged when retrying. For a finalized Call, a retry with the same key returns the original receipt instead of dispatching again. A changed payload under that key is rejected with JSON-RPC `-32009` on the governed MCP path.

If the agent regenerates arguments for a retry, a key hashed from those arguments changes with them, and the retry becomes a new request that can dispatch and charge again. A hash of the arguments is therefore a poor identity for a retry. Persist the key from the first attempt and resend the original payload. The [request keys guide](https://github.com/agentmiddleware/agent-middleware/blob/main/gateway/docs/concepts/request-keys.md) covers the optional argument-derived helper and its limits.

The gateway checks the Permit before charging. On the configured upstream MCP path, an accepted Request key can produce at most one gateway dispatch and one ledger debit. A Call still in progress can return an in progress response. An upstream timeout after dispatch can produce `delivery_uncertain`, which requires reconciliation.

Remember that a new key is a new request. The upstream tool must honor the forwarded key to avoid duplicate downstream effects.

Read [Permits](/docs/permits/), [Receipts](/docs/receipts/), and [Limits](/docs/limits/) before relying on a Call for a consequential tool.
