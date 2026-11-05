# Demo script

Every endpoint and command below exists in this repository. Code locations are
given so a judge can check. Expected values come from code defaults in this
repository, and the file is cited for each one. This kit states no
observations of the hosted gateway at `https://api.agentmiddleware.dev`. A
deployment can override defaults, so run the curl shown in each step to
confirm a value on the hosted gateway.

Never paste a real API key into a shared document, issue, or screen recording.
The commands below read keys from environment variables.

## Step 1: health check (no credential)

```bash
curl -s https://api.agentmiddleware.dev/health/dependencies
```

- Route: `GET /health/dependencies` in `gateway/app/main.py`. Unauthenticated.
- Per `gateway/app/main.py` (`health_dependencies`) and
  `build_public_dependency_report` in `gateway/app/core/health.py`, with proof
  surfaces unmounted (`ENABLE_PROOF_SURFACES: bool = False` in
  `gateway/app/core/config.py`) the response should include `status`
  (`healthy` or `degraded`), `version`, `commit_sha`, and dependency entries
  for the transaction integrity boundary (database, cache, signing key,
  upstream MCP).
- Per `gateway/app/core/config.py` defaults (`APP_VERSION: str = "1.3.0"`,
  matching `__version__ = "1.3.0"` in `gateway/app/__init__.py`), `version`
  should be `1.3.0` for a build of this commit. Run the curl above, with `-i`
  to see the status line, to confirm on the hosted gateway.

## Step 2: mint a demo key

```bash
export API_URL=https://api.agentmiddleware.dev
DEMO_JSON=$(curl -s -X POST "$API_URL/v1/demo/keys" \
  -H "Content-Type: application/json" -d '{"utm_source":"competition-kit"}')
export AMW_API_KEY=$(echo "$DEMO_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["api_key"])')
export AMW_WALLET_ID=$(echo "$DEMO_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["wallet_id"])')
```

- Route: `POST /v1/demo/keys` in `gateway/app/routers/demo_keys.py`. Anonymous,
  returns 201 when the demo tenant is enabled and 404 when it is not
  (`ENABLE_DEMO_TENANT: bool = False` by default in
  `gateway/app/core/config.py`). `GET /v1/demo/keys` returns usage help.
- The key is shown once. Do not print `$DEMO_JSON` on a shared screen.
- Response fields defined in code (`DemoKeyIssueResponse`): `api_key`,
  `key_id`, `key_prefix`, `wallet_id`, `sponsor_wallet_id`, `tenant`,
  `allowed_tools`, `expires_at`, `max_uses`, `budget_credits`, `base_url`,
  `note`.
- Per `gateway/app/services/demo_tenant.py` (`DEMO_TENANT_LABEL = "demo"`),
  the response should include `tenant` = `demo`.
- Per `gateway/app/core/config.py` defaults
  (`DEMO_WALLET_CREDITS: Decimal = Decimal("1000")`, used for
  `budget_credits` in `gateway/app/services/demo_tenant.py`), the response
  should include `budget_credits` = 1000. Credits are metering units, not
  money.
- Per `gateway/app/core/config.py` defaults (`RATE_LIMIT_PER_MINUTE: int = 120`)
  and `RateLimitMiddleware` in `gateway/app/core/rate_limiter.py`, which takes
  that default and, in `dispatch`, sets `X-RateLimit-Limit` to that limit on
  the response, the response should include `X-RateLimit-Limit: 120`.
- To confirm these on the hosted gateway, run the `POST /v1/demo/keys` curl
  by itself with `-i` added (not inside the `DEMO_JSON=$(...)` assignment,
  where the headers would break the JSON parsing), on a private screen, and
  read the status line, `tenant`, `budget_credits`, and the
  `X-RateLimit-Limit` header. This mints a new key and prints it, so do not
  share that output.
- Issuance is rate limited per client; per
  `gateway/app/routers/demo_keys.py`, a 429 carries `Retry-After`.

TODO(T-00): `permit_id`, a permit level `expires_at`, and `demo_tool` in the
demo key response are not shipped. The response schema today has a key level
`expires_at` field, which may be null; it is not a permit expiry. Do not demo
those fields until T-00 lands.

## Step 3: governed call and retry on the hosted gateway

The gateway code defines the standard MCP endpoint `POST /mcp`
(`gateway/app/routers/mcp_standard.py`). Send the key as the `X-API-Key`
header. The reference tool in this repository is `partner.echo`, registered
as `name="partner.echo"` in `gateway/app/partner_mcp.py`, a stateless echo
with no persistence or external side effects. Which tools a deployment exposes
is configuration, so this kit does not state what the hosted gateway lists.
Run a `tools/list` call to confirm on the hosted gateway (the endpoint is
stateless and answers in plain JSON, per the module docstring of
`gateway/app/routers/mcp_standard.py`):

