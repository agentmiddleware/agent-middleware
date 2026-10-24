# Agent Middleware agent guide

Run commands from the repository root. See [CONTRIBUTING.md](CONTRIBUTING.md) for the full developer and pull request process.

## Setup

Use Python 3.11 or newer and `uv`. CI tests Python 3.11 through 3.14; the commands below use 3.12, which CI uses for quality checks. Install gateway test dependencies from `gateway/requirements.txt` and the local SDK with its `verify` extra, as in [CI](.github/workflows/ci.yml):

```bash
uv run --no-project --isolated --python 3.12 --with-requirements gateway/requirements.txt --with-editable './sdk/python[verify]' python -m pytest --version
```

## Test

Run the tests serially. Use `python -m pytest` because the `pytest` launcher can have a bad interpreter path when this checkout path contains spaces.

```bash
uv run --directory gateway --no-project --isolated --python 3.12 --with-requirements requirements.txt --with-editable '../sdk/python[verify]' python -m pytest tests/test_money_model_typecheck.py tests/test_packaging_deps.py -q -rs --tb=short
uv run --no-project --isolated --python 3.12 --with-requirements gateway/requirements.txt --with-editable './sdk/python[verify,mcp]' python -m pytest sdk/python/tests -q -rs
```

The full gateway suite also needs permission to bind local sockets. PostgreSQL proof tests need a disposable PostgreSQL database and explicit opt in. See [CONTRIBUTING.md](CONTRIBUTING.md) for the full suite.

## Lint

Read the Ruff pin from [gateway/requirements-dev.txt](gateway/requirements-dev.txt), then check every Python project found by the same [discovery script](.github/scripts/detect_projects.py) as CI and the separate lint workflow:

The loop below requires bash.

```bash
RUFF_VERSION=$(sed -n 's/^ruff==//p' gateway/requirements-dev.txt)
while IFS= read -r project; do
  uv run --directory "$project" --no-project --isolated --with "ruff==${RUFF_VERSION}" ruff check .
  uv run --directory "$project" --no-project --isolated --with "ruff==${RUFF_VERSION}" ruff format --check .
done < <(python3 .github/scripts/detect_projects.py | sed -n 's/^python=//p' | python3 -c 'import json, sys; print(*json.load(sys.stdin), sep="\n")')
uv run --directory gateway --no-project --isolated --python 3.12 --with-requirements requirements.txt --with-editable '../sdk/python[verify]' --with 'mypy==2.4.0' mypy app/
uv run --no-project --isolated --python 3.12 --with-editable './sdk/python[verify]' python verifier/check_vectors.py
```

## Build

Build the Python SDK into a temporary directory:

```bash
uv build sdk/python --out-dir /tmp/amw-build
```

## House rules

### Maintainer rules

- Do not use em dashes or en dashes in user facing copy.
- Use real data only. Do not create synthetic data.
- Do not force push.
- Do not use admin merge.
- Never touch the archived `agent-middleware-api` repository.
- Use one branch per issue.
- QA PASS means the AMW QA Engineer posts PASS naming the head SHA.
- The AMW PR Merger merges only after QA PASS, green `CI result`, `License result`, and `gitleaks`, an up-to-date branch with main, and all review threads resolved. See [CI](.github/workflows/ci.yml), [license check](.github/workflows/license-check.yml), and [secrets scan](.github/workflows/secrets.yml) for the check names.

### Documented in this repo

- Never commit `.env` files, keys, or local databases; run `pre-commit install` to enable the local secret scan. [CONTRIBUTING.md](CONTRIBUTING.md)
- Sign off each commit with `git commit -s`. [CONTRIBUTING.md](CONTRIBUTING.md)
- Open an RFC issue before changing normative material in `spec/`. [CONTRIBUTING.md](CONTRIBUTING.md)
- The main ruleset requires linear history, resolved review threads, a passing `CI result`, and `gitleaks`. [Main ruleset](gateway/docs/PROOF_MATRIX.md)
- Confirm a single Alembic head with `alembic heads` before migration work. [Partner runbook](gateway/docs/partner-first-tool-runbook.md)
- Add or update tests for behavior changes. [Pull request template](.github/PULL_REQUEST_TEMPLATE.md)
