# Schema 050: demo key first use (ported)

Migration `050_demo_key_first_used` revises `049_demo_key_attribution`, ported
with the same revision id. `043_demo_key_fields` already adds
`api_keys.first_used_at` and the `demo_key_funnel_by_source` view, so this adds
them only when missing and backfills only NULL `first_used_at` from
`last_used_at`. Staging and production are at this revision.

## Rollback boundary

Downgrade is a no-op; the column and view belong to 043, which retains them.
