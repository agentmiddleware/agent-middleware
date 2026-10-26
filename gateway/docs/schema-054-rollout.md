# Schema 054: wallet product activation claim

Migration `054_wallet_product_activation` revises `053_repair_api_lineage`. It
adds nullable `wallets.product_activated_at`, the account-level claim behind the
once-per-account PostHog `activation` event (see
[product analytics](product-analytics.md)). The gateway sets the column with a
conditional UPDATE (`WHERE product_activated_at IS NULL`) on a key's first use,
whether or not tracking is enabled.

The upgrade backfills root wallets whose keys, or whose descendants' keys up to
three levels, were already used. Each gets its earliest `first_used_at`, so
existing accounts emit no late activation. Only NULL rows are updated.

## Compatibility release

Production boot requires the database revision to match the packaged head, so
a 053-packaged image cannot restart after 054 is applied. Qualify a
054-capable image against a disposable PostgreSQL upgrade from 053 first.
These release checks have not been run against staging or production.

## Rollback boundary

Downgrade drops the column. It holds only an analytics marker, so no signed or
financial data is lost. After a downgrade and re-upgrade, the backfill
re-derives the marker from `api_keys.first_used_at`.
