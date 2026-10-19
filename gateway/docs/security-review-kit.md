# Security Review Kit — Rules of Engagement

This kit is for an external reviewer who has agreed to attack this trust
plane. [SECURITY_LIMITATIONS.md](../SECURITY_LIMITATIONS.md) and
[TRUST_MODEL.md](../TRUST_MODEL.md) say what to read; this says what to
attack, with which credentials, what already counts as known, and what turns
an observation into a finding worth filing.

This kit does not require an NDA for ordinary repository observations.
Report security vulnerabilities privately under §6 and [SECURITY.md](../../SECURITY.md).
The hard constraint on testing is target selection (§1): the deployed
instances are single-tenant projects that belong to design partners, and a
write battery aimed at one of them damages a customer, not the vendor.

## 1. Pick a target you own

| Target | What it is | What you may run |
|---|---|---|
| `make quickstart` (local, SQLite) | The documented golden path — real routers, real signing, self-serve keys, one governed tool | Everything. This is the intended target. |
| A local or staging instance you deploy from this repo | Production-shaped: PostgreSQL row locking, Redis limiter, production trust flags | Everything, including the write batteries (`make trust-conformance-live`, `make adversarial-battery-live`). |
| The public Railway API | A vendor-managed deployment carrying a partner's data | Credential-less black-box observation only — reads of `/.well-known/*`, `/health*`, `/openapi.json`, and the discovery surfaces. No write batteries, no credential stuffing, no load generation. |

The SQLite quickstart and a PostgreSQL-backed instance are not
interchangeable for concurrency work: the headline finding of the previous
campaign was a SQLite-only overspend caused by `SELECT ... FOR UPDATE` being
silently dropped there
([invariant-attack-report.md](invariant-attack-report.md)). Race attacks are
worth running on both engines, and a result on one is not a result on the
other. `scripts/invariant_attacks/attack2_budget_postgres.py` exists for
exactly that comparison.

Boot instructions: [quickstart.md](quickstart.md) for the local plane.
Hosted deployment requires a separately reviewed operator process; see
[`SECURITY_LIMITATIONS.md`](../SECURITY_LIMITATIONS.md) for its limits.

## 2. Mint your own credentials

Do not ask anyone for a production key, and do not accept one if offered — a
finding produced with vendor-issued production credentials is not reproducible
by the next reviewer, and the transfer itself is the kind of out-of-band key
handling this repo documents as a weakness.

Two local-only surfaces exist so you never have to:

- **Wallet-scoped key, no restart.** With the server running under
  `ENABLE_DEV_KEY_SELF_PROVISION=true` (the quickstart default),
  `POST /v1/dev-keys/self-provision` with an empty body mints a sponsor
  wallet, an agent wallet, and a wallet-scoped key. This is the non-admin
  caller you want for most attacks; the invariant-attack scripts provision
  themselves this way. Call it from a CLI or SDK — it refuses cross-origin
  browser calls by `Origin` check.
- **Admin-shaped static key.** `python scripts/generate_static_dev_keys.py`
  prints a value for `STATIC_DEV_API_KEYS` in `.env`. Bootstrap-admin, never
  rotated by design, for the operator-side attacks.

Both are refused at boot by production-like environments, so neither is a
production auth path — see [static-dev-api-keys.md](static-dev-api-keys.md).
If you can reach either one on a production-posture instance, that *is* the
finding, and a serious one.

## 3. The contract you are trying to break

Five claims. Breaking any of them on a correctly configured instance is a
finding regardless of severity framing.

| # | Claim | Enforced in | Existing attack to start from |
|---|---|---|---|
| 1 | **Charge once under retry.** One idempotency key returns the original receipt — no second execution, no second debit. A key reused with different content fails closed. | `app/services/permits.py`, `app/routers/mcp.py` | `attack1_double_charge.py` |
| 2 | **Budget is a cap, not a suggestion.** A permit cannot authorize spend past its cap, cumulatively or under concurrency, and a denied call moves no money. | `authorize_and_reserve` in `app/services/permits.py` | `attack2_budget.py`, `attack2_budget_postgres.py` |
| 3 | **Terminalized or reconciled invocations have one signed terminal accounting.** For the configured upstream tool, a death after the dispatch checkpoint stays charged as `delivery_uncertain` and is never redispatched; a death before it nets to zero without dispatching. A local post-effect crash can require manual review without a receipt; a missing receipt does not prove that no effect occurred. See [failure semantics](failure-semantics.md). | dispatch/reconciliation path; `docs/failure-semantics.md` | `make prove-crash-recovery`, `attack5_crash_sqlite.py`, `reconcile_probe.py` |
| 4 | **Receipts verify offline.** A stranger with no credentials and no access to the issuing server can verify a portable receipt against the published key set, and a single flipped byte is detected. | Ed25519 signing; `/v1/receipts/{id}/portable`, `/.well-known/trust-keys.json` | `attack4_forgery.py`, the stranger test |
| 5 | **Authority before money.** An out-of-scope, unpermitted, expired, revoked, or tampered call is denied with a concrete reason *before* any charge, and the denial is itself a signed receipt with no ledger linkage. | permit validation on the governed MCP path | `attack3_scope.py`, `attack6_key_misuse.py`, `scripts/red_team_trust_plane.py` |

