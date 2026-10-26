# Constant Test Loop

Production-ready smoke test for the governed trust plane. Exercises the complete loop:

1. **Permit** → issue scoped permit for governed tool
2. **Invoke** → call the tool with the permit → signed receipt
3. **Meter** → verify ledger debit matches receipt charge
4. **Replay** → same idempotency key → same receipt_id, no second debit
5. **Deny** (optional) → out-of-scope tool is denied with 0 charge

## Local Testing

Against a running quickstart server:

```bash
# Terminal 1: start quickstart
make quickstart

# Terminal 2: run the constant test (auto-provisions its own key)
python scripts/constant_test_loop.py
```

The script self-provisions an agent key via `/v1/dev-keys/self-provision` when no credentials are provided.

## Production Use

Off loopback the loop refuses to start without `--tool` and `--tool-args`
(or `$CI_SMOKE_TOOL` / the equivalent flags). This is deliberate, not a
papercut:

- **Tool selection.** `partner.echo` and `partner.notes.write` are tried in
  order when present, but which tool a monitor invokes on every run is an
  operator's decision, not the registry ordering's.
- **Payload.** Arguments derived from a tool's `inputSchema` satisfy its
  declared *shape*, not its *semantics*, and the derivation fills required
  fields from types, defaults, and the **first enum member** — which for a
  consequential tool could be `delete`. Schema validity is not evidence of
  safety. Pass `--tool-args '{}'` if the tool genuinely needs no arguments;
  the point is that it be a decision.

The permit is sized from the selected tool's advertised
`annotations.creditsPerCall` rather than a fixed cap, so pointing the loop at
a pricier tool does not fail with `permit_budget_exceeded` on a healthy
deployment.

```bash
python scripts/constant_test_loop.py \
  --api-url https://api.thisisatest.tech \
  --tool partner.echo \
  --tool-args '{"text": "constant test loop"}'
```

Every flag has an environment equivalent, so a CI job can be configured
entirely through secrets without putting anything in argv:

| Flag | Environment variable |
|---|---|
| `--api-url` | `API_URL` |
| `--tool` | `CI_SMOKE_TOOL` |
| `--other-tool` | `CI_SMOKE_OTHER_TOOL` |
| `--tool-args` | `CI_SMOKE_TOOL_ARGS` (a JSON object) |

A flag beats the corresponding variable when both are set. A malformed
`CI_SMOKE_TOOL_ARGS` — invalid JSON, or valid JSON that is not an object —
exits 2 as a configuration error rather than 1, so a payload typo never
looks like the trust plane failing.

Set `CI_SMOKE_AGENT_KEY` for a pre-provisioned agent credential. Optionally provide `CI_SMOKE_WALLET_ID` and `CI_SMOKE_KEY_ID` for faster startup (the script will fetch them from the API if not provided):

```bash
# Provision an agent key once (using bootstrap key). The minted secret is
# never printed; --key-file writes it (mode 0600, path must not already exist).
export BOOTSTRAP_KEY="amw_live_..."

set +x
KEY_FILE="${TMPDIR:-/tmp}/amw-ci-smoke-key.$$"
PROVISION_JSON="$(python scripts/partner_api_key_bootstrap.py \
  --api-url https://api.thisisatest.tech \
  --agent-id ci-smoke-agent \
  --key-name constant-test-loop \
  --budget-credits 5000 \
  --json \
  --key-file "$KEY_FILE")"
export CI_SMOKE_AGENT_KEY="$(cat "$KEY_FILE")"
export CI_SMOKE_WALLET_ID="$(printf '%s' "$PROVISION_JSON" | jq -er .wallet_id)"
export CI_SMOKE_KEY_ID="$(printf '%s' "$PROVISION_JSON" | jq -er .key_id)"
rm -f "$KEY_FILE"
unset PROVISION_JSON

# Run the constant test
API_URL=https://api.thisisatest.tech python scripts/constant_test_loop.py
```

## Machine-Readable Bootstrap Output

`partner_api_key_bootstrap.py --json` prints JSON identity fields to stdout
without the secret. Persist the secret with `--key-file` (do not
pre-create the path; the script refuses an existing file):

```bash
KEY_FILE="${TMPDIR:-/tmp}/amw-ci-smoke-key.$$"

# JSON identity fields on stdout; full key only in KEY_FILE
BOOTSTRAP_KEY="..." python scripts/partner_api_key_bootstrap.py \
  --api-url https://api.thisisatest.tech \
  --agent-id ci-smoke-agent \
  --key-name test-key \
  --budget-credits 5000 \
  --json \
  --key-file "$KEY_FILE"

# Or print only the key-file path, then load it into a secret store
KEY_FILE="${TMPDIR:-/tmp}/amw-ci-smoke-key.$$"
BOOTSTRAP_KEY="..." python scripts/partner_api_key_bootstrap.py \
  --api-url https://api.thisisatest.tech \
  --agent-id ci-smoke-agent \
  --key-name constant-test-loop \
  --budget-credits 5000 \
  --key-only \
  --key-file "$KEY_FILE"
gh secret set CI_SMOKE_AGENT_KEY --repo agentmiddleware/agent-middleware < "$KEY_FILE"
rm -f "$KEY_FILE"
```

### Security Properties

- Human/status text goes to stderr (never stdout in `--json` mode)
- Bootstrap/admin key is never printed to stdout or stderr
- The minted agent key is never printed to stdout or stderr; `--key-file` stores it
- Agent key is never logged or printed during constant test execution
- All secrets read from environment variables, never hardcoded

## Exit Codes

- `0` — all invariants held
- `1` — invariant failure (test failed)
- `2` — configuration error or network failure

## Tests

```bash
pytest tests/test_constant_test_loop.py -v
```

Covers:
- `--json` mode produces pipeable stdout (identity fields only, never `api_key`)
- Bootstrap key never leaks to stdout or stderr
- `--key-file` persists the minted secret at mode 0600
- Constant test loop runs against local instance
- Self-provisioning when no key provided
- Agent key never logged during execution
