# Schema 047: dispatch reconcile lease (ported)

Migration `047_dispatch_reconcile_lease` revises `046_api_key_usage_events`. It is
ported from the archived agent-middleware-api chain with the same revision id,
so databases that service already migrated to 047-050 (staging and production,
both at `050_demo_key_first_used`) are at a revision this repository knows.

It adds nullable `reconcile_status`, `reconcile_lease_owner`,
`reconcile_lease_expires_at`, `reconcile_next_probe_at`, `reconciled_at`, and
`reconcile_probe_count` (NOT NULL, default 0) to `mcp_dispatch_attempts`, plus
three indexes. Columns and indexes are added only when missing. No row is
updated. `McpDispatchAttemptModel` maps the columns; nothing here writes them yet.

## Lineage gap for databases at the api chain's 050

The two chains share 001-042 and then diverge. A database migrated by the api
chain to 050 never ran this repository's `044_receipt_resolutions`,
`045_approval_gate_approvals`, or `046_api_key_usage_events`. Because 047 now
follows this repository's 046, Alembic treats those as applied, so such a
database stays at 050 without `receipt_resolutions`, `resolution_chain_heads`,
`approval_gate_approvals`, `api_key_usage_events`, or the `api_key_usage_daily`
view. It also keeps the api-only insights tables, which no model here maps.
This port does not repair that; a later idempotent migration must create the
missing objects before code that uses them serves against such a database.

## Compatibility release

Production boot requires the database revision to match the packaged head.
Fresh databases from this chain gain 047-050 normally. Qualify against a
disposable PostgreSQL upgrade from 046 and from an api-chain 050 copy.
These release checks have not been run against staging or production.

## Rollback boundary

Downgrade to 046 refuses while any row has a non-NULL `reconcile_status` and
otherwise drops the six columns and three indexes.
