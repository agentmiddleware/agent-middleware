# Threat Model

This threat model covers the production-beta surface of Agent Middleware API:
authentication, wallet billing, MCP/tool invocation, telemetry, IoT bridging,
and sandboxed execution.

## Scope

"Current Controls" below describe the routers a production-posture boot
mounts, chiefly `CORE_TRUST_ROUTERS` in `app/main.py`. The telemetry, IoT,
sandbox, media, comms, protocol, factory, broadcast, RTaaS, AI, and AWI
routers, among others, are `PROOF_SURFACE_ROUTERS` — frozen scaffolding
mounted only with `ENABLE_PROOF_SURFACES=true`, which production-like boots
refuse (`app/core/trust_mode.py`). Those controls are not claimed for them,
and known gaps remain there, for example:

- Several proof-surface handlers carry only the router-level API-key check,
  with no per-resource wallet ownership check — e.g. `DELETE
  /v1/iot/devices/{device_id}` and `POST
  /v1/telemetry/anomalies/{anomaly_id}/auto-pr`.
- `GET /v1/sandbox/environments` and `GET /v1/sandbox/environments/{env_id}`
  are unauthenticated.
- The outbound-URL guard (`app/core/url_guard.py`) that screens agent-supplied
  URLs for the AWI browser bridge and the behavioral sandbox proxy skips its
  loopback/RFC1918/link-local checks under `ALLOW_PRIVATE_NETWORK_TARGETS=true`;
  production-like boots refuse that flag.

See [PROOF_SURFACES.md](PROOF_SURFACES.md) for the freeze list and
[SECURITY_LIMITATIONS.md](../SECURITY_LIMITATIONS.md) for the production
boot refusals and the "Rate Limit Posture" section.

## Security Objectives

- Prevent unauthenticated access to state-changing or execution endpoints.
- Prevent one tenant or wallet from reading or mutating another wallet.
- Preserve ledger integrity for credits, debits, transfers, top-ups, and dry-run
  commits.
- Ensure governed MCP actions are authorized by signed permits, protected from
  replay, bound to a wallet budget, and represented by signed receipts.
- Preserve tamper-evident audit history for wallet-scoped action review.
- Ensure API-key creation, rotation, revocation, and logs are wallet-scoped.
- Keep untrusted code or tool execution from compromising the host.
- Preserve enough audit history to investigate security and billing incidents.

## Assets

- Env bootstrap/admin API keys from `VALID_API_KEYS`.
- DB-created API keys and their hashes.
- Wallet balances, ledger entries, KYC state, and Stripe payment references.
- Ed25519 public signing keys, permit records, receipt records, idempotency
  records, and audit-chain hashes.
- MCP tool registry and service invocation inputs/outputs.
- Sandbox code, tool payloads, execution output, and environment state.
- Telemetry, anomaly reports, generated patches, and agent session history.
- IoT device metadata, ACLs, broker URLs, and audit events.

## Trust Boundaries

- External callers to FastAPI endpoints.
- Env bootstrap/admin keys versus DB-backed wallet keys.
- Agent wallet keys versus sponsor wallet keys.
- API server versus database.
- API server versus Redis/cache.
- API server versus Stripe and KYC provider.
- API server versus MCP tools, IoT brokers, browser automation, and sandbox
  subprocesses.

## Current Controls

- API-key dependency protects normal authenticated routes.
- Env keys act as bootstrap/admin credentials and are compared with
  `hmac.compare_digest`.
- DB-created keys are stored as SHA-256 hashes, compared constant-time
  (`APIKeyService.validate_key`), and can be revoked or expired.
- Wallet-scoped auth context enforces exact wallet ownership for DB keys.
- API-key management requires bootstrap/admin or exact wallet access.
- Behavioral sandbox routes (`/v1/sandbox/behavioral`, a proof surface)
  require authentication.
- MCP invoke uses shared auth context and checks wallet access.
- Governed MCP invoke validates signed permits and idempotency before billing.
- Signed receipts bind governed MCP attempts to permit, ledger, and audit IDs.
- Audit events are signed and hash-linked per wallet.
- Alembic migrations cover wallet/API-key/KYC/service registry schema drift.
- Tests cover revoked/expired DB keys, cross-wallet denial, sandbox auth, MCP
  header auth, and migration creation.

