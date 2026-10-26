# Schema 052: receipt enforcement mode and gateway build

Migration `052_receipt_mode_build` revises `051_sqlite_boolean_defaults`. It
adds nullable `receipts.enforcement_mode` (`enforce` or `permissive`) and
`receipts.gateway_build` (the image commit SHA from
`app.core.build_metadata.get_build_commit_sha`, NULL when unknown). Receipts
issued after 052 sign both fields when set; existing receipts stay NULL and
keep verifying with their original signatures. No row is updated.

## Compatibility release

Production boot requires the database revision to match the packaged head, so
a 051-packaged image cannot restart after 052 is applied. Qualify a
052-capable image against a disposable PostgreSQL upgrade from 051 first.
These release checks have not been run against staging or production.

## Rollback boundary

Downgrade to 051 refuses while any receipt carries either field, because
dropping signed fields would make those receipts fail verification. Recover
with a 052-capable image and the database left at 052.
