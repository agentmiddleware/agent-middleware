---
title: Receipts
section: Concepts
order: 3
description: A Receipt records a signed outcome and can be verified with a trusted key set.
---

# Receipts

A Receipt is a signed statement about a governed Call. A successful Call produces one. A denied or failed Call can also produce a Receipt after a valid Permit and executable tool are established. A request rejected before that point can end without a Receipt.

The Receipt names the tool, Permit, cost, and hashes of the request and response. On the configured upstream path, its signed identifiers also bind the ledger entry, Request key record, and dispatch attempt. The portable bundle is available from `GET /v1/receipts/{id}/portable`.

Offline verification checks the signature against a trusted key set. The public key document is at `/.well-known/trust-keys.json`. Verification establishes what the issuer signed and that the signed bytes have not changed. The portable bundle alone does not prove that the upstream tool acted or that the named ledger row matches the signed identifier.

The [published sample Receipt](https://www.agentmiddleware.dev/proof/) comes from a self issued `partner.echo` Call. It is historical sample evidence, not partner validation.

Read [Limits](/docs/limits/) for the boundaries of Receipt evidence.
