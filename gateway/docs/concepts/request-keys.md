# Request keys

A request key names one action. When the full request is identical and the
first call completed, a retry with the same key returns the original receipt instead of dispatching again.

Use a stable key when a response may be lost. A request key does not prove that
a remote tool ran exactly once. If delivery is uncertain, reconcile the remote
action before deciding what to do next.

## Key ownership for agent retries

The caller or orchestrator mints one key for each **logical operation** and
keeps it across retries. An LLM tool call's `tool_call.id` is a suitable key
when it is stable for that operation, is at most 128 characters, and has no
control characters. Pass it as `idempotency_key` to the
Python SDK, or as `key` to its synchronous `call` wrapper. Do not mint the key
by hashing the arguments: two intentional tool calls can have identical
arguments and still be different operations. Use a distinct key for each.

Keep the key, tool, full arguments, wallet, and permit stable across a retry.
Once the first call has completed, a retry with the same key returns the original receipt instead of dispatching again.
While it is still running, the gateway can return `idempotency_in_progress`.
A key reused with different arguments is refused as `idempotency_key_reused`:
MCP REST invoke returns HTTP 409 with `detail` set to that string; legacy
`/mcp/messages` and standard `/mcp` return a JSON-RPC error with code
`-32603` and that message. On standard `/mcp`, changing the tool can conflict
earlier during auto-permit minting with JSON-RPC code `-32003` and the same
message. Billing charge uses HTTP 409 with `detail.error` set to the code;
permit creation uses HTTP 409 with string `detail`.

When the caller supplies no key to a mutating tool, standard `/mcp` and the
legacy `/mcp/messages` demo auto-permit path derive an `mcp-retry-` key from
wallet, API key, verified enterprise principal, tool, and canonical arguments.
An identical retry within `MCP_UNKEYED_RETRY_WINDOW_SECONDS` (default 300) of
the first call replays its receipt; the window slides from that first call,
so a retry across a clock bucket edge still replays. If the stored result is
a refunded pre-dispatch failure (nothing reached the tool), the retry runs
again instead. Tools registered `read_only` keep a fresh key per call.
Identical deliberate calls inside one window collapse into one, so send your
own key when that matters. A window of `0` restores a fresh `mcp-auto-` key
per call. The SDK's `invoke_tool`
requires a key; its `"auto"` option derives one from arguments and is
unsuitable when distinct calls can have the same arguments.

## Optional argument-derived helper

| Python | TypeScript | CLI |
|---|---|---|
| Available | Coming | Coming |

