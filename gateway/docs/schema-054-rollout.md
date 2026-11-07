# Schema 054: scrypt API-key hashes with a lookup id

Migration `054_api_key_scrypt_lookup` revises `053_repair_api_lineage`. It
widens `api_keys.key_hash` from 64 to 255 characters for the encoded scrypt
hash and adds the nullable, indexed `api_keys.key_lookup` column
(`ix_api_keys_key_lookup`). It writes no rows. On SQLite only the column
and index are added: SQLite does not enforce VARCHAR length.

## Rollout

1. Set `API_KEY_HASH_PEPPER` in every production-like environment first (see
   [API key hashing](api-key-hashing.md)). Without it, the new code refuses to boot.
2. Run `alembic upgrade head`. Old code ignores both changes, so the migration
   can run before the deploy.
3. Deploy. Rows from before 054 keep a NULL `key_lookup` and a SHA-256
   digest, so they never authenticate again. Reissue every key.

## Rollback

Downgrade drops the index and column and narrows `key_hash` back to 64
characters. It fails while any scrypt-hashed row exists, so delete or
revoke those rows first. Keys issued after 054 do not work on old code.

## Numbering

PR #245 also adds a `054_*` revision. Whichever PR merges second
must renumber its revision to 055 and repoint `down_revision`.
