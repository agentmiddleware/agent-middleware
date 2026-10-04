# Permit requests (agent asks, human approves, middleware mints)

**Audience:** operators and partner integrators.
**Product lens:** [`WEDGE.md`](../WEDGE.md) — this is the step *before* the
governed permit→invoke→receipt loop: an agent with no authority asks a human
for some, and gets a signed permit only if a human says yes.

The [human approval gate](human-approval-gate.md) pauses a call under a permit
that already exists. This covers where the permit comes from.

## The flow

```
agent → POST /v1/permit-requests        {scope, budget, expiry, justification}
          fail-closed checks (Sentinel configured? issuer funds the subject?)
          → row written, permit id reserved, human paged via Sentinel
          → 202 {request_id, status: "pending", poll_url}

human ← Sentinel email/SMS + the approval card (scope, budget, justification)

agent → GET /v1/permit-requests/{id}    (poll)
          ├─ still pending   → 202 {status: "pending"}
          ├─ Sentinel down   → 503 human_approval_unavailable (retryable)
          ├─ rejected        → 200 {status: "rejected"}   nothing minted
          ├─ window elapsed  → 200 {status: "expired"}    nothing minted
          ├─ mint in flight  → 202 {status: "minting"}
          └─ approved        → 200 {status: "approved", permit_id, permit{…}}
```

The returned `permit` is an ordinary signed permit: it appears in
`GET /v1/permits/{permit_id}`, carries the signature and key id, and drives
governed invokes exactly like any other.

## What the human is deciding

Scopes, tools, budget, permit expiry, and the `requires_human_approval` flag
are frozen on the `permit_requests` row at request time and hashed into
`request_hash`. The mint reads that row — never the polling request — so an
approved request cannot be re-aimed at a wider scope or a bigger budget after
the decision. A retry of the same `Idempotency-Key` with different terms is a
`409 permit_request_terms_conflict`, not a second page to the human.

Scopes default to `tool:<name>:invoke` for each requested tool, plus
`billing:charge`, mirroring `POST /v1/permits`.

## The approver card

`GET /v1/permit-requests/{id}/card` renders the approver's view — scope,
budget, justification, decision deadline — as a hosted HTML page. The
notification email renders from the same template
(`app/services/approval_card.py`), so the two surfaces cannot show different
terms for one decision. Both are read-only disclosures: **approve/reject
happens in Sentinel**, whose magic link is the card's primary action when the
create response carried one. Only an absolute `https://` link is kept; any
other scheme (`javascript:`, `data:`, plain `http:`, relative) is dropped and
logged as `sentinel_approval_url_rejected`, and the card falls back to
pointing the approver at the Sentinel notification.

The card is authorized like the request itself (issuer wallet, subject wallet,
or bootstrap admin) and served `no-store`, `X-Robots-Tag: noindex`.

Email delivery is best effort and only fires for the caller that actually
created the row, so a retry cannot page the approver twice. It goes out via
Resend to the email entries in `SENTINEL_APPROVERS` when `RESEND_API_KEY` is
set; Sentinel remains the decision channel either way.

## A permit is minted at most once

- The `pending → minting` transition is a conditional UPDATE. Concurrent
  pollers and multiple workers race there; exactly one wins and mints.
- The permit id is **reserved when the human is paged** and used as the minted
  permit's primary key. A mint retried after a crash therefore collides on
  `permits.permit_id` and adopts the existing permit instead of issuing a
  second one carrying the same authority.
- A `minting` claim older than 120s is assumed to belong to a dead worker and
  may be retaken — safe only because of the reservation above.
- If minting fails against the world (the subject wallet no longer covers the
  approved budget, the requested expiry has passed), the request is terminal
  `failed` with the reason. Asking again requires a new human decision.

## Expiry is enforced here, not in Sentinel

Sentinel has no expired state: a timed-out approval stays `"pending"` in its
API forever. The middleware stamps
`expires_at = requested_at + PERMIT_REQUEST_TIMEOUT_SECONDS` and checks it
**before** polling Sentinel, so a decision arriving after the window mints
nothing.

## Seeing what is outstanding

`GET /v1/me/permit-requests` lists the calling wallet's own requests, newest
first, with an optional `?status=` filter. Unlike polling one request by id, it
**never advances a decision** — an agent can survey what it is waiting on
without paging a human or minting anything as a side effect of looking.

## Authorization

- The caller must hold the **subject** wallet — it is asking for authority over
  its own spend.
- The **issuer** must be the subject wallet or a wallet that funds it in the
  sponsor → agent → child hierarchy. Asking an unrelated wallet for authority
  is `403 issuer_wallet_access_denied`.
- Reading a request (poll or card) is limited to the issuer wallet, the subject
  wallet, or a bootstrap admin.

## Configuration

| Variable | Default | Notes |
|----------|---------|-------|
| `SIMULATION_MODE_HUMAN_APPROVAL` | `true` | Shared with the invoke gate |
| `SENTINEL_API_URL` / `SENTINEL_API_KEY` | empty | Required in real mode |
| `SENTINEL_APPROVERS` | empty | Comma-separated; email entries also get the card |
| `SENTINEL_RISK_LEVEL` | `high` | Forwarded on the approval request |
| `PERMIT_REQUEST_TIMEOUT_SECONDS` | `3600` | Local decision window (60..86400) |
| `RESEND_API_KEY` / `ALERT_FROM_EMAIL` | empty | Optional card email delivery; set both, with a sender address you control |

## Fail-closed rules

- **Simulated requests never mint production authority.** With
  `SIMULATION_MODE_HUMAN_APPROVAL=true` in a production-like environment, the
  request is refused (`human_approval_not_configured`) — at creation, and on
  poll for a request banked earlier in dev. In local/dev, simulation approves
  instantly and the permit comes back on the first call, marked `simulated`.
- **Real mode without Sentinel config fails closed**, the same way.
- **A Sentinel outage never mints.** Create returns `503` having written
  nothing, so the same `Idempotency-Key` can be retried; poll returns `503`
  leaving the request pending.

## Example

```bash
REQ=$(curl -sS -X POST "$API_URL/v1/permit-requests" \
  -H "X-API-Key: $AGENT_KEY" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H 'Content-Type: application/json' \
  -d '{
        "issuer_wallet_id": "'"$SPONSOR_WALLET"'",
        "subject_wallet_id": "'"$AGENT_WALLET"'",
        "allowed_tools": ["fetch-url", "summarize", "translate", "send-email"],
        "max_credits": 20,
        "expires_at": "2026-09-01T00:00:00Z",
        "justification": "Draft and send the weekly digest to the ops list."
      }' | jq -r .request_id)

# poll until the human decides
curl -sS "$API_URL/v1/permit-requests/$REQ" -H "X-API-Key: $AGENT_KEY" | jq '.status, .permit_id'
```

Migration `030_permit_requests` creates the table; no configuration beyond the
Sentinel variables above is required to roll it out.
