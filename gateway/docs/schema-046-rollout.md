# Schema 046: API key usage reporting route

Migration `046_api_key_usage_reporting` revises `045_api_key_usage_events`.
It is a no-op migration: schema 045 already created the event table, index,
and daily view proposed by the archived PR #840. No backfill is required.
The application adds the admin-only
`GET /v1/api-keys/usage/daily?key_id=apk_...&limit=100` route using that view.
The route returns the PR #840 field names (`count_4xx`, `count_5xx`,
`first_call`, `last_call`) mapped from the existing view. It exposes no raw
request data or API key values.

## Compatibility release

Production boot requires the database revision to match the packaged head.
Qualify a 046-compatible image and recovery artifact against a disposable
PostgreSQL upgrade from 045. Verify both admin read routes with the intended
runtime role. Record the exact image SHA, database revision, and private parity
result. These release checks have not been run in this worktree.

## Rollback boundary

Downgrading 046 to 045 changes only the Alembic revision marker; it does not
drop usage events. Do not restart a 045-packaged image against a database at
046. Use a qualified 046-compatible recovery image until the database and
application revisions are aligned.
