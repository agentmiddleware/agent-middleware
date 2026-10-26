# Schema 045: approval-gate rows and rollback boundary

Migration `045_approval_gate_approvals` revises `044_receipt_resolutions`. It adds the
`approval_gate_approvals` table for the AMW V2 human approval gate (SEP-2322) without
changing existing tables. The gate is not mounted yet, so no serving path writes this
table; the rows become load-bearing only when the `mcp_standard.py` hook lands.

## Compatibility release before migration

This is a release plan, not evidence of a deployment. Before applying 045, confirm the
database is at `044_receipt_resolutions` and qualify an immutable 045-capable
compatibility image as the recovery target. Rehearse the upgrade and recovery on
disposable PostgreSQL, including existing approval-adjacent reads. Keep new approval
writes stopped until the migration has completed, schema parity is verified, and every
serving worker is 045-capable. Record the exact image, database revision, and
verification results before enabling writes. An image packaged with head 044 cannot
restart against a database upgraded to 045.

## Rollback after activation

Stop new approval writes and recover with the qualified 045-capable image, leaving the
database at 045. Pending rows are operational pause state, not signed evidence: denial,
timeout, tamper, and replay receipts are emitted through the gate's receipt sink, so
abandoning paused rows on recovery only forces those clients to start a new paused call.
The `downgrade()` drops the new table and its indexes; run it only after confirming no
serving worker still writes approvals, and never as a substitute for the qualified
recovery image above.
