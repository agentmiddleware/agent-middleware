# Agent Middleware

Agent Middleware is a gateway you put in front of an agent's risky tools (refunds, payouts, deploys) so each action needs a signed permit, runs at most once per request key, and leaves a signed receipt anyone can check offline.

**Status:** Pre release. Solo maintained. Install from source; nothing is published to PyPI or npm.

## Try it in 3 minutes

Use the [hosted demo](https://github.com/agentmiddleware/amw-demo). Get a demo key at the [demo key page](https://www.agentmiddleware.dev/demo-key/). The example also needs the associated wallet ID.

```bash
git clone https://github.com/agentmiddleware/amw-demo.git
cd amw-demo
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
export AMW_API_KEY='<your key>'
export AMW_WALLET_ID='<your wallet id>'
python examples/permit_echo_retry.py
```

The example creates a permit, calls the reference echo tool, retries with the same key, and checks the receipt offline.

## Run it locally

Use the gateway Makefile to start a local gateway. [QUICKSTART.md](QUICKSTART.md) has the full walk through.

```bash
git clone https://github.com/agentmiddleware/agent-middleware
cd agent-middleware/gateway
make quickstart
```

## Concepts

- **Permit:** A signed grant that limits an agent to specified tools, a budget, and an expiry.
- **Call:** A governed MCP tool invocation under a permit.
- **Request key:** The `Idempotency-Key` header that identifies a request for retry handling.
- **Receipt:** A signed record of a call's outcome and hashes of its request and response, with a debit link when applicable.
- **Credits:** Internal metering units, not money.

## Guarantees and limits

- a retry with the same key returns the original receipt instead of dispatching again
- a new key is a new request
- Reusing a key with a different request fails with a conflict before dispatch.
- A `delivery_uncertain` receipt means no trustworthy answer came back. The debit is kept, and the operator must reconcile with the downstream tool. The gateway does not resend automatically.
- The gateway limits dispatch and debit per key, but remote effects depend on the tool honoring the forwarded key. A receipt signs request and response hashes, not raw tool content, and cannot prove a remote effect happened. This is not exactly once.

Read [Security Limitations](gateway/SECURITY_LIMITATIONS.md) before relying on the gateway for a consequential action.

## Repo map

This repository holds the reference gateway, protocol spec, SDKs,
integrations, and design-partner pilot loop (start with
[QUICKSTART.md](QUICKSTART.md)). The separate `agent-middleware-api`
repository is outside this checkout; partner evaluation guidance is here.

| Path | What it contains |
| --- | --- |
| [gateway/](gateway/) | Gateway implementation and local Makefile |
| [sdk/](sdk/) | Python and TypeScript client and verification code |
| [verifier/](verifier/) | Offline verification guide and vector checker |
| [spec/](spec/) | Draft permit, receipt, and key formats |
| [integrations/](integrations/) | Framework wrappers and adapters |
| [examples/](examples/) | Runnable examples and MCP client config |
| [amw-demo](https://github.com/agentmiddleware/amw-demo) | Hosted demo client and receipt verifier |
| [amw-paypal-agent-demo](https://github.com/agentmiddleware/amw-paypal-agent-demo) | PayPal agent demo |

Apache 2.0 (gateway is Fair Source, FSL-1.1)
