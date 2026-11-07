# Permit format v0.1 (draft)

A permit is the operator's signed grant of authority to one agent identity. An
agent cannot mint its own permit; the gateway signs it at issuance.

## Signed payload

Required members:

| Member | Type | Meaning |
|---|---|---|
| `permit_id` | string | Unique permit id |
| `issuer_wallet_id` | string | The operator (sponsor) account that granted the permit |
| `subject_wallet_id` | string | The agent account the permit is for |
| `subject_key_id` | string or null | Agent API key this permit is bound to. JSON `null` when it is not bound to one key, and that null is part of the signed bytes |
| `scopes` | string[] | Granted scopes, for example `tool:<tool>:invoke`, `billing:charge` |
| `allowed_tools` | string[] | Tools this permit may invoke |
| `max_credits` | decimal string | Budget ceiling for the permit's lifetime |
| `issued_at`, `expires_at` | ISO-8601 UTC string | Validity window |
| `nonce` | string | Issuance nonce |
| `status` | string | Always `"active"` in the signed bytes; revocation is enforced at validation time, not by changing the signature |
| `alg` | string | `"Ed25519"` |
| `kid` | string | Signing key id |
| `payload_hash` | hex string | See conventions in [`README.md`](README.md) |

Optional members are **additive**: each appears only when set, so permits
signed before a member existed keep verifying.

| Member | Meaning |
|---|---|
| `requires_human_approval` | `true` when each call needs a human approval first |
| `max_calls_per_tool` | Map of tool to maximum accepted calls |
| `aggregate_value_cap` | Decimal string cap on a declared value field |
| `forbidden_fields` | Argument names the agent may not send |
| `recipient_domain` | Restricts a recipient argument to one domain |
| `allow_identical_repeats`, `repeat_window_seconds` | Repeat-call policy |
| `action_*` | Binding of the permit to one typed action contract (all six members present together, or none) |

Mutable server state (credits spent, per-tool counters) is deliberately **not**
signed; it is enforced by the gateway's atomic budget update.

## Validation (gateway side)

A call is accepted only if the permit signature verifies, the permit is not
revoked or expired, the tool is in `allowed_tools`, the calling key matches
`subject_key_id` when bound, and the call fits the remaining budget and per-tool
caps. All of this is evaluated **before** any debit or dispatch; a denial
produces a `denied` receipt and no debit.

Portable permit bundles verify offline beside the receipt verifiers:
`b2a_sdk.receipt_verifier.verify_permit_bundle` (Python) and
`verifyPermitBundle` (TypeScript), checked against
[`test-vectors/permits.json`](test-vectors/permits.json). The Python SDK can
also rebuild and check a cached permit's signing payload locally:
`b2a_sdk.edge_client` (`permit_signing_payload`, `verify_permit`).
Offline signature verification does not see revocation: a revoked permit
still carries a valid signature, so an exported permit must be rechecked
against live gateway status (for example `POST /v1/permits/verify`) before it
is treated as authority to act.

## Example (signed bytes from a test fixture)

```json
{"alg":"Ed25519","allowed_tools":["golden-path-echo"],"expires_at":"2026-09-16T13:00:00+00:00","issued_at":"2026-09-16T12:00:00+00:00","issuer_wallet_id":"agt-golden-issuer","kid":"sk_golden_0001","max_credits":"50","nonce":"nonce_golden_0001","payload_hash":"dbc43d163c80c2472a87cba4ae844e3a811e60e9ce068bedbebbcb950bf1eb90","permit_id":"pmt_golden_0001","scopes":["tool:golden-path-echo:invoke","billing:charge"],"status":"active","subject_key_id":"key_golden_0001","subject_wallet_id":"agt-golden-subject"}
```
