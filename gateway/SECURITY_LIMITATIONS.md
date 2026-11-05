# Security Limitations

This repository is not yet compliance-grade autonomous economic actor
infrastructure.

## Current Trust Boundary

The implemented trust boundary is governed MCP tool invocation. Other modules
are proof surfaces unless they consume the same permit, receipt, idempotency,
and audit-chain primitives.

The remote pilot supports one operator-configured HTTPS Streamable HTTP MCP
origin and one exact tool. Wallet checks provide application-layer isolation;
there is no row-level-security or public multi-tenant security claim.

The supported design-partner deployment is one vendor-managed Railway project
per customer, with dedicated API, PostgreSQL, Redis, domain, signing material,
and administrator credentials. It is restricted to synthetic or redacted,
low-sensitivity data: PHI, PCI data, regulated production records, and
sensitive tool arguments are out of scope. Shared SaaS, customer-VPC/BYOC, and
customer-operated on-premises deployments are not supported in this pilot.

High-risk AWI HTTP routes (`/v1/awi/execute`, passkey, rag index/query,
`dom/sync`) also require `X-Permit-Id` + `Idempotency-Key`; successful metered
calls emit receipts. Their current abort paths do not have the durable
dispatch/charge linkage needed to mint trustworthy denial receipts, so use the
MCP gateway for portable denial evidence.

## Not Yet Solved (Deferred By Design)

Keep these out of the wedge until a design partner requires them:

- No external KMS integration is implemented.
- No settlement, dispute, or compliance reporting workflow is implemented.
- Receipt signatures can be verified offline by any third party
  (`/v1/receipts/{id}/portable` plus the unauthenticated
  `/.well-known/trust-keys.json`), but no external transparency log exists. A
  receipt proves what happened, never what did not: absence of a receipt is
  not evidence that no action occurred.
- Offline verification trusts the issuing origin for key distribution. Keys
  arrive over TLS from the same origin being audited, so a compromised origin
  can serve a key set that validates forged receipts. Out-of-band key pinning
  is not implemented and is frozen until a named partner cannot trust this
  origin for keys **and** that independently trusted key source is named
  (Fulcio/TUF, pinned SPIFFE, or an out-of-band pin) and verification uses
  that root. Wrapping origin-signed claims under Sigstore or SPIFFE is not
  that slice. See [`WEDGE.md`](WEDGE.md) § What To Freeze.
- Wrap-and-anchor evidence is not implemented. The intended later
  composition is anchoring/publication: wrap today's receipt claims as an
  in-toto/DSSE predicate and submit the statement hash to Rekor or a
  SCITT log. That does not provision independently trusted issuer keys.
  Signing the wrap under a Sigstore or SPIFFE gateway identity still
  authenticates the inner claims with this origin unless the slice names
  a trust root that is not this origin (Fulcio/TUF, pinned SPIFFE, or
  an out-of-band pin) and verification uses that root. Unfreeze for
  anchoring when a named partner needs an independently timestamped
  receipt hash. Unfreeze for key distribution only when that partner
  cannot trust this origin for keys and that independently trusted key
  source is named. This freeze does not specify `receipt_id` as a log
  subject, conflict detection, or trusted-checkpoint consistency/witness
  evidence, so it does not claim non-equivocation. A log still does not
  prove an action did not occur, and it does not replace the ledger. See
  [`WEDGE.md`](WEDGE.md) § What To Freeze.
- A new portable permit envelope and a portable audit-chain API are not
  implemented. Receipts already export (`GET /v1/receipts/{id}/portable`).
  Permits already return signed fields plus `signature` and `key_id` from
  `GET /v1/permits/{id}`, and SDK `LocalPermitValidator` verifies those
  bytes without importing the application. What is frozen is a receipt-style
  permit envelope around that existing verifier, and a public export of
  audit-chain signed bytes plus an SDK verifier that imports none of the
  application. Unfreeze the permit envelope only when a named partner cannot
  use the shipped permit verifier. Unfreeze the audit export only when that
  partner cannot trust this origin's first-party audit verification
  (`/v1/audit/verify-chain`, `GET /v1/receipts/{id}/evidence`). That slice
  is not wrap-and-anchor, not out-of-band key distribution, and not an
  audit-log product. See [`WEDGE.md`](WEDGE.md) § What To Freeze.
