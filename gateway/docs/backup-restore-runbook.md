# Backup, restore, and disaster recovery

This runbook covers the Agent Middleware gateway database: what to copy before
an Alembic change, how this repository applies and reverses schema, how to
restore, and how to verify. Statements about this gateway are taken from
files in this tree. The `pg_dump`, `pg_restore` and `psql` templates rely on
standard PostgreSQL client behavior, documented at
<https://www.postgresql.org/docs/current/app-pgdump.html>,
<https://www.postgresql.org/docs/current/app-pgrestore.html> and
<https://www.postgresql.org/docs/current/app-psql.html>.
This repository does not define a backup schedule, RPO, RTO, credential owner,
or Railway backup product.

The hosted-service deploy runbook is not in this repository
(`gateway/docs/human-onboarding.md` Quick reference; `gateway/WEDGE.md`
Deploy SOP; `gateway/SECURITY_LIMITATIONS.md` Required Production Posture).

## Scope

In scope:

- The gateway ORM database selected by `DATABASE_URL` (`gateway/app/core/config.py`
  Settings, Durable Runtime State; `gateway/app/db/database.py` `_get_database_url`).
- Alembic schema in `gateway/migrations/` as configured by `gateway/alembic.ini`
  (`script_location = migrations`). There is no `gateway/alembic/` directory.
- Production-like boot rules that refuse SQLite and require PostgreSQL
  (`gateway/app/core/trust_mode.py` `validate_trust_mode_config`;
  `gateway/TROUBLESHOOTING.md` DATABASE_URL sections).
- The upgrade, downgrade, and verification pattern in
  `gateway/docs/schema-040-rollout.md` through `gateway/docs/schema-054-rollout.md`.

Out of scope unless an operator separately backs them up. This repository
defines no dump or restore procedure for them:

- Durable key/value state when `STATE_BACKEND` is `redis` or `sqlite`
  (`gateway/app/core/durable_state.py` module docstring and `SQLITE_URL`
  handling; `gateway/app/core/config.py` `STATE_BACKEND`, `REDIS_URL`,
  `SQLITE_URL`).
- Rate-limiter Redis when `REDIS_URL` is set (`gateway/app/main.py` `/health`;
  `gateway/SECURITY_LIMITATIONS.md` Required Production Posture).
- AWI vector files at `RAG_VECTOR_STORE_PATH` (`gateway/app/core/config.py`
  Phase 9 RAG Engine).
- Signing material (`TRUST_SIGNING_PRIVATE_KEY_B64`, `TRUST_SIGNING_KEY_ID`)
  and `API_KEY_HASH_PEPPER`. Schema rollouts require preserving signing
  material (`gateway/docs/schema-040-rollout.md` Rollback after activation;
  `gateway/docs/schema-042-rollout.md` Release prerequisites step 5). They are
  environment secrets, not database rows.

`gateway/SECURITY_LIMITATIONS.md` (opening production claims) states that no
public uptime SLA, compliance scope, RTO/RPO, tenant-isolation guarantee, or
immutable-ledger claim is made.

Local `gateway/docker-compose.yml` is labeled "Local development only". It
runs the API image from `gateway/Dockerfile.dev`, loads `gateway/.env`, and
does not start a database container.

This tree contains no `railway.json`, `railway.toml`, or `Procfile`.
`gateway/Makefile` `check-railway-iac` tests that `Dockerfile`,
`requirements.txt`, and an executable `scripts/docker_entrypoint.sh` exist,
and states that the hosted `.railway/railway.ts` graph is not in this tree.
`gateway/docs/schema-040-rollout.md` names `prepare_railway_release.py`; that
script is not in this tree.

## Database engines

The ORM engine is SQLAlchemy/SQLModel async (`gateway/app/db/database.py`
module docstring). Two backends appear in this repository:

| Engine | When | Source |
| --- | --- | --- |
| PostgreSQL | Production-like environments. Required for wallets, permits, receipts, and the ledger. | `gateway/app/core/trust_mode.py` (`DATABASE_URL` must be set and must not be SQLite); `gateway/TROUBLESHOOTING.md` |
| SQLite | Local, test, and ephemeral non-production. `create_all` is reserved for that path. | `gateway/app/db/database.py` schema boot policy; `gateway/.env.example` Durable Runtime State |

