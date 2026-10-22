# Request key v1

A request key names one intended action. The Python helper derives it from the
tool name, selected arguments, optional business ID, and optional scope.

The exact input to SHA-256 is the UTF-8 encoding of this JSON object:

```json
{"v":1,"tool":"partner.refund","args":{"amount":"12.00","order_id":"A1"},"business_id":"A1","scope":null}
```

Object keys are sorted at every level. JSON has no extra whitespace, keeps
Unicode characters as UTF-8, and rejects NaN and Infinity. These are the
strict settings of the gateway's `canonical_json(payload, ensure_ascii=False,
allow_nan=False)`, matching the byte rules used by `amw-action-json-v1`.
The helper does not use the full action permit envelope. It encodes `null` for
an omitted business ID or scope. Arrays retain their order.

The key is `amwk1_` followed by the first 52 characters of uppercase, unpadded
base32 of the SHA-256 digest. Its length is 58 characters.

Before hashing, the helper removes argument fields named `timestamp`, `ts`,
`nonce`, `request_id`, `trace_id`, `attempt`, `requested_at`, `now`, `note`,
`reason`, `memo`, or `permit_id`, at any object depth. `exclude` also removes
named dotted field paths. `include` selects only named dotted paths and can
retain a field normally removed by the default list. `explain` reports sorted
dotted leaf paths for fields used and dropped. The committed
[`vectors.json`](vectors.json) pins the input, filtered arguments, canonical
bytes, and expected key for independent implementations.
Paths through arrays apply to every item and omit array indices in `explain`.
A literal dot or backslash in a field name is escaped with a backslash in a
field path.

The gateway compares the full arguments sent with a request to its first
accepted request. Changing an excluded argument while reusing the same key
causes `idempotency_key_reused`; the first result is not replayed. The billing
REST and MCP REST invoke routes return HTTP 409 for that code. JSON-RPC
surfaces report it inside their protocol error envelope. Keep the arguments
sent on a retry the same as the first try.

License: Apache-2.0. The gateway implementation is FSL-1.1.
