#!/usr/bin/env bash
# Boot the local trust plane under our control so the crash test can
# kill -9 / restart it. Mirrors scripts/quickstart.py's env exactly, but binds
# the state dir / port / signing seed to env vars for reproducibility.
#
#   TP_STATE_DIR  (default: <repo>/data/quickstart)   database + signing seed
#   TP_PORT       (default: 8000)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STATE="${TP_STATE_DIR:-$ROOT/data/quickstart}"
PORT="${TP_PORT:-8000}"
mkdir -p "$STATE"
SEED_FILE="$STATE/signing-seed.b64"
if [ ! -s "$SEED_FILE" ]; then
  python3 -c "import base64,secrets;print(base64.b64encode(secrets.token_bytes(32)).decode())" > "$SEED_FILE"
fi
cd "$ROOT"
export PYTHONPATH="$ROOT"
export ENVIRONMENT=local DEBUG=false
export DATABASE_URL="sqlite+aiosqlite:///$STATE/api.db"
export STATE_BACKEND=sqlite SQLITE_URL="$STATE/state.db"
export VALID_API_KEYS="" STATIC_DEV_API_KEYS=""
export TRUST_MODE_ENABLED=true ALLOW_LEGACY_UNPERMITTED_MCP=false
export ENABLE_PROOF_SURFACES=false ENABLE_DOGFOOD_TOOL=true
export ENABLE_DEV_KEY_SELF_PROVISION=true ENABLE_STANDARD_MCP_ENDPOINT=true
export MCP_UPSTREAM_ENABLED=false
export TRUST_SIGNING_KEY_ID=quickstart-local-ed25519
export TRUST_SIGNING_PRIVATE_KEY_B64="$(cat "$SEED_FILE")"
exec python -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
