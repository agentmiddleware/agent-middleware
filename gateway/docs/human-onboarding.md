# Human onboarding — what to verify before trusting this API

> **Status: legacy proof-surface operator checklist.** This page includes
> simulated and dormant workloads that are outside the supported one-tool MCP
> pilot. For current evaluation, integration, security, and pilot material,
> start with the [documentation guide](README.md).

**Audience:** Human **operators** who deploy or secure this service. Autonomous
clients should **not** start here — use `GET /.well-known/agent.json`,
`GET /llms.txt`, and `GET /openapi.json` first (see `agent_first` in the
manifest; `/llm.txt` remains a legacy alias).

This API is **agent-first**: many endpoints look like normal SaaS, but several
product areas **default to simulation** (synthetic data) until real
integrations are wired. Use this page as a **human operator** checklist so you
do not mistake demos for production behavior.

## 1. “It runs” vs “it’s real”

**Risk:** Assuming oracle, red-team, IoT, media, RTaaS, telemetry PM, comms, or
content factory perform real external work when they are still simulated.

**Do this:**

- [ ] On a deployment with `ENABLE_PROOF_SURFACES=true`, call
      `GET /health/dependencies` and read `simulation_modes`. Any service with
      value `true` is using **simulation** behavior for that domain (see
      `app/core/runtime_mode.py`). A local deployment does not expose this map
      unless that flag is enabled.
- [ ] On the supported production posture (`ENABLE_PROOF_SURFACES=false`), use
      the startup log entry with `phase="runtime_posture"` and inspect the
      deployed `SIMULATION_MODE_*` configuration. The public dependency report
      intentionally omits proof-surface simulation flags.
- [ ] Regenerate and skim [Simulation & MCP honesty inventory](simulations-inventory.md)
      (`python scripts/generate_sim_inventory.py`) for a pillar × MCP tool matrix.
- [ ] Compare deployed configuration to `.env.example` (`SIMULATION_MODE_*`
      variables). Defaults in code treat simulation as **on** for those domains.
- [ ] If you need a real integration, set the corresponding flag to `false`
      **only after** the real backend is implemented and tested; otherwise you
      may hit `NotImplementedError` paths.

**Related:** Production beta roadmap (what
“credible beta” means).

## 2. Know which hat you are wearing

| Role | You are responsible for | Start here |
|------|-------------------------|------------|
| **Operator** | Hosting, secrets, DB, migrations, Stripe/KYC env, sandbox isolation | This doc + Railway deploy SOP + [Threat model](threat-model.md) |
| **Integrator** | Calling the API from code or agents, keys, wallet scope | [Golden path](golden-path.md) + OpenAPI `/docs` |
| **End user** | Often **none** — the designed “customer” may be an autonomous agent | Your product’s UX, if any |

If you are “just trying the product,” you are usually **integrator + partial
operator** (local or Docker/Railway).

## 3. Money, keys, and dry-run (rehearse once)

**Risk:** Accidental real charges, confused wallet boundaries, or agents using
overpowered keys.

**Do this:**

- [ ] Walk [Golden path: wallet-scoped agent tool call](golden-path.md)
      end-to-end with **bootstrap** vs **DB-issued** keys as documented.
- [ ] Confirm **cross-wallet denial** (`403`) with an agent key before relying on
      tenancy.
- [ ] Use **Stripe test mode** and test webhooks until you intentionally move to
      production billing.
- [ ] Use **dry-run** flows where available before committing charges.

## 4. Sandbox and untrusted code

**Risk:** Treating behavioral Python execution as “safe” because it is behind
auth.

**Do this:**

- [ ] Read the README section on **behavioral sandbox** (Docker vs host Python).
- [ ] Read **Sandbox execution** and **Tool invocation** sections in
      [Threat model](threat-model.md).
- [ ] Do **not** enable `ALLOW_UNSAFE_HOST_PYTHON_SANDBOX=true` outside local
      development.

## 5. Discovery surfaces must agree

**Risk:** Agents read `agent.json`, `llms.txt`, and MCP manifests and assume
capabilities that are simulated or undocumented.

**Do this:**

- [ ] Fetch and skim:
      - `GET /.well-known/agent.json`
      - `GET /v1/discover` (full capability index; includes the same `agent_first`
        block as the manifest — they must stay in sync)
      - `GET /llms.txt`
      - `GET /mcp/tools.json` (canonical MCP manifest from the MCP router)
