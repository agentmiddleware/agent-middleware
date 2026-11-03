# Schema 056: receipt permit/approval digests and chain

Migration `056_receipt_digests_chain` revises `055_wallet_product_activation`.
It adds five nullable columns on `receipts`:

- `permit_digest` (String 64) — SHA-256 hex of the signed permit's signing_input
- `approval_digest` (String 64) — SHA-256 hex of a signed approval when present
- `counterparty` (String 255) — recipient domain / counterparty bound into the receipt
- `sequence` (Integer) — per-permit receipt sequence starting at 0
- `prev_receipt_digest` (String 64) — digest of the previous receipt in the chain;
  omitted / NULL at sequence 0

Existing rows stay NULL. Pre-056 signatures do not cover these fields, so they
keep verifying. New receipts that set any of these values sign them when set
(same additive pattern as `approval_id` / `enforcement_mode`).

## Compatibility release

Production boot requires the database revision to match the packaged head, so
a 055-packaged image cannot restart after 056 is applied. Qualify a
056-capable image against a disposable PostgreSQL upgrade from 055 first.
These release checks have not been run against staging or production.

## Rollback boundary

Downgrade drops the five columns. It refuses while any receipt has a non-NULL
`permit_digest`, `approval_digest`, `counterparty`, `sequence`, or
`prev_receipt_digest`, because those values are part of post-056 signed
receipts that 055 cannot store. Clear or archive such rows before downgrading,
or restore from a pre-056 backup. After a successful downgrade, only
pre-056-shaped receipts remain.
