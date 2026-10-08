# Agent Middleware gateway

License: **Fair Source, FSL-1.1-ALv2** ([`LICENSE.md`](LICENSE.md)). You may
run, modify, and self-host it for your own agents; you may not offer it as a
competing commercial product. Each release becomes Apache-2.0 two years after it
is published. See [`../LICENSING.md`](../LICENSING.md).

This directory is the reference gateway: permits, idempotency, the dispatch
fence, metering against an internal credit ledger, Ed25519 receipts, the
hash-chained audit log, policy, and the MCP endpoints. Python 3.11+, FastAPI,
SQLite or PostgreSQL, Alembic.

## Run it

```bash
make demo-ambiguous-retry   # the lost-response fault, with and without the gateway
make prove-trust-plane      # asserts the charge-once, replay, audit, denial, and tamper invariants
make failure-lab            # the same fault against a native-idempotency baseline
make quickstart             # a local strict-trust server on 127.0.0.1:8000
make test                   # the test suite (excluding proof-surface tests)
```

Walkthrough: [`docs/quickstart.md`](docs/quickstart.md) and the shorter
[`../QUICKSTART.md`](../QUICKSTART.md).

## How it works

```text
                               agent
                                 |  API key + permit + idempotency key
                                 v
        +-------------------------------------------------+
        |   permit        tools, scope, budget, expiry     |
        |   policy        allow / deny before any debit    |
        |   idempotency   one accepted key, one record     |
        |   dispatch fence   claimed before the send       |
        +------------------------+------------------------+
                                 |  at most one dispatch
                                 v
                         upstream MCP tool
                                 |  result, or silence
                                 v
        +-------------------------------------------------+
        |   receipt, evidence, audit chain                |
        |   Ed25519-signed, verifiable offline            |
        +-------------------------------------------------+
```

The dispatch fence is a gateway record, not proof that the downstream effect
occurred. When a send is claimed and no trustworthy result comes back, the call
is receipted `delivery_uncertain` and never silently re-sent. See
[`docs/failure-semantics.md`](docs/failure-semantics.md).

## Guarantees and limits

- At most one gateway dispatch and at most one debit per accepted idempotency
  key; a same-key retry returns the original receipt; same key with a different
  payload fails closed.
- Budget, scope, expiry, and revocation are checked before money moves.
- **Not exactly-once.** A remote side effect is deduplicated only if the remote
  tool honors the forwarded idempotency key, and an agent that restarts with a
  new key can still cause a second effect.
- Internal credit ledger only: not settlement, not a compliance record, not IAM.
- MCP is the only governed adapter; one operator-configured upstream tool.

## Money, pricing, and top-up

- Credits are internal metering units, not money. The default rate is 1000
  credits per 1.00 USD, and it is operator-configurable (`EXCHANGE_RATE`).
  Each tool costs a fixed number of credits per call from one pricing table
  (for example 2 credits per metered call on the demo tool, 0.1 credits base
  platform fee); `GET /v1/billing/pricing` returns the table in effect.
- The only fiat path is Stripe top-up, USD only, and only where the operator
  configured Stripe keys. Sponsor wallets may require KYC before a top-up is
  prepared. There is no second rail, no subscription or invoice flow, and no
  settlement or payouts: x402 and ACP surfaces only prepare and receipt an
  authorization against a permit budget, and the payer wallet still signs and
  submits anything on chain.
- Always send `Idempotency-Key` on money calls (wallet creation, charges,
  transfers). A retry with the same key and the same payload returns the
  original result; the same key with different arguments is refused with 409.
  A retry without the header is a new operation and can charge twice.

Read [`SECURITY_LIMITATIONS.md`](SECURITY_LIMITATIONS.md),
[`TRUST_MODEL.md`](TRUST_MODEL.md), and [`docs/threat-model.md`](docs/threat-model.md)
before evaluating it for real use. Reviewers: [`docs/security-review-kit.md`](docs/security-review-kit.md).

## Status

Pre-release (source version 1.3.0, untagged in this repository). No production
deployment of this code is claimed here. Production-like configuration requires
`TRUST_MODE_ENABLED=true`, `ENABLE_PROOF_SURFACES=false`, and PostgreSQL; the
application refuses unsafe combinations at boot.

Some routers in `app/` (IoT, media, content, oracle, and similar) are frozen
**proof surfaces**: unmounted unless `ENABLE_PROOF_SURFACES=true`, refused in
production, and not part of the product. See
[`docs/PROOF_SURFACES.md`](docs/PROOF_SURFACES.md). They are scheduled for
removal from this repository.

## Layout

| Path | Role |
|---|---|
| `app/trust/` | Protocol-neutral trust facade (permits, receipts, signing, idempotency, metering, audit) |
| `app/routers/mcp*.py` | Governed MCP orchestration and endpoints |
| `app/services/` | Permits, receipts, keys, ledger, audit, idempotency, upstream MCP |
| `app/db/`, `migrations/` | Schema and Alembic history |
| `tests/`, `scripts/`, `failure_lab/` | Tests, adversarial proofs, the failure lab |
| `docs/` | Gateway documentation ([index](docs/README.md)) |
