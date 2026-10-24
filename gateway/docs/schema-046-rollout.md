# Schema 046: SQLite boolean defaults and rollback boundary

Migration `046_sqlite_boolean_defaults` revises `045_api_key_usage_events`
from PR #195. PR #61 must merge after PR #195.
On SQLite it repairs legacy text booleans in refresh tokens, human approvals,
and permits, then replaces their text defaults with integer false defaults.
PostgreSQL schema and data are unchanged.

## Compatibility release before migration

Before an authorized rollout, confirm the database revision, back up the
database, and qualify a 046-capable recovery image. Rehearse the migration on
a copy of any SQLite database created before the corrected 022 and 023
migrations, including existing rows and inserts that omit these columns.
The application requires its packaged Alembic head, so an older image cannot
restart after the database reaches 046.

## Rollback after activation

The downgrade retains corrected values and defaults. Recover with a
046-capable image and the database at 046. Returning to an older packaged
head requires a separately reviewed migration and compatibility plan.
