# Troubleshooting

Common issues when setting up or running the Agent Middleware API locally.

## Quick-start fails

### `make: command not found`
Install `make` via your system package manager, or run the manual `pytest` equivalent shown in README.md.

### `uv: command not found`
Install uv:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```
Then restart your shell or run `source $HOME/.local/bin/env`.

### `ModuleNotFoundError: No module named 'app'`
You are running commands from outside the repo root. `cd` into `agent-middleware/gateway/` before running `uvicorn` or `pytest`.

---

## Local API startup fails

### `SigningKeyError: trust_signing_private_key_required`
Trust mode is on (the default) and `TRUST_SIGNING_PRIVATE_KEY_B64` is empty. A
value that is not strict base64 of a 32-byte Ed25519 seed fails instead with
`SigningKeyError: invalid_trust_signing_private_key`. Generate a key and export it:
```bash
python3 -c 'import base64, secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())'
export TRUST_SIGNING_PRIVATE_KEY_B64='<output>'
```
Save it in `.env` (gitignored) for reuse across restarts.

### `DurableStateConfigError: STATE_BACKEND=sqlite is not allowed in production-like environments`
You set `ENVIRONMENT=production` (or another production-like value) with
`STATE_BACKEND=sqlite`. `STATE_BACKEND=memory` fails the same way
(`STATE_BACKEND=memory is not allowed in production-like environments`).
Either:
- Switch to `ENVIRONMENT=local` for local development, **or**
- Set `STATE_BACKEND=postgres` and provide a `DATABASE_URL` with `postgresql+asyncpg://`.

### `TrustModeGuardrailError: DATABASE_URL must not be SQLite in production-like environments`
You set `ENVIRONMENT=production` (or another production-like value) with a
SQLite `DATABASE_URL`. SQLAlchemy silently drops `SELECT ... FOR UPDATE` on
SQLite, so concurrent charges, budget reservations, and velocity counters lose
the serialization the money and permit paths depend on — two callers can each
debit a balance only one of them fits inside. Either:
- Switch to `ENVIRONMENT=local` for local development, **or**
- Point `DATABASE_URL` at PostgreSQL (`postgresql+asyncpg://...`).

This is a different check from `STATE_BACKEND` above: that one governs the
key/value state store, this one governs the ORM engine. Satisfying one does
not satisfy the other.

### `TrustModeGuardrailError: DATABASE_URL must be set in production-like environments`
Wallets, permits, receipts, and the ledger are relational. Without
`DATABASE_URL` the engine is never created and the trust plane has nowhere
durable to record what it authorized. Set it to your PostgreSQL DSN.

### `TrustModeGuardrailError: ENABLE_PROOF_SURFACES must be false in production-like environments`
Set `ENABLE_PROOF_SURFACES=false`. Proof surfaces are for local demos only.

### `alembic.util.exc.CommandError: Can't locate revision identified by '...'`
Your database was created with an older migration set. Run:
```bash
alembic upgrade head
```
Or delete the SQLite file and let it recreate (loses data).

---

## API runs but requests fail

### `404` on `/v1/billing/...` or dry-run endpoints
These are **proof surfaces**. Start the API with `ENABLE_PROOF_SURFACES=true` to access them locally. Do not enable in production.

### `410 Gone` on `POST /v1/billing/top-up`
Direct top-ups are disabled by design. Use `POST /v1/billing/top-up/prepare` to create a Stripe PaymentIntent instead. See README.md "Core API surfaces" and [docs/settlement-rails.md](docs/settlement-rails.md).

### `401 Unauthorized` or `403 Forbidden`
- Check that `X-API-Key` header is present and matches a valid wallet-scoped or bootstrap key.
- Bootstrap keys go in `VALID_API_KEYS` (comma-separated). Wallet keys are created via `POST /v1/api-keys`.
- Wallet-scoped keys can only access their own wallet's permits and receipts.
- On REST MCP invocation, a permit denial is a `403` whose `detail` carries the
  reason (for example `permit_required`, `permit_not_found`, `permit_expired`,
  `permit_revoked`, `permit_tool_not_allowed`); on `/mcp/messages` the same
  reasons arrive as JSON-RPC error `-32003`. Verify the permit is valid, not
  expired or revoked, and names the tool you are calling.

### `409 Conflict` on permit creation
You reused an `Idempotency-Key` with different payload. Use a fresh UUID for each distinct request, or replay the exact same payload.

