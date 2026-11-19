# Schema 058: evidence for operator receipt resolutions

Migration `058_resolution_evidence` revises `057_receipt_permit_seq_unique`.
It adds nullable `evidence` (canonical JSON text) and `evidence_hash` (SHA-256)
columns to `receipt_resolutions`. Existing rows remain unchanged and retain
resolution-v0.1 signatures. Operator writes use resolution-v0.2 and sign the
evidence hash. Application writes cap canonical evidence at 16 KiB.

## Compatibility release

Boot requires the packaged migration head. Qualify the 058 image and upgrade
from 057 against disposable PostgreSQL before scheduling the production
maintenance window. Apply migration 058 before starting the 058 image. No
staging or production migration is part of this change.

## Rollback boundary

Downgrading drops both evidence columns only while all rows are v0.1. The
migration refuses downgrade if evidence is present, preserving signed v0.2
evidence.
Export resolutions, original portable receipts, and trusted public keys first.
An older application cannot validate v0.2 signatures. Restore a database backup
from before the first v0.2 append if an older image is required. Downgrade and
re-upgrade are safe only while all rows are v0.1 (or the table is empty).

Refund ledger entries survive downgrade. A schema rollback does not reverse
credits or recover the signed refund intent. Never delete a resolution to
retry a refund; repeat the original operator request on the 058 application.
