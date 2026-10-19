# Human approval gate (Sentinel)

**Audience:** operators and partner integrators.
**Product lens:** [`WEDGE.md`](../WEDGE.md) — this extends the governed
permit→invoke→receipt loop with a human-in-the-loop pause, backed by
[Sentinel](https://pauseapi.app) (`sentinel-api`).

For the step before this one — an agent with no authority asking a human for a
permit — see [`permit-requests.md`](permit-requests.md). Both gates page the
same Sentinel tenant.

## What it does

A permit created with `requires_human_approval: true` makes every governed
invoke under it block on a human decision **before any budget is reserved or
credits are charged**. The decision is requested from Sentinel
(`POST /v1/approvals` → email/SMS magic links → approve/reject), and the
outcome is welded into the trust plane:

- the signed **receipt** carries `approval_id`
- the signed **audit chain** metadata records `approval_id`,
  `sentinel_action_id`, `approval_status` (and `approval_simulated` when the
  approval was simulated)
- tampering with the stored permit flag or a receipt's `approval_id` fails
  signature verification in either direction (the field is signed only when
  set, so pre-existing permits and receipts keep verifying)

## Invoke lifecycle

```
agent → POST /mcp/messages (tools/call, mcpContext.idempotency_key = K)
  gate: permit.requires_human_approval?
    ├─ approval missing  → create in Sentinel → JSON-RPC error -32005
    │                      "human_approval_pending" {approval_id, ...}
    │                      (no receipt, no charge, K released for reuse)
    ├─ still pending     → -32005 again (Sentinel re-polled, not re-paged)
    ├─ Sentinel down     → -32005 "human_approval_unavailable" (retryable)
    ├─ rejected          → -32003 "human_approval_rejected", denied receipt
    ├─ expired locally   → -32003 "human_approval_expired", denied receipt
    └─ approved          → reserve budget → charge → execute → success
                           receipt with approval_id
```

The agent retries the **same** invoke (same body, same idempotency key `K`)
until the decision lands. Retryable outcomes (`-32005`) release `K`; terminal
outcomes (`-32003`) complete it, so replays return the same denial receipt.
On `/mcp/tools/{id}/invoke` the same states surface as HTTP `202` (pending),
`503` (unavailable), and `403` (denied).

One approval is created per `(wallet, permit, tool, idempotency_key)` — kept
in the `human_approvals` table — so retries re-check the existing approval
instead of paging a human again.

## Expiry is enforced here, not in Sentinel

Sentinel's `timeout_seconds` only expires its magic-link tokens; a timed-out
approval stays `"pending"` in its API forever. The middleware stamps
`expires_at = requested_at + SENTINEL_APPROVAL_TIMEOUT_SECONDS` on each
approval and checks it **before** polling Sentinel, so a decision that
arrives after the window is not honored.

## Configuration

| Variable | Default | Notes |
|----------|---------|-------|
| `SIMULATION_MODE_HUMAN_APPROVAL` | `true` | See fail-closed rules below |
| `SENTINEL_API_URL` | empty | Root HTTPS origin, e.g. `https://api.pauseapi.app`; HTTP loopback is local/test only. Credentials, paths, queries, fragments, private/reserved literals, and internal hostnames are rejected. |
| `SENTINEL_API_KEY` | empty | Sentinel tenant key (`sk_live_…`) |
| `SENTINEL_APPROVAL_TIMEOUT_SECONDS` | `300` | Local expiry; forwarded as Sentinel `timeout_seconds` (1..86400) |
| `SENTINEL_WAIT_SECONDS` | `0` | >0: long-poll Sentinel on first invoke for an instant decision (max 300) |
| `SENTINEL_APPROVERS` | empty | Comma-separated (`email`, `mailto:`, `sms:+E164`); empty defers to Sentinel tenant defaults |
| `SENTINEL_RISK_LEVEL` | `high` | `low\|medium\|high\|critical` |

Use the canonical Sentinel origin unless a customer has a reviewed custom
deployment. Application validation blocks unsafe URL shapes, internal names,
and non-global address literals, but it does not pin DNS resolution. Custom
hostnames therefore still require trusted DNS and network-level egress policy.

The full dependency report returned when `ENABLE_PROOF_SURFACES=true` includes
a `sentinel` entry: `not_used` while simulated, `not_configured` when
simulation is disabled and both settings are absent, `down` for
partial/unsafe/unreachable configuration, and `up` after a sanitized live probe
of Sentinel's `/health`. The supported production public projection
(`ENABLE_PROOF_SURFACES=false`) intentionally omits Sentinel; it is an optional
approval integration, not an Agent Middleware release dependency.
Approval-required permits and invokes still fail closed when the integration
is unavailable, incomplete, or configured with an unsafe origin.

## Fail-closed rules

- **Simulated approvals never authorize production invokes.** With
  `SIMULATION_MODE_HUMAN_APPROVAL=true` in a production-like environment,
  creating a `requires_human_approval` permit fails
  (`human_approval_not_configured`) and the invoke-time gate denies the same
  way even for permits minted earlier. In local/dev, simulation auto-approves
  instantly with the approval row and audit metadata marked simulated.
- **Real mode without config fails closed.** `SIMULATION_MODE_HUMAN_APPROVAL=false`
  without both `SENTINEL_API_URL` and `SENTINEL_API_KEY` denies at permit
  creation and at the gate.
- **Sentinel outages never execute the tool.** Transport failures surface as
  retryable `human_approval_unavailable`; nothing is charged and the
  idempotency key stays usable.
- **Sentinel 4xx is terminal.** A rejected create (bad key, no approvers
  configured anywhere, bad risk level) denies with
  `human_approval_request_rejected` — retrying cannot succeed without an
  operator fix.

## Railway rollout

If this optional integration is enabled, configure it after applying migration
`023_human_approval_gate`:

```bash
railway variables set SENTINEL_API_URL=https://api.pauseapi.app
railway variables set SENTINEL_API_KEY=sk_live_...   # from your Sentinel tenant
railway variables set SIMULATION_MODE_HUMAN_APPROVAL=false
```

Deploy through a separately reviewed exact-SHA operator process; this
repository has no hosted deploy runbook. Do not expect the production public
dependency projection to attest Sentinel readiness. Validate the integration
with a separate deliberate synthetic or redacted gated-approval flow, then
create a gated permit (note the extra field) and drive the loop with a
separately approved synthetic test — the first
`/mcp/messages` call returns `human_approval_pending`, the approver gets the
Sentinel email/SMS, and the retried call executes after approval.
