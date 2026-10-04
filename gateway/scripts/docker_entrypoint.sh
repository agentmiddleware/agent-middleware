#!/usr/bin/env sh
# Container entrypoint: optionally run Alembic, then start uvicorn.
# Set RUN_MIGRATIONS_ON_START=true when DATABASE_URL points at your durable DB
# (postgresql:// or postgresql+asyncpg:// — Alembic/env normalizes via as_sqlalchemy_url).
#
# Fail-closed: RUN_MIGRATIONS_ON_START=true with an empty DATABASE_URL exits
# non-zero instead of starting uvicorn against an unmigrated database.

set -e

if [ "${RUN_MIGRATIONS_ON_START:-}" = "true" ]; then
  if [ -z "${DATABASE_URL:-}" ]; then
    echo "docker_entrypoint: RUN_MIGRATIONS_ON_START=true but DATABASE_URL is empty; refusing to start" >&2
    exit 1
  fi
  echo "docker_entrypoint: alembic upgrade head"
  alembic upgrade head
fi

# Docker/Compose command overrides retain their arguments after the same
# fail-closed migration gate used by the default application command.
if [ "$#" -gt 0 ]; then
  exec "$@"
fi

app_module="${APP_MODULE:-app.main:app}"
exec uvicorn "$app_module" --host 0.0.0.0 --port "${PORT:-8000}"
