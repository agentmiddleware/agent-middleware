# Static Development / Training API Keys

Stable, non-rotating API keys for local testing, development, and training
material. This is the companion to `docs/api-key-rotation.md`, which covers
the rotated `amw_live_` bootstrap keys in `VALID_API_KEYS`; this document
covers the `amw_dev_` keys in `STATIC_DEV_API_KEYS`, which are deliberately
**exempt from rotation**.

## Why a separate key class

Rotation is the right posture for anything that can touch production, but it
is actively hostile to development and training workflows: every rotation
breaks recorded demos, notebooks, onboarding docs, local `.env` files, and
test fixtures that embedded the old key. The fix is not to stop rotating the
live keys — it is a key class whose *only* power is local, so it never needs
rotating in the first place:

- **Static by design.** Nothing rotates, expires, or revokes an `amw_dev_`
  key. Once generated, it keeps working for the lifetime of your local
  setup, so training material stays reproducible.
- **Worthless outside local.** Two independent layers enforce this:
  1. `app.core.trust_mode.validate_trust_mode_guardrails` refuses to boot a
     production-like deployment (`production`, `staging`, `preview`, …) when
     `STATIC_DEV_API_KEYS` is set at all.
  2. The auth path (`app.core.auth.get_auth_context`) ignores static dev
     keys whenever `ENVIRONMENT` is production-like, even if the guardrail
     were somehow bypassed.
  A leaked dev key therefore compromises nothing that matters — which is
  exactly why it is allowed to be static.
- **Never confusable with a live key.** Static dev keys must carry the
  `amw_dev_` prefix; entries without it are ignored by the auth path. A
  rotated `amw_live_` bootstrap key pasted into `STATIC_DEV_API_KEYS` never
  authenticates, and the prefix keeps dev keys greppable for secret
  scanners.

## Setup

1. **Generate** (never hand-write key material):

   ```bash
   python scripts/generate_static_dev_keys.py --count 2
   ```

2. **Configure** your local `.env` (see `.env.example`):

   ```bash
   ENVIRONMENT=local
   STATIC_DEV_API_KEYS=amw_dev_...,amw_dev_...
   ```

3. **Use** the key exactly like any other API key (shown via an env var —
   keeping the literal out of shell history and scanner reports):

   ```bash
   export STATIC_DEV_KEY=amw_dev_...   # one of the generated values
   curl -H "X-API-Key: $STATIC_DEV_KEY" http://localhost:8000/v1/audit/summary
   ```

## What a static dev key can do

In a local-compatible environment (`local`, `dev`, `development`, `test`,
`testing`, `ci`, `localhost`, or unset), a static dev key authenticates as a
**bootstrap admin** — the same power as a `VALID_API_KEYS` entry: it passes
`require_bootstrap_admin()`, can read the audit plane, mint wallets, and
create DB-backed wallet keys. Audit records distinguish it with
`auth_source="static-dev"` so local traces show which key class acted.

In a production-like environment it does nothing: the server refuses to
boot with the variable set, and the auth path would not honor the key even
if it did.

## Rules

- **Never** put an `amw_dev_` key in `VALID_API_KEYS`, and never "promote" a
  dev key to production use. Production keys come from
  `scripts/rotate_api_keys.py generate` and live in the host secret manager.
- **Never** set `STATIC_DEV_API_KEYS` on a shared or hosted deployment.
  Production-like environments fail startup on it by design; local-shaped
  shared instances (e.g. a team demo box) should use `VALID_API_KEYS` with
  rotation instead, because anything shared is leakable and anything
  leakable must be rotatable.
- Committing a dev key to a **private** training doc or local fixture is the
  intended use. Still keep them out of the public repo — greppable
  `amw_dev_` strings in public history invite copy-paste confusion even
  though the keys hold no production power.

## Self-serve provisioning for agents

An agent talking to a running local instance can provision its own
wallet-scoped dev key with **no pre-shared secret**, so nothing needs to be
handed to it out of band. Opt in on the local server:

```bash
ENABLE_DEV_KEY_SELF_PROVISION=true
```

Then the agent calls:

```bash
curl -X POST http://localhost:8000/v1/dev-keys/self-provision \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "my-dev-agent"}'
```

The response contains a sponsor wallet, an agent wallet funded with bounded
synthetic dev credits (`budget_credits`, default 1000, max 100000), and a
wallet-scoped API key shown once — the same shape
`scripts/partner_api_key_bootstrap.py` produces with a bootstrap admin, so
self-served agents exercise the real credential class end to end
(permits, metering, receipts, audit).

Scope and safety:

- The minted key is **wallet-scoped, never bootstrap-admin**: a credential
  anyone can mint must not read the audit plane or touch other tenants.
- The route answers **404 until the flag is set**, so the surface is never
  available by accident (same pattern as `ENABLE_STANDARD_MCP_ENDPOINT`).
- **Production-like environments refuse to boot** with the flag set, and
  the handler independently fails closed with 403 there — the same
  containment as `STATIC_DEV_API_KEYS`.
- **Cross-origin browser requests are rejected (403).** The endpoint takes
  no auth and returns a live secret in its body, so under the default
  wildcard CORS a page you merely visit could otherwise `fetch` it against
  your localhost and read the minted key. A browser always sends an `Origin`
  header on such a request; real dev agents (CLIs, SDKs, curl) send none and
  pass through. This is the same DNS-rebinding hardening the standard MCP
  endpoint uses.
- Self-provisioned keys are ordinary DB-backed wallet keys (`b2a_` class),
  so unlike static env keys they **are** covered by the DB-backed-key
  guidance in the rotation runbook, and they can be rotated or revoked
  through `/v1/api-keys`.

Never enable this on a shared or hosted deployment: anyone who can reach
the port can mint keys and synthetic credits, and the `Origin` check stops
only *browser* abuse, not a direct non-browser client. It exists for
single-operator local instances and CI-style environments only.

## Relationship to the rotation runbook

`docs/api-key-rotation.md` and its incident table apply **only** to
`VALID_API_KEYS` / `amw_live_` keys and DB-backed wallet keys. Static dev
keys are out of scope there on purpose: the runbook's threat model (a leaked
key is a full-control compromise) does not apply to a key class that no
production deployment will ever accept.

For the full agent workflow — self-provision, then prove the economic
invariants (charge-once, replay, concurrency, scope denial, signature
verification) with `scripts/agent_self_credential_proof.py` — see
[`docs/agent-self-credentialing.md`](agent-self-credentialing.md).
