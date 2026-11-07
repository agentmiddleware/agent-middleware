# Demo Script: Concrete Trust-Plane Proof

This is the design-partner demo for the current trust-plane proof. It shows one
bounded agent tool call moving through the control plane:

```text
scoped signed permit -> governed MCP invoke -> wallet charge -> signed receipt
-> ledger -> audit chain -> replay no double charge -> out-of-scope denial
```

The proof is intentionally narrow. It demonstrates that the governed MCP path
can enforce scope, meter a call, produce verifiable artifacts, and reject misuse.
It does not claim production readiness, settlement rails, or a complete
autonomous economic actor infrastructure.

## Design Partner Tool

Local reference tool id: **`trust-plane-echo`** (registered by
`scripts/demo_trust_plane.py`).

For a real partner, register **one** of their internal tools under the same
MCP path, issue a permit scoped only to that tool, and keep
`ENABLE_PROOF_SURFACES=false`. Do not demo AWI/media/oracle until that single
tool loop is trusted. Checklist:
[`docs/partner-first-tool-runbook.md`](docs/partner-first-tool-runbook.md).
See [`WEDGE.md`](WEDGE.md).

## Environment

Trust mode requires an Ed25519 signing seed; without it the server exits at
startup with `trust_signing_private_key_required`. Generate one **once** and
save it — this demo uses a persistent `./trust-demo.db`, and rebinding the same
`TRUST_SIGNING_KEY_ID` to new key material is rejected with
`signing_key_id_public_key_mismatch`:

```bash
python3 -c 'import base64, secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())'
```

Run the API in trust mode, reusing that seed on every restart:

