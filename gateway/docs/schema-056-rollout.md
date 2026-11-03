# Schema 056: receipt permit/approval digests and chain

Migration `056_receipt_digests_chain` revises `055_wallet_product_activation`.
It adds five nullable columns on `receipts`:

- `permit_digest` (String 64) - SHA-256 hex of the signed permit's signing_input
- `approval_digest` (String 64) - SHA-256 hex of a signed approval when present
- `counterparty` (String 255) - recipient domain / counterparty bound into the receipt
- `sequence` (Integer) - per-permit receipt sequence starting at 0 when a caller
  opts into the chain; left NULL until then
- `prev_receipt_digest` (String 64) - digest of the previous receipt in the chain;
  omitted / NULL at sequence 0 (and whenever sequence is unset)

Existing rows stay NULL. Pre-056 signatures do not cover these fields, so they
keep verifying. New receipts that set any of these values sign them when set
(same additive pattern as `approval_id` / `enforcement_mode`). `create_receipt`
computes `permit_digest` from the live permit when the caller omits it;
`sequence` / `prev_receipt_digest` are not invented.

## Compatibility release

Production boot requires the database revision to match the packaged head, so
a 055-packaged image cannot restart after 056 is applied. Qualify a
056-capable image against a disposable PostgreSQL upgrade from 055 first.
These release checks have not been run against staging or production.

## Rollback boundary

Downgrade drops the five columns. **Removing them destroys the stored copy of
any digest/chain values that post-056 receipts signed**; 055 cannot keep those
bytes, so offline verifiers that need the DB row will no longer see them.

Downgrade refuses while any receipt has a non-NULL `permit_digest`,
`approval_digest`, `counterparty`, `sequence`, or `prev_receipt_digest`. Clear
or archive such rows before downgrading, or restore from a pre-056 backup.
After a successful downgrade, only pre-056-shaped receipts remain.

## Restore / reapply

If you must roll back past 056:

1. Take a backup that still has the five columns (or keep the pre-downgrade
   snapshot).
2. Clear or archive every receipt that carries non-NULL digest/chain values,
   then run the 056 downgrade; or restore the backup instead of downgrading.
3. To move forward again, deploy a 056-capable image and upgrade to head.
   New receipts can re-populate digests; previously signed post-056 rows that
   were cleared are not reconstructed automatically -- restore them from the
   backup if verifiers still need those exact signed fields.