`gateway/app/core/db_urls.py` accepts PostgreSQL schemes `postgres://`,
`postgresql://`, and `postgresql+asyncpg://` (`is_postgres_url`,
`as_sqlalchemy_url`, `as_asyncpg_url`). SQLAlchemy async form is
`postgresql+asyncpg://`. Raw asyncpg form is `postgresql://`. Railway and
many hosts provide `postgresql://`; Alembic and the app normalize
(`gateway/migrations/env.py` `get_url`; `gateway/scripts/docker_entrypoint.sh`
header comments).

SQLite schemes include `sqlite://` and SQLAlchemy driver spellings such as
`sqlite+aiosqlite://` (`gateway/app/core/db_urls.py` `_SQLITE_SCHEME`,
`is_sqlite_url`).

When `DATABASE_URL` is empty, Alembic warns and uses
`sqlite+aiosqlite:///./agent_middleware.db`
(`gateway/migrations/env.py` `run_migrations_offline` and
`run_async_migrations`). The ORM engine is not created
(`gateway/app/db/database.py` `get_engine`). Production-like boots refuse that
posture (`gateway/app/core/trust_mode.py`).

`gateway/alembic.ini` `sqlalchemy.url` is the placeholder
`driver://user:pass@localhost/dbname`. Runtime URL comes from `DATABASE_URL`
(`gateway/migrations/env.py` `get_url`).

Durable key/value state (`STATE_BACKEND=auto`) prefers PostgreSQL
(`DATABASE_URL`), then Redis (`REDIS_URL`), then a SQLite file (`SQLITE_URL`),
then in-memory (`gateway/app/core/durable_state.py` module docstring).
Production-like environments refuse `STATE_BACKEND=sqlite` and
`STATE_BACKEND=memory` (`gateway/TROUBLESHOOTING.md`).

PostgreSQL-only objects: migration `048_tenant_rls` (`gateway/docs/schema-048-rollout.md`;
`gateway/migrations/versions/048_tenant_rls.py` downgrade returns immediately
when the dialect is not PostgreSQL). SQLite-only repair: migration
`051_sqlite_boolean_defaults` (`gateway/docs/schema-051-rollout.md`; PostgreSQL
schema and data are unchanged).

Packaged Alembic revisions in `gateway/migrations/versions/` run from
`001_initial` (`down_revision = None`) through `054_api_key_scrypt_lookup`
(`down_revision = "053_repair_api_lineage"`). Two files share the numeric
prefix 025 (`025_refresh_token_key_binding.py` then
`025_remove_plaintext_owner_keys.py`). `gateway/scripts/check_alembic_chain.py`
documents that duplicate prefix as a legacy naming anomaly.

## Environment variable names

Names only. Never log or paste values.