```bash
curl -s -X POST "$API_URL/mcp" -H "X-API-Key: $AMW_API_KEY" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

The hosted client lives in the separate
[amw-demo](https://github.com/agentmiddleware/amw-demo) repository, as
described in the [repository README](../README.md):

```bash
git clone https://github.com/agentmiddleware/amw-demo.git
cd amw-demo
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
# AMW_API_KEY and AMW_WALLET_ID exported in step 2
python examples/permit_echo_retry.py
```

Per the README, the example creates a permit, calls the echo tool, retries with
the same key, and checks the receipt offline. What to point at:

- the first call and the retry report the same receipt ID;
- the receipt verifies offline against the public trust keys.

## Step 4: the same flow on a local gateway

Use this if the hosted gateway is unavailable, or to show the tool's side
effect directly. Full detail is in [QUICKSTART.md](../QUICKSTART.md).

Terminal 1:

```bash
git clone https://github.com/agentmiddleware/agent-middleware
cd agent-middleware/gateway
make quickstart
```

Wait for `[quickstart] Trust plane is up: http://127.0.0.1:8000`. The server
binds to loopback only. The local tool is `partner.notes.write`, which appends a
line to `data/dogfood_partner_notes.jsonl`.

Terminal 2, from the directory that contains `agent-middleware`:

```bash
cd agent-middleware/gateway
export API_URL=http://127.0.0.1:8000
PROVISION_JSON=$(curl -s -X POST "$API_URL/v1/dev-keys/self-provision" \
  -H "Content-Type: application/json" -d '{"agent_id": "quickstart"}')
export AGENT_API_KEY=$(echo "$PROVISION_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["api_key"])')

wc -l data/dogfood_partner_notes.jsonl   # note the count (file may not exist yet)
uv run --with "mcp>=1.29,<2" python ../examples/mcp-client/call_with_retry.py
wc -l data/dogfood_partner_notes.jsonl   # count went up by one for two calls
```

- `POST /v1/dev-keys/self-provision`: `gateway/app/routers/dev_keys.py`. Local
  development only; a production configuration refuses to boot with self
  provisioning enabled.
- `examples/mcp-client/call_with_retry.py` calls the tool twice with the same
  `Idempotency-Key` and prints `first receipt:` and `retry receipt:` lines. It
  exits non zero if the receipt IDs differ or either call errors.
- What to point at: both lines show the same receipt ID, and the notes file
  grew by one line for the pair of calls.

This client sends a `text` argument, which fits `partner.notes.write`. It is
not the amw-demo echo client; use step 3 for that.

## Step 5: verify the receipt offline

Replace `rcpt-...` with the receipt ID printed in step 4:

```bash
export RECEIPT_ID=rcpt-...
mkdir -p data/quickstart
curl -s "$API_URL/v1/receipts/$RECEIPT_ID/portable" -H "X-API-Key: $AGENT_API_KEY" \
  -o data/quickstart/receipt-bundle.json
curl -s "$API_URL/.well-known/trust-keys.json" -o data/quickstart/trust-keys.json

PYTHONPATH=../sdk/python/src uv run --with "cryptography>=50" --with "httpx>=0.27,<1" \
  python -m b2a_sdk.verify_cli \
  --bundle data/quickstart/receipt-bundle.json --keys data/quickstart/trust-keys.json
```

- `GET /v1/receipts/{receipt_id}/portable`: `gateway/app/routers/receipts.py`.
- `GET /.well-known/trust-keys.json`: `gateway/app/routers/well_known.py`.
  No credential needed.
- The verifier does not import the gateway.
- Tamper test: `signing_input` is a JSON object stored as a string inside the
  bundle. Change one letter or digit inside a string value in it (for example
  the `tool` value), keeping both the bundle and `signing_input` valid JSON:
  do not touch quotes, backslashes, braces, colons, or commas. Do not edit the
  `kid` value either; an unknown key id reports `UNKNOWN_KEY` and exits 2.
  Rerun the verifier. It prints
  `INVALID  signature does not verify over signing_input` and exits 1
  (`verify_bundle` in `sdk/python/src/b2a_sdk/receipt_verifier.py`, exit codes
  in `sdk/python/src/b2a_sdk/verify_cli.py`). Check with `echo $?`.
- An edit that breaks the JSON is not a tamper verdict. If `signing_input` no
  longer parses, the verifier prints
  `MALFORMED  signing_input is not valid JSON` and exits 2. If the bundle file
  itself no longer parses, it prints `could not read bundle: ...` and exits 2.
  Exit 2 means "could not determine", not "tampered".
- Keys come from the same origin that issued the receipt. Offline verification
  shows the receipt matches that origin's signing key; it does not provide an
  independent trust root. See
  [SECURITY_LIMITATIONS.md](../gateway/SECURITY_LIMITATIONS.md).

## Optional: local proofs with no network

From `gateway/`:

```bash
make demo-trust-plane      # permit, governed call, charge, receipt, replay, denial
make failure-lab           # response lost after the action completed
```

From the repository root:

```bash
bash examples/retry-safe-refund/run.sh   # payout retry against an in memory stub
```

These use local stubs and throwaway databases. No money moves and no provider
is called. Read their own output on the day; this kit does not quote numbers
from them.

## Install path

Use `git clone` as above. TODO(T-01): the `agent-middleware` 0.5.0 package is
not published to PyPI yet. Do not tell judges to `pip install
agent-middleware`.