## Primary Threats And Required Mitigations

### Cross-Wallet Access

Threat: A tenant learns another wallet ID and reads balance, ledger, keys, or
mutates funds.

Required controls:
- Keep exact-wallet checks on all wallet-bound routes.
- Treat destination wallet IDs in transfers as public identifiers, but require
  source wallet access.
- Add regression tests for every new wallet-bound endpoint.

### Bootstrap Key Leakage

Threat: An env bootstrap/admin key leaks and can operate across all wallets.

Required controls:
- Use env keys only for provisioning and emergency operations.
- Rotate env keys outside the database.
- Store env keys in the hosting platform secret manager only.
- Add audit logs that distinguish `env` versus `db` auth source.

### API-Key Lifecycle Bypass

Threat: Revoked or expired DB keys continue to work, or a tenant rotates another
tenant's keys.

Required controls:
- Runtime auth must always check DB key status and expiration.
- API-key management must require exact wallet access or bootstrap/admin.
- Rotation logs must be immutable enough for incident review.

### Sandbox Escape Or Host Abuse

Threat: A caller submits code that executes on the API host.

Required controls:
- Keep sandbox endpoints authenticated.
- Do not expose arbitrary-code execution publicly without stronger isolation.
- Move from server subprocess execution to a container, VM, Firecracker, or
  managed sandbox boundary before public production use.
- Apply CPU, memory, time, network, filesystem, and package-install limits.
- Log sandbox creation, execution, deletion, and errors.

### Ledger Tampering Or Double Spend

Threat: Race conditions or direct mutation corrupt wallet balances or ledger.

Required controls:
- Use database transactions for debit/credit/transfer flows.
- Preserve immutable ledger entries for committed charges.
- Keep dry-run simulations separate from real ledger entries until commit.
- Maintain idempotency keys for Stripe payment intent handling.

### MCP Tool Abuse

Threat: A tool call bypasses auth, bills the wrong wallet, or invokes a service
outside expected constraints.

Required controls:
- MCP invoke must accept header auth through shared auth dependencies.
- Tool calls must require wallet context before billable execution.
- Governed tool calls must require a signed permit with wallet, key, tool,
  scope, budget, expiry, and nonce constraints.
- Governed tool calls must require an idempotency key to prevent double charge
  on retry.
- Denied and failed governed attempts should produce receipts when a valid
  permit was present.
- Registry tools should declare pricing, input schema, and owner wallet.
- Persistent tools should enforce source wallet billing before invocation.

### Agentic Workflow Injection And Tool Abuse

Threat: Untrusted GitHub issues, PRs, comments, webhook bodies, tool outputs,
prompt text, or generated scripts influence shell commands, browser automation,
billing decisions, authorization decisions, or receipt payloads.

Required controls:
- Treat all external workflow context as attacker-controlled.
- Do not let tool output mutate permit scope, wallet authority, billing amount,
  or receipt content without a separate policy decision.
- Keep AWI/browser automation and auto-PR generation outside the core trust
  plane until provenance, allowlists, escaping, and abuse tests exist.
- Never execute untrusted code or generated patches without explicit sandboxing
  and human review.

### IoT ACL Bypass

Threat: A device credential or topic wildcard permits access to another device's
sensitive topic.

Required controls:
- Keep deny-by-default topic ACLs.
- Prefer exact or narrowly wildcarded ACLs.
- Persist device registration and audit events.
- Test cross-device topic denial.

## Production Launch Gates

- `pytest -q` passes.
- `ruff check app/ tests/` passes.
- `mypy app/` passes under the configured baseline.
- Alembic upgrade from empty DB succeeds.
- Golden path in `docs/golden-path.md` works against local and hosted demo.
- Sandbox limitation is visible in docs and product copy.
- Incident response path in `SECURITY.md` is current.

## Open Security Work

- Add structured audit events for every auth decision on sensitive routes.
- Add admin-only audit export endpoints.
- Replace subprocess sandbox execution with a hardened isolation provider.
- Extend `tests/test_route_auth_inventory.py` beyond the `/v1/permits`,
  `/v1/receipts`, `/v1/audit`, and `/mcp` prefixes it checks today.
- Add ownership tests whenever new wallet-bound resources are introduced.
