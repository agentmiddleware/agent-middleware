# Agent Middleware agent guide

Run commands from the repository root. See [CONTRIBUTING.md](CONTRIBUTING.md) for the full developer and pull request process.

## Setup

Use Python 3.12 and `uv`. This command verified that the gateway, the local Python SDK, and test dependencies resolve together:

```bash
uv run --no-project --isolated --python 3.12 --with './gateway' --with './sdk/python[verify]' --with 'pytest==9.1.1' --with 'pytest-asyncio>=1.4.0' --with 'pytest-cov>=7.1.0' --with 'mypy==2.4.0' pytest --version
```

## Test

These tests ran serially:

```bash
uv run --no-project --isolated --python 3.12 --with './gateway' --with './sdk/python[verify]' --with 'pytest==9.1.1' --with 'pytest-asyncio>=1.4.0' --with 'pytest-cov>=7.1.0' --with 'mypy==2.4.0' sh -c 'cd gateway && pytest tests/test_money_model_typecheck.py tests/test_packaging_deps.py -q -rs --tb=short'
uv run --no-project --isolated --python 3.12 --with './sdk/python[dev,verify,mcp]' pytest sdk/python/tests -q -rs
```

The full gateway suite also needs permission to bind local sockets. PostgreSQL proof tests need a disposable PostgreSQL database and explicit opt in. See [CONTRIBUTING.md](CONTRIBUTING.md) for the full suite.

## Lint

Ruff is pinned to the version already used by CI. Both checks pass on the current source:

```bash
uv run --with ruff==0.17.1 ruff check gateway
uv run --with ruff==0.17.1 ruff check sdk/python
```

## Build

Build the Python SDK into a temporary directory:

```bash
uv build sdk/python --out-dir /tmp/amw-build
```

## House rules

- Do not use em dashes or en dashes in user facing copy.
- Use real data only. Do not create synthetic data.
- Do not force push.
- Never touch the archived `agent-middleware-api` repository.
- Use one branch per issue.
- Pull requests need QA PASS and are merged only by the PR Merger.
