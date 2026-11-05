#!/usr/bin/env bash
# T-02 acceptance: POST /mcp Streamable HTTP lifecycle, run by hand with curl.
#
#   initialize -> notifications/initialized -> tools/list
#   -> tools/call (Idempotency-Key header) -> same tools/call again
#
# The retry must return the same receipt; check the wallet ledger afterward
# for a single debit. This script does not claim or check anything beyond that.
#
# Usage:
#   export API_URL=http://127.0.0.1:8000      # gateway origin, no trailing slash
#   export AGENT_API_KEY=...                  # wallet-scoped agent key (X-API-Key)
#   export TOOL_NAME=...                      # a tool returned by tools/list
#   export TOOL_ARGUMENTS='{"text":"t02"}'    # JSON object for that tool
#   bash artifacts/t02-acceptance-curl.sh
#
# Optional: IDEMPOTENCY_KEY (defaults to a fresh random key per run).
#
# The key is read from the environment and is never echoed. Do not run this
# with `bash -x`, and do not paste its output anywhere if your tool returns
# sensitive data.

set -euo pipefail

: "${API_URL:?set API_URL to the gateway origin}"
: "${AGENT_API_KEY:?set AGENT_API_KEY to a wallet-scoped agent key}"
: "${TOOL_NAME:?set TOOL_NAME to a tool listed by tools/list}"
TOOL_ARGUMENTS="${TOOL_ARGUMENTS:-"{}"}"
IDEMPOTENCY_KEY="${IDEMPOTENCY_KEY:-t02-$(date +%s)-$RANDOM$RANDOM}"
MCP_URL="${API_URL%/}/mcp"

mcp_post() {
    # $1 = JSON-RPC body; extra args are passed through to curl.
    local body="$1"
    shift
    curl --silent --show-error \
        --request POST "$MCP_URL" \
        --header "X-API-Key: ${AGENT_API_KEY}" \
        --header "Accept: application/json, text/event-stream" \
        --header "Content-Type: application/json" \
        "$@" \
        --data "$body"
    echo
}

echo "== 1. initialize"
mcp_post '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t02-curl","version":"0.0.0"}}}'

echo "== 1b. notifications/initialized (expect HTTP 202, empty body)"
mcp_post '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
    --output /dev/null --write-out 'HTTP %{http_code}'

echo "== 2. tools/list"
mcp_post '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'

CALL_BODY=$(printf '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"%s","arguments":%s}}' \
    "$TOOL_NAME" "$TOOL_ARGUMENTS")

echo "== 3. tools/call with Idempotency-Key"
mcp_post "$CALL_BODY" --header "Idempotency-Key: ${IDEMPOTENCY_KEY}"

echo "== 4. tools/call retry, same Idempotency-Key (expect the same receipt)"
mcp_post "$CALL_BODY" --header "Idempotency-Key: ${IDEMPOTENCY_KEY}"

# The same key can instead ride in the JSON-RPC body:
#   "params": {..., "_meta": {"io.agentmiddleware/idempotency_key": "<key>"}}
# If both are sent they must match, or the call is refused.
