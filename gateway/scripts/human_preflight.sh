#!/usr/bin/env bash
# Human preflight: verify discovery URLs and the public dependency report.
# Usage: API_URL=http://localhost:8000 bash scripts/human_preflight.sh
# Requires: curl, python3. Optional: jq for pretty JSON.
#
# Sends only unauthenticated GETs. Production-like services lock their tool
# catalogs behind the same credentials as invoke (#444), so there the catalog
# routes must answer 401 and the /v1/discover comparison needs an operator key.

set -u

API_URL="${API_URL:-http://127.0.0.1:8000}"
API_URL="${API_URL%/}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

http_code() {
  local path="$1"
  curl -sS -o /dev/null -w "%{http_code}" --connect-timeout 3 --max-time 10 \
    "${API_URL}${path}" 2>/dev/null || echo "000"
}

echo "Human preflight — API_URL=${API_URL}"
echo ""

fail=0
check_http() {
  local name="$1"
  local path="$2"
  local want="${3:-200}"
  local code
  code="$(http_code "$path")"
  if [[ "$code" == "$want" ]]; then
    echo -e "${GREEN}OK${NC}  ${code}  ${name}  ${path}"
  else
    echo -e "${RED}BAD${NC} ${code} (expected ${want})  ${name}  ${path}"
    fail=1
  fi
}

deps_json="$(curl -sS --connect-timeout 3 --max-time 15 "${API_URL}/health/dependencies" 2>/dev/null || true)"
if [[ -z "$deps_json" ]]; then
  echo -e "${RED}FAILED${NC} to fetch /health/dependencies"
  exit 1
fi

# Tool catalogs answer 401 without a key on production-like services and 200
# on local-compatible ones. An unreadable posture fails and expects the lock.
production_like="$(echo "$deps_json" | python3 -c '
import json, sys
value = json.load(sys.stdin).get("production_like")
print("true" if value is True else "false" if value is False else "unknown")
' 2>/dev/null || true)"
case "$production_like" in
  true) catalog_want="401" ;;
  false) catalog_want="200" ;;
  *)
    echo -e "${RED}BAD${NC} /health/dependencies does not report production_like as true or false"
    fail=1
    catalog_want="401"
    ;;
esac

check_http "Liveness" "/health" "200"
check_http "OpenAPI" "/openapi.json" "200"
check_http "Agent manifest" "/.well-known/agent.json" "200"
check_http "Discover index" "/v1/discover" "$catalog_want"
check_http "LLM docs" "/llm.txt" "200"
check_http "MCP tools manifest" "/mcp/tools.json" "$catalog_want"
check_http "MCP tools list" "/mcp/tools" "$catalog_want"
check_http "Well-known MCP (alternate route)" "/.well-known/mcp/tools.json" "$catalog_want"
if [[ "$catalog_want" == "401" ]]; then
  echo "    (production_like=${production_like}: tool catalogs require an API key)"
fi

echo ""

dependency_shape="$(echo "$deps_json" | python3 -c '
import json, sys
d = json.load(sys.stdin)
proof = d.get("enable_proof_surfaces")
has_modes = isinstance(d.get("simulation_modes"), dict)
if proof is True and has_modes:
    print("full")
elif proof is False and "simulation_modes" not in d:
    print("public")
else:
    print("invalid")
' 2>/dev/null || true)"
if [[ "$dependency_shape" == "invalid" || -z "$dependency_shape" ]]; then
  echo -e "${RED}FAIL${NC} dependency report proof-surface fields are inconsistent"
  fail=1
fi

echo "Dependency report summary:"
if command -v jq >/dev/null 2>&1; then
  echo "$deps_json" | jq '
    {status, version, unhealthy}
    + if .enable_proof_surfaces == true and has("simulation_modes")
      then {simulation_modes}
      elif .enable_proof_surfaces == false and (has("simulation_modes") | not)
      then {simulation_modes: "omitted (enable_proof_surfaces=false)"}
      else {simulation_modes: "inconsistent dependency response"}
      end
  '
else
  echo "$deps_json"
  echo ""
  echo -e "${YELLOW}Tip:${NC} install jq to print a compact dependency summary."
fi

overall="$(echo "$deps_json" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("status",""))' 2>/dev/null || true)"
if [[ "$overall" == "degraded" ]]; then
  echo ""
  echo -e "${YELLOW}Warning:${NC} status is degraded — inspect unhealthy dependencies in the JSON above."
fi

if [[ "$fail" -eq 0 && "$catalog_want" != "200" ]]; then
  echo ""
  echo "agent_first alignment:"
  echo -e "${YELLOW}SKIP${NC} /v1/discover requires an API key on this production-like service; compare its agent_first block with /.well-known/agent.json using an operator key"
elif [[ "$fail" -eq 0 ]]; then
  echo ""
  echo "agent_first alignment:"
  export API_URL
  if python3 -c "
import json
import os
import sys
import urllib.request

base = os.environ['API_URL'].rstrip('/')

def load(path: str) -> dict:
    req = urllib.request.Request(
        f'{base}{path}', headers={'User-Agent': 'human_preflight'}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)

try:
    a = load('/.well-known/agent.json')
    d = load('/v1/discover')
except Exception as exc:
    print(exc, file=sys.stderr)
    raise SystemExit(1) from exc
if a.get('agent_first') != d.get('agent_first'):
    print('agent_first mismatch between manifest and /v1/discover', file=sys.stderr)
    raise SystemExit(1)
"; then
    echo -e "${GREEN}OK${NC}   /.well-known/agent.json and /v1/discover agree on agent_first"
  else
    echo -e "${RED}BAD${NC}   agent_first alignment (see stderr above)"
    fail=1
  fi
fi

echo ""
echo "Next steps for humans:"
echo "  - Read docs/human-onboarding.md"
echo "  - If simulation_modes are present and true, those domains are simulated unless you wired real backends."
echo "  - With proof surfaces disabled, inspect the startup runtime_posture log and deployed SIMULATION_MODE_* values."
echo "  - Run through docs/golden-path.md for wallet-scoped keys and billing rehearsal."

exit "$fail"
