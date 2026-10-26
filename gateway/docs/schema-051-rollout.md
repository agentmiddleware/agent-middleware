# Schema 051: SQLite boolean defaults and rollback boundary

Migration `051_sqlite_boolean_defaults` revises
`050_demo_key_first_used` (the api-chain port, PR #234). PR #61 must merge
after PR #234.
On SQLite it repairs legacy text booleans in refresh tokens, human approvals,
and permits, then replaces their text defaults with integer false defaults.
PostgreSQL schema and data are unchanged.

## Compatibility release before migration

Before an authorized rollout, confirm the database revision, back up the
database, and qualify a 051-capable recovery image. Rehearse the migration on
a copy of any SQLite database created before the corrected 022 and 023
migrations, including existing rows and inserts that omit these columns.
The application requires its packaged Alembic head, so an older image cannot
restart after the database reaches 051.

## Rollback after activation

SQLite downgrade raises `sqlite_boolean_defaults_downgrade_unsupported`
instead of marking a database downgraded while retaining corrected values
and defaults. Recover with a 051-capable image and the database at 051.
Returning to an older packaged head requires a separately reviewed migration
and compatibility plan.