The Agent Middleware (AMW) Python helper derives a key from the tool, selected
arguments, an optional business ID, and an optional scope. The output starts
with `amwk1_` and is 58 characters long. Install the SDK from this checkout as
shown in the
[Python SDK guide](https://github.com/agentmiddleware/agent-middleware/blob/main/sdk/python/README.md).
Use this helper only when your operation rules make that combination identify
one action. For LLM tool calls, pass the stable `tool_call.id` instead.

```python
from amw import request_key

args = {"order_id": "A1", "amount": "12.00"}
key = request_key("partner.refund", args, business_id="A1")
```

`AgentMiddlewareClient` currently calls governed tools with `invoke_tool`.
Pass the key explicitly, or ask that method to derive it:

```python
result = await client.invoke_tool(
    "partner.refund",
    args,
    wallet_id=wallet_id,
    permit_id=permit_id,
    idempotency_key=key,
)

result = await client.invoke_tool(
    "partner.refund",
    args,
    wallet_id=wallet_id,
    permit_id=permit_id,
    idempotency_key="auto",
    business_id="A1",
)
```

The examples assume that `client`, `wallet_id`, and `permit_id` have already
been set up for the governed tool. Both calls above use the same derived key;
two separate calls with the same inputs would also share it.

## Retry after a timeout

Keep the key and the full arguments when retrying after a lost response. If the
first call completed, the retry returns its stored receipt ID. The
[local demo](https://github.com/agentmiddleware/agent-middleware/blob/main/QUICKSTART.md)
prints `first receipt` and `retry receipt` with the same ID for this case.
Here `tool_call` is the original call object retained by the orchestrator.

```python
args = {"order_id": "A1", "amount": "12.00"}
key = tool_call.id  # Keep this operation ID when retrying.

first = await client.invoke_tool(
    "partner.refund",
    args,
    wallet_id=wallet_id,
    permit_id=permit_id,
    idempotency_key=key,
)
retry = await client.invoke_tool(
    "partner.refund",
    args,
    wallet_id=wallet_id,
    permit_id=permit_id,
    idempotency_key=key,
)
assert first.receipt.receipt_id == retry.receipt.receipt_id
```

If the response is `idempotency_in_progress`, wait and retry with the same key
and arguments. If it is `delivery_uncertain`, check the remote action before
considering another dispatch.

## Refund by order ID

Use a business ID that identifies this particular action. An order ID works
when the rule is one refund per order. If two separate refunds for the same
order are valid, give each refund its own business ID.

```python
request_key(
    "partner.refund",
    {"order_id": "A1", "amount": "12.00", "currency": "USD"},
    business_id="A1",
)
```

## See what the helper dropped

`explain` returns the key plus the argument paths used and dropped. Known
fields that change on each try are dropped by default. The helper also warns
with `unstable_field` when it drops one.

```python
info = request_key.explain(
    "partner.refund",
    {"order_id": "A1", "amount": "12.00", "requested_at": "2026-10-21T09:00:00Z"},
    business_id="A1",
)
print(info.key)
print(info.used_fields)
print(info.dropped_fields)  # includes requested_at
```

**Exclusion changes only key generation.** The gateway compares the full
arguments sent with each call. If `requested_at` changes between calls but the
key stays the same, the gateway returns `idempotency_key_reused`. Keep the
actual arguments identical for a retry.

## What goes into the key

| Input | Default |
|---|---|
| Tool name | Included |
| Business arguments such as amount, currency, and recipient | Included |
| `business_id` | Included when supplied |
| `scope` | Included when supplied |
| `timestamp`, `ts`, `requested_at`, `now`, `nonce`, `request_id`, `trace_id`, `attempt` | Dropped |
| `note`, `reason`, `memo`, `permit_id` | Dropped |

Use `include=["note"]` to keep a field that the helper would normally drop,
or `exclude=["custom_time"]` to drop another field. The lists accept argument
field paths. Choose one list per call. Inspect `explain` whenever you change
these rules. A path through an array applies to every item. Escape a literal
dot or backslash in a field name with a backslash. Changing these rules changes
future keys for the same input.

The [version 1 specification and vectors](https://github.com/agentmiddleware/agent-middleware/tree/main/spec/request-key-v1)
pin the canonical JSON and SHA 256 derivation. The prefix identifies this
version. The helper rejects NaN and Infinity. It warns with `float_argument`
for included floats because decimal values can shift between tries.

## When a key is reused for another action

If an included argument changes, the helper makes a different key. If a caller
reuses an old key with changed arguments, the
gateway returns `idempotency_key_reused`. This also happens when only an
excluded argument changes, because the gateway still checks the full request.
The billing and MCP REST invoke routes use HTTP 409 for this code. JSON RPC
calls report it in their protocol error response.

`idempotency_key_conflict` means that the key in the header differs from the
key in the request body. It is a reason under `invalid_idempotency_key`, not
the code for changed action arguments.

| Code | Message | What to do |
|---|---|---|
| `idempotency_key_reused` | This request key was already used for a different action. | For a new action, make a new key. For a retry, send the same arguments as the first time. |
| `idempotency_key_conflict` | You sent two different request keys in one call. | Send the key in one place, or the same key in both. |
| `idempotency_key_too_long` | This request key is too long. | Use 128 characters or fewer. Keys from the helper always fit. |
| `idempotency_in_progress` | The first try with this key is still running. | Wait a moment and try again with the same key. |
| `unstable_field` | This field changes on every try, so we left it out of the key: `{field}`. | Nothing to do. To keep it, add it to `include`. |
| `float_argument` | Decimal numbers can change slightly between tries. | Send amounts as text, like `"12.00"`. |

## Common mistakes

1. A custom time or random ID can create a new key on every retry. Add it to
   `exclude` and inspect `explain`. Resend identical full arguments to the
   gateway.
2. A model can rewrite a note on retry. Excluding `note` keeps the helper key
   stable, but a changed note in the request still gets
   `idempotency_key_reused`.
3. Use text such as `"12.00"` or integer minor units for amounts. Avoid floats.
4. Two intentional actions with the same tool and arguments need distinct
   caller-owned keys, such as their separate `tool_call.id` values. If you use
   the helper for a different workflow, provide a distinct business ID.
5. Changing `include` or `exclude` changes the derived key. Keep the rule
   stable for retries.
6. The helper handles JSON field order and spacing. Do not hash a hand made
   JSON string.
7. A key is a hash, but avoid logging raw arguments beside it, especially if
   the arguments contain secrets.
8. A stable key does not guarantee a remote effect happened exactly once.
   `delivery_uncertain` still needs reconciliation.

## Bring your own key

You may send a key you made yourself. It must be a nonblank string of at most
128 characters with no control characters. The gateway records it exactly as
sent and does not trim or normalize it. Keep it and the full request stable
across retries. The Python SDK strips leading and trailing whitespace from a
supplied key before sending it.

The Python SDK and the [specification](https://github.com/agentmiddleware/agent-middleware/tree/main/spec/request-key-v1)
are covered by:

Apache 2.0 (gateway is Fair Source, FSL-1.1)

See [licensing](https://github.com/agentmiddleware/agent-middleware/blob/main/LICENSING.md).
