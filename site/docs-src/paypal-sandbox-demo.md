---
title: PayPal sandbox demo
section: Guides
order: 7
description: Run the public PayPal checkout example with mock services first.
---

# PayPal sandbox demo

Run the [public PayPal agent example](https://github.com/agentmiddleware/amw-paypal-agent-demo) with mock services first. It includes its own `mini_gateway/` reference implementation and vendored AMW client code. It does not connect to a production AMW gateway.

```bash
git clone https://github.com/agentmiddleware/amw-paypal-agent-demo
cd amw-paypal-agent-demo
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python demo.py --mock-paypal --mock-llm
```

The demo includes a checkout, a lost response followed by a retry, an over budget denial, and offline Receipt verification. Its offline path needs no PayPal or OpenAI key. To attempt the PayPal sandbox path with a scripted model, supply sandbox credentials and a sandbox test card, then run:

```bash
export PAYPAL_CLIENT_ID='YOUR_PAYPAL_SANDBOX_CLIENT_ID'
export PAYPAL_CLIENT_SECRET='YOUR_PAYPAL_SANDBOX_CLIENT_SECRET'
export PAYPAL_SANDBOX_CARD_NUMBER='YOUR_SANDBOX_TEST_CARD'
export PAYPAL_SANDBOX_CARD_EXPIRY='YYYY-MM'
.venv/bin/python demo.py --mock-llm
```

The example repository says its card path has not been run against PayPal's sandbox service. Treat the sandbox command as an integration example until that run is verified. For the AMW gateway and its supported MCP path, see [Run locally](/docs/run-locally/) and [Limits](/docs/limits/).