- Audit chains are wallet-scoped, but database administrators can still delete
  rows unless append-only storage or external anchoring is added.
- Multi-protocol governed adapters beyond MCP are not implemented (MCP only).
- A timeout or disconnect after the durable dispatch checkpoint is inherently
  ambiguous. The gateway retains the debit, signs `delivery_uncertain`, never
  redispatches automatically, and requires operator/upstream reconciliation.
- The gateway's at-most-once dispatch and debit per key does not make a
  remote side effect happen only once unless the upstream honors the forwarded idempotency key.
- The configured upstream path atomically enforces the permit's `max_credits`
  ceiling and `max_calls_per_tool` counters (supported since 8c95229, PR #476).
  It rejects permits carrying `aggregate_value_cap` before reservation or
  dispatch because it does not yet implement an atomic remote counter-and-release
  lifecycle for that field. On the local path, `aggregate_value_cap` is a
  predicate of the same
  guarded reservation `UPDATE` as `max_credits`, against `spent_credits`
  (settled charges plus in-flight reservations) floored to the permit's
  receipt total. The configured upstream path still rejects it before
  reservation or dispatch.
- URL validation rejects unsafe destinations and redirects, then pins one
  validated resolved address through the later connection while preserving the
  configured HTTP Host and TLS SNI. Production should still enforce a network
  egress allowlist/proxy for the single partner origin as defense in depth.
- The upstream limit is enforced on the streamed identity-encoded HTTP body,
  including the JSON-RPC envelope, before buffering and parsing. Retained
  decoded discovery and result payloads are bounded again after validation.
- Inbound request bodies are bounded on every route by
  `MAX_REQUEST_BODY_BYTES` (1 MiB default), refused with a 413 before the rate
  limiter or any handler buffers them. An oversized declared `Content-Length`
  is rejected without reading the body; an understated one is caught by
  measuring the stream. The opt-in MCP transports keep their own tighter caps
  (256 KiB public, 64 KiB partner). This bounds per-request memory, not
  aggregate concurrency — a request-count/connection limit at the edge is
  still the operator's job.
- No public uptime SLA, compliance scope, RTO/RPO, tenant-isolation guarantee,
  or immutable-ledger claim is made.
- Sandbox and AWI/browser automation are not production isolation boundaries.
- Auto-PR and agentic workflow automation must treat GitHub issues, PRs,
  comments, webhook bodies, tool outputs, and generated scripts as untrusted.

## Required Production Posture

Operator deploy path and variable checklist:
the hosted-service deploy runbook (not in this repository) (`railway up` from this
repo; do not Redeploy from GitHub source).

- `TRUST_MODE_ENABLED=true` and `ALLOW_LEGACY_UNPERMITTED_MCP=false` are the
  shipped defaults. A production-like environment cannot boot under any
  permissive combination — `app.core.trust_mode.validate_trust_mode_guardrails`
  refuses to start. Local/dev/test deployments that need legacy behavior must
  set both env vars explicitly; the startup log emits a `trust_mode_permissive`
  warning so the opt-out is loud.
- Configure `TRUST_SIGNING_PRIVATE_KEY_B64` from a secret manager or KMS-backed
  runtime injection.
- Production-like boots also refuse `DEBUG=true`, `WEBAUTHN_ALLOW_MOCK=true`,
  `ENABLE_PROOF_SURFACES=true`, `ENABLE_PUBLIC_MCP_ENDPOINT=true`,
  `ALLOW_PRIVATE_NETWORK_TARGETS=true` (it disables the outbound-URL guard's
  private-address checks), `ALLOW_UNSAFE_HOST_PYTHON_SANDBOX=true`, a
  host `BEHAVIORAL_SANDBOX_PYTHON_BACKEND` (`unsafe_host`/`host`), and the
  local/CI dogfood tools `ENABLE_DOGFOOD_TOOL=true` /
  `ENABLE_DOGFOOD_SECOND_TOOL=true` (they register executable `partner.*`
  tools on the core MCP path). Set
  `ENABLE_PROOF_SURFACES=false` so only core trust routers and MCP are mounted.
  Leave proof surfaces frozen unless a partner demo explicitly needs them.
  Anonymous MCP (`POST /mcp/public`) is local-only; receipt verification stays
  on `/.well-known/trust-keys.json`.
- On production-like boots, tool catalogs (`/mcp/tools.json`, `/mcp/tools`,
  `/v1/discover`, `/.well-known/mcp/tools.json`) require the same credentials
  as invoke. A stranger must not learn which tools exist. Local-compatible
  environments keep anonymous discovery for quickstart.
- Set `PUBLIC_URL` to the public HTTPS API origin (Railway host or custom
  domain). Agents and `/llm.txt` use it; do not leave production pointing at
  localhost.
- Set `VALID_API_KEYS` only via host secrets / Railway service variables. The
  API-only `.railway/railway.ts` graph preserves the key
  name without owning or exposing its value; every configured API key uses
  `preserve()`. Never commit real keys or use the placeholder `change-me` in
  production.
- Disable or isolate proof surfaces that execute code, drive browsers, generate
  patches, crawl external URLs, or touch third-party systems.
- When `REDIS_URL` is set, production-like environments fail closed on Redis
  rate-limiter outage (HTTP 503) instead of silently using per-process memory.
  `/health/dependencies` exposes `runtime_degradation` when any configured
  backend has fallen back to in-memory.
- A production-like deployment that never sets `REDIS_URL` still boots, and its
  rate limiter counts in process memory: each worker and replica keeps its own
  counters, so every limit is multiplied by the process count, and nothing
  fails closed because there is no shared backend to lose. The runtime
  reports this instead of looking healthy: `/health/dependencies` shows
  `runtime_degradation.degraded` as `true` with the stable reason
  `rate_limiter_redis_not_configured`, and the app logs one startup warning
  with the same reason per process. The HTTP status of `/health/dependencies`
  and the set of configurations that boot are unchanged. Requiring
  `REDIS_URL` at boot would be a breaking change for single-process
  deployments, so it is left as a separate, announced decision for the
  maintainers.
- All Phase 9 AWI MCP tools always require signed permits (`requirePermit` in
  `/mcp/tools.json`), even if legacy unpermitted MCP is enabled. Those stubs
  are not wedge product; Phase 2 drops them from discovery when proof
  surfaces are off — see partner inventory note in
  [`DESIGN_PARTNER_GUIDE.md`](DESIGN_PARTNER_GUIDE.md#mcp-discovery-gate-phase-2).
- Run migrations (`alembic upgrade head` or `RUN_MIGRATIONS_ON_START=true` on
  the Docker entrypoint) instead of relying on
  `SQLModel.metadata.create_all`. Production-like boots skip `create_all`,
  verify required trust tables, and fail closed if the schema is missing.
  `create_all` remains only for ephemeral non-production SQLite (tests/local).
- Keep CI trust invariant tests required before merge. CI also runs a
  `production_trust` subset with production-like trust flags.

## CORS Posture

The default `CORS_ORIGINS` is empty, granting no cross-origin access. An
explicitly configured wildcard remains a deliberate posture, not an
oversight:

- Every authenticated route takes explicit header credentials (`X-API-Key`
  or `Authorization: Bearer`), never cookies or other ambient browser
  credentials, so there is nothing a cross-origin page can ride.
- `app.main.add_cors_middleware` refuses to pair a wildcard (or an empty
  list) with credentialed CORS: under `*` or with no origins configured,
  `Access-Control-Allow-Credentials` is never emitted. An explicit origin
  list is required before credentialed cross-origin requests are possible
  at all.
- Production-like boots refuse to start with `*` in `CORS_ORIGINS` (see the
  trust-mode guardrails).
- What an explicitly configured wildcard actually grants is cross-origin
  *reads of remaining public surfaces*
  (`/.well-known/trust-keys.json`, `/.well-known/jwks.json`,
  `/.well-known/agent.json`, `/health*`, `/llms.txt`, `/openapi.json`) — not
  tool catalogs, which require a key on production-like boots.
- The one route that is unauthenticated yet returns a secret
  (`/v1/dev-keys/self-provision`, local-only) independently rejects
  cross-origin browser calls by `Origin` check, and production-like
  environments refuse to boot with it enabled.

Operators who put a credentialed browser app in front of this API must set
`CORS_ORIGINS` to an explicit comma-separated origin list. Startup logs
`cors_wildcard_active` whenever the wildcard posture is in effect.

## Rate Limit Posture

The application-layer limiter is present on auth-gated routes. A burst of
a few dozen requests drawing zero `429`s is the intended threshold, not a
missing control.

- Default and production value: `RATE_LIMIT_PER_MINUTE=120`, 60-second window,
  no burst allowance. The 121st counted request in that window is the first
  that returns `429` with `Retry-After`.
- **Window accounting differs by backend, so the published contract states the
  budget and the window length, not an algorithm.** The shared Redis limiter
  counts fixed 60-second buckets, so a caller straddling a bucket boundary can
  land up to twice the budget inside one arbitrary 60-second span. The
  in-memory limiter counts a rolling 60 seconds and is strictly tighter. It
  counts whenever `REDIS_URL` is unset, in any environment. With `REDIS_URL`
  set, a production-like environment never falls back to it and fails closed
  instead; other environments fall back to it when Redis is unreachable.
  `rate_limits.window_accounting` in discovery names the difference between the
  two algorithms. `rate_limits.counter_scope` reports the configured scope:
  `shared_redis` when `REDIS_URL` is set, otherwise `per_process`. It is
  configuration, not live state. With `REDIS_URL` set but unreachable in a
  non-production-like environment, the limiter counts in memory while the field
  still says `shared_redis`; fallbacks that have happened show under
  `runtime_degradation` on `/health/dependencies`.
- **Counters are shared only with Redis.** Every bound below (per key,
  anonymous, readiness and pre-auth) is per process when `REDIS_URL` is unset,
  so with several workers or replicas the effective ceiling is the stated
  ceiling times the process count. Words such as deployment-wide and shared
  below describe the Redis-backed limiter.
- **Authenticated:** one bucket per `X-API-Key` value. The bucket is named by
  a domain-separated SHA-256 digest of the value, never the key itself, so the
  limiter's Redis key names hold neither live API keys nor the `key_hash` the
  key table stores for them.
- **No key:** one anonymous bucket per observed client address, so one
  stranger can only spend their own budget and cannot starve other clients,
  `/.well-known/trust-keys.json` or `/v1/discover`. An anonymous backstop at
  `RATE_LIMIT_ANONYMOUS_GLOBAL_MULTIPLIER` times the per-client limit
  (default 20) still brakes a flood spread over many addresses. It is
  deployment-wide when the counters are shared through Redis; without
  `REDIS_URL` each worker or replica has its own backstop. When it trips,
  anonymous callers get 429 until the window resets, and callers with a key
  are unaffected. Callers that share an egress
  address (a corporate NAT or proxy) still share one per-client anonymous
  bucket. Behind a reverse proxy or load balancer, every caller shares the
  proxy's address until `FORWARDED_ALLOW_IPS` names it; see the forwarded
  address contract under unaccepted credentials below.
- **Readiness:** `/health/ready` has its own per-client and route-wide
  budgets and ignores credential headers, so traffic on other routes, anonymous
  or keyed, cannot consume the readiness budget. Readiness requests themselves
  can still exhaust the route-wide readiness bucket (ten times the per-client
  limit), and probes then receive 429 until the window resets. It is limited
  rather than exempt because it runs state store, MQTT and database checks.
  `/health/dependencies` stays on the ordinary limits because it runs live
  Stripe and Sentinel probes.
- **Namespaced buckets:** per-key and anonymous bucket names, and so their
  Redis keys, carry `STATE_NAMESPACE` and the public URL (or app name), so two
  deployments that share one Redis do not share budgets.
- **Unaccepted credentials:** the per-key bucket is selected from a
  caller-supplied header before the key has been verified, so every request
  whose credentials the app does not accept is additionally charged to one
  shared per-client bucket at ten times the per-key limit. Rotating a fresh
  `X-API-Key` per request therefore buys no extra budget; the client is bounded
  by that bucket no matter how many key values it invents.
  - **Not accepted, rather than refused.** A route that never authenticates
    — public discovery, `/health/dependencies`, a `404` — never refuses an
    invented key either, so a ceiling charged only on refusal left those
    routes with a fresh budget per invented value. The reservation is handed
    back only when the auth dependency (`get_auth_context`) accepted the
    credentials during the request; every other request that ran keeps it,
    including one that raised on its way through. That includes a valid key
    sent to a public route, which is harmless: one key value hits its own
    per-key limit, a tenth of this bucket, first.
  - **Refused means `401` *or* `403`.** An unknown but well-formed key — what
    a rotating caller actually sends — is answered `403 invalid_api_key`, not
    `401`; both are charged even if another credential on the same request
    was accepted.
  - **A denial is not a refusal.** An authenticated caller denied on scope
    (`wallet_access_denied`, `insufficient_scope`, an IGA decision) is
    ordinary governed-loop traffic and is never charged here. The two are told
    apart by the auth layer itself: it records the acceptance, and marks a
    refusal with an internal header stripped before the response leaves the
    innermost middleware.
  - **The budget is reserved before the request runs**, and handed back only
    if the credentials were accepted. Reading the bucket and charging it after
    the response would let every request already in flight pass the same read,
    so the ceiling would only bound callers who arrive one request at a time.
  - A request whose credentials the app accepts leaves the bucket exactly as
    it found it. Client identity is the ingress peer address (Railway's
    `X-Real-IP` only where the platform marker is present, per the public-MCP
    rule above), so callers sharing one egress address share that bucket, and
    callers spread across many source addresses get one such bucket each. A
    distributed flood is still the edge's job.
  - **Forwarded address contract.** The client address is the connection peer
    as the ASGI server reports it. `docker_entrypoint.sh` starts uvicorn
    without `--forwarded-allow-ips`, so uvicorn trusts forwarding headers only
    from `127.0.0.1` and `::1` (its default, or the `FORWARDED_ALLOW_IPS`
    environment variable when set). Behind a reverse proxy, load balancer or
    Kubernetes ingress that is not on loopback, every caller therefore shares
    the proxy's address, and the per-address buckets above (anonymous,
    readiness and this one) become one deployment-wide bucket for all of them.
    Behind a shared address, a caller with a valid key of any kind can then
    receive this bucket's `429` while other callers send invalid keys. No key
    class is exempt from it.
    To count real client addresses, set `FORWARDED_ALLOW_IPS` to the exact
    address or addresses of your ingress, or to a CIDR range that covers it
    (uvicorn accepts both, comma separated). Use the narrowest CIDR that
    covers only your ingress peers. A catch-all range (`0.0.0.0/0` or
    `::/0`) trusts every peer of that address family, and for entries of that
    family uvicorn then takes the left-most one, exactly as it does for `*`,
    so do not use one. Uvicorn reads the client from `X-Forwarded-For` only on
    connections from the trusted addresses, and it takes the right-most entry
    that is not itself a trusted address. **That entry is the address the
    ingress observed only if the ingress overwrites `X-Forwarded-For` with the
    client address it saw, or appends that address to the inbound chain.** An
    ingress that forwards the inbound header unchanged lets the caller choose
    the address even when `FORWARDED_ALLOW_IPS` names that exact ingress, so do
    not trust such an ingress for address-based limiting. **Do not set it to
    `*`.** With `*`
    uvicorn trusts every connection and takes the left-most
    `X-Forwarded-For` entry. A caller controls that entry whenever the ingress
    appends to the header instead of replacing it, so any caller can send its
    own value and receive a fresh bucket per request. That holds even when the
    application is reachable only through the ingress.
    On the standard container launch path, set `FORWARDED_ALLOW_IPS` in the
    process environment of the container that runs uvicorn. If a command
    override passes uvicorn's `--forwarded-allow-ips` option instead, that
    option takes precedence over the environment variable. The uvicorn
    command line also reads `UVICORN_FORWARDED_ALLOW_IPS` as the value of
    that option, so it has the same effect and the same precedence, and it
    must not be set to `*` either. The gateway application does not read
    `FORWARDED_ALLOW_IPS` from `.env` itself. A `.env` file reaches uvicorn only
    when whatever launches the container loads it into the process
    environment. The Compose file in this repository does that
    (`env_file: .env`), so on that stack a value in `.env` applies, and the
    same rules hold for it: never `*`, `0.0.0.0/0` or `::/0`. Elsewhere a
    `.env` file the launcher does not load has no effect. **No setting is
    needed on Railway.** There the gateway reads the single canonical `X-Real-IP` value, as described
    above, under Railway's platform marker. The setting applies to other
    ingress. This document does not assume any other platform sets `X-Real-IP`.
    The gateway does not refuse to start without a trusted-proxy setting.
    Whether it should is a maintainer decision.
- **Counted responses** — including `401`s — carry `X-RateLimit-Limit`,
  `X-RateLimit-Remaining`, and `X-RateLimit-Reset`. Remaining dropping from
  119 toward 80 with no `429` means the budget has not been reached.
- **Exempt paths** (no count, no `429`): `/`, `/health`, `/docs`, `/redoc`,
  `/openapi.json`, `/.well-known/agent.json`, `/llms.txt`, and the served
  markdown docs. `/health/dependencies` is counted.
- This is a request-count ceiling, not a connection or bandwidth limit. An
  edge request-count/connection cap remains the operator's job, as noted
  under inbound body bounding above.
- With `REDIS_URL` set, a Redis outage in production-like environments fails
  closed (`503`), never silently wider than declared. Without `REDIS_URL`
  there is no shared limiter to lose: each worker or replica enforces the
  declared limit on its own, so the effective limit is the declared limit times
  the process count, and a production-like deployment is reported as degraded
  (see the startup posture notes above).

`GET /` and `GET /v1/discover` both publish this as `rate_limits`.

## Response Hardening Headers

Every response gets `X-Content-Type-Options: nosniff`, `X-Frame-Options:
SAMEORIGIN`, `Referrer-Policy: strict-origin-when-cross-origin`, and a
`Content-Security-Policy`. JSON is `default-src 'none'`. First-party HTML
(dashboard, approval cards) allows inline CSS and no scripts. `/docs` and
`/redoc` additionally allow jsDelivr plus an inline boot script, a `blob:` Web
Worker, and Google Fonts, because that is how FastAPI's stock Swagger UI /
ReDoc load. HSTS (`max-age=63072000;
includeSubDomains`, no `preload`) is emitted only on requests that arrived
over TLS. Tenant-sensitive paths (`/v1/*` except `/v1/discover`, `/mcp`
except the public tools manifest) send `Cache-Control: no-store`. Public
discovery is left cacheable so agents can keep OpenAPI and well-known
documents.

## One Auth Story, One Invoke Story (Dormant Surfaces)

The wedge contract is **send the API key** (`X-API-Key`). Surfaces that told
a second story are unmounted in production and absent from the public
OpenAPI contract (they mount only with `ENABLE_PROOF_SURFACES=true`, which
production-like boots refuse):

- `/v1/auth/token|refresh|revoke` (JWT exchange) — `app.core.auth` still
  *validates* Bearer JWTs, but nothing can mint one while the router is
  unmounted, so the key header is the only production auth path.
- `/v1/kyc/*` (Stripe Identity), `/v1/planner/optimize`, and the billing
  expansion surfaces (child/swarm wallets, transfers, top-ups, marketplace,
  velocity status, dry-run sandbox) — real code, dormant demand; see
  `DORMANT_TRUST_ROUTERS` in `app/main.py`.
- `/v1/webhooks/stripe*` mount only when Stripe is actually configured.
- `/v1/dev-keys/self-provision` stays runtime-gated by its own flag and is
  advertised in the schema only when that flag is on (never in production).

The legacy invoke entry points `POST /mcp/messages` and
`POST /mcp/tools/{id}/invoke` remain mounted for existing clients and the
local proof scripts but are marked `deprecated` in the spec; the standard
MCP Streamable HTTP endpoint at `POST /mcp` is the supported path. All entry
points run the same governed permit→meter→receipt path.

## Public Health Reporting

With proof surfaces unmounted, the unauthenticated `/health/dependencies`
payload reports only what the wedge runs on (postgres, redis, signing key,
upstream MCP, version + commit SHA, environment posture). Per-service
simulation flags and proof-surface dependency probes are not published
there; they appear in the startup log (`phase="runtime_posture"`) and on
instances that mount proof surfaces, where they describe live routes.
