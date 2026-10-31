# Test tiers: plain pytest vs Postgres vs live

The gateway suite runs at three levels. A green run at one level says
nothing about the others, so read the level before quoting a result.

## Tier 1: plain pytest (default, no credentials or containers)

SQLite plus in-memory state, permissive trust flags, no rate limits.
This is what most tests prove.

```bash
cd gateway
make test      # fast loop: trust-plane tests only (pytest -m "not proof")
make test-all  # full suite, what CI runs (proof surfaces included)
```

CI additionally runs the strict production-posture subset with
production flags (`TRUST_MODE_ENABLED=true`,
`ALLOW_LEGACY_UNPERMITTED_MCP=false`, `ENABLE_PROOF_SURFACES=false`,
marker `production_trust` in `tests/test_production_trust_posture.py`).
The default run never exercises strict trust mode, real rate limiting,
or real row locks: those are pinned by `tests/test_suite_posture_defaults.py`
so the gap stays visible.

Golden signing snapshots (`tests/test_*_signing_input_snapshot.py`) run
here. Regenerating them after an intentional format change requires
`GOLDEN_REGEN_ACK=1` next to the `UPDATE_*_GOLDEN=1` flag, plus a schema
version bump and a CHANGELOG entry (see the test module docstrings).

## Tier 2: Postgres opt-in (needs a disposable local database)

Concurrency and row-lock proofs skip by default because SQLite treats
`SELECT ... FOR UPDATE` as a silent no-op with different semantics from
Postgres. To run them, point at a throwaway database you create and drop
yourself. Never use a shared or production database. Ordinary local Postgres runs
require the fixed disposable action-test profile below.

```bash
# Ordinary local runs only accept postgresql+asyncpg://sellers@127.0.0.1:55439/amw_action_*
# with ENVIRONMENT=test and an explicit Postgres test flag. createdb, Alembic,
# and pytest must all use port 55439.
# If the server listens on another port, forward that port onto 127.0.0.1:55439
# before these commands. There is no forward example in this file.
createdb -h 127.0.0.1 -p 55439 -U sellers amw_action_local
cd gateway
DATABASE_URL=postgresql+asyncpg://sellers@127.0.0.1:55439/amw_action_local \
  ENVIRONMENT=test alembic upgrade head
RUN_POSTGRES_CONCURRENCY_TESTS=1 ENVIRONMENT=test \
  DATABASE_URL=postgresql+asyncpg://sellers@127.0.0.1:55439/amw_action_local \
  pytest tests/test_action_postgres.py \
         tests/test_permit_postgres_concurrency.py \
         tests/test_duplicate_guard_postgres_concurrency.py \
         tests/test_action_database_guard.py -q
dropdb -h 127.0.0.1 -p 55439 -U sellers amw_action_local
```

The action-proof guard (`tests/support/action_database_guard.py`) only
accepts `postgresql+asyncpg://sellers@127.0.0.1:55439/amw_action_*` with
`ENVIRONMENT=test`; anything else fails before connecting. The
two-process crash harness (`make prove-crash-recovery`,
`tests/test_mcp_postgres_multiprocess.py`) additionally needs
`STATE_BACKEND=postgres` and `MCP_STRESS_DB_ISOLATED=1`.

CI runs tier 2 in `postgres-concurrency` and `postgres-proofs`
(`.github/workflows/ci.yml`) on every push to `main`, every merge-group run and
every manual run. On pull requests the two jobs run when the change touches
non-documentation files under `gateway/`, a project gateway depends on (for
example `sdk/python`), or a shared path. They skip a pull request that changes
only another project, for example `sdk/typescript` (`limit_matrix` in
`.github/scripts/project_dependents.py`), and a docs-only pull request, where
every changed file has a documentation suffix such as `.md` (changes under
`spec/`, nested READMEs and deletions never count as docs-only; the `detect` job
in `.github/workflows/ci.yml`). Both jobs use disposable
Postgres service databases and set `AMW_TEST_DB_DISPOSABLE=1` in their job
environment; the shared fixture then accepts PostgreSQL URLs with
`ENVIRONMENT=test`. `postgres-proofs` runs row-lock, action
accounting, migration, asyncpg driver, concurrent-invoke accounting
(`tests/test_security_fuzz_battery.py::test_rapid_fire_invokes_all_accounted`),
and both multiprocess crash suites
(`tests/test_mcp_postgres_multiprocess.py` and
`tests/test_action_multiprocess.py`) with `CI_STRICT=1`; its final step
fails if any proof logged a skip.

## Tier 3: live batteries (needs a running server, sometimes keys)

These drive an already-running gateway over real HTTP and may create
persistent rows on their target, so use staging unless you intend to
keep the data.

```bash
cd gateway
make trust-conformance-live   # needs AGENT_MIDDLEWARE_API_KEY plus a target URL
make adversarial-battery-live # needs BOOTSTRAP_KEY plus a target URL
```

Live verdict scripts (`scripts/stress_test_live.py` and friends) are
covered in-process by fakes in tier 1; a green tier 1 does not prove
the live deployment batteries pass. Stripe tests mock the Stripe SDK;
no suite test touches real money movement.
