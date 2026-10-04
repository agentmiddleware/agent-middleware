# shellcheck shell=bash
#
# Shared interpreter resolution for the gate scripts.
#
# Set these BEFORE sourcing to tune the selection:
#   PYTHON_ENV_REQUIRE     space-separated modules the interpreter must import
#                          (default: "pytest")
#   PYTHON_ENV_EXTRA_WITH  space-separated extra packages for the uv fallback,
#                          for tools absent from requirements.txt (e.g. "ruff")
#
# Sourcing this file sets two arrays:
#   PYTHON_CMD  — runs a Python module/script ("${PYTHON_CMD[@]}" -m ruff check .)
#   PYTEST_CMD  — runs pytest              ("${PYTEST_CMD[@]}" -q tests/)
#
# Selection order:
#   1. Explicit PYTHON / PYTEST environment overrides, used verbatim.
#   2. uv, matching every Makefile test target and resolving the exact pinned
#      requirements. A system interpreter can import a dependency by name while
#      still holding an incompatible version (the MCP client caused this gate to
#      collect against a stale global install).
#   3. When uv is unavailable, the first of python3.12, python3, python that can
#      import every module in PYTHON_ENV_REQUIRE. CI setup-python environments
#      remain supported through this fallback.

_python_env_repo_root() {
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  cd "$script_dir/../.." && pwd
}

REPO_ROOT="$(_python_env_repo_root)"

# The gates reference tests/ and scripts/ relatively, and uv needs to find
# requirements.txt, so anchor everything at the repo root.
cd "$REPO_ROOT"

_python_env_require="${PYTHON_ENV_REQUIRE:-pytest}"
_python_env_extra_with="${PYTHON_ENV_EXTRA_WITH:-}"

_python_env_can_import_all() {
  local interpreter="$1" module
  for module in $_python_env_require; do
    "$interpreter" -c "import $module" >/dev/null 2>&1 || return 1
  done
  return 0
}

PYTHON_CMD=()
PYTEST_CMD=()

if [[ -n "${PYTHON:-}" ]]; then
  PYTHON_CMD=("$PYTHON")
elif command -v uv >/dev/null 2>&1; then
  PYTHON_CMD=(uv run --with-requirements requirements.txt)
  for _extra in $_python_env_extra_with; do
    PYTHON_CMD+=(--with "$_extra")
  done
  PYTHON_CMD+=(python)
else
  for _candidate in python3.12 python3 python; do
    if command -v "$_candidate" >/dev/null 2>&1 && _python_env_can_import_all "$_candidate"; then
      PYTHON_CMD=("$_candidate")
      break
    fi
  done
fi

if [[ ${#PYTHON_CMD[@]} -eq 0 ]]; then
  echo "[python-env] No interpreter provides: $_python_env_require" >&2
  echo "[python-env] and uv is not available. Fix with one of:" >&2
  echo "[python-env]   - install uv (https://docs.astral.sh/uv/) and re-run" >&2
  echo "[python-env]   - python3 -m pip install -r requirements.txt $_python_env_extra_with" >&2
  echo "[python-env]   - PYTHON=/path/to/python scripts/$(basename "$0")" >&2
  exit 1
fi

if [[ -n "${PYTEST:-}" ]]; then
  PYTEST_CMD=("$PYTEST")
else
  PYTEST_CMD=("${PYTHON_CMD[@]}" -m pytest)
fi

echo "[python-env] python: ${PYTHON_CMD[*]}"
