---
title: Limits
section: Trust
order: 1
description: The supported pilot boundary and limits of gateway and Receipt evidence.
---

# Limits

AMW currently governs MCP tool invocation. The supported remote pilot uses one operator configured HTTPS Streamable HTTP MCP origin and one exact tool in a vendor managed, single tenant deployment. The repository contains other proof surfaces that are outside this supported boundary.

- A gateway dispatch record does not prove that the upstream tool executed. The upstream tool must honor the forwarded Request key for duplicate downstream effects to be prevented.
- A timeout after the dispatch checkpoint can produce a charged `delivery_uncertain` Receipt. It needs upstream or operator reconciliation.
- Offline Receipt verification confirms the issuer signature against a trusted key set. It does not prove an absent action, independently verify the ledger contents, or replace an external transparency log.
- Credits are internal metering units. AMW does not provide settlement, dispute handling, or compliance reporting.
- The supported pilot uses synthetic or redacted, low sensitivity data. PHI, PCI data, and regulated production records are out of scope.

Read the full [Security limitations](https://github.com/agentmiddleware/agent-middleware/blob/main/gateway/SECURITY_LIMITATIONS.md) and [Trust model](https://github.com/agentmiddleware/agent-middleware/blob/main/gateway/TRUST_MODEL.md) before evaluating a production use case.
