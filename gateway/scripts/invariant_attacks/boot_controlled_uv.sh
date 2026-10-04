#!/usr/bin/env bash
# Controllable boot for attack_combined.py (shared crash-storm attack).
#
# Same trust-plane posture as boot_controlled.sh, but launched via `uv` so it
# runs on a fresh clone without a pre-activated venv. Because `uv run` keeps
# uvicorn as a CHILD process, the launcher's PID is the group leader and uvicorn
# is in its group — attack_combined.py kills the whole PROCESS GROUP (killpg),
# so this must be started as a session leader (the README recipe does that).
# attack5_crash_sqlite.py, which kills a single PID, uses boot_controlled.sh
# (plain `exec python`, PID == uvicorn) instead — do not swap them.
#
#   TP_STATE_DIR           (required)  database + signing seed dir
#   TP_PORT                (default: 8000)
#   TP_PYTHON              (optional)  use an already-provisioned interpreter
#                          instead of resolving dependencies through uv.
#   RATE_LIMIT_PER_MINUTE  (optional)  inherited from the environment; raise it
#                          so the limiter does not throttle the invariant the
#                          storm is actually testing.
set -euo pipefail
# The seed file below is private signing material; create it unreadable to other
# local users (a permissive umask would let them read it and forge receipts).
umask 077
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STATE="${TP_STATE_DIR:?set TP_STATE_DIR}"
PORT="${TP_PORT:-8000}"
mkdir -p "$STATE"
SEED_FILE="$STATE/signing-seed.b64"
# Stable seed across restart so receipts minted before the crash still verify.
if [ ! -s "$SEED_FILE" ]; then
  python3 -c "import base64,secrets;print(base64.b64encode(secrets.token_bytes(32)).decode())" > "$SEED_FILE"
fi
chmod 600 "$SEED_FILE"
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
TRUST_SIGNING_PRIVATE_KEY_B64="$(cat "$SEED_FILE")"
export TRUST_SIGNING_PRIVATE_KEY_B64
if [ -n "${TP_PYTHON:-}" ]; then
  exec "$TP_PYTHON" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
fi
exec uv run --with-requirements requirements.txt python -m uvicorn app.main:app \
  --host 127.0.0.1 --port "$PORT"