| Name | Role |
| --- | --- |
| `DATABASE_URL` | ORM and Alembic database URL. Settings field in `gateway/app/core/config.py`. Alembic reads `os.getenv("DATABASE_URL")` in `gateway/migrations/env.py`. |
| `STATE_BACKEND` | `auto`, `postgres`, `redis`, `sqlite`, `memory` (`gateway/app/core/config.py`; `gateway/.env.example`). Production-like deploys use `postgres` (`gateway/docs/human-onboarding.md` section 7). |
| `SQLITE_URL` | Durable-state SQLite path or SQLite URL (`gateway/app/core/durable_state.py`; `gateway/.env.example`). |
| `REDIS_URL` | Durable-state Redis and shared rate limiter (`gateway/app/core/config.py`; `gateway/app/main.py` `/health`). Required when `ENABLE_DEMO_TENANT` is true in production-like environments (`gateway/app/core/trust_mode.py`). |
| `STATE_NAMESPACE` | Durable-state key prefix (`gateway/app/core/config.py`; default `agent_middleware`). |
| `ENVIRONMENT` | Selects production-like vs local-compatible (`gateway/app/core/trust_mode.py` `PRODUCTION_LIKE_ENVIRONMENTS`, `RUNTIME_ENVIRONMENT_VARIABLE`). Default in Settings is `local` (`gateway/app/core/config.py`). |
| `RUN_MIGRATIONS_ON_START` | When `true`, the Docker entrypoint runs `alembic upgrade head` then uvicorn (`gateway/scripts/docker_entrypoint.sh`; `gateway/Dockerfile` comment; `gateway/.env.example`). |
| `TENANT_RLS_MODE` | `off` or `enforce` (`gateway/app/core/config.py`; `gateway/docs/schema-048-rollout.md`). Published on `/health` as `tenant_rls_mode` (`gateway/app/main.py`). |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | SQLAlchemy pool for non-SQLite (`gateway/app/core/config.py`; `gateway/app/db/database.py` `get_engine`). |
| `ALLOW_METADATA_CREATE_ALL` | Pytest/ephemeral SQLite harness only. Never for Postgres (`gateway/app/db/database.py` `allows_metadata_create_all`). |
| `PORT` | Listen port. Default `8000` in Settings and in `gateway/scripts/docker_entrypoint.sh`. Docker `HEALTHCHECK` uses `PORT` (`gateway/Dockerfile`). |
| `APP_MODULE` | Uvicorn target. Default `app.main:app` (`gateway/scripts/docker_entrypoint.sh`). Partner smoke server uses `app.partner_mcp:app` (`gateway/docs/partner-first-tool-runbook.md`). |
| `HOST` | Bind address (`gateway/app/core/config.py`). |
| `DEBUG` | Engine echo and other local behavior (`gateway/app/core/config.py`; `gateway/app/db/database.py`). Must be false in production-like environments (`gateway/app/core/trust_mode.py`). |
| `API_KEY_HASH_PEPPER` | Required in production-like environments before 054 code boots (`gateway/docs/schema-054-rollout.md`; `gateway/docs/api-key-hashing.md`). |
| `TRUST_SIGNING_PRIVATE_KEY_B64`, `TRUST_SIGNING_KEY_ID` | Receipt and permit signing (`gateway/app/core/config.py`; `gateway/app/core/trust_mode.py`). |
| `ENABLE_PERMIT_REPEAT_WINDOW_ISSUANCE` | Schema 040 issuance gate (`gateway/docs/schema-040-rollout.md`; `gateway/app/core/config.py`). |
| `ENABLE_DEMO_TENANT` | Demo key issuance. Keep off until 043 is applied (`gateway/docs/schema-043-rollout.md`). |
| `RAILWAY_GIT_COMMIT_SHA` | Hosted build SHA (`gateway/Dockerfile` ARG; `gateway/app/core/build_metadata.py` tests). |
| `RAILWAY_ENVIRONMENT_ID`, `RAILWAY_ENVIRONMENT`, `RAILWAY_PROJECT_ID`, `RAILWAY_SERVICE_ID` | Hosted-runtime markers (`gateway/app/core/trust_mode.py` `HOSTED_RUNTIME_MARKER_VARS`). |
| `TENANT_RLS_OFF_MIGRATION_DATABASE_URL` | Disposable Postgres URL for 047/048 tests (`gateway/tests/test_047_048_negative_paths.py`). Not a production backup URL. |
| `AMW_DS_READONLY_DATABASE_URL` | Read-only diagnostic DSN for `gateway/scripts/amw_dup_new_key.py`. Not a backup URL. |

`.env` is listed as the Settings env file (`gateway/app/core/config.py`
`model_config`) and as `env_file` in `gateway/docker-compose.yml`. Do not
commit `.env` (`AGENTS.md` Documented in this repo; `CONTRIBUTING.md`).

## Pre-migration backup

Schema rollout docs that name a backup before changing the database:

- `gateway/docs/schema-043-rollout.md` Compatibility release: record the
  serving image, database backup, and a tested 043-capable recovery image.
- `gateway/docs/schema-051-rollout.md` Compatibility release: confirm the
  database revision, back up the database, and qualify a 051-capable recovery
  image.

Other 040-054 rollout docs require a qualified recovery image and a rehearsal
on disposable PostgreSQL before applying the migration. They do not ship a
dump command.

This repository does not define `pg_dump` or `pg_restore` invocations. The
templates below are the standard PostgreSQL client tools, not in-repo
scripts. Use a libpq URL (`postgresql://` or `postgres://`). Do not pass
`postgresql+asyncpg://` to `pg_dump`; that driver suffix is for SQLAlchemy
(`gateway/app/core/db_urls.py`). Do not print the URL.