```bash
export VALID_API_KEYS=dev-bootstrap-key
export DATABASE_URL=sqlite+aiosqlite:///./trust-demo.db
export TRUST_MODE_ENABLED=true
export ALLOW_LEGACY_UNPERMITTED_MCP=false
export ENABLE_PROOF_SURFACES=false
export TRUST_SIGNING_KEY_ID=local-dev-ed25519
export TRUST_SIGNING_PRIVATE_KEY_B64='<paste-the-saved-seed>'
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## One-Command Proof

For a local proof that exercises the real FastAPI routers, a throwaway SQLite
database, signed trust artifacts, and the governed MCP path without running a
server:

```bash
make demo-trust-plane
```

For CI or pre-merge verification:

```bash
make demo-trust-plane-check
```

For an operator-facing timeline that is easier to narrate in a live design
partner walkthrough:

```bash
make agent-ops-war-room
```

For machine-readable verification of the same war-room flow:

```bash
make agent-ops-war-room-check
```

For the full loop over **real HTTP** as a self-provisioned non-admin caller
— discover → authenticate → authorize → invoke → meter → receipt → replay →
audit-chain verification → out-of-scope denial, with both receipts verified
offline — plus a partner handoff bundle (portable receipts, public key set,
and independent verification instructions) written to
`data/live-loop-proof/`:

```bash
make quickstart        # terminal 1: boots the server
make live-loop-proof   # terminal 2: drives the loop, writes the bundle
```

Handing that bundle's directory to a partner engineer, and having them run
the verifier themselves, rehearses the independent-verification mechanics of
the customer-validation milestone; the milestone step itself requires the
receipt to come from a partner-owned agent and staging tool and be verified
in the partner's environment
(`docs/30-day-customer-validation.md`, not included in this repository;
rehearse with the in-repo [design partner guide](DESIGN_PARTNER_GUIDE.md)).

The proof artifact shape is captured in
[`docs/demo-trust-plane-output.md`](docs/demo-trust-plane-output.md). Use the
live demo flow below when walking a partner through the product story.

## Live Demo Flow

1. Fetch `/.well-known/agent.json` and `/mcp/tools.json`.
2. Create a sponsor wallet with a bootstrap key.
3. Create an agent wallet.
4. Create an agent API key for the agent wallet.
5. Register or use an MCP tool (`trust-plane-echo` in the one-command proof;
   replace with the partner's real tool id in a live engagement).
6. Create a signed permit with:
   - `allowed_tools: ["trust-plane-echo"]` (or the partner tool id)
   - `scopes: ["tool:trust-plane-echo:invoke", "billing:charge"]`
   - `max_credits`
   - `expires_at`
   - `Idempotency-Key`
7. Invoke `/mcp/messages` with:
   - agent API key
   - `mcpContext.wallet_id`
   - `mcpContext.permit_id`
   - `mcpContext.idempotency_key`
8. Show the wallet charge in the billing ledger.
9. Verify the signed receipt with `/v1/receipts/verify`.
10. Inspect the signed receipt with `/v1/receipts` and
    `/v1/permits/{permit_id}/receipts`.
11. Inspect the permit with `/v1/permits/{permit_id}` and the active public
    signing key with `/v1/signing-keys/active`.
12. Verify the wallet audit chain with `/v1/audit/verify-chain`.
13. Replay the same MCP request and confirm the receipt ID is unchanged and no
    second ledger debit appears.
14. Invoke a different tool under the same permit and confirm the request is
    denied as out of scope.

## Talk Track

Lead with the debit. Signed receipts ship in several competing products now, so
a track that opens on the signature opens on the least differentiated thing in
the demo. See [`WEDGE.md`](WEDGE.md) §"Signed receipts are table stakes now".

- "One agent action, at most one debit — no matter how many times the agent
  retries under the same accepted idempotency key. That is the whole product in one
  sentence." (A retry that mints a *new* key is a new operation, evaluated under
  its own permit, budget and crash outcome — it may be charged, denied, or
  refunded like any other. The key is the unit of deduplication, not a promise
  of a charge.)
- "Watch the wallet. This call charges it once for this key, and the ledger entry is
  keyed to the idempotency record, so a duplicate debit cannot be written even
  under a race."
- "Now I replay the identical request under the same key. Same receipt, no second
  dispatch, no second debit — and the balance has not moved."
- "The permit is the bounded authority that made the charge legitimate in the
  first place: wallet, tool, scope, budget, expiry, nonce, and signature. It is
  checked before the tool is allowed to run, not after."
- "Trying a different tool with the same permit is denied, and the denial is
  itself signed and moves no money."
- "The receipt's signature covers the ledger entry and the idempotency record
  together — and, **when the call goes to the configured upstream MCP tool**,
  the dispatch attempt as well. Plenty of tools sign receipts; that binding
  is what lets you check the relationship between authority, money and outcome
  instead of taking three separate assertions on trust — and you can check the
  signature offline, without us. What actually *prevents* the second charge is
  one layer down: the ledger's `operation_key` uniqueness constraint and the
  idempotency replay path. The signature is evidence, not enforcement."
- **Do not overstate the local proof.** `trust-plane-echo` is the *local*
  reference tool, so the receipt this demo produces has no `dispatch_attempt_id`
  and binds two identifiers, not three. Demonstrate the three-way binding only
  when running the partner's configured upstream tool. Claiming it off the local
  proof is the exact overclaim a partner engineer will check first.
- If asked about a crash on **this local demo tool**: "An incomplete idempotency
  record stays in progress — the replay you just saw only returns a receipt once
  the record is terminal. If a crash could have landed the side effect, the
  record goes to operator review and is deliberately never retried. We would
  rather block one call than fire it twice."
- If asked about crashes **on the configured upstream tool**: "If we die after the
  request leaves the gateway but before the acknowledgement arrives, that becomes
  a durable `delivery_uncertain` state. The charge stands and we never redispatch,
  because we can no longer prove the call did not land. We would rather be
  honestly ambiguous than silently double-fire."

## Proof Artifacts

- Permit JSON with Ed25519 signature.
- Receipt JSON with Ed25519 signature.
- Public signing-key metadata, with no private key material.
- Ledger entry ID referenced by the receipt.
- Audit event ID referenced by the receipt.
- Permit and receipt inspection responses filtered to the agent wallet.
- Audit-chain verification response.
- Replay response with the same receipt ID and no duplicate debit.
- Out-of-scope denial response such as `permit_tool_not_allowed`.

## Keep The Claim Narrow

Say: "This proves a governed MCP trust-plane path for scoped, metered,
replay-safe tool calls."

Do not say: "This is production-grade agent banking," "full autonomous economic
actor infrastructure," or "complete cross-framework governance."
