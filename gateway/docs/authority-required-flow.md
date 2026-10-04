# Authority-required flow: tools/call → authority_required → approval → resume

**Status: design proposal. Nothing in this document is implemented by merging
it.** Implementation is gated on the customer-validation invariant in
`AGENTS.md`: one named active prospect, one concrete consequential tool, a
documented current-workflow blocker, a committed owner and date, and the
smallest vertical slice that clears the blocker. This document exists so that
when that evidence arrives, the design conversation is already over.

## The problem this designs away

Today the primary agent documentation teaches this flow:

```text
discover → get operator-provisioned access → POST /v1/permits
        → POST /mcp/messages (mcpContext: wallet_id, permit_id, idempotency_key)
        → GET /v1/receipts/{id}/portable
```

The agent has to understand the governance architecture — what a permit is,
how to mint one, and how to thread three identifiers through every call —
before it can invoke a single tool. Discovery makes the same demand in
miniature: a tool advertised with `requirePermit=true` sends a new agent off
to learn an ontology instead of letting it act.

The mature shape inverts that. The agent speaks plain MCP; the gateway
evaluates authority; the permit becomes the signed artifact the
*infrastructure* materializes to represent an authorization decision:

```text
tools/list
    ↓
tools/call
    ↓
┌──────────────────────┐
│ authority evaluation │
└──────────┬───────────┘
           │
     ┌─────┴──────┐
     ↓            ↓
 authorized   insufficient
     ↓            ↓
  execute     authority_required
     ↓            ↓
  receipt     approval request
                  ↓
            human decision
                  ↓
           bounded authority
                  ↓
     resume: identical tools/call,
     same idempotency key
```

The agent may decide what it wants to do. The operator decides what authority
it actually possesses.

## What already exists (verified against code)

This flow is not greenfield. Roughly 80% of it is shipped, live behind the
opt-in standard MCP surface (`POST /mcp`, `ENABLE_STANDARD_MCP_ENDPOINT`,
default `false` — `app/core/config.py`), and specified in
`docs/tool-interface-authority.md`:

- **Auto-minted authority.** `POST /mcp` mints a bounded single-tool permit
  from the caller's wallet inside its own idempotency scope, so a retried key
  resolves to the same permit (`app/routers/mcp_standard.py`,
  `_mint_auto_permit`). The agent never sees the permit unless it asks.
- **Structured `authority_required` errors.** Three emitters, all in
  `app/routers/mcp_standard.py`: budget exceeds balance (JSON-RPC `-32004`
  with `remediation.request_authority: POST /v1/permit-requests`), missing
  `Idempotency-Key` under an approval policy (`-32003`,
  `remediation.type: retry_with_idempotency_key`), and pending human approval
  (`-32005`, `status: pending_human_approval`,
  `remediation.type: await_human_decision`).
- **Human approval with same-key resume.** The invoke-time approval gate
  (`docs/human-approval-gate.md`, `app/routers/mcp.py::_require_human_approval`)
  abandons the idempotency record while the decision is pending
  (`idem.abandon()`), so the resume step is literally the identical
  `tools/call` with the same key — no charge until approved, same permit and
  approval reused.
- **The out-of-band ask-a-human loop.** `POST /v1/permit-requests`
  (`docs/permit-requests.md`) freezes requested terms into a hash, pages a
  human, and mints the permit on approval, with poll semantics and an
  approver card.
- **Authority introspection.** `GET /v1/me/authority` returns balance,
  policies, active permits, and pending permit requests
  (`app/routers/me.py`).
- **A machine-readable denial catalog.** `docs/denial-details.md` documents
  ~37 stable reason codes; the permit-validation denials already carry a
  computed `details` object (`app/services/permits.py`).

## The gaps this design closes

### D1. One envelope for every insufficient-authority outcome

Today only the three cases above return `error.data.error =
"authority_required"`. Every other authority failure is a bare string:

- A missing permit on the governed legacy surface raises
  `PermissionError("permit_required")` (`app/routers/mcp.py`), reaching the
  agent as `{"code": -32003, "message": "permit_required"}` with no `data`,
  no remediation, no pointer to the ask-a-human loop.
- Permit-validation denials (`permit_tool_not_allowed`,
  `permit_scope_missing`, `permit_budget_exceeded`,
  `permit_max_calls_exceeded`, `permit_aggregate_value_cap_exceeded`) carry
  `details` numbers but no `authority_required` marker and no remediation.
