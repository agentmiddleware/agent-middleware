# Schema 048: tenant RLS policies (ported)

Migration `048_tenant_rls` revises `047_dispatch_reconcile_lease`, ported from
the agent-middleware-api chain with the same revision id. PostgreSQL only.
It creates the `amw_tenant_isolation` and `amw_delegated_issuer_read`
policies. This repository has no `TENANT_RLS_MODE` setting and no
`app.rls_bypass` in `migrations/env.py`, so the setting is read with an `off`
default and row-level security is never enabled or forced: policies exist but
do nothing, which matches the original's default. Enabling RLS is out of scope.

## Compatibility release

No behaviour change while RLS stays disabled. A database already at 050 from
the api chain is unchanged.

## Rollback boundary

Downgrade drops the policies and runs NO FORCE / DISABLE ROW LEVEL SECURITY on
each listed table.
