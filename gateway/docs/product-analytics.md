# Product analytics (PostHog)

The gateway can send three product events to PostHog. It is off unless
`POSTHOG_API_KEY` is set.

| Env var | Default | Meaning |
| --- | --- | --- |
| `POSTHOG_API_KEY` | empty (off) | PostHog project API key. Blank or unset: every tracking call is a no-op. |
| `POSTHOG_HOST` | `https://us.i.posthog.com` | PostHog ingestion host, e.g. `https://eu.i.posthog.com` or a self-hosted URL. |

Each is read from the process environment first, then from `Settings`, so
`.env` entries work. The key is a `SecretStr` and is masked in settings dumps.
Never commit a real key.

## Events

The distinct id is the account's root (sponsor) wallet id.

| Event | Where | Properties |
| --- | --- | --- |
| `$identify` | Every signup, just before `signup` | `$set.email` when the signup carries an email (sponsor wallets only) |
| `signup` | `POST /v1/billing/wallets/sponsor` (`method=sponsor_wallet`), `POST /v1/demo/keys` (`method=demo_key`), `POST /v1/dev-keys/self-provision` (`method=dev_self_provision`) | `method` |
| `activation` | First successful API key authentication for the account (`APIKeyService.validate_key`) | `key_id`, `wallet_id` |

Demo and dev self-provision signups are anonymous: no email is sent (dev
wallets only carry a synthetic `@dev.local` address).

`activation` fires once per account. On a key's first use, the gateway walks
`parent_wallet_id` to the root wallet and claims
`wallets.product_activated_at` with a conditional `... IS NULL` UPDATE, so
concurrent first uses of one key or of several keys produce exactly one event.
The claim is recorded even when tracking is off, so turning tracking on later
doesn't replay old activations. Migration 055 backfills accounts that were
already active ([Schema 055](schema-055-rollout.md)). `first_used_at` is still
set in the same single usage update as before. Derived (JWT) authority recorded through
`consume_derived_key_use` is not an activation trigger.

## Failure behaviour

Sends go to `<POSTHOG_HOST>/capture/` over httpx as background tasks with a
2 s timeout. They reuse one shared httpx client and are capped at 256 in-flight
tasks: beyond that, events are dropped. App shutdown drains them and closes the
client. Every error is caught and logged at debug level with the
exception type only (never the key), so tracking cannot fail or slow a
request. Code lives in `app/core/product_analytics.py`; tests in
`tests/test_product_analytics.py` mock the HTTP send.

## Privacy

Only `$identify` sends personal data (the sponsor email). Enabling tracking
in a deployment is an operator decision and should be reflected in that
deployment's privacy notice.