- [ ] Optionally compare with `GET /.well-known/mcp/tools.json` (separate route;
      may differ — if in doubt, treat `/mcp/tools.json` as the primary tool
      discovery path used in examples).
- [ ] On a production-like deployment the tool catalogs (`/v1/discover`,
      `/mcp/tools.json`, `/mcp/tools`, `/.well-known/mcp/tools.json`) require
      the same credentials as invoke and answer `401` without them. Send an
      operator key (`X-API-Key`) to read them.
- [ ] Cross-check core capabilities against `/health/dependencies`. When proof
      surfaces are mounted, also inspect its `simulation_modes`; otherwise use
      the startup posture log and deployment configuration.

**Automation:** Run `scripts/human_preflight.sh` against your base URL (see
below).

## 6. Beta scope vs your expectations

**Risk:** Expecting general-purpose serverless compute, full marketplace
settlement, or public arbitrary-code execution without strong isolation.

**Do this:**

- [ ] Read **Non-goals for beta** and milestones in
      Production beta roadmap.
- [ ] Treat **audit export**, **stronger sandbox isolation**, and **commercial
      beta** checklists as ongoing work unless your deployment explicitly
      satisfies them.

---

## 7. Database migrations (production)

**Risk:** API starts against an empty database, or Phase 1 tables never appear.

**Do this:**

- [ ] Set **`DATABASE_URL`** to a URL your app and Alembic can use (PostgreSQL: either `postgresql://…` or `postgresql+asyncpg://…` — the app normalizes for SQLAlchemy and raw asyncpg). Set **`STATE_BACKEND=postgres`** in production-like deploys so durable state does not silently fall back to memory.
- [ ] **Before or on first deploy**, bring the schema to head:
  - **One-off (any host):** `alembic upgrade head` with the same `DATABASE_URL` in the environment.
  - **Docker / Railway using this repo’s image:** set **`RUN_MIGRATIONS_ON_START=true`** (via Railway variables) so the container entrypoint runs migrations then starts uvicorn. If the flag is true and **`DATABASE_URL` is missing, the entrypoint exits non-zero** (fail closed — see `scripts/docker_entrypoint.sh`).
  - **Legacy create_all DB:** if tables already exist but `alembic_version` is missing, stop for manual review. Compare physical schema and data-migration history; stamp only a proven matching historical revision, then apply required migrations using [the current controlled rollout](schema-042-rollout.md). Table presence does not establish parity with head.
- [ ] Do **not** rely on `SQLModel.metadata.create_all` in production-like environments — the API skips it and verifies required trust tables (`permits`, `receipts`, `idempotency_records`) at boot. Missing tables refuse startup.
- [ ] Do **not** rely on multi-replica races: for many instances, run migrations once (release job or single boot) instead of flipping the flag on every replica simultaneously — Alembic is usually safe, but your platform may prefer a dedicated migrate step.

---

## Preflight script

From the gateway directory (the directory that contains `scripts/human_preflight.sh`), with the API running:

```bash
export API_URL=http://127.0.0.1:8000   # or your deployed URL
bash scripts/human_preflight.sh
```

Optional: install `jq` for formatted output. `simulation_modes` appears only
when proof surfaces are mounted.

The script sends only unauthenticated `GET`s. It checks liveness, the public
dependency report, and discovery URLs. It reads `production_like` from
`/health/dependencies` first: on a production-like deployment it requires the
four tool catalogs to answer `401` and skips the `/v1/discover` `agent_first`
comparison (that needs a key); on a local-compatible deployment it requires
them to answer `200` and compares `agent_first`. It prints simulation flags
when the selected deployment exposes the full proof-surface report. It does
**not** perform authenticated wallet flows; use the golden path for that.

---

## Quick reference

| Question | Where to look |
|----------|----------------|
| What is simulated right now? | Proof-surface deployment: `GET /health/dependencies` → `simulation_modes`; supported production: startup `runtime_posture` log + deployed configuration |
| Can I trust sandbox isolation? | README + `docs/threat-model.md` |
| End-to-end wallet + key + tool flow | `docs/golden-path.md` |
| What the supported pilot still is | [Supported pilot boundary](README.md#supported-pilot-boundary) |
| Env flags | `.env.example` |
| Hosted deploy | Separately reviewed exact-SHA operator process; this repository has no hosted deploy runbook or IaC graph |
| Database migrations | This section §7 + `scripts/docker_entrypoint.sh` |
