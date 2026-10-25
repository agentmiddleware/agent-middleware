---
title: Put a refund tool behind AMW
section: Guides
order: 1
description: Configure one refund MCP tool as the governed upstream.
---

# Put a refund tool behind AMW

Start with a refund tool that your team owns and exposes through Streamable HTTP MCP. AMW connects to one exact upstream tool and exposes it under a public tool ID. The `partner.refund` tool in this gateway checkout is a synthetic staging fixture, not a refund processor.

## Configure the gateway

Set these values on the gateway deployment. Keep the upstream bearer token in the deployment secret store.

```bash
export MCP_UPSTREAM_ENABLED=true
export MCP_UPSTREAM_URL='https://YOUR_REFUND_HOST/mcp'
export MCP_UPSTREAM_TOOL_NAME='YOUR_UPSTREAM_TOOL_NAME'
export MCP_UPSTREAM_PUBLIC_TOOL_ID='YOUR_REFUND_TOOL_ID'
export MCP_UPSTREAM_BEARER_TOKEN='YOUR_UPSTREAM_BEARER_TOKEN'
export MCP_UPSTREAM_CREDITS_PER_CALL='YOUR_CREDITS_PER_CALL'
```

At startup, AMW initializes the upstream, lists its tools, and checks for the exact configured name. Read `GET /mcp/tools.json` to confirm the public ID. Then issue a Permit for `YOUR_REFUND_TOOL_ID` with scopes `tool:YOUR_REFUND_TOOL_ID:invoke` and `billing:charge`. Follow the [partner tool runbook](https://github.com/agentmiddleware/agent-middleware/blob/main/gateway/docs/partner-first-tool-runbook.md) for the wallet, Permit, Call, and Receipt sequence.

Keep the request body and Request key stable during a retry. For a finalized Call, a retry with the same key returns the original receipt instead of dispatching again. A new key is a new request. A `delivery_uncertain` outcome needs reconciliation with the upstream before anyone decides whether the refund happened. The upstream must honor the forwarded key to prevent duplicate downstream effects. Read [Limits](/docs/limits/) before using this path for real refunds.
