# Receipt resolution format v0.1

A resolution is a new signed statement about a receipt whose original outcome
was `delivery_uncertain`. It never changes the original receipt or its signing
bytes. The resolution identifies that receipt by the SHA-256 hash of its exact
portable bundle `signing_input` UTF-8 bytes.

## Signed record

The gateway signs this payload with the existing Ed25519 signing key service:

| Field | Rule |
|---|---|
| `version` | Literal `resolution-v0.1` |
| `resolution_id` | Unique resolution identifier |
| `receipt_id` | Identifier of the original receipt |
| `wallet_id` | Wallet whose resolution chain contains this record |
| `sequence` | One-based position in that wallet's chain |
| `prev_hash` | JSON `null` at sequence 1; otherwise the preceding record's `resolution_hash` |
| `receipt_hash` | Lowercase hex SHA-256 of the original receipt bundle's exact `signing_input` UTF-8 bytes |
| `outcome` | `confirmed_success` or `confirmed_not_delivered` |
| `created_at` | UTC timestamp formatted `YYYY-MM-DDTHH:MM:SS[.ffffff]+00:00` (never `Z`); fractional seconds appear when nonzero |
| `alg` | Literal `Ed25519` |
| `kid` | Identifier of the published signing key |
| `payload_hash` | The signing service's SHA-256 of the canonical payload including `alg` and `kid`, before adding `payload_hash` |

`signing_input` is the exact `awi-canonical-json/1` serialization of this
payload, including `payload_hash`: sorted keys, compact separators, and escaped
non-ASCII. The Ed25519 signature covers its UTF-8 bytes directly. Verifiers
must use those exact bytes; parsing and reserializing before signature or hash
checks can change the evidence.

`resolution_hash` is the lowercase hex SHA-256 of the exact resolution
`signing_input` UTF-8 bytes. It is the hash named by the next record's signed
`prev_hash`. It is not a hash of the response envelope or the signature.
The v0.1 verifier checks that `signing_input` itself is canonical and that
`payload_hash` matches the two-stage signing service calculation.

The two outcomes are statements by the issuing gateway about a provider
reconciliation. `confirmed_success` says the provider confirmed delivery;
`confirmed_not_delivered` says the provider confirmed no delivery. Neither
outcome, by itself, states that a debit or refund occurred. SEL-352 provides
the append operation but does not call it from SEL-351's provider-outcome
commit path.

## Portable response and public reads

Each record response contains `signing_input`, base64 `signature`,
`canonicalization: "awi-canonical-json/1"`, and `resolution_hash`. It also
mirrors the signed fields above at the top level for display and includes
`keys_url: "/.well-known/trust-keys.json"` and `issuer`, the issuing plane's
public origin (empty when `PUBLIC_URL` is unset). `issuer` is outside the
signature, as on the portable receipt bundle.
Every signed-field mirror must equal the corresponding field parsed from the
verified `signing_input`. The mirrors, `issuer`, and `resolution_hash` are
outside the signature; the signed bytes remain the source of truth.

- `GET /v1/receipts/{receipt_id}/resolution` returns one record by original
  receipt, or 404 when no resolution exists.
- `GET /v1/receipts/resolutions/wallet/{wallet_id}?limit=...&offset=...`
  returns a page ordered by `sequence` with `records`, `total`, `limit`,
  `offset`, `has_more`, and `next_offset`. These resolution reads require no
  credential.
- `GET /.well-known/trust-keys.json` publishes the verification keys. An
  offline verifier must receive this document through a channel it trusts;
  the record's unauthenticated `kid` mirror or key URL cannot establish the
  issuer on its own.

The original portable receipt bundle is still needed to check the
`receipt_hash` link. Its `signing_input` must first pass the receipt's own
signature check, then its exact UTF-8 bytes are hashed and compared with the
resolution's signed `receipt_hash`. The receipt's signed `receipt_id` and
`wallet_id` must also match the resolution.

## Offline verification

Python callers can parse a previously obtained key document with
`key_set_from_document`, then call `verify_resolution(record, receipt_bundle,
keys)`. `verify_resolution_chain(records, receipt_bundle, keys)` checks a
complete, ordered wallet sequence starting at 1. For the chain call, collect
**every** wallet page from offset 0, in order. The verifier rejects gaps,
reordering, a supplied fork, repeated receipts or resolution IDs, broken
`prev_hash` links, a wrong original receipt, and altered signed bytes.
There is no TypeScript resolution verifier yet.

The chain verifier checks every supplied record signature and hash. It checks
the original receipt hash for the supplied target receipt. Checking every
other record's receipt association requires its original portable bundle as
well.

**Withheld-fork limitation:** An offline verifier can detect conflicting
records only when they are both supplied. A server or intermediary can show a
single valid branch and withhold a competing fork or a later suffix. A valid
signature and contiguous sequence therefore do not prove that the supplied
chain is the only or latest chain. To assess completeness, compare its head
hash and length against an independently witnessed checkpoint.
