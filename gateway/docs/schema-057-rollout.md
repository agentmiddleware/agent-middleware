# Schema 057: unique opted-in receipt (permit_id, sequence)

Migration `057_receipt_permit_seq_unique` revises `056_receipt_digests_chain`.
It installs the partial unique index `uq_receipts_permit_sequence` on
`(permit_id, sequence) WHERE sequence IS NOT NULL`.

Multiple NULL sequences (pre-056 / non-chain receipts) remain allowed. The
application SELECT in `_assert_opted_in_chain` stays as a fast path; this index
is the concurrent-write safety net so two writers cannot both commit the same
opted-in `(permit_id, sequence)`.

Upgrade fails closed when duplicate non-NULL `(permit_id, sequence)` groups
already exist: operators must adjudicate before the invariant can install
(same posture as migration 027's uniqueness preflight).

## Compatibility release

Production boot requires the database revision to match the packaged head, so
a 056-packaged image cannot restart after 057 is applied. Qualify a
057-capable image against a disposable PostgreSQL upgrade from 056 first.
These release checks have not been run against staging or production.

## Rollback boundary

Downgrade drops `uq_receipts_permit_sequence` only. No columns are removed and
no receipt bytes are destroyed. After downgrade, concurrent writers can again
race past the SELECT fast path and insert duplicate opted-in sequences until
057 is re-applied. Re-upgrade reinstalls the same partial unique index (and
again fails closed if duplicates appeared while the index was absent).
