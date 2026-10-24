---
title: Receipt spec v0.1
section: Reference
order: 5
description: Read the draft portable Receipt format and verification boundary.
---

# Receipt spec v0.1

The [Receipt format v0.1](https://github.com/agentmiddleware/agent-middleware/blob/main/spec/receipt-v0.1.md) is a draft interoperability spec. Its version is the document version. The current gateway's portable response has a separate `schema_version` field set to `1.0`.

Install the [Python SDK](/docs/python-sdk/) with its `verify` extra. Export your Receipt with a wallet key, then fetch the public key set:

```bash
API_URL='YOUR_API_URL'
curl -sS -H 'X-API-Key: YOUR_DEMO_KEY' \
  "$API_URL/v1/receipts/YOUR_RECEIPT_ID/portable" > receipt.json
curl -sS "$API_URL/.well-known/trust-keys.json" > trust-keys.json
b2a-verify-receipt --bundle receipt.json --keys trust-keys.json
```

The bundle carries `signing_input`, `signature`, and `kid`. For a plain Ed25519 signature, verify over the exact UTF-8 bytes of `signing_input`; do not serialize the parsed payload again. A tagged `Ed25519-sha256-envelope` signature instead signs a canonical hash envelope. The Python verifier above handles both forms. Read claims from the signed input. The signed payload names the Permit, wallet, tool, outcome, cost, request and response hashes, and any available gateway linkage. A key set can include active, retired, and disabled keys; disabled keys cannot verify a Receipt.

Verification establishes the issuer's signed claim, not whether the upstream effect occurred or whether the ledger row matches the signed identifier. See the [key set format](https://github.com/agentmiddleware/agent-middleware/blob/main/spec/key-set-v0.1.md), [test vectors](https://github.com/agentmiddleware/agent-middleware/tree/main/spec/test-vectors), and [Limits](/docs/limits/).
