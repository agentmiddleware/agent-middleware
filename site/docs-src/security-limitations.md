---
title: Security limitations
section: Trust
order: 2
description: Check the supported pilot boundary and security limits before using a consequential tool.
---

# Security limitations

Check the supported boundary before putting a consequential tool behind AMW. The remote pilot supports one operator configured HTTPS Streamable HTTP MCP origin and one exact tool in a vendor managed, single tenant deployment. Use synthetic or redacted, low sensitivity data. PHI, PCI data, regulated production records, and sensitive tool arguments are out of scope.

## Understand the receipt boundary

A timeout after the dispatch checkpoint can leave a charged `delivery_uncertain` Receipt. The Receipt does not prove whether the remote tool executed. The upstream tool must honor the forwarded Request key to prevent duplicate downstream effects. For a finalized Call, a retry with the same key returns the original receipt instead of dispatching again; a new key is a new request.

Offline verification checks the signature against a trusted key set. The issuing origin serves its public key document without an API key:

```bash
curl -fsS https://api.thisisatest.tech/.well-known/trust-keys.json
```

Fetching that document does not independently authenticate who supplied the keys. AMW has no external transparency log or implemented out of band key pinning. An absent Receipt does not prove that no action occurred. The portable bundle does not independently verify the named ledger record.

Wallet checks provide application layer isolation. PostgreSQL row level security is opt in, does not cover every table, and does not support a public multi tenant security claim. AMW makes no public uptime SLA, compliance scope, tenant isolation guarantee, or immutable ledger claim.

Read [Limits](/docs/limits/), the full [Security limitations](https://github.com/agentmiddleware/agent-middleware/blob/main/gateway/SECURITY_LIMITATIONS.md), and the [Trust model](https://github.com/agentmiddleware/agent-middleware/blob/main/gateway/TRUST_MODEL.md).
