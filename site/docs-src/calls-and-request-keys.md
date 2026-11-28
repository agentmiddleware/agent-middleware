---
title: Calls and request keys
section: Concepts
order: 2
description: A Call carries a Permit and a stable Request key across retries.
---

# Calls and request keys

A Call invokes one governed MCP tool. It carries the Permit identifier and a Request key. The Request key is sent as `idempotency_key` in `mcpContext` or through the `Idempotency-Key` header.

For a Call to `partner.echo`, the Request key identifies one logical operation. Keep both the key and the payload unchanged when retrying. For a finalized Call, a retry with the same key returns the original receipt instead of dispatching again. A changed payload under that key is a terminal `idempotency_key_reused` error: `POST /mcp/messages` returns JSON-RPC `-32603` with message `idempotency_key_reused` (match the message, not the code); `POST /mcp` returns JSON-RPC `-32003`; REST `POST /mcp/tools/{id}/invoke` returns HTTP 409 with `{"detail":{"error":"idempotency_key_reused"}}`. Never retry with a new key; fix the caller.

The gateway checks the Permit before charging. On the configured upstream MCP path, an accepted Request key can produce at most one gateway dispatch and one ledger debit. A Call still in progress can return an in progress response. An upstream timeout after dispatch can produce `delivery_uncertain`, which requires reconciliation.

Unkeyed mutating Calls on `POST /mcp` receive a derived key for a bounded suppression window. The effective window is the minimum of `MCP_UNKEYED_RETRY_WINDOW_SECONDS` (300 seconds by default) and the auto Permit TTL (120 seconds by default), so it is 120 seconds by default. After that window, an identical retry runs and bills again. Refunded pre-dispatch failures may run again within the window. If a derived key cannot be supplied, the mutating Call is refused. Explicit-permit Calls always require a key. Consequential Calls must send a persistent caller-owned [Request key](/docs/calls-and-request-keys/).

Remember that a new key is a new request. The upstream tool must honor the forwarded key to avoid duplicate downstream effects.

Read [Permits](/docs/permits/), [Receipts](/docs/receipts/), and [Limits](/docs/limits/) before relying on a Call for a consequential tool.
