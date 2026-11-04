# LangGraph agent with AMW permits

This example runs a LangGraph tool through Agent Middleware's governed loop on your own machine. It starts a local gateway, gets a permit for one mock tool, calls the tool once with an idempotency key you choose, and prints the signed receipt. It then retries with the same key and gets the same receipt back without running the tool again. Last, it shows two denied calls: one with no permit and one for a tool the permit does not allow.

Everything is local. The tool only writes to a list in memory. No model is called and no network service is used. The SDK supports an ASGI transport, so the gateway runs in process with a `http://127.0.0.1` base URL and no listening socket. Database state lives in a temporary directory and is removed on exit. The signing key and both API keys are generated fresh in memory and are never printed or saved.

## Run

From the repository root with Python 3.12 and uv installed:

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r gateway/requirements.txt -e './sdk/python[verify]' 'langgraph==1.2.19' 'langchain-core==1.7.5' pytest-timeout
PYTHONPATH="$PWD/integrations:$PWD/gateway" .venv/bin/python examples/langgraph-permit/demo.py
```

After installing, `examples/langgraph-permit/run.sh` runs the same command. No user supplied keys are needed. The LangGraph version satisfies the existing integration dependency floor. Both framework packages use the MIT license.

Smoke test, with a pytest timeout of 110 seconds and a subprocess deadline of 100 seconds:

```bash
PYTHONPATH="$PWD/integrations:$PWD/gateway" .venv/bin/python -m pytest examples/tests/test_langgraph_permit_example.py -q
```

The test blocks non loopback socket connections, name resolution and datagrams, including during imports. It verifies the exported receipt again outside the gateway process. No proof surfaces are enabled.

## What you should see

The following shape is illustrative. Receipt identifiers and charged credits come from each actual run. Angle brackets are placeholders, not recorded receipts.

```text
First receipt: <receipt_id> outcome: success credits_charged: <credits_charged>
Replay receipt: <same_receipt_id>
Local denial: permit_tool_not_allowed; HTTP requests: 0
No permit denial: -32003 permit_required
Offline Ed25519 receipt verification: verified
Tool executions: 1
Receipt: examples/langgraph-permit/output/receipt-bundle.json
Public keys: examples/langgraph-permit/output/trust-keys.json
```

The exported files contain the actual portable receipt and public verification keys. They are ignored by git.

## Notes

* The idempotency key is yours. Reuse it for a retry of the same action. Use a new key for a new action. If you omit it, the wrapper derives one from the permit, tool name and arguments, so two identical calls share a key.
* A compiled LangGraph graph runs `ToolNode` with a hand built `AIMessage`. The client stubs describe tools and never execute their bodies. Only `demo.notes.append` is registered with the gateway.
* The wrapper stores `last_receipt` in the calling task's context. ToolNode runs tools in child tasks and `as_tool` exposes the framework tool rather than that wrapper. This example therefore reads the actual gateway receipt summary in its ASGI transport, then fetches and verifies the portable receipt. It does not read a parent task's empty receipt context.
* The local permit check is a fast mirror. The gateway still decides and can deny a call the local check allowed.
* This example calls `/mcp/messages`, where a missing permit is refused. The standard `POST /mcp` endpoint mints a short lived permit for you when none is sent, because standard MCP clients cannot send permit context.
* The example only reads receipts. It does not change how receipts are made.
* This is a local demonstration, not a production setup. Read [SECURITY_LIMITATIONS.md](../../gateway/SECURITY_LIMITATIONS.md) before using this pattern for real actions.
