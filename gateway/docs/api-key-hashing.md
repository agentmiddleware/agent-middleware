# API key hashing

API keys are stored only as a digest. The digest is **HMAC-SHA256** of the full
key, keyed by a server-side pepper (`API_KEY_HASH_PEPPER`). Lookups compute the
HMAC digest and compare it in constant time (`hmac.compare_digest`). There is
**no unkeyed fallback**: plain SHA-256 rows from before this change are never
looked up, accepted, or rewritten.

## Pepper resolution

1. `API_KEY_HASH_PEPPER` (env var or `Settings.API_KEY_HASH_PEPPER`, a
   `SecretStr` that is masked in logs and reprs).
2. Local and test environments only: if no pepper is configured, the public,
   non-secret dev pepper `DEV_API_KEY_HASH_PEPPER` in
   `app/services/api_key_service.py` is used so the gateway runs out of the box.
   It is not a secret and must never be relied on for real keys.
3. Production-like environments (`is_production_like_environment()`): no dev
   pepper. Without a configured pepper the gateway **refuses to boot**
   (`validate_api_key_hashing_config()` in the lifespan), key generation raises
   `APIKeyHashingUnavailable`, and `validate_key` rejects every key.

## Generating a pepper

Use 32+ random bytes from your secret store, e.g.
`python -c "import secrets; print(secrets.token_urlsafe(48))"`. Never commit it
or log it. Changing the pepper invalidates every existing key.

## Migration and key rotation (breaking)

Every key issued before this change was stored as plain SHA-256 and **stops
working the moment this version is deployed**. Rotation order:

1. Generate the pepper and set `API_KEY_HASH_PEPPER` in the production secret
   store (and every other production-like environment).
2. Deploy. Startup fails closed if step 1 was missed.
3. Reissue every API key (new keys get HMAC digests), then revoke or delete the
   old rows, and send the new keys to their holders through the usual secure
   channel.
4. Expect `401`s from clients still on old keys until they switch.

Rotating the pepper later follows the same steps: every key must be reissued.
