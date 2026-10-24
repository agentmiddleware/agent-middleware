# Schema 045: SQLite boolean defaults and rollback boundary

Migration `045_sqlite_boolean_defaults` revises `044_receipt_resolutions`.
On SQLite it repairs legacy text booleans in refresh tokens, human approvals,
and permits, then replaces their text defaults with integer false defaults.
PostgreSQL schema and data are unchanged.

## Compatibility release before migration

Before an authorized rollout, confirm the database revision, back up the
database, and qualify a 045-capable recovery image. Rehearse the migration on
a copy of any SQLite database created before the corrected 022 and 023
migrations, including existing rows and inserts that omit these columns.
The application requires its packaged Alembic head, so an older image cannot
restart after the database reaches 045.

## Rollback after activation

The downgrade retains corrected values and defaults. Recover with a
045-capable image and the database at 045. Returning to an older packaged
head requires a separately reviewed migration and compatibility plan.
