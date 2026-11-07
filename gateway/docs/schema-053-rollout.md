# Schema 053: repair databases built by the archived api chain

Migration `053_repair_api_lineage` revises `052_receipt_mode_build`. A database
that reached `050_demo_key_first_used` through the archived agent-middleware-api
chain never ran this repo's 044-046, so it lacks `receipt_resolutions`,
`resolution_chain_heads`, `approval_gate_approvals`, `api_key_usage_events` and
the `api_key_usage_daily` view. 053 creates each one only when absent, with the
columns, indexes and constraints of 044-046, and writes no rows. On a database
built from this chain it changes nothing.

On PostgreSQL, 053 also gives `receipt_resolutions` and
`resolution_chain_heads` the `amw_tenant_isolation` policy from
`048_tenant_rls` wherever that policy is missing. This is the case on an
api-chain database, because 048 ran there before these tables existed. RLS is
enabled and forced on them when `TENANT_RLS_MODE` is `enforce`, or when
`api_keys` already has forced RLS. A fresh database and a repaired api-chain
database therefore end up with identical policies. Downgrade leaves the
policies in place, together with the tables.

The api-only tables such as `insight_reporting_principals` are left in place.

## Compatibility release

Production boot requires the database revision to match the packaged head, so
a 052-packaged image cannot restart after 053 is applied. Qualify a
053-capable image against a disposable PostgreSQL copy of the database first.
These release checks have not been run against staging or production.

## Rollback

Downgrade is a documented no-op. After upgrade nothing records whether 053 or
044-046 created an object, so dropping them could destroy tables a fresh
chain owns. Rolling back to 052 leaves the repaired objects in place, which is
compatible with 052 code.
