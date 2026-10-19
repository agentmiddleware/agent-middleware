# API key rotation

This is the runbook for API keys. It is not the trust-plane signing key.
Signing-key custody, rotation, and compromise response are in
[key-management.md](key-management.md). There is no script and no route that
retires signing-key metadata.

Run the script commands below from the gateway directory, which contains
`scripts/rotate_api_keys.py`.

Two API key classes rotate. A third class must not.

| Class | Where it lives | How it dies |
| --- | --- | --- |
| Bootstrap admin key | Host secret `VALID_API_KEYS`, comma-separated | Replace the secret and restart every worker |
| Wallet key | Database, minted by `POST /v1/api-keys`. The secret starts with `b2a_` and is shown once | HTTP revoke or rotate. No worker restart |
| Static dev key | `STATIC_DEV_API_KEYS`, prefix `amw_dev_` | Not rotated. See [static-dev-api-keys.md](static-dev-api-keys.md) |

`get_settings` caches `VALID_API_KEYS` for the life of the process. A worker
keeps accepting the old bootstrap list until that worker restarts.

## Bootstrap keys

Generate replacements. The command prints key material to stdout and a short
note to stderr. The default count is 2. Each generated key starts with
`amw_live_`. That prefix is only a label from the generator. The server does
not require it: any comma-separated entry of at least 8 characters in
`VALID_API_KEYS` is a bootstrap admin key.

```bash
python scripts/rotate_api_keys.py generate
```

Put the new comma-separated list in the host secret manager. Restart or
redeploy every worker. Until a worker restarts, that worker still accepts the
old key.

For a cutover with no gap where nobody can authenticate, deploy once with
both the old key and the new key in `VALID_API_KEYS`, wait until every worker
has restarted, then deploy again with the old key removed.

Verify only after the old key is gone from every worker. The checker fails
if the old key still returns 200.

```bash
export AGENT_MIDDLEWARE_API_URL=https://the-origin-you-just-rotated
export OLD_API_KEY=...   # must now be rejected
export NEW_API_KEY=...   # must authenticate
python scripts/rotate_api_keys.py verify
```

`verify` sends `GET /v1/audit/summary` with `X-API-Key`. It does not follow
redirects and it does not write. Exit codes:

- 0: the old key is rejected with 401 or 403, and the new key returns 200
- 1: either of those checks failed
- 2: an env var is missing, or the URL is unsafe (embedded credentials, a
  path, a query, a fragment, or remote HTTP)

A key the server does not know is `403` with `invalid_api_key`, not 401.
401 is a missing credential. Both count as "rejected" for the old key.

The checker allows the canonical production origin. Point it at the
deployment whose secret you just changed, and only after the old key should
already be dead.

## Wallet keys

These routes take effect as soon as the database write commits. They do not
wait for a restart.

Who may call them:

- A bootstrap admin key may act on any wallet.
- A database key may act only on its own `wallet_id`. Any other wallet is
  `403` `wallet_access_denied`.
- A JWT cannot mint a replacement key (`403` `jwt_cannot_mint_api_key`).
- A bounded database key (`max_uses` or `expires_at`) cannot create a fresh
  key or rotate another key. An omitted or different `key_id` on rotate is
  `403` `bounded_key_cannot_mint`. It may rotate its own key, and the
  replacement keeps its remaining bounds. A key with `max_uses` must set
  `revoke_old: true`, or rotate returns `422` `invalid_rotation_request`.
- A bounded wallet key may call emergency revoke. If it requests a replacement,
  the new key inherits only the caller key's remaining bounds.

### Rotate one key

`POST /v1/api-keys/rotate`

```json
{
  "wallet_id": "wallet_...",
  "key_id": "key_...",
  "revoke_old": true,
  "reason": "suspected_leak"
}
```

`revoke_old` defaults to false. A rotation that omits it leaves the old key
active. Set it to true when the old secret must stop working.

The response includes the new secret once. An optional `Idempotency-Key`
header replays that first result on a retry, and the replay does not include
the secret again (`new_key.api_key` is null). Save the first response.

### Revoke one key

`DELETE /v1/api-keys/{wallet_id}/{key_id}`

Returns 204. The optional `reason` query parameter defaults to
`user_request`. This does not mint a replacement.

### Revoke every key on a wallet

`POST /v1/api-keys/emergency-revoke`

```json
{
  "wallet_id": "wallet_...",
  "reason": "security_incident",
  "create_new_key": false
}
```

`create_new_key` defaults to true. An emergency call that omits it mints a
replacement and returns that secret once. Set it to false when you only want
the old keys dead. This route also revokes the wallet's refresh tokens.

Rotation history for a wallet is `GET /v1/api-keys/{wallet_id}/logs`.

## Incident table

| What happened | What to do |
| --- | --- |
| A `VALID_API_KEYS` bootstrap key leaked, or may have leaked | Generate a replacement, remove the old value from the host secret, restart every worker, then run `verify`. Treat every worker that has not restarted as still accepting the old key. |
| A `b2a_` wallet key leaked | `DELETE` that key, or `POST /v1/api-keys/rotate` with `revoke_old` true. To revoke every wallet key and its refresh tokens, use `POST /v1/api-keys/emergency-revoke` and set `create_new_key` explicitly. No restart. |
| An `amw_dev_` static key leaked | Production-like processes refuse to boot when `STATIC_DEV_API_KEYS` is set, and they ignore those keys if that guard is bypassed. Remove the value from the local env if the machine is shared. Do not copy it into `VALID_API_KEYS`. Entries without the `amw_dev_` prefix never authenticate, including a live key pasted into the wrong variable. |
| The signing private key leaked, or may have leaked | Stop and follow [key-management.md](key-management.md). This runbook does not retire signing keys, and nothing in `scripts/` does either. |

## What this does not rotate

Stripe webhook secrets, database credentials, and TLS material stay in the
host secret manager. Changing them is a deploy change, not an API call.
