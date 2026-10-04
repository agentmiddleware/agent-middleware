#!/usr/bin/env bash
#
# Core quality gate: the same lint checks CI's `lint` job runs, in one command.
#
# scripts/repo_guardian.py has always invoked this file, but it did not exist,
# so the guardian's "core quality gate (ruff+mypy core)" check failed
# unconditionally on every run and inflated its failure count.
set -euo pipefail

# ruff is not in requirements.txt (CI installs it separately), so ask for it
# explicitly in the uv fallback.
PYTHON_ENV_REQUIRE="ruff mypy"
PYTHON_ENV_EXTRA_WITH="ruff"
# shellcheck source=scripts/lib/python_env.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/python_env.sh"

echo "[core-quality] ruff check ."
"${PYTHON_CMD[@]}" -m ruff check .

echo "[core-quality] mypy app/"
"${PYTHON_CMD[@]}" -m mypy app/

echo "[core-quality] core quality gate passed"