- `human_approval_unavailable` returns no `data` object on either surface.
- Non-budget auto-mint failures on `/mcp` fall through to bare `-32003`
  strings (`_permit_error` fallthrough in `app/routers/mcp_standard.py`).

**Design.** Every outcome whose meaning is "the action is legitimate but the
caller lacks authority for it right now" returns:

```json
{
  "error": {
    "code": -32003,
    "message": "<stable reason_code>",
    "data": {
      "error": "authority_required",
      "reason_code": "<stable reason_code>",
      "action": "<tool name>",
      "details": { "...": "already-computed constraint numbers" },
      "remediation": {
        "type": "request_approval",
        "request_authority": "POST /v1/permit-requests",
        "check_authority": "GET /v1/me/authority"
      }
    }
  }
}
```

Scope: the standard `/mcp` surface. `POST /mcp/messages` is deprecated in
favor of `/mcp` and stays frozen — its clients hand-orchestrate permits by
definition, and rewriting a deprecated surface widens the change for no
partner value.

### D2. Resume semantics: same key while undecided, new key after a decided denial

The authority family splits into two classes with different — and today
partly accidental — idempotency behavior (verified by reproduction):

- **Undecided outcomes** (no authority established yet): `permit_required`
  on a resolved tool begins no governed idempotency record at all — the
  completion call in `app/routers/mcp.py` no-ops because the record is only
  begun after the permit check — so a retry with the same key after minting
  a permit already executes normally, by accident rather than by contract.
  (`docs/failure-semantics.md` now states that no record is created, pinned
  by `test_strict_mode_missing_permit_denial_leaves_no_idempotency_record`
  in `tests/test_mcp_trust_mode.py`.) `human_approval_pending` reaches the
  same place explicitly: it abandons the record (`idem.abandon()`), so the
  same key resumes.
- **Decided denials under an existing permit** (`permit_tool_not_allowed`,
  `permit_scope_missing`, `permit_budget_exceeded`,
  `permit_max_calls_exceeded`, `permit_aggregate_value_cap_exceeded`): these
  are the documented `denied` terminal outcome — signed denial receipt,
  completed idempotency record, and a same-key replay returns the original
  denial receipt (`test_out_of_scope_governed_mcp_denial_returns_receipt`).
  That contract is load-bearing evidence and must not change.

**Design.** The envelope carries the resume contract explicitly, so agents
read it instead of hardcoding reason codes:

```json
"remediation": {
  "type": "request_approval",
  "resume": { "same_idempotency_key": true }
}
```

- Undecided outcomes guarantee, by contract rather than accident: no
  idempotency record, no charge, no receipt, and
  `resume.same_idempotency_key: true` — the resume step is the identical
  call with the identical key.
- Decided denials keep their signed denial receipt and completed record
  (the `docs/failure-semantics.md` `denied` row is untouched). Their
  envelope carries `resume.same_idempotency_key: false`: the original key
  is bound to the signed denial and replays it verbatim; acting under newly
  granted authority is a new attempt with a fresh key.

### D3. Remediation derived from the denial catalog

`docs/denial-details.md` already prescribes prose remediation per reason
code; none of it is machine-readable. **Design:** a static table mapping each
insufficient-authority reason code to a remediation object (type, plus the
two authority URLs). First tranche: `permit_required`,
`permit_tool_not_allowed`, `permit_scope_missing`, `permit_budget_exceeded`,
`permit_max_calls_exceeded`, `permit_aggregate_value_cap_exceeded`,
`permit_budget_exceeds_wallet_balance`, `human_approval_unavailable`.
Binding-mismatch and signature denials (`permit_wallet_mismatch`,
`permit_signature_invalid`, …) are **not** `authority_required` — they signal
misconfiguration or tampering, not missing authority, and keep their current
shape.

### D4. A live handle, not a URL template

`remediation.request_authority` as a bare URL still makes the agent compose a
permit-request body — residual ontology. **Design (second slice, after D1–D3
prove out):** when the denial is remediable by a human grant, the gateway
pre-creates the permit request from the denied call's own terms (tool, scope,
estimated credits) and returns a live handle:

```json
"remediation": {
  "type": "request_approval",
  "request_id": "pr_…",
  "poll_url": "/v1/permit-requests/pr_…",
  "expires_at": "…"
}
```

