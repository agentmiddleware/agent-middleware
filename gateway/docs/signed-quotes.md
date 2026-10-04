# Signed quotes (a price you can rely on)

**Audience:** operators and partner integrators.
**Product lens:** [`WEDGE.md`](../WEDGE.md) — the governed loop already tells an
agent what it *was* charged. A quote tells it what it *will* be charged, in a
form it can check.

## What it does

Tool prices move. An agent that budgets against a price it read a moment ago —
or that must clear a [human approval](permit-requests.md) before it can spend —
has no way to know what the call will actually cost. A quote closes that gap:

```
agent → POST /v1/quotes {wallet_id, tool}
          → 201 signed quote: quoted_credits, expires_at, signature, key_id

agent → tools/call  mcpContext.quote_id = <quote>
          → the charge uses quoted_credits, whatever the tool costs now
```

The quote **locks the price**. If the tool's registered price rises before the
invoke, the wallet is charged the quoted number; if it falls, the wallet is
still charged the quoted number. A lock is a commitment, not a best-price
guarantee.

## Single use, short window

A quote authorizes **one** charge. It is spent by an atomic
`active → consumed` UPDATE that also requires `expires_at > now`, so two
concurrent invokes cannot both ride it and the active-vs-expired boundary is
decided in one place. Without single use, a cheap quote would be a standing
right to the old price at unlimited volume for the whole window.

`QUOTE_TTL_SECONDS` (default **600**, clamped to 30..3600) sets the window —
long enough for a human-in-the-loop hop, short enough to bound exposure to
price drift.

## Invalid quotes deny; they never silently reprice

| Reason | Meaning |
|--------|---------|
| `quote_not_found` | No such quote id |
| `quote_wallet_mismatch` | Quote belongs to a different wallet |
| `quote_tool_mismatch` | Quote was issued for a different tool |
| `quote_expired` | The window elapsed before the invoke |
| `quote_already_consumed` | Already spent (or lost the single-use race) |

`POST /v1/quotes` itself answers `400 tool_price_invalid` and signs nothing when
the tool's registered price is not finite, positive, and losslessly storable
in the shared eight-decimal credit format. Zero-price governed calls are currently
unsupported: the billing layer requires positive charge units. The quote endpoint
therefore refuses a zero price before issuing a commitment that cannot be honored.
This does not add a free-execution billing path.

All of these deny the invoke (`403`, JSON-RPC `-32603`) rather than falling
back to the live price. The caller asked to be charged a specific number;
substituting a different one is the one outcome a price lock must never
produce. Re-quote and retry.

An invoke that consumed a quote and then failed to charge (insufficient funds)
**returns the quote to `active`**, so a top-up inside the window can still use
the price it was promised.

## Verifiable offline

The signature covers `(quote_id, wallet_id, tool, quoted_credits, category,
issued_at, expires_at)` using the same canonical payload and Ed25519 key as
permits and receipts, so a quote verifies offline against the published key
document (`/.well-known/trust-keys.json`) without trusting this API's read
endpoint.

The signed `status` is pinned to `active`: the signature attests to the
*commitment*, so spending a quote does not invalidate the proof of what was
promised. The `quotes` row records `consumed_by_idempotency_key`, linking a
spent quote to the invoke that spent it.

## Endpoints

| Method | Path | Notes |
|--------|------|-------|
| `POST` | `/v1/quotes` | Body `{wallet_id, tool}`. Caller must own the wallet. No `Idempotency-Key`: quoting has no side effect on the wallet, and a duplicate quote is just a second unspent commitment that expires on its own. |
| `GET` | `/v1/quotes/{quote_id}` | Wallet-scoped read. Reports `expired` once the window has passed. |
| `GET` | `/v1/me/quotes` | The calling wallet's own quotes. `?status=active` lists the spendable ones; `?status=expired` also catches rows still stored as `active` but past their window. |

Spending happens on the existing governed invoke — pass `quote_id` in
`mcpContext` alongside `wallet_id`, `permit_id`, and `idempotency_key`.

## Example

```bash
QUOTE=$(curl -sS -X POST "$API_URL/v1/quotes" \
  -H "X-API-Key: $AGENT_KEY" -H 'Content-Type: application/json' \
  -d '{"wallet_id": "'"$WALLET"'", "tool": "summarize"}')

echo "$QUOTE" | jq '{quoted_credits, expires_at, signature}'

curl -sS -X POST "$API_URL/mcp/tools/summarize/invoke" \
  -H "X-API-Key: $AGENT_KEY" -H 'Content-Type: application/json' \
  -d '{
        "name": "summarize",
        "arguments": {"text": "..."},
        "mcp_context": {
          "wallet_id": "'"$WALLET"'",
          "permit_id": "'"$PERMIT"'",
          "quote_id": "'"$(echo "$QUOTE" | jq -r .quote_id)"'",
          "idempotency_key": "'"$(uuidgen)"'"
        }
      }' | jq '.receipt.credits_charged'
```

Migration `031_quotes` creates the table. No configuration is required beyond
the optional `QUOTE_TTL_SECONDS`; invokes that pass no `quote_id` are
unchanged and pay the live price.
