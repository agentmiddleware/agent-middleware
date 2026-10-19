# Examples

These scripts are source-level demonstrations for the Agent Middleware API and
its Python SDK (`b2a_sdk`). Start with the [documentation guide](../gateway/docs/README.md)
for the supported evaluation, integration, security, and pilot paths.

## Start with the supported trust loop

For a real local server, your own wallet-scoped key, one governed tool, a
replay attempt, and offline receipt verification, run:

```bash
make quickstart
```

Then follow [docs/quickstart.md](../gateway/docs/quickstart.md). The scripts below are
not the supported design-partner deployment path.

## Prerequisites

- Python 3.11+
- The API running locally (see [docs/quickstart.md](../gateway/docs/quickstart.md))
- `b2a_sdk` installed: `python -m pip install -e './sdk/python[dev]'`

## Available Examples

### `mcp-client/call_with_retry.py` — Golden-path idempotent retry demo

This is the one demo to show a buyer next to `make quickstart`: call a
governed tool through the standard MCP endpoint, then retry with the same
idempotency key. The gateway returns the original signed receipt instead of
dispatching or charging again.

**Run:**

```bash
# From gateway/, after `make quickstart` and the key mint in QUICKSTART.md:
AGENT_API_KEY=<wallet-scoped-local-key> uv run --with "mcp>=1.29,<2" python ../examples/mcp-client/call_with_retry.py
```

Optional overrides: `AMW_MCP_URL` (default `http://127.0.0.1:8000/mcp`) and
`AMW_TOOL` (default `partner.notes.write`).

**Output shape** (both receipt ids match; a real id is `rcpt-` plus 16 hex characters):

```text
first receipt: rcpt-9f2c41a7b6e04d21 outcome: success
retry receipt: rcpt-9f2c41a7b6e04d21 outcome: success
```

An illustrative copy of that shape lives in
[`mcp-client/expected_output.txt`](mcp-client/expected_output.txt).
`uv run --with "mcp>=1.29,<2"` supplies the MCP SDK for that one command.
`outcome: success` is the receipt field the gateway writes on a successful call.

### `mcp-client/mcp.json` — local client config template

Copy-paste starting point for connecting a standard MCP client to a local
gateway. It is a template, not a working buyer config: replace
`${AGENT_API_KEY}` with a real wallet-scoped key (and set a fresh
`${AMW_IDEMPOTENCY_KEY}` per call), or export both variables if your client
expands environment placeholders. The URL points at a local gateway
(`http://127.0.0.1:8000/mcp`); point it at your deploy for anything else.

### `dry_run_example.py` — Legacy Billing Simulation

Demonstrates safe cost estimation without affecting real wallet balances.

**What it shows:**
- Creating a sponsor wallet
- Simulating a multi-step workflow (`generate_video` → `distribute_clip` → `send_iot_message`)
- Comparing two workflow strategies side by side
- Single-shot charge estimation

**Status:** proof-surface demonstration only. It is not a pilot or production
integration path.

**Run locally:**

```bash
# Follow README.md "Run the API locally" first, then use local-only proof flags.
export ENABLE_PROOF_SURFACES=true
uv run --with-requirements requirements.txt uvicorn app.main:app \
  --host 127.0.0.1 --port 8000

# In another shell
B2A_API_KEY=<wallet-scoped-local-key> python examples/dry_run_example.py
```

**Note:** This example uses the **billing router**, which is a proof surface. Production-like deployments keep `ENABLE_PROOF_SURFACES=false`, so these endpoints return 404 unless explicitly enabled. See [docs/PROOF_SURFACES.md](../gateway/docs/PROOF_SURFACES.md).

---

### `mcp_tool_example.py` — MCP Tool Registration & Invocation

Demonstrates how to create, register, and invoke MCP-enabled tools.

**Status:** source-level registration example. It does not configure the
supported one-tool upstream gateway path; use the
[partner first-tool runbook](../gateway/docs/partner-first-tool-runbook.md) for that.

**What it shows:**
- Defining billable tools with `@mcp_tool`
- Registering tools with the service registry
- Generating a `tools.json` manifest
- Running a standalone MCP server

**Run:**

```bash
# 1. Register tools (typically done at app startup)
python examples/mcp_tool_example.py --register

# 2. List available tools
python examples/mcp_tool_example.py --list

# 3. Generate tools.json
python examples/mcp_tool_example.py --generate

# 4. Run standalone MCP server
python examples/mcp_tool_example.py --serve
```

---

### `acta_receipt_interop.py` — Research experiment (not a product path)

Executable research (see the module docstring for the research question):
a signed portable receipt can be transcoded into the ACTA
envelope (an individual Internet-Draft with no IETF standing) without
changing the trust plane. It is not a product surface, route, or SDK
export, the outer signature uses a throwaway generated key, and the demo
entry point reads the API repo's `site/proof/` artifacts, which are not in
this checkout. Do not show it as a buyer integration. Covered by
`gateway/tests/test_acta_receipt_interop.py`.

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| `ModuleNotFoundError: No module named 'b2a_sdk'` | SDK not installed or wrong Python path | Run `python -m pip install -e './sdk/python[dev]'` from the repo root |
| `404 wallet_not_found` | Using a made-up wallet ID | Let the server create the wallet (as `dry_run_example.py` does) |
| `404` on dry-run endpoints | Proof surfaces disabled | Start the API with `ENABLE_PROOF_SURFACES=true` |
| `Connection refused` | API not running | Start `uvicorn app.main:app` first |

---

## Contributing New Examples

If you add a new example, please:
1. Include a module docstring explaining what it demonstrates
2. Add a "Run:" section with copy-pasteable commands
3. Update this README with a new subsection
4. Note if the example depends on proof surfaces
