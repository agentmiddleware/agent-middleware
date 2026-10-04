# Trust Plane Demo Output

Generated with:

```bash
make demo-trust-plane-check
```

The exact IDs are intentionally different on every run because the demo uses a
fresh throwaway SQLite database and signs new permits/receipts.

Example proof artifact:

```json
{
  "agent_key_id": "key_cd84a4595f5b",
  "agent_wallet_id": "agt-4b8ae3c2cbe9",
  "audit_chain_checked_events": 1,
  "cross_wallet_status": 403,
  "denial_reason": "permit_tool_not_allowed",
  "denial_replay_receipt_id": "rcpt-5c957b40350e4b3a",
  "denial_receipt_id": "rcpt-5c957b40350e4b3a",
  "gateway_latency": {
    "max_ms": 100.0,
    "mean_ms": 69.7,
    "min_ms": 59.9,
    "p50_ms": 67.3,
    "p95_ms": 83.7,
    "path": "POST /mcp/messages",
    "samples": 100,
    "tool": "trust-plane-echo",
    "transport": "in-process ASGI client, local SQLite, no network"
  },
  "inspected_audit_events": 1,
  "inspected_receipts": 1,
  "ledger_entry_id": "543e21a1-5056-4df8-8773-fbf6ba9c720c",
  "permit_id": "permit-0f62fdfee59640e4",
  "replay_receipt_id": "rcpt-26e46941ba4a4bb6",
  "signing_key_id": "demo-ed25519",
  "sponsor_wallet_id": "spn-54322a836193",
  "success_receipt_id": "rcpt-26e46941ba4a4bb6",
  "ungoverned_denial_reason": "permit_required"
}
```

What this proves:

- The permit is scoped to one MCP tool.
- The successful MCP invocation produces a signed receipt tied to a ledger entry.
- Replaying the same idempotency key returns the same receipt and does not
  create a second debit.
- An out-of-scope MCP tool is denied and produces a denial receipt.
- Replaying that denial returns the same denial receipt and response semantics.
- An MCP call with no permit at all is denied with `permit_required`, proving
  the trust plane fails closed when `ALLOW_LEGACY_UNPERMITTED_MCP=false`.
- The agent API key cannot read the sponsor wallet.
- A run of fresh governed calls is timed, so the public site can publish what
  the boundary costs in time. The numbers differ on every run and every
  machine: they are measured in-process against local SQLite with a stand-in
  echo tool, which makes them a reference point for the gateway's own handler
  time, not a production latency.
- The wallet-scoped audit chain verifies after the governed action, and the
  audit event links back to permit, idempotency key, request hash, and ledger
  entry.
- Public signing-key metadata can be inspected without exposing private key
  material.