Connection security for these client tools (standard libpq behavior, see
<https://www.postgresql.org/docs/current/libpq-connect.html> and
<https://www.postgresql.org/docs/current/libpq-pgpass.html>):

- Require TLS for any database reached over a network. Add
  `sslmode=verify-full` (with the server CA in `sslrootcert`) to the URL, or
  at minimum `sslmode=require`, which encrypts but does not verify the server.
- Keep the password out of the URL and the command line, where process
  listings and shell history can expose it. Put it in a password file named
  by `PGPASSFILE` (mode 0600), or in `PGPASSWORD` set only in the shell that
  runs the command. `PGDUMP_URL` then carries host, port, database, user and
  `sslmode` only.

```bash
# Generic dump (custom format). Substitute a libpq URL. This command is not
# defined in the repository.
pg_dump --format=custom --no-owner --file=DUMP_FILE "$PGDUMP_URL"

# Generic restore into an empty (newly created) database. This command is
# not defined in the repository. --exit-on-error stops at the first failure.
pg_restore --exit-on-error --no-owner --dbname="$PGDUMP_URL" DUMP_FILE
```

Restore into an empty, newly created database, not over the existing one.
`pg_restore --clean` drops only the objects the archive will restore, so
tables added by later migrations survive. For example, a dump taken at 043
restored over a 044 database leaves `receipt_resolutions` and
`resolution_chain_heads`, whose foreign keys reference `receipts`, `wallets`
and `signing_keys` (`gateway/migrations/versions/044_receipt_resolutions.py`).
Those dependencies break the restore, and the leftover tables block a later
044 upgrade from creating them. Treat any restore error as a failed restore
and do not continue to migrations or startup.

Plain-SQL equivalent, also not defined in this repository:

```bash
pg_dump --format=plain --no-owner --file=DUMP_FILE.sql "$PGDUMP_URL"
# Empty target database only; stop on the first error.
psql "$PGDUMP_URL" --set=ON_ERROR_STOP=1 --single-transaction --file=DUMP_FILE.sql
```

A plain-SQL dump has no cleanup commands. Against an existing database it
fails on existing tables and primary keys and leaves rows written after the
backup in place. Without `ON_ERROR_STOP=1`, psql continues after errors and
can exit as if it succeeded. Use it only on an empty, newly created database.

Take the dump before `alembic upgrade head` or before a deploy with
`RUN_MIGRATIONS_ON_START=true`. Rehearse restore on disposable PostgreSQL
first (`gateway/docs/schema-040-rollout.md` step 3;
`gateway/docs/schema-042-rollout.md` step 1;
`gateway/docs/schema-044-rollout.md` Compatibility release).

SQLite local files named by `DATABASE_URL` and `SQLITE_URL`
(`gateway/.env.example`) have no dump command in this repository. Copying the
file is outside this tree's documented procedure.

## Alembic commands as used in this repository

Run Alembic from the `gateway/` directory, which holds `alembic.ini`
(`prepend_sys_path = .`, `script_location = migrations`). Inside the
production image, `WORKDIR` is `/app` and `gateway/` is copied there
(`gateway/Dockerfile`), so the same `alembic.ini` is at `/app/alembic.ini`.

### `alembic upgrade head`

This is the upgrade command used in this repository.

- `gateway/scripts/docker_entrypoint.sh` prints `docker_entrypoint: alembic upgrade head` and runs `alembic upgrade head` when `RUN_MIGRATIONS_ON_START=true`.
- `gateway/docs/human-onboarding.md` section 7: one-off `alembic upgrade head` with the same `DATABASE_URL`.
- `gateway/SECURITY_LIMITATIONS.md` Required Production Posture.
- `gateway/TROUBLESHOOTING.md` (`Can't locate revision`).
- `gateway/docs/schema-054-rollout.md` Rollout step 2.
- `gateway/docs/api-key-hashing.md` Migration and key rotation step 2: `alembic upgrade head` (054).
- `gateway/docs/TESTING_TIERS.md` local Postgres proof:

  ```bash
  cd gateway
  DATABASE_URL=postgresql+asyncpg://sellers@127.0.0.1:55439/amw_action_local \
    ENVIRONMENT=test alembic upgrade head
  ```

  That URL is the documented disposable proof database, not production.
- `gateway/app/db/database.py` `SchemaInitError` messages tell operators to
  run `alembic upgrade head` or set `RUN_MIGRATIONS_ON_START=true`.

`RUN_MIGRATIONS_ON_START=true` with an empty `DATABASE_URL` exits non-zero and
does not start uvicorn (`gateway/scripts/docker_entrypoint.sh`).

PostgreSQL migrations set `app.rls_bypass` to `on` for the migration
transaction only (`gateway/migrations/env.py` `do_run_migrations`;
`gateway/docs/schema-048-rollout.md`).

### `alembic downgrade`

The repository documents `alembic downgrade` as a schema-stamp or data-loss
operation, not as the application rollback path.

- `gateway/docs/schema-041-rollout.md` Rollback after activation: do not run
  `alembic downgrade` to 040 as a rollback. It restores no data and only
  moves the stamp.
- `gateway/docs/schema-046-rollout.md` Rollback boundary: Alembic downgrade to
  045 drops the usage view and table. It is a data-loss operation, not an
  application rollback; never perform it while the API serves traffic.
- `gateway/docs/schema-054-rollout.md` Rollback: downgrade drops the lookup
  index and column and narrows `key_hash`. `gateway/migrations/versions/054_api_key_scrypt_lookup.py`
  module docstring: run it only after deleting rows whose `key_hash` is
  longer than 64 characters.

Several `downgrade()` functions refuse or no-op. See Restore and rollback.

This repository does not document a target such as `alembic downgrade -1` as
an operational command. When a rollout names a revision, it names that
revision id (for example 040, 045, 046, 051, 052).

### `alembic heads`

Confirm a single packaged head before migration work:

- `AGENTS.md` Documented in this repo: confirm a single Alembic head with
  `alembic heads`.
- `gateway/docs/partner-first-tool-runbook.md` Rolling deployment safety:
  confirm with `alembic heads` rather than trusting the head named in that
  paragraph. That paragraph still names `033_drop_optimizer_telemetry`; the
  versions directory currently packages through `054_api_key_scrypt_lookup`.
  Trust `alembic heads` and the versions directory.

### `alembic current`

This repository does not document `alembic current`. Boot compares the
database stamp to packaged heads in `gateway/app/db/database.py`
`_stale_alembic_message` (`MigrationContext.get_current_heads()` vs
`ScriptDirectory.from_config(Config("alembic.ini")).get_heads()`).

## Upgrade procedure

Pattern taken from `gateway/docs/schema-040-rollout.md` through
`gateway/docs/schema-054-rollout.md` and `gateway/docs/human-onboarding.md`
section 7.

1. Confirm the live database revision and the serving image's packaged head.
   Production boot requires them to match (`gateway/app/db/database.py`
   `verify_required_schema`; `gateway/docs/schema-046-rollout.md`
   Compatibility release). A 045-packaged image cannot restart after 046;
   the same packaged-head rule is restated for 044, 045, 047, 052, 053, 054.
2. Qualify an immutable recovery image whose packaged head already
   understands the *target* revision. Record that image before changing the
   database (`gateway/docs/schema-040-rollout.md` Compatibility release first;
   `gateway/docs/schema-042-rollout.md` step 2).
3. Rehearse the upgrade on disposable PostgreSQL
   (`gateway/docs/schema-040-rollout.md` step 3;
   `gateway/docs/schema-042-rollout.md` step 1). Do not use operational
   credentials in local rehearsals (`gateway/docs/schema-042-rollout.md`
   step 1).
4. Back up the database (Pre-migration backup). Schema 043 and 051 name this
   step explicitly.
5. Stop or freeze writes that the new schema will own, when the rollout says
   so (repeat-window issuance flag in schema 040; demo tenant flag in schema
   043; resolution writes in schema 044; approval writes in schema 045).
6. Apply schema:
   - One-off: `alembic upgrade head` with the same `DATABASE_URL`
     (`gateway/docs/human-onboarding.md` section 7;
     `gateway/docs/schema-054-rollout.md` step 2).
   - Docker / Railway using this repo's image: `RUN_MIGRATIONS_ON_START=true`
     (`gateway/docs/human-onboarding.md` section 7;
     `gateway/docs/schema-041-rollout.md` Release step 2;
     `gateway/scripts/docker_entrypoint.sh`).
7. Do not rely on `SQLModel.metadata.create_all` in production-like
   environments (`gateway/docs/human-onboarding.md` section 7;
   `gateway/SECURITY_LIMITATIONS.md`; `gateway/app/db/database.py`).
8. For many instances, run migrations once (release job or single boot)
   instead of flipping `RUN_MIGRATIONS_ON_START` on every replica at the same
   time (`gateway/docs/human-onboarding.md` section 7).
9. Legacy `create_all` databases with trust tables but no `alembic_version`
   stamp: stop for manual review. Stamp only a proven matching historical
   revision, then apply required migrations under
   `gateway/docs/schema-042-rollout.md`. Table presence is not parity
   (`gateway/docs/human-onboarding.md` section 7;
   `gateway/app/db/database.py` schema boot policy).
10. Enable new writes only after schema parity is verified and every serving
    worker is capable of the new head (schema 040 step 6; schema 043; schema
    044; schema 045).

Do not migrate 048 under `TENANT_RLS_MODE=enforce` until this gateway sets
`app.current_wallet_id` per transaction (`gateway/docs/schema-048-rollout.md`
Compatibility release). This gateway does not yet set that variable
(`gateway/app/core/config.py` `TENANT_RLS_MODE` comment).

A database that reached `050_demo_key_first_used` through the archived
agent-middleware-api chain lacks this repo's 044-046 objects until
`053_repair_api_lineage` (`gateway/docs/schema-047-rollout.md` Lineage gap;
`gateway/docs/schema-053-rollout.md`).

`gateway/docs/schema-054-rollout.md` Numbering: another change also adds a
`054_*` revision; whichever merges second must renumber to 055.

## Restore and rollback

### Restore from a PostgreSQL dump

This repository does not define the restore command. After a dump taken with
the generic `pg_dump` template above:

1. Stop all writers before restoring: stop admitting requests, drain
   in-flight work, and stop serving workers and background jobs. Keep them
   stopped until the restore and the checks below are complete. Writes
   accepted during a restore can be lost, and a governed MCP call can reach
   the upstream while its ledger, receipt or idempotency records are being
   replaced (`gateway/docs/partner-first-tool-runbook.md` relies on those
   idempotency records to stop a repeated dispatch).
2. Restore with the generic `pg_restore` (or `psql`) template into an empty,
   newly created database. Stop if the restore reports any error.
3. Point the gateway at the restored database. The gateway and Alembic read
   `DATABASE_URL`, not `PGDUMP_URL` (`gateway/app/core/config.py`;
   `gateway/migrations/env.py` `get_url`). Update `DATABASE_URL` in the
   secret store or deploy environment to the restored database, in its
   `postgresql+asyncpg://` form (`gateway/app/core/db_urls.py`). Before any
   start, confirm with `alembic current`, run from `gateway/` with that
   `DATABASE_URL`, that it reports the restored stamp. If it does not, the
   service is still pointed at the old database.
4. If the restored stamp is behind the packaged head of the image you will
   run, apply `alembic upgrade head` with that image's migrations
   (`gateway/app/db/database.py` stale-revision error).
5. Start only an image whose packaged Alembic head matches the database
   stamp. An older packaged head cannot restart
   (`gateway/docs/schema-041-rollout.md` both fail-closed checks;
   `gateway/app/db/database.py` `verify_required_schema`).
6. Preserve signing keys and `API_KEY_HASH_PEPPER` from the secret store.
   Schema 054 rows issued after the scrypt change do not authenticate on old
   code (`gateway/docs/schema-054-rollout.md` Rollback). Pre-054 rows stop
   authenticating once 054 code is deployed (`gateway/docs/api-key-hashing.md`).

### Application rollback after a migration

The 040-054 pattern is: stop new writes, recover with the qualified image
that already understands the *current* database revision, leave the database
at that revision. Do not restart an older packaged head. Do not treat
`alembic downgrade` as the rollback.

| After revision | Documented recovery | Downgrade behavior in this tree |
| --- | --- | --- |
| 040 | Recover with the recorded 040-capable image. Leave database 040. Never downgrade 040 or drop its column as application rollback (`gateway/docs/schema-040-rollout.md`). | `gateway/migrations/versions/040_permit_repeat_window.py` `downgrade()` drops `repeat_window_seconds`. The rollout forbids using that as rollback. |
| 041 | Roll back only to an image whose packaged head is 041 or later. Do not `alembic downgrade` to 040 (`gateway/docs/schema-041-rollout.md`). | `downgrade()` is a no-op. Scrubbed credentials are not restored (`gateway/migrations/versions/041_scrub_content_owner_keys.py`). |
| 042 | Recover with the recorded 042-capable image. Do not bypass parity or use a stamp-only downgrade (`gateway/docs/schema-042-rollout.md`). | Refuses with `action_authority_retained` while any action field is non-null or an action owner tombstone exists (`gateway/migrations/versions/042_permit_action_binding.py`). |
| 043 | Turn `ENABLE_DEMO_TENANT` off. Keep a 043-capable image. Downgrade does not restore a 042 schema (`gateway/docs/schema-043-rollout.md`). | `downgrade()` retains demo labels and attribution (`gateway/migrations/versions/043_demo_key_fields.py`). |
| 044 | Recover with the 044-capable image. Leave database at 044. Do not delete resolution evidence to make downgrade pass (`gateway/docs/schema-044-rollout.md`). | Raises `resolution_evidence_retained` if `receipt_resolutions` has a row, then would drop both new tables (`gateway/migrations/versions/044_receipt_resolutions.py`). |
| 045 | Recover with the 045-capable image. Leave database at 045 (`gateway/docs/schema-045-rollout.md`). | `downgrade()` drops `approval_gate_approvals`. Run only after no serving worker writes approvals; not a substitute for the recovery image. |
| 046 | Use a 046-compatible recovery image. Retain the database at 046 (`gateway/docs/schema-046-rollout.md`). | Drops `api_key_usage_daily` and `api_key_usage_events` (`gateway/migrations/versions/046_api_key_usage_events.py`). Data-loss. |
| 047 | (Compatibility release; checks not run against staging or production: `gateway/docs/schema-047-rollout.md`.) | Refuses while any `mcp_dispatch_attempts.reconcile_status` is non-NULL (`gateway/migrations/versions/047_dispatch_reconcile_lease.py`). |
| 048 | (No behaviour change in `off` mode: `gateway/docs/schema-048-rollout.md`.) | Drops policies and runs `NO FORCE` / `DISABLE ROW LEVEL SECURITY` on listed tables (`gateway/migrations/versions/048_tenant_rls.py`). |
| 049, 050 | No-op downgrade; columns belong to 043 (`gateway/docs/schema-049-rollout.md`, `gateway/docs/schema-050-rollout.md`). | Matching no-ops in the version files. |
| 051 | Recover with a 051-capable image. Database stays at 051 (`gateway/docs/schema-051-rollout.md`). | SQLite raises `sqlite_boolean_defaults_downgrade_unsupported` (`gateway/migrations/versions/051_sqlite_boolean_defaults.py`). |
| 052 | Recover with a 052-capable image. Database stays at 052 (`gateway/docs/schema-052-rollout.md`). | Refuses while any receipt carries `enforcement_mode` or `gateway_build` (`gateway/migrations/versions/052_receipt_mode_build.py`). |
| 053 | Downgrade is a documented no-op. Rolling back to 052 leaves repaired objects in place (`gateway/docs/schema-053-rollout.md`). | `downgrade()` is a no-op (`gateway/migrations/versions/053_repair_api_lineage.py`). |
| 054 | Keys issued after 054 do not work on old code (`gateway/docs/schema-054-rollout.md`). | Drops `key_lookup` and narrows `key_hash` (`gateway/migrations/versions/054_api_key_scrypt_lookup.py`). |

Emergency code rollback that predates 040: `gateway/docs/partner-first-tool-runbook.md`
Rolling deployment safety says keep migration 027 in place. Old workers remain
compatible and fail closed if they collide with a canonical row. Dropping that
index after canonical rows exist reopens the duplicate debit/dispatch race.

## Verification

After backup, upgrade, restore, or rollback:

1. Packaged head: from `gateway/`, run `alembic heads` (`AGENTS.md`;
   `gateway/docs/partner-first-tool-runbook.md`). Expect a single head.
2. Process start: production-like boots call `verify_required_schema`
   (`gateway/app/db/database.py` `init_db`). Missing trust tables, missing
   required columns, missing `api_key_usage_daily`, or a stamped revision
   mismatch raise `SchemaInitError` and refuse start. A stamped revision
   mismatch is any stamped revision that differs from the packaged head,
   behind or ahead (`_stale_alembic_message`, `current != heads`). An empty
   `alembic_version` table also refuses. A legacy database with no
   `alembic_version` table skips the revision comparison and is checked only
   for the required tables, columns and view.
3. Required trust tables (`gateway/app/db/database.py` `REQUIRED_TRUST_TABLES`):
   `permits`, `receipts`, `idempotency_records`, `mcp_dispatch_attempts`,
   `control_plane_audit_events`, `audit_chain_heads`, `signing_keys`,
   `human_approvals`, `receipt_resolutions`, `resolution_chain_heads`,
   `api_key_usage_events`. Required columns and the `api_key_usage_daily`
   view are listed in `REQUIRED_TRUST_COLUMNS` and `_missing_required_views`
   in the same file.
4. HTTP checks documented in this repository:

   | Check | What it proves | Source |
   | --- | --- | --- |
   | `GET /health` | Process up. Redis PING when `REDIS_URL` is set. Returns `commit_sha`, `build_provenance`, `tenant_rls_mode`. | `gateway/app/main.py` `/health`; Docker `HEALTHCHECK` in `gateway/Dockerfile` and `gateway/docker-compose.yml` |
   | `GET /health/ready` | Durable state store, MQTT (or `not_used` when simulated), and database `SELECT 1`. Database not `up` is not ready. | `gateway/app/main.py` `/health/ready`; `gateway/app/core/health.py` `check_database_readiness` |
   | `GET /health/dependencies` | Postgres, Redis, signing key, upstream MCP, version and commit SHA (production posture). | `gateway/app/main.py` `/health/dependencies` |
   | `scripts/human_preflight.sh` | Unauthenticated GETs: `/health`, `/health/dependencies`, OpenAPI, agent manifest, catalogs. | `gateway/scripts/human_preflight.sh`; `gateway/docs/human-onboarding.md` Preflight script |

   Schema 040 also requires private schema parity, runtime posture, healthy
   public dependencies, and both health routes reporting the exact baked SHA
   with provenance `stamped` (`gateway/docs/schema-040-rollout.md` step 5).
   Those extra private checks are named there; this repository does not
   contain their scripts.

5. Image SHA: `/health` `commit_sha` and `build_provenance`
   (`gateway/app/main.py`). Dockerfile bakes `.build_commit_sha`
   (`gateway/Dockerfile`; `gateway/scripts/write_build_commit_sha.py`).

6. Revision-specific checks from the matching `schema-0NN-rollout.md` (legacy
   permits for 040, demo kill switch for 043, resolution verification for 044,
   usage view for 046, and so on).

`gateway/docs/schema-047-rollout.md`, `schema-052-rollout.md`, and
`schema-053-rollout.md` state that their release checks have not been run
against staging or production. `gateway/docs/schema-042-rollout.md` records
REQUIRED / UNVERIFIED for current deployment parity.

## Open questions

The repository does not show:

- Backup frequency.
- RPO or RTO. `gateway/SECURITY_LIMITATIONS.md` states that no RTO/RPO claim
  is made.
- Who holds production credentials or the production `DATABASE_URL`.
- Whether Railway managed backups are enabled, what they cover, or how to
  restore from them. No Railway backup setting exists in this tree.
- Backup retention. `API_KEY_USAGE_RETENTION_DAYS` is opt-in usage-event
  cleanup, not backup retention (`gateway/app/core/config.py`).
  `TELEMETRY_RETENTION_HOURS` is telemetry retention
  (`gateway/app/core/config.py`).
- Where dump files are stored, encrypted, or tested.
- Whether production currently sets `RUN_MIGRATIONS_ON_START=true`.
- The live production Alembic revision. `gateway/docs/schema-050-rollout.md`
  says staging and production are at `050_demo_key_first_used`; later
  revisions 051-054 exist in `gateway/migrations/versions/`.
- The hosted-service deploy runbook, `.railway/railway.ts`, and
  `prepare_railway_release.py` (named, not shipped).
- An in-repo `alembic current` command or a dump/restore wrapper.
- Backup and restore for Redis, `SQLITE_URL` files, or `RAG_VECTOR_STORE_PATH`.
- A restore drill that has been executed against production (schema 040:
  qualification of a fallback is not proof that a production rollback has
  been exercised).
- How `API_KEY_HASH_PEPPER` and `TRUST_SIGNING_PRIVATE_KEY_B64` are backed
  up, other than "secret store" / Railway variables
  (`gateway/docs/api-key-hashing.md`; `gateway/.env.example`).
- Whether production `TENANT_RLS_MODE` is `off` or `enforce`.
- Operator identity for issuance/admission stop
  (`gateway/docs/schema-042-rollout.md` Required record table).
