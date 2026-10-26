---
title: MCP client setup
section: Guides
order: 2
description: Connect an MCP client to the local standard MCP endpoint.
---

# MCP client setup

Run the local gateway from the [AMW repository](https://github.com/agentmiddleware/agent-middleware). It enables the standard `POST /mcp` endpoint on loopback. Provision a wallet scoped key by following the [gateway quickstart](https://github.com/agentmiddleware/agent-middleware/blob/main/gateway/docs/quickstart.md).

```bash
git clone https://github.com/agentmiddleware/agent-middleware
cd agent-middleware/gateway
make quickstart
```

In another terminal, register the local endpoint with Claude Code using the wallet key you provisioned:

```bash
claude mcp add --transport http agent-middleware \
  http://127.0.0.1:8000/mcp \
  --header 'X-API-Key: YOUR_WALLET_KEY'
```

The standard endpoint creates a bounded Permit for a wallet scoped caller. It rejects a bootstrap admin key because that key has no wallet.

`POST /mcp` is disabled by default outside the local quickstart. Check that an operator has enabled it on the deployment before adding a remote client. A client must keep its own `Idempotency-Key` header or `_meta["io.agentmiddleware/idempotency_key"]` value stable across a retry. For a finalized Call, a retry with the same key returns the original receipt instead of dispatching again. A new key is a new request. See [Calls and request keys](/docs/calls-and-request-keys/).
