#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$root"
PYTHONPATH="$root/integrations:$root/gateway" exec "$root/.venv/bin/python" examples/langgraph-permit/demo.py
