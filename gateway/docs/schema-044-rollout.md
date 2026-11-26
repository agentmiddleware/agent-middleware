# Schema 044: receipt resolutions and rollback boundary

Migration `044_receipt_resolutions` revises `043_demo_key_fields`. It adds the
`receipt_resolutions` and `resolution_chain_heads` tables without changing
existing tables or receipts. SEL-351/PR #187 may land another migration first;
if so, renumber this migration and note, update its parent revision, and review
the resulting migration order before release.

## Compatibility release before migration

This is a release plan, not evidence of a deployment. Before applying 044,
confirm the database is at `043_demo_key_fields` and qualify an immutable
044-capable compatibility image as the recovery target. Rehearse the upgrade
and recovery on disposable PostgreSQL, including existing receipt reads and
resolution verification. Keep new resolution writes stopped until the migration
has completed, schema parity is verified, and every serving worker is
044-capable. Record the exact image, database revision, and verification
results before enabling writes. An image packaged with head 043 cannot restart
against a database upgraded to 044.

## Rollback after activation

Stop new resolution writes and recover with the qualified 044-capable image,
leaving the database at 044 and retaining signed resolution evidence. The
`downgrade()` drops both new tables, including their chain heads; that would
lose resolution records. It therefore refuses to run while any row remains in
`receipt_resolutions` (`resolution_evidence_retained`). An empty-table
downgrade is only a schema reversal, not a recovery plan for issued
resolutions. Do not delete resolution evidence to make downgrade pass.
