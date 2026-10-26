---
title: What is AMW
section: Start
order: 1
description: How Agent Middleware governs a consequential MCP tool Call.
---

# What is AMW

Agent Middleware (AMW) puts an authorization and transaction boundary in front of an MCP tool. A wallet scoped API key authenticates the caller. A Permit limits the tool, scope, Credits, and time available to that caller. A Call presents the Permit and a Request key. A Receipt records a finalized outcome.

For the configured upstream MCP tool, AMW allows at most one gateway dispatch and one ledger debit for an accepted Request key. Use the same key and payload for a retry. For a finalized Call, a retry with the same key returns the original receipt instead of dispatching again. A changed payload under that key is rejected.

The boundary is narrow. A gateway dispatch record cannot prove that the upstream tool acted. The upstream tool must also honor the forwarded Request key to avoid duplicate downstream effects. An uncertain delivery outcome needs reconciliation.

Start with the [hosted sandbox quickstart](/docs/three-minute-quickstart/), then read [Permits](/docs/permits/), [Calls and request keys](/docs/calls-and-request-keys/), and [Limits](/docs/limits/).

The [source repository](https://github.com/agentmiddleware/agent-middleware) and [organization](https://github.com/agentmiddleware) contain the project. The [website](https://www.agentmiddleware.dev) and [API discovery document](https://api.thisisatest.tech/.well-known/agent.json) are separate public surfaces.

Apache 2.0 (gateway is Fair Source, FSL-1.1)
