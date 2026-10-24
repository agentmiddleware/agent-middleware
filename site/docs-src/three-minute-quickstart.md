---
title: 3 minute quickstart
section: Start
order: 2
description: Get a free hosted sandbox key, run the demo Call and retry, and verify its Receipt.
---

# 3 minute quickstart

Use the hosted AMW sandbox to make one `partner.echo` Call, retry it with the same Request key, and check its Receipt. You need Python 3 and `git`.

## Get a demo key

Get a free demo key with the button on [the demo key page](https://www.agentmiddleware.dev/demo-key/) or run:

```bash
curl -s -X POST https://api.thisisatest.tech/v1/demo/keys
```

Save `api_key` and `wallet_id` from the response. Never paste the key into an issue or chat.

## Run the example

Replace `YOUR_DEMO_KEY` and `YOUR_WALLET_ID` with the values you saved:

```bash
git clone https://github.com/agentmiddleware/amw-demo.git
cd amw-demo
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
export AMW_API_KEY='YOUR_DEMO_KEY'
export AMW_WALLET_ID='YOUR_WALLET_ID'
python examples/permit_echo_retry.py
```

## What success looks like

The retry shows the same Receipt id because a retry with the same key returns the original receipt instead of dispatching again. The example then verifies the Receipt offline. Read [Receipts](/docs/receipts/) for what that verification establishes and see the separate [published sample proof](https://www.agentmiddleware.dev/proof/).

For the gateway checkout and local server, see [Run locally](/docs/run-locally/).
