# API key hashing

API keys are stored only as digests. New digests are **HMAC-SHA256 keyed by a
server pepper** (`API_KEY_HASH_PEPPER`), so a leaked `api_keys` table cannot be
checked offline against candidate keys without also holding the pepper.

## Configuration

| Variable | Required | Notes |
|---|---|---|
| `API_KEY_HASH_PEPPER` | Yes in production-like environments | Long random secret (32+ random bytes). Read from the process environment, then `.env` via Settings (stored as a `SecretStr`). Never logged. |

## Lookup and migration

`APIKeyService.validate_key` computes both candidate digests and looks the row
up by digest and prefix:

1. **HMAC-SHA256(pepper, key)**: the current format.
2. **Legacy SHA-256(key)**: rows written before the pepper existed.

Both digests are compared in constant time. When a key matches on its legacy
digest and a pepper is configured, the row is rewritten to the HMAC digest after
the authentication succeeds. The update is guarded on the old digest, so
concurrent requests upgrade it only once. A failed rehash is logged by key id
and exception type only, and the legacy row keeps working.

No schema change is needed, because both digests are 64 hex characters.

## When the pepper is unset

| Environment | Issue new keys | Validate keys | Startup |
|---|---|---|---|
| Local-compatible (`local`, `test`, ...) | Legacy SHA-256 digest | Legacy digest only | Boots |
| Production-like | Refused (`APIKeyHashingUnavailable`) | Every key rejected | **Refuses to boot** |

Production fails closed so that a missing secret can never quietly put the
fleet back on unkeyed hashing.

## Deploy steps

1. Generate the pepper and store it in the secret manager for every
   production-like environment **before** deploying this version. Without it the
   gateway will not start.
2. Deploy. Existing keys keep working through the legacy fallback and are
   rehashed on first use.
3. Keys that are never used stay in the legacy format. Rotate or revoke them
   if they should be retired.
4. Do not change the pepper. Changing it invalidates every rehashed or newly
   issued key. Rotating the pepper needs its own dual-pepper plan.
