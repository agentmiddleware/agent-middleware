# Goose + Agent Middleware

Runs Agent Middleware (AMW) as a normal Goose **StreamableHttp MCP extension**
that proxies the official `@modelcontextprotocol/server-filesystem` server
(`read_text_file`, `write_file`), plus a Goose **PreToolUse deny hook**.

Every proxied call goes through permit -> idempotency -> invoke -> receipt.

## Pieces

| Piece | What it does |
|---|---|
| `goose-amw-proxy` | StreamableHttp MCP server (protocol 2026-07-28 and older). Verifies the caller, enforces permits, dedupes writes, signs receipts. |
| `goose-amw-hook` | Goose `PreToolUse` hook. A call with no permit is blocked **before it runs**; Goose shows `Tool call denied by policy hook ...: AMW permit denied: <reason>`. |
| `plugin/amw-permit/hooks/hooks.json` | Template plugin that wires the hook to `amw__*` tools. |

The proxy enforces the same permit rules server-side, so a client without the
hook is still refused.

## Upstream server

The filesystem server is pinned: `AMW_FS_SERVER_PACKAGE` (default
`@modelcontextprotocol/server-filesystem@2.0.1`) is passed to `npx -y`, so a
new upstream release never runs without an explicit change.

A failure before the tool call is sent (spawn, session setup, `initialize`)
is safe to retry: AMW restores the idempotency key to its state before the
attempt and issues a `not_dispatched` receipt. Only a failure after dispatch
is recorded as `failed_unknown`.

## Permit policy

```json
{"rules": [
  {"id": "write-allowed", "tool": "write_file", "folder": "/path/to/allowed"},
  {"id": "read-sandbox", "tool": "read_text_file", "folder": "/path/to/sandbox"}
]}
```

A call is allowed only if a rule names the tool and the resolved `path` is
inside the rule's folder (`..` and symlinks are resolved first). Anything else
is denied with a reason.

## Idempotency key

AMW keys a write by **verified caller identity + tool name + canonical
argument hash + `agent-session-id`**.

Goose (protocol 2026-07-28) sends `_meta["agent-tool-call-request-id"]`, but
that id is the model's tool-call id: when a call fails and the model retries,
Goose sends a **new** id (recorded in `tests/fixtures/g1_gate_calls.jsonl`).
AMW therefore logs it on receipts and in the ledger but never uses it as the
key. A **write without `agent-session-id`** (for example an older protocol with
no `_meta`) is refused with a clear reason and a `refuse` receipt, because it
cannot be deduplicated safely. Reads do not need a session id.

The caller is verified from the `Authorization: Bearer` header against
`AMW_CALLERS` (a JSON map of `sha256(token)` to caller id). Unverified calls
are refused.

## Replay window

`AMW_IDEMPOTENCY_WINDOW_SECONDS` (default **600**, must be > 0).

* A completed write repeated with the same key **inside** the window is not
  run again; AMW returns the stored result and issues a `replayed` receipt.
* The same write made **after** the window runs normally, so deliberate
  identical writes later in a session are not swallowed.
* A write whose outcome is unknown (`failed_unknown`: the call was
  dispatched and the response was lost, or AMW crashed mid-call) is **never
  re-run automatically**, inside or outside the window. Retries are refused
  with a reason until an operator resolves the entry.
* A definite upstream error (`failed`, no effect) may be retried.

Choose a window longer than your client's retry horizon and shorter than the
interval at which you write identical content on purpose.

## Receipts

Each decision (allow, deny, replay, refuse) appends an Ed25519-signed receipt
to `$AMW_STATE_DIR/receipts.jsonl`: `tool`, `args_hash`, `decision`,
`outcome`, `issued_at`, plus caller, session, key and the logged request id.
The signature covers the canonical JSON of `body`; the public key is written
to `receipt_public_key.b64`.

## Goose setup

```yaml
extensions:
  amw:
    type: streamable_http
    name: amw
    uri: http://127.0.0.1:8791/mcp
    headers:
      Authorization: Bearer ${AMW_TOKEN}
    timeout: 120
    enabled: true
```

Install `plugin/amw-permit` into `.agents/plugins/` (project) or the Goose
plugins directory, replacing the paths in its `hooks.json`.

## Tests

`pytest` needs no external data. `tests/fixtures/g1_gate_calls.jsonl` is the
real call log recorded by AMW during the G1 gate session.
`tests/e2e/fault_proxy.py` is a test-only launcher that injects one fault into
the shipped proxy to record the cut-stream and unknown-outcome sessions; it
is not part of the package.

An error result from the upstream server for a write is recorded as
`failed_unknown`: the pinned server writes files in place, so the file may be
partly written, and the write is never re-run automatically.
