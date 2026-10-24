# Schema 046: API key usage events

Migration `046_api_key_usage_events` revises `045_approval_gate_approvals`. It adds
the metadata-only `api_key_usage_events` table, an index on `(key_id,
created_at)`, and the read-only `api_key_usage_daily` view. It changes no
existing rows. Usage before 046 is not backfilled; `api_keys.use_count` is a
bounded-key budget counter, not a historical call total.

## Compatibility release

Production boot requires the database revision to match the packaged Alembic
head. A 045-packaged image cannot restart after 046 is applied. Before applying
046, qualify a 046-compatible image and a recovery artifact against a
disposable PostgreSQL upgrade from 045. Verify the event insert, the daily
view, and the admin-only read route with the intended runtime role. Record
the exact image SHA, database revision, and private parity result.
These release checks are required and have not been run in this worktree.

The new event writer is best effort: an insert failure leaves a reporting gap
without failing the API request. The view contains only events recorded after
the migration. This repository has no tenant RLS layer. The application exposes
the view through a bootstrap-admin-only route; database privileges are a
separate deployment boundary.

## Rollback boundary

Do not deploy a 045-packaged image against schema 046. Use a qualified
046-compatible recovery image while retaining the database at 046. Alembic
downgrade to 045 drops the view and table, permanently discarding every usage
event recorded since 046. It is a data-loss operation, not an application
rollback; never perform it while the API serves traffic.
