# Schema 043: demo key fields and rollback boundary

Migration `043_demo_key_fields` revises `042_permit_action_binding`. It adds
nullable tenant labels to wallets and API keys, nullable API key tool and
attribution fields, and `first_used_at`. It backfills `first_used_at` from
existing `last_used_at` values where missing, and creates a demo funnel view.
The upgrade checks for existing columns, indexes, and the view so it can be
reapplied without dropping data.

## Compatibility release before migration

This is a release plan, not evidence of a deployment. Before a separately
authorized rollout, confirm that the database is at revision 042 and record
the serving image, database backup, and a tested 043-capable recovery image.
Stage the 043-capable application and its migration together. The new code
uses these columns during demo issuance and authentication, so do not enable
`ENABLE_DEMO_TENANT` until migration 043 is applied and every serving worker
is 043-capable. Verify schema parity, authentication, key issuance limits,
tenant isolation, and the kill switch on a disposable staging database first.
Production and staging state have not been inspected for this change.

## Rollback after activation

Turn `ENABLE_DEMO_TENANT` off to stop issuance and reject existing demo keys.
Keep a 043-capable image available for recovery and authorized reads. The
migration's downgrade deliberately leaves tenant, attribution, and first-use
evidence in place; it does not restore a database to a 042 schema or authorize
starting a 042 image. This repository enforces packaged-head schema parity,
so recovery after applying 043 requires a verified 043-capable image. Preserve
demo wallets, keys, ledger entries, permits, and receipts for audit and
reconciliation. Any return to an older packaged head needs a separate review.
