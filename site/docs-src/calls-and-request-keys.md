---
title: Calls and request keys
section: Concepts
order: 2
description: A Call carries a Permit and a stable Request key across retries.
---

# Calls and request keys

A Call invokes one governed MCP tool. It carries the Permit identifier and a Request key. The Request key is sent as `idempotency_key` in `mcpContext` or through the `Idempotency-Key` header.

For a Call to `partner.echo`, the Request key identifies one logical operation. Keep both the key and the payload unchanged when retrying. For a finalized Call, a retry with the same key returns the original receipt instead of dispatching again. A changed payload under that key is rejected with JSON-RPC `-32009` on the governed MCP path.

The gateway checks the Permit before charging. On the configured upstream MCP path, an accepted Request key can produce at most one gateway dispatch and one ledger debit. A Call still in progress can return an in progress response. An upstream timeout after dispatch can produce `delivery_uncertain`, which requires reconciliation.

Remember that a new key is a new request. The upstream tool must honor the forwarded key to avoid duplicate downstream effects.

Read [Permits](/docs/permits/), [Receipts](/docs/receipts/), and [Limits](/docs/limits/) before relying on a Call for a consequential tool.
