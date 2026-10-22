# Agent Middleware permit and receipt format (v0.1, draft)

License: Apache-2.0 (see [`../LICENSE`](../LICENSE)).

This directory describes the two signed objects an Agent Middleware gateway
produces, so that someone else can verify them, or implement a compatible
gateway, without reading the gateway's code.

| Document | What it covers |
|---|---|
| [`permit-v0.1.md`](permit-v0.1.md) | The scoped, signed authority an operator issues to an agent |
| [`receipt-v0.1.md`](receipt-v0.1.md) | The signed record of one governed tool call, and replay rules |
| [`key-set-v0.1.md`](key-set-v0.1.md) | How verification keys are published, rotated, and disabled |
| [`test-vectors/`](test-vectors/) | Signed receipt bundles plus tampered variants, with expected verifier results |

Status: **draft**. The format is what the reference gateway in
[`../gateway/`](../gateway/) emits today. Field additions are expected;
breaking changes will bump the version. Propose changes with a spec RFC issue
(see [`../CONTRIBUTING.md`](../CONTRIBUTING.md)).

## Conventions

- **Signature:** Ed25519 over the UTF-8 bytes of `signing_input`, exactly as
  transmitted. Verifiers must not re-serialize before verifying.
- **Canonical JSON (`awi-canonical-json/1`):** object keys sorted at every
  level, no insignificant whitespace (`,` and `:` separators), non-ASCII
  escaped as lowercase `\uXXXX`.
- **Amounts and timestamps:** credit amounts and timestamps are strings before
  they are signed. Amount strings are fixed point, with no exponent and with
  trailing zeros removed (`2.50` is signed as `2.5`, `10.00` as `10`).
  Timestamps are UTC with a numeric offset, for example
  `2026-09-16T12:00:00+00:00`. A verifier does not reformat these values. It
  checks the bytes it was given.
- **`payload_hash`:** lowercase hex SHA-256 of the canonical JSON of the signed
  payload *without* the `payload_hash` member. It is itself part of the signed
  bytes, so the signature covers it: editing it without re-signing is
  `invalid`. Verifiers treat it as advisory and never recompute it, because
  signed payloads may carry JSON numbers (floats, exponents, integers above
  2^53) whose rendering differs between languages, and re-serializing the
  parsed payload would reject genuine receipts.
- **`kid`:** the signing key id, inside the signed payload. Key selection uses
  the signed `kid`, never the unauthenticated envelope.

## What a valid signature does and does not prove

A verified receipt proves that the holder of the named key signed that record.
It does **not** prove that a downstream side effect happened, or happened only
once. The gateway guarantees at most one gateway dispatch and at most one debit
per accepted idempotency key; a remote side effect is deduplicated only if the
remote tool also honors the forwarded idempotency key. This is not
exactly-once. See [`../gateway/docs/failure-semantics.md`](../gateway/docs/failure-semantics.md)
and [`../gateway/SECURITY_LIMITATIONS.md`](../gateway/SECURITY_LIMITATIONS.md).
