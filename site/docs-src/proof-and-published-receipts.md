---
title: Proof and published receipts
section: Trust
order: 3
description: Verify the published sample Receipt offline and understand what it proves.
---

# Proof and published receipts

Verify the historical sample from the [published proof page](https://www.agentmiddleware.dev/proof/). It is a self issued `partner.echo` Receipt. It is not customer evidence or a test of the deployment running today.

## Verify the sample

Download the Receipt, matching public key snapshot, and standalone verifier. Then verify the saved files:

```bash
curl -fsSLO https://www.agentmiddleware.dev/proof/receipt.json
curl -fsSLO https://www.agentmiddleware.dev/proof/trust-keys.json
curl -fsSLO https://www.agentmiddleware.dev/proof/verify_receipt.py
python3 -m pip install cryptography
python3 verify_receipt.py \
  --bundle receipt.json \
  --keys trust-keys.json \
  --expect-issuer https://api.thisisatest.tech
```

The verifier makes no network request. Exit 0 means the Receipt matches the supplied key snapshot. Exit 1 means verification rejected it; exit 2 means the verifier could not determine a result. The command does not independently authenticate who supplied the snapshot. Pin the key through a trusted channel before relying on issuer identity.

The signature covers hashes of the request and tool response, not the response body or the correctness of the tool output. It does not prove that an upstream effect occurred. Read [Receipts](/docs/receipts/) and [Security limitations](/docs/security-limitations/).
