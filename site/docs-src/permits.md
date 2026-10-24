---
title: Permits
section: Concepts
order: 1
description: A Permit binds a wallet and API key to tools, scope, Credits, and expiry.
---

# Permits

A Permit is signed authority for a wallet scoped agent. It defines what a Call may do before the gateway considers a debit or tool invocation.

A Permit binds:

- An issuer wallet and a subject wallet.
- An optional subject API key.
- Allowed tools and scopes.
- A maximum amount of Credits.
- An expiry and revocation state.

For example, a Permit for `partner.echo` can carry `tool:partner.echo:invoke` and `billing:charge` scopes. The gateway checks the wallet, key, tool, scope, budget, expiry, revocation, and signature before charging.

`POST /v1/permits` creates a Permit and requires an `Idempotency-Key` header. Permits are reusable budget envelopes. A live single action Permit issuance path is frozen in the current repository.

Read [Calls and request keys](/docs/calls-and-request-keys/) to see how a Call presents a Permit. Read [Credits](/docs/credits/) for the budget rule.
