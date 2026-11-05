# Demo script

Every endpoint and command below exists in this repository. Code locations are
given so a judge can check. Facts marked "observed 2026-11-05" were observed by
the AMW maintainers with curl against the hosted gateway at
`https://api.agentmiddleware.dev` on 2026-11-05 at about 3:18 AM PT. They are
not a guarantee and can change. Re-run the curl command shown in that step to
confirm them yourself. Each observed value also names the code default that
backs it, so you can check the value in this repository.

Never paste a real API key into a shared document, issue, or screen recording.
The commands below read keys from environment variables.

## Step 1: health check (no credential)

```bash
curl -s https://api.agentmiddleware.dev/health/dependencies
```

- Route: `GET /health/dependencies` in `gateway/app/main.py`. Unauthenticated.
- Reports dependency status for the transaction integrity boundary (database,
  cache, signing key, upstream MCP) plus version and commit.
- Observed 2026-11-05 (maintainers, curl, about 3:18 AM PT; not a guarantee):
  HTTP 200, version `1.3.0`. Confirm by re-running the curl above with
  `-i` to see the status line. Code default: `__version__ = "1.3.0"` in
  `gateway/app/__init__.py` and `APP_VERSION: str = "1.3.0"` in
  `gateway/app/core/config.py`.

## Step 2: mint a demo key

```bash
export API_URL=https://api.agentmiddleware.dev
DEMO_JSON=$(curl -s -X POST "$API_URL/v1/demo/keys" \
  -H "Content-Type: application/json" -d '{"utm_source":"competition-kit"}')
export AMW_API_KEY=$(echo "$DEMO_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["api_key"])')
export AMW_WALLET_ID=$(echo "$DEMO_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["wallet_id"])')
```

- Route: `POST /v1/demo/keys` in `gateway/app/routers/demo_keys.py`. Anonymous,
  returns 201 when the demo tenant is enabled. `GET /v1/demo/keys` returns
  usage help.
- The key is shown once. Do not print `$DEMO_JSON` on a shared screen.
- Response fields defined in code (`DemoKeyIssueResponse`): `api_key`,
  `key_id`, `key_prefix`, `wallet_id`, `sponsor_wallet_id`, `tenant`,
  `allowed_tools`, `expires_at`, `max_uses`, `budget_credits`, `base_url`,
  `note`.
- Observed 2026-11-05 (maintainers, curl, about 3:18 AM PT; not a guarantee):
  HTTP 201 with `api_key`, `wallet_id`, `key_id`, `sponsor_wallet_id`,
  `tenant` = `demo`, `budget_credits` = 1000. Code default:
  `DEMO_WALLET_CREDITS: Decimal = Decimal("1000")` in
  `gateway/app/core/config.py`. Credits are metering units, not money.
- Observed 2026-11-05 (maintainers, curl, about 3:18 AM PT; not a guarantee):
  response header `X-RateLimit-Limit: 120`. Code default:
  `RATE_LIMIT_PER_MINUTE: int = 120` in `gateway/app/core/config.py`; the
  header is written by `RateLimitMiddleware` in
  `gateway/app/core/rate_limiter.py`, which takes that default.
- To confirm both yourself, run the `POST /v1/demo/keys` curl by itself with
  `-i` added (not inside the `DEMO_JSON=$(...)` assignment, where the headers
  would break the JSON parsing), on a private screen, and read the status
  line, the `X-RateLimit-Limit` header, and `budget_credits`. This mints a new
  key and prints it, so do not share that output.
- Issuance is rate limited per client; a 429 carries `Retry-After`.

TODO(T-00): `permit_id`, a permit level `expires_at`, and `demo_tool` in the
demo key response are not shipped. The response schema today has a key level
`expires_at` field, which may be null; it is not a permit expiry. Do not demo
those fields until T-00 lands.

## Step 3: governed call and retry on the hosted gateway

The hosted gateway exposes the standard MCP endpoint `POST /mcp`
(`gateway/app/routers/mcp_standard.py`). Send the key as the `X-API-Key`
header. The public reference tool on the hosted demo is `partner.echo`
(observed 2026-11-05 by the maintainers with curl, about 3:18 AM PT; not a
guarantee). Code: registered as `name="partner.echo"` in
`gateway/app/partner_mcp.py`, a stateless echo with no persistence or external
side effects. To confirm which tools the hosted gateway lists for your key,
re-run a `tools/list` call yourself (the endpoint is stateless and answers in
plain JSON, per the module docstring of `gateway/app/routers/mcp_standard.py`):

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
not the hosted `partner.echo` client; use step 3 for that tool.

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
