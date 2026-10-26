# Schema 048: tenant RLS policies (ported)

Migration `048_tenant_rls` revises `047_dispatch_reconcile_lease`. It is ported
from the agent-middleware-api chain with the same revision id and is
PostgreSQL only. It creates the `amw_tenant_isolation` policy on the 15
archived wallet-scoped tables and the `amw_delegated_issuer_read` policies.

The two wallet-scoped tables from this repo's 044, `receipt_resolutions` and
`resolution_chain_heads`, get the same `amw_tenant_isolation` policy when they
already exist. On an api-chain database they don't exist yet;
`053_repair_api_lineage` creates them and applies the identical policy.

`TENANT_RLS_MODE` is ported from the api Settings with the same default (`off`)
and the same validation, plus one opt-in mode added here (`off`, `enable` or
`enforce`).

- `off`: the policies exist, but row-level security stays disabled, so nothing
  changes. This matches the original's default.
- `enable` (opt-in, not in the original): running the migration enables RLS
  without FORCE. The table owner, superusers and BYPASSRLS roles are
  unaffected; any other role sees only rows for its `app.current_wallet_id`
  (none when unset).
- `enforce`: running the migration enables and forces RLS on every listed table.

`migrations/env.py` sets `app.rls_bypass` to `on` for the migration
transaction only, as the archived env.py did. That lets data migrations, such
as the 050 backfill, see every row under FORCE with a non-superuser owner.

## Compatibility release

There is no behaviour change in `off` mode.

Warning: this gateway does not yet set `app.current_wallet_id` per transaction
(the api repo's runtime tenant scope is not ported). A database that was
migrated under `enforce` therefore fails closed for a NOSUPERUSER NOBYPASSRLS
runtime role: wallet-scoped tables read as empty and writes are refused. That
includes an api-chain 050 database that already has RLS forced. Don't migrate
under `enforce`, and don't point this gateway at such a database with a
policy-bound role, until the runtime scope is ported.

## Rollback

Downgrade drops the policies. It also runs NO FORCE / DISABLE ROW LEVEL
SECURITY on each listed table, including the two late tables when they exist.