Two adjacent invariants are worth the same treatment: a wallet-scoped key must
not read or invoke across tenants (`attack6_key_misuse.py`), and the audit
chain must be tamper-evident per event (`make prove-trust-plane`).

The claims are asserted in-process by
[`tests/test_adversarial_five_claims.py`](../tests/test_adversarial_five_claims.py),
and [PROOF_MATRIX.md](PROOF_MATRIX.md) maps every proof command to what it
does *not* prove — the gaps in that column are where an outside attack is most
likely to find something the suite cannot.

## 4. What is already known

These are documented, accepted limits, not undiscovered bugs. Reporting one as
a vulnerability tells us nothing we have not already written down; **breaking
the containment around one is a finding.**

- Offline verification trusts the issuing origin for key distribution. A
  compromised origin can serve a key set that validates forged receipts. No
  out-of-band pinning.
- Audit chains are tamper-*evident*, not immutable — a database administrator
  can delete rows. No external anchoring or transparency log.
- Isolation is application-layer only: no row-level security, no external
  IdP/KMS, single-tenant vendor-managed pilot.
- A receipt proves what happened, never what did not. Absence of a receipt is
  not evidence that no action occurred.
- Gateway deduplication (at most one dispatch and debit per key) does not make
  the upstream side effect happen once unless that upstream honors the forwarded idempotency key.
- MCP is the only governed adapter. One operator-configured upstream origin,
  one exact tool, in the pilot.
- Dormant proof surfaces (AWI, browser/DOM, RAG, passkey, sandbox, red-team,
  RTaaS, agent comms) mount only under `ENABLE_PROOF_SURFACES=true` or an
  explicit simulation flag, both of which a production-like boot refuses. A
  weakness reachable *only* behind those flags is a known-dormant surface; the
  same weakness reachable on a production-posture boot is a real finding,
  because it means the gate leaked.
- Application-layer rate limiting is 120 requests per minute per API-key
  value (shared `anonymous` bucket without a key). A burst of 40 will not
  `429`. Counted responses, including `401`s, carry `X-RateLimit-*` headers.
  Discovery, docs, and `/health` are exempt. Requests whose credentials are refused
  (`401`, and the `403` an unknown key gets) are also charged to one shared
  per-client bucket at ten times the limit, reserved before the request runs,
  so neither rotating `X-API-Key` values nor concurrency mints fresh budgets.
  Scope denials against a valid key are not charged there. Client identity is
  an address, so a distributed flood and an edge connection limit are still
  the operator's job.

The full list is [SECURITY_LIMITATIONS.md](../SECURITY_LIMITATIONS.md), with
the boundary itself in [TRUST_MODEL.md](../TRUST_MODEL.md) and
[threat-model.md](threat-model.md). Attacks aimed *at* these documented limits
— rather than reports *of* them — are the most useful ones available.

## 5. What makes a finding land

Include, at minimum:

- The commit SHA under attack (`/health/dependencies` reports `commit_sha` on
  a running instance; a deployed origin can lag `main` by a long way, as
  documented in external-surface-review-2026-08-23.md).
- The environment posture: engine (SQLite vs PostgreSQL), `TRUST_MODE_ENABLED`,
  `ENABLE_PROOF_SURFACES`, and whether either dev-key surface was enabled.
- The exact request and the observed response, plus receipt IDs and wallet
  balances where money is involved — redact keys to a prefix, as
  `redact_evidence.py` does.
- A reproduction. A stdlib-only script in the style of
  `scripts/invariant_attacks/` is ideal: it can be run in CI as a regression
  gate the same day the fix lands.
- Which of the five claims (or which documented limit's containment) it
  breaks. If it breaks none of them, say what invariant you believe it breaks
  instead — that is useful too, and may mean a claim is missing.

A finding that cannot be reproduced on an instance the maintainers can boot is
much harder to act on than one that can.

## 6. Where to send it

Private security advisory on GitHub, per [SECURITY.md](../../SECURITY.md) — not a
public issue. Triage target is five business days. If the advisory flow is
unavailable, open a minimal issue asking for private contact.

## 7. What previous passes already found

Read these before starting, so you spend your time on new ground:

- [invariant-attack-report.md](invariant-attack-report.md) — the hostile
  concurrency campaign, with the one invariant that broke (the SQLite budget
  race), its root cause, and the fix.
- hard-run-report-2026-08-12.md — a dated,
  credential-less black-box run against the deployment, with reproduction
  commands.
- external-surface-review-2026-08-23.md
  — the deployed origins reviewed from outside.
- [owasp-agentic-top10-mapping.md](owasp-agentic-top10-mapping.md) — the same
  posture in ASI01–ASI10 vocabulary, with the honest gap named per risk.
