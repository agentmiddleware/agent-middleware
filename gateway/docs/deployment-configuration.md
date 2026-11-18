# Deployment Configuration

This document covers configuration for production and production-like deployments of the Agent Middleware gateway, including reverse proxy integration and database migration settings.

## Reverse Proxy and Client IP

The gateway uses the client IP address for per-client rate limiting, logging, and telemetry. Behind a reverse proxy, load balancer, or Kubernetes ingress, every client shares the proxy's address unless you configure the gateway to read the real client IP from forwarding headers.

### FORWARDED_ALLOW_IPS

The gateway runs uvicorn, which reads the client IP from `X-Forwarded-For` headers only when the connection comes from a trusted address. You configure that trust with the `FORWARDED_ALLOW_IPS` environment variable (read by uvicorn, not by the gateway application itself).

**Default behavior** (when `FORWARDED_ALLOW_IPS` is unset):

Uvicorn trusts only loopback addresses (`127.0.0.1` and `::1`). Behind a reverse proxy that is not on loopback, every client shares the proxy's address, and:

- Per-client rate limit buckets become deployment-wide (all clients share one bucket)
- A flood of invalid keys from one client can lock out valid keys from other clients at the same address
- Logs and telemetry show the proxy's address instead of the real client

**What it does**:

When `FORWARDED_ALLOW_IPS` names an exact address or CIDR range, uvicorn trusts `X-Forwarded-For` headers from connections whose peer address matches that value. Uvicorn then uses the right-most entry in `X-Forwarded-For` that is not itself a trusted address as the client IP.

This only works correctly if your reverse proxy either:
- Overwrites the inbound `X-Forwarded-For` header with the client address it observed, or
- Appends the client address to the inbound chain

A proxy that forwards the inbound header unchanged lets the caller choose the address even when `FORWARDED_ALLOW_IPS` names that exact proxy.

**Recommended values**:

- **Direct to the internet** (no reverse proxy): leave `FORWARDED_ALLOW_IPS` unset.
- **Railway deployments**: leave `FORWARDED_ALLOW_IPS` unset. The gateway reads Railway's canonical `X-Real-IP` header under the platform marker (`RAILWAY_ENVIRONMENT_ID`), as implemented in `gateway/app/core/rate_limiter.py` (the `_client_id` function, around line 216).
- **Single reverse proxy or load balancer**: set `FORWARDED_ALLOW_IPS` to the exact IP address of the proxy (for example, `FORWARDED_ALLOW_IPS=10.0.0.50`).
- **Multiple proxy hops or a CIDR range**: set `FORWARDED_ALLOW_IPS` to the narrowest CIDR that covers only your ingress peers (for example, `FORWARDED_ALLOW_IPS=10.0.0.0/24` for proxies in that range).
- **Cloudflare or similar CDN**: set `FORWARDED_ALLOW_IPS` to Cloudflare's published IP ranges (updated periodically at https://www.cloudflare.com/ips/). Note that catch-all CIDRs like `0.0.0.0/0` or `::/0` trust every peer and are not recommended (see below).

**Never set to `*`, `0.0.0.0/0`, or `::/0`**:

These values trust every connection and take the left-most entry in `X-Forwarded-For`. When your proxy appends to the header (the common behavior), a caller controls that entry and can send a fresh value on every request to receive a fresh rate limit bucket per request. This breaks per-client rate limiting and allows a single client to bypass limits.

**How to set it**:

On the standard container launch path (`gateway/scripts/docker_entrypoint.sh`, line 27), the uvicorn command does not pass `--forwarded-allow-ips`, so uvicorn reads the value from the process environment. Set `FORWARDED_ALLOW_IPS` in the environment where uvicorn runs:

- **Docker Compose**: the provided `docker-compose.yml` loads `.env` via `env_file: .env`, so a value in `.env` applies. The `.env.example` file documents this at line 63.
- **Kubernetes**: set it in your Pod or Deployment spec under `env`.
- **Systemd or direct launch**: export it in the shell or service unit before running `docker_entrypoint.sh` or uvicorn.

A command override that passes uvicorn's `--forwarded-allow-ips` option takes precedence over the environment variable. The uvicorn command line also reads `UVICORN_FORWARDED_ALLOW_IPS` (the prefixed form) as the value of that option, so it has the same effect and the same precedence.

The gateway does not read `FORWARDED_ALLOW_IPS` from a `.env` file itself. That file reaches uvicorn only when the launcher (such as Docker Compose) loads it into the process environment.

**Verification**:

The forwarded address contract is tested in `gateway/tests/test_forwarded_address_contract.py`. Those tests pin the uvicorn behavior described above: the default trusts only loopback, an exact address or CIDR takes the right-most untrusted entry, and `*` or a catch-all CIDR takes the left-most entry.

**Related documentation**:

- `gateway/SECURITY_LIMITATIONS.md` (lines 307-353) documents the forwarded address contract in the context of rate limiting.
- `gateway/DESIGN_PARTNER_GUIDE.md` (lines 88-93 and 106-107) summarizes the setting for design partners.
- `gateway/.env.example` (lines 63-69) documents it as a comment in the example environment file.

## Database Migrations on Container Start

When `RUN_MIGRATIONS_ON_START=true` and `DATABASE_URL` points at a PostgreSQL database, the container runs Alembic migrations (`alembic upgrade head`) before starting uvicorn. This is documented in `gateway/scripts/docker_entrypoint.sh` (lines 1-18).

The script fails closed: setting `RUN_MIGRATIONS_ON_START=true` with an empty `DATABASE_URL` exits non-zero instead of starting uvicorn against an unmigrated database.

**When to use it**:

- Enable this on deployments where the gateway owns the database schema (single-tenant or per-service database).
- Do not enable it on shared databases where schema changes are controlled separately, or where multiple replicas starting concurrently would conflict.

**Alternatives**:

- Run migrations manually before deployment (`alembic upgrade head` in CI or an init container).
- Use a separate migration job or database provisioning step.

The shipped `.env.example` has this commented out with a note: "Required for production schema (create_all is disabled in production-like environments)."