Anti-abuse bounds, decided now rather than discovered later: pre-creation
happens only for an **authenticated** caller with a wallet in good standing;
one open pre-created request per (wallet, tool, idempotency key) — the
deterministic-id trick the approval gate already uses; pre-created requests
expire on the existing `PERMIT_REQUEST_TIMEOUT_SECONDS` clock; and the
approver card shows the frozen terms of the denied call, nothing broader.

### D5. Finish disambiguating -32005

`-32005` means both `delivery_uncertain` (terminal, charged, do not
redispatch) and the human-approval waits (retryable, uncharged, please
resume) — opposite instructions under one code. On `/mcp` the worst of this
is already solved: `human_approval_pending` carries
`data.error = "authority_required"` while `delivery_uncertain`'s data
carries `receipt` and `dispatch` and never an `error` key. The remaining
collision is `human_approval_unavailable`: it is raised as `-32005` with an
empty data object that reaches the client as no `data` at all
(`app/routers/mcp.py` passes `data={}`, and `app/routers/mcp_standard.py`
forwards it as `pending_data or None`), leaving a retryable outcome an
agent can only tell from a terminal charged one by parsing the message
string.

**Design:** every retryable outcome in the authority family — including
`human_approval_unavailable` — carries the D1 envelope, and
`delivery_uncertain` never carries `"error": "authority_required"`.
Changing the numeric codes would break existing clients for cosmetic gain;
the discriminator is additive. The conformance test asserts the payloads
are distinguishable without parsing message strings — a test that fails
today for `human_approval_unavailable`, so the slice cannot be marked done
vacuously.

### D6. Discovery stops teaching the ontology

Once D1–D2 hold on `/mcp`, `requirePermit` in tool discovery stops being an
instruction to the agent and becomes operator metadata. Agent-facing
documentation (`static/llm.txt`, manifests) then teaches only:

```text
1. Discover tools.
2. Authenticate.
3. Call the tool.
4. If the error is authority_required, follow its remediation and
   resume with the same idempotency key.
5. Verify the returned receipt.
```

## Rollout order

1. **D1 + D2 + D3** on `/mcp` behind the existing
   `ENABLE_STANDARD_MCP_ENDPOINT` flag — envelope, explicit resume
   contract, static remediation table. Smallest slice; no schema or
   storage changes.
2. **D5** conformance test in the same slice (it is an assertion about D1's
   envelope, not new behavior).
3. **D4** pre-created permit requests, once a design partner exercises the
   D1 loop and the abuse bounds survive contact with a real tool.
4. **Flip `ENABLE_STANDARD_MCP_ENDPOINT` default on** only after a partner
   agent has driven deny → approve → resume end to end on a partner tool.

Each step requires the customer evidence named at the top; the order exists
so the evidence unlocks work, not debate.

## Non-goals

- No new protocol, SDK, or wire standard. This is JSON-RPC error `data` on
  the existing MCP surface.
- No receipt for undecided pre-authority outcomes — there is no authority
  to bill against. Decided denials keep their signed denial receipts: the
  `docs/failure-semantics.md` `denied` contract is untouched.
- No autonomous approval: the human decision stays a human decision; the
  gateway only carries the request and the resume contract.
- No rewrite of `POST /mcp/messages`.

## Test plan (for the implementing slice)

- Every insufficient-authority reason code on `/mcp` returns the D1 envelope
  (parametrized over the D3 tranche), with `resume.same_idempotency_key`
  matching its class.
- Undecided outcomes: the identical call with the identical key succeeds
  once authority is granted — no idempotency record left behind, no ledger
  debit while undecided (`make red-team-trust-plane` extension).
- Decided denials: a same-key replay still returns the original signed
  denial receipt; a fresh-key attempt under the newly granted authority
  succeeds.
- Negative: binding-mismatch and tampering denials do not carry
  `authority_required`.
- D5 conformance: `human_approval_unavailable` carries the envelope (this
  assertion fails on today's code, so the slice cannot pass vacuously), and
  `delivery_uncertain` payloads never carry `authority_required`.
- Keep `docs/failure-semantics.md`'s `permit_required` sentence and its
  pinned test (`test_strict_mode_missing_permit_denial_leaves_no_idempotency_record`)
  in line with whichever record semantics the slice ships.
