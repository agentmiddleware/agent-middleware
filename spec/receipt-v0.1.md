# Receipt format v0.1 (draft)

A receipt is the gateway's signed record of one governed tool call under a
permit and an idempotency key.

## Portable bundle

```json
{
  "receipt_id": "rcpt_...",
  "kid": "sk_...",
  "alg": "Ed25519",
  "canonicalization": "awi-canonical-json/1",
  "signing_input": "{...canonical JSON of the signed payload...}",
  "signature": "<base64 Ed25519 signature over signing_input>"
}
```

Everything outside `signing_input` and `signature` is an unauthenticated
envelope. A verifier reads every claim from the signed payload and treats any
envelope value that disagrees with it as a `mismatch`.

## Signed payload

| Member | Meaning |
|---|---|
| `receipt_id` | Unique receipt id |
| `permit_id` | The permit the call ran under |
| `wallet_id`, `key_id` | The agent account and API key that made the call |
| `tool` | Tool name |
| `idempotency_record_id` | The gateway's record for the idempotency key (the raw key is not signed) |
| `dispatch_attempt_id` | The one-shot dispatch claim, when a dispatch happened |
| `request_hash`, `response_hash` | SHA-256 of the request and response bodies |
| `outcome` | See below |
| `credits_authorized`, `credits_charged` | Decimal strings |
| `ledger_entry_id` | The debit, when one exists |
| `audit_event_id` | Link into the hash-chained audit log |
| `created_at` | ISO-8601 UTC |
| `alg`, `kid`, `payload_hash` | See [`README.md`](README.md) |

Additional members (approval ids, action bindings, constraint denials) are
additive, as for permits.

## Outcomes

| `outcome` | Meaning |
|---|---|
| `success` | The tool returned a valid result; charged |
| `denied` | Refused before any money moved (permit, policy, approval); not charged |
| `insufficient_funds` | The charge itself was refused; not charged |
| `failed_refunded` | Confirmed failure; the debit was refunded |
| `failed_unrefunded` | A refund was owed and failed; an operator work item exists |
| `delivery_uncertain` | The dispatch was sent but no trustworthy result came back. The debit is kept, the gateway **never** re-sends on its own, and the operator must reconcile with the downstream system |
| `response_rejected` | The downstream responded with something unusable |

Full trigger table: [`../gateway/docs/failure-semantics.md`](../gateway/docs/failure-semantics.md).

## Replay rules

- Same idempotency key, same request: the gateway returns the **original**
  receipt. No second dispatch, no second debit.
- Same idempotency key, different request: fails closed with an idempotency
  conflict. No new receipt, no dispatch, no debit.
- New idempotency key: a new call. An agent that restarts and generates a new
  key can still cause a second effect; the receipt format cannot prevent that.

## Verifier statuses

| Status | Meaning | Accept? |
|---|---|---|
| `verified` | Signature holds over the signed bytes and the envelope agrees | yes |
| `invalid` | Signature does not verify | reject (tampered) |
| `mismatch` | Signature verified, but envelope or caller expectation disagrees | reject |
| `unknown_key` | Verifier does not hold the signed `kid` | cannot judge; refresh keys |
| `malformed` | Bundle cannot be parsed | cannot judge |
| `unsupported` | Signed `alg` or `canonicalization` not implemented, and the signature over `signing_input` checked out | cannot judge |

Reference verifiers: Python (`sdk/python`, CLI `b2a-verify-receipt`) and
TypeScript (`sdk/typescript`). Both pass [`test-vectors/`](test-vectors/).
