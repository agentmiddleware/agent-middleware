#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$root/examples/retry-safe-refund/.uv-cache}"
cd "$root/gateway"
exec uv run --with-requirements requirements.txt --with "../sdk/python[verify]" \
  python ../examples/retry-safe-refund/demo.py
