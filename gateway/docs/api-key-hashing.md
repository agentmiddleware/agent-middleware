# API key hashing

API keys are stored only as a **salted, peppered scrypt hash**. Each key is
found by a **non-secret, indexed lookup id** and then verified with the KDF in
constant time. There is no unkeyed or SHA-256 fallback.

## Format

- Key: `b2a_` + 43 URL-safe random characters (`secrets.token_urlsafe(32)`).
- `api_keys.key_lookup`: the first 16 characters (`b2a_` + 72 random bits),
  indexed. It reveals nothing useful: the remaining ~31 characters (~184 bits)
  stay secret.
- `api_keys.key_hash`: `scrypt$N$r$p$<salt>$<hash>`, base64url, with a 16-byte
  random salt per row. The KDF salt is `pepper || row salt`, so a database dump
  alone cannot be attacked offline.

## Why scrypt, N=2^14, r=8, p=1

- scrypt is memory-hard (16 MiB per hash), which makes GPU and ASIC guessing
  costly. PBKDF2 is compute-only.
- It ships in the standard library (`hashlib.scrypt`, OpenSSL), so there is no
  new dependency. hashlib releases the GIL, and the verify runs in
  `asyncio.to_thread`, so it never blocks the event loop.
- N=2^14, r=8, p=1 is the RFC 7914 interactive setting, about 22 ms on an
  Apple M4 Pro. Argon2id would be the first choice but needs a native
  dependency. API keys already carry about 184 secret bits, so the KDF is
  defence in depth for a leaked table. It is not there to protect weak secrets.

## Lookup and verified-key cache

`validate_key` selects ACTIVE rows by `key_lookup` and `key_prefix`, then
verifies each candidate with scrypt and `hmac.compare_digest`. A key whose
lookup id matches no row costs no KDF at all.

Successful verifications go into an in-process cache: bounded (4096 entries,
LRU) with a 60 s TTL. Each entry is keyed by the stored row hash, which is
already non-reversible. It holds a cheap verifier of the presented key
(minimal-cost scrypt salted with a per-process random secret, never persisted
or logged) and the pepper in use. A hit therefore needs the exact same key and
pepper. The row is still read from the database on every request, so
revocation, expiry and `max_uses` take effect immediately. Only the KDF is
skipped.

## Pepper resolution

1. `API_KEY_HASH_PEPPER` (env var or `Settings.API_KEY_HASH_PEPPER`, a
   `SecretStr` that is masked in reprs). It is never logged.
   An explicitly empty environment variable masks any dotenv value and makes
   production fail closed. Remove the variable to use dotenv; setting it empty
   must not revive a stale pepper from a file.
2. Local and test only: if no pepper is set, the public, non-secret
   `DEV_API_KEY_HASH_PEPPER` is used.
3. Production-like environments: there is no dev pepper. Startup refuses to
   boot (`validate_api_key_hashing_config()`), key generation raises
   `APIKeyHashingUnavailable`, and `validate_key` rejects every key.

## Agent comms owner digests

Bootstrap and env-key owners in the agent-comms registry are stored as
`scrypt:<hex>`: a deterministic scrypt (same cost) of the key, salted with a
fixed domain tag plus the pepper. Ownership is an equality match, so it cannot
use a per-record salt. The digest is computed once per request. Records still
holding the retired `sha256:` digest are left as they are but match nobody, so
agents registered by env keys before this change must be re-registered.

## Migration and key rotation (breaking)

Every key issued before this change **stops working the moment this version
is deployed**. See [schema 054 rollout](schema-054-rollout.md).

1. Generate a pepper of 32+ random bytes
   (`python -c "import secrets; print(secrets.token_urlsafe(48))"`). Store it as
   `API_KEY_HASH_PEPPER` in the production secret store and in every
   production-like environment. Never commit or log it.
2. Run `alembic upgrade head` (054).
3. Deploy. Startup fails closed if step 1 was missed.
4. Reissue every API key, revoke or delete the old rows, and send the new keys
   to their holders through the usual secure channel. Re-register agent-comms
   agents owned by env or bootstrap keys.
5. Expect HTTP 403 `invalid_api_key` from clients still on old keys until they switch.

Rotating the pepper later invalidates every key in the same way, so repeat
steps 1, 3 and 4.
