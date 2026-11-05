# Claims

Use this page as the boundary for anything said about AMW in a submission,
pitch, or judge conversation. If a statement is not on the "we claim" list,
do not make it.

## What we claim

Each claim points at where it is stated or implemented in this repository.

- **Permits.** A tool call through the governed path needs a signed permit that
  limits the agent to specified tools, a budget in credits, and an expiry.
  ([README](../README.md), `gateway/app/routers/permits.py`)
- **Idempotent retry.** A retry with the same request key returns the original
  receipt instead of dispatching again. A new key is a new request. Reusing a
  key with a different request fails with a conflict before dispatch.
  ([README](../README.md))
- **Per key dispatch and debit limit.** The gateway dispatches and debits at
  most once per request key. This is a property of the gateway, not of the
  remote tool. ([SECURITY_LIMITATIONS.md](../gateway/SECURITY_LIMITATIONS.md))
- **Honest uncertainty.** When no trustworthy answer comes back after
  dispatch, the gateway signs a `delivery_uncertain` receipt, keeps the debit,
  and does not resend automatically. The operator reconciles with the
  downstream tool. ([README](../README.md))
- **Signed receipts, verifiable offline.** A receipt records the call's outcome
  and hashes of its request and response, with a debit link when applicable.
  It can be checked offline with `GET /v1/receipts/{id}/portable`, the public
  `/.well-known/trust-keys.json`, and the SDK verifier, which does not import
  the gateway. ([QUICKSTART.md](../QUICKSTART.md), [verifier/](../verifier/))
- **Works with MCP clients.** Governed calls go through `POST /mcp`, usable by
  standard Streamable HTTP MCP clients with an `X-API-Key` header.
  (`gateway/app/routers/mcp_standard.py`)
- **Licensing.** `gateway/` is Fair Source under FSL-1.1-ALv2 (source
  available, not OSI open source). `spec/`, `verifier/`, the SDKs,
  `integrations/`, `examples/`, and root files are Apache 2.0.
  ([LICENSING.md](../LICENSING.md)) Do not call the whole project open source.

## What we explicitly do not claim

- **Not exactly once.** We do not claim exactly once delivery or exactly once
  execution. Say "a retry with the same key returns the same receipt". Remote
  effects depend on the tool honoring the forwarded key.
- **Receipts do not prove remote effects.** Receipts sign hashes of request and
  response, not raw tool content. A receipt cannot prove a remote effect
  happened, and the absence of a receipt is not evidence that no action
  occurred.
- **No independent trust root.** Verification keys come from the same origin
  that issues receipts. There is no external transparency log or out of band
  key pinning.
- **No settlement.** AMW does not settle payments, move money, or run a
  dispute workflow. Credits are internal metering units, not money. The
  existence of design notes in the repo is not a settlement claim.
- **No compliance.** No compliance scope, compliance reporting, or regulatory
  status is claimed.
- **No certification.** We claim no certification, accreditation, or third
  party audit result.
- **Not a standard.** The formats in [spec/](../spec/) are drafts. AMW is not a
  standard and is not endorsed by any standards body.
- **No customers or production deployments.** AMW is pre revenue and pre
  release.
  AMW makes no customer or deployment claims. It is pre-revenue Preview software.
- **No metrics or benchmarks.** We publish no performance, adoption, cost
  savings, or reliability numbers. Do not cite any.
- **No uptime promise.** No public uptime SLA, RTO, RPO, tenant isolation
  guarantee, or immutable ledger claim.
- **MCP is the default governed surface.** Governed HTTP execution routes
  exist in [gateway/app/routers/awi.py](../gateway/app/routers/awi.py) (for
  example `POST /v1/awi/execute`, which requires `X-Permit-Id` and
  `Idempotency-Key`), but they are proof surfaces. They are mounted only when
  `ENABLE_PROOF_SURFACES` is true, which defaults to false
  ([gateway/app/core/config.py](../gateway/app/core/config.py)) and must stay
  false in production like environments. Do not treat them as part of the
  default product.
- **Not on PyPI.** The `agent-middleware` 0.5.0 package is not published yet:
  TODO(T-01). Install from source.
- **Demo key permit fields not shipped.** `permit_id`, a permit level
  `expires_at`, and `demo_tool` in the demo key response: TODO(T-00).
- **Stripe test mode demo not built.** No results exist: TODO(T-03). See
  [DUPLICATE-COST.md](DUPLICATE-COST.md).

## Phrasing guide

| Avoid | Say instead |
| --- | --- |
| exactly once | a retry with the same key returns the same receipt, and the gateway does not dispatch again |
| guarantees no double charge | the gateway debits at most once per request key |
| proves the refund happened | the receipt signs hashes of the request and response the gateway saw |
| settles payments | meters tool calls in credits, which are not money |
| compliant, certified, standard | (do not say) |
| production ready, used by customers | pre release, looking for design partners |

The full list of known limits is in
[gateway/SECURITY_LIMITATIONS.md](../gateway/SECURITY_LIMITATIONS.md).
