---
title: Run locally
section: Start
order: 3
description: Run the local AMW quickstart and continue to the governed Call walkthrough.
---

# Run locally

Use the repository quickstart for local work. It starts a strict trust server on loopback, prepares the local signing material and database, and enables local key self provisioning. This mode is for a machine you control. Production deployments refuse to start with local self provisioning enabled.

From the [repository](https://github.com/agentmiddleware/agent-middleware) root:

```bash
cd gateway
make quickstart
```

Keep the server running. In another terminal, set the local API address and inspect discovery:

```bash
export API_URL=http://127.0.0.1:8000
curl -s "$API_URL/.well-known/agent.json"
curl -s "$API_URL/mcp/tools.json"
```

The local catalog identifies the available tool. Follow the [gateway quickstart](https://github.com/agentmiddleware/agent-middleware/blob/main/gateway/docs/quickstart.md) for the tested sequence that provisions a wallet key, creates a Permit, makes a Call, checks a retry, and verifies the Receipt offline. Do not assume the public sample tool `partner.echo` is installed in the local quickstart.

The raw server has separate signing key setup. `make quickstart` handles that setup for this local path. Its state persists across restarts under `data/quickstart/`.