### Errors on MCP invocation
`POST /mcp/messages` returns these as JSON-RPC errors in an HTTP `200`; the deprecated REST route (`POST /mcp/tools/{service_id}/invoke`) answers with the REST status shown.
- `idempotency_key_reused` (`-32603`; REST `400`): the `idempotency_key` in `mcpContext` was already used for a different payload. Use a fresh key per distinct invocation (not the same as the permit's idempotency key).
- `idempotency_key_required` (`-32003`; REST `400`): governed calls need an `idempotency_key` in `mcpContext`.
- `Tool not found: <name>` (`-32001`; REST `404`): confirm the tool name exists in `/mcp/tools.json`.
- `Missing wallet_id in mcpContext` (`-32602`): A wallet bound key may omit `wallet_id`; a key without a bound wallet must supply it in `params.mcpContext`.
- REST `422 Unprocessable Entity`: the body failed schema validation (for example `name` is missing). The REST body spells the context `mcp_context`, not `mcpContext`.

### `delivery_uncertain` receipt
The gateway claimed a send but has no trustworthy terminal result. Delivery and the downstream effect may or may not have occurred; this receipt does not prove upstream acceptance or execution. Under the configured conservative policy, the charge stands and replaying the same key returns the uncertain outcome without redispatching. Do not retry automatically. Reconcile against authoritative downstream state before making a new attempt. See [docs/partner-first-tool-runbook.md](docs/partner-first-tool-runbook.md).

If the upstream exposes a status endpoint, a deployment can register an optional per-origin status lookup. Call `register_upstream_status_lookup(origin, lookup)` from `app.services.mcp_dispatch_reconciliation` in your own startup code. There is no environment setting, and the gateway registers none by default, so out of the box nothing changes. The reconciler asks that lookup about a stale `dispatch_claimed` attempt *before* it writes `delivery_uncertain`, with a 5-second timeout per attempt. The outcomes:

- The provider confirms the call ran and returns its MCP result: the result goes through the same checks as a live response. A normal result gives a `success` receipt and the charge stands. An `isError` result gives a `failed_refunded` receipt (reason `upstream_returned_error`) and is refunded, as on the live path.
- The provider says the call never ran: you still get `delivery_uncertain` and no refund. A paused worker that holds the claim could still send, so this answer is logged as `mcp_dispatch_status_lookup_not_delivered_unfenced` for your manual reconciliation.
- The lookup is not registered, raises, times out, returns nothing, returns a malformed answer or a non-boolean `delivered`, or returns a result that fails the checks: you still get `delivery_uncertain`.

The gateway never re-sends the call on this path. Your lookup is your own code, so keep it read-only. The lookup must be an `async def` function, an object with an `async def __call__`, or a `functools.partial` of either. A plain function raises `TypeError` at registration, because the timeout cannot interrupt synchronous code. A result whose `structuredContent` does not match the tool's `outputSchema` stays `delivery_uncertain`, as on the live path. The gateway never touches an attempt that already has a signed `delivery_uncertain` receipt, so those still need manual reconciliation. For action-permit calls, the query's `forwarded_idempotency_key` is `None` because the native key sent upstream is not recorded, so match on `invocation_id`. The cleanup log field `dispatch_status_resolved` counts the attempts the lookup resolved.

---

## Examples fail

### `ModuleNotFoundError: No module named 'b2a_sdk'`
Install the SDK in editable mode from the repo root:
```bash
python -m pip install -e '../sdk/python[dev]'
```

### `404 wallet_not_found` in `dry_run_example.py`
The example creates its own wallet — do not edit the script to use a hardcoded wallet ID. Just run it as-is with the API running.

### `404` when running examples against a fresh `make prove-trust-plane` database
`make prove-trust-plane` uses a throwaway SQLite database. The examples need a running server with `ENABLE_PROOF_SURFACES=true`. Start the server separately:
```bash
ENABLE_PROOF_SURFACES=true uvicorn app.main:app
```

---

## Database and migrations

### `pytest` fails with `asyncpg` or PostgreSQL errors
The PostgreSQL concurrency and crash-recovery tests need a real PostgreSQL database. Set `DATABASE_URL` to an empty, dedicated test database:
```bash
export DATABASE_URL=postgresql+asyncpg://user:pass@localhost/b2a_test
```
SQLite-only tests will still pass; PostgreSQL-specific tests skip if unavailable.

### Crash-recovery test hangs or fails
`make prove-crash-recovery` needs a **dedicated, empty** PostgreSQL database. It intentionally kills worker processes. Do not point it at a shared or production database.

---

## Still stuck?

- Read [docs/golden-path.md](docs/golden-path.md) for the complete wallet-scoped HTTP flow.
- Read [docs/partner-first-tool-runbook.md](docs/partner-first-tool-runbook.md) to connect one real upstream tool.
- Check [docs/PROOF_MATRIX.md](docs/PROOF_MATRIX.md) for what each proof command asserts (and what it does not).
- Review [CONTRIBUTING.md](../CONTRIBUTING.md) for development setup and `make` targets.

If you believe you've found a bug, use the [bug report template](https://github.com/PetrefiedThunder/agent-middleware/issues/new?template=bug_report.yml). For security issues, see [SECURITY.md](../SECURITY.md).
