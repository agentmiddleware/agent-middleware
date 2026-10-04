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
  escaped as `\uXXXX`. Credit amounts and timestamps are strings by the time
  they are signed, so no number-formatting rules are needed.
- **`payload_hash`:** lowercase hex SHA-256 of the canonical JSON of the signed
  payload *without* the `payload_hash` member. It is itself part of the signed
  bytes.
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
