# Schema 054: wallet product activation claim

Migration `054_wallet_product_activation` revises `053_repair_api_lineage`. It
adds nullable `wallets.product_activated_at`, the account-level claim behind the
once-per-account PostHog `activation` event (see
[product analytics](product-analytics.md)). The gateway sets the column with a
conditional UPDATE (`WHERE product_activated_at IS NULL`) on a key's first use,
whether or not tracking is enabled.

The upgrade backfills root wallets whose keys, or any descendant's keys, were
already used. It walks ancestry with one recursive CTE to the same 64-hop
depth the runtime resolver uses (`_MAX_WALLET_DEPTH`; a test asserts they
match). `api_keys` is aggregated once per wallet and parents are followed by
the `wallets` primary key, so cost scales with used wallets times depth rather
than roots times keys. Each root gets its earliest `first_used_at`, so existing
accounts emit no late activation. Only NULL rows are updated, and cycles are
bounded by the depth limit and never reach a root.

At runtime the resolver returns no account (and skips the claim, so no event)
when the chain is deeper than 64, has a cycle, or reaches a row the session
cannot read. It never claims an intermediate wallet.

## Tenant RLS

With `TENANT_RLS_MODE` on, a request session scoped to a child wallet may not
be able to read or update its root wallet row. The resolver then cannot prove
the root and skips the claim: activation is not recorded for that first use
and no event fires. It fails safe (no wrong account, no auth impact). That is
harmless while RLS is off by default; enabling RLS needs a privileged claim
path before activation counts can be trusted.

## Claim write with tracking off

The claim UPDATE runs even when PostHog is not configured. It is one
conditional UPDATE per key lifetime (first use only). Skipping it would leave
accounts that activated while tracking was off unmarked, and they would emit a
late `activation` once tracking is enabled, which is the inconsistency this
column exists to prevent. The write is kept on purpose.

## Compatibility release

Production boot requires the database revision to match the packaged head, so
a 053-packaged image cannot restart after 054 is applied. Qualify a
054-capable image against a disposable PostgreSQL upgrade from 053 first.
These release checks have not been run against staging or production.

## Rollback boundary

Downgrade drops the column. It holds only an analytics marker, so no signed or
financial data is lost. After a downgrade and re-upgrade, the backfill
re-derives the marker from `api_keys.first_used_at`.
