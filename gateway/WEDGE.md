# Wedge: Replay-safe MCP permits and at-most-once debits

Agent Middleware API should not initially sell itself as a full platform for
autonomous economic actors. The credible wedge is narrower:

> At most one gateway dispatch and at most one debit per accepted idempotency
> key, with one finalized signed receipt, for metered MCP calls.

Or in one line:

> Authorize one agent action. Charge it once. Prove what happened.

Start with the [documentation guide](docs/README.md) for the supported
evaluation, integration, SDK, security, and pilot paths.

**Why this project does not say "exactly-once".** The guarantee is
*deduplication* at the gateway — no duplicate dispatch, no
duplicate debit — not a promise that every accepted call results in a charge.
Two paths produce no net debit at all: a crash before dispatch reconciles to a
refund, and an out-of-scope or over-budget call is denied before money moves.
The enforceable claim is **never a duplicate charge, not always a charge.**
Every precise statement of the guarantee below says "at most one" for that
reason. See [`docs/failure-semantics.md`](docs/failure-semantics.md).

The core job is to put one enforceable **economic** boundary between autonomous
agents and tools:

```text
scoped signed permit -> governed MCP invoke -> wallet charge -> signed receipt
-> ledger -> audit chain -> replay no double charge -> out-of-scope denial
```

The loop now has a front door. Two questions an agent has to answer *before*
it can act — "may I?" and "what will it cost?" — are answered in the same
signed, checkable way as the action itself:

```text
permit request -> human approves -> minted permit
signed quote   -> price locked   -> charge honors it
```

Both feed the same loop rather than sitting beside it: the human's decision
mints an ordinary signed permit, and the quote is honored by the ordinary
metered charge. Neither adds a second way to authorize or to spend.

Category language (“MCP trust plane,” “governance gateway”) is occupied. The
differentiating primitive is at-most-once economic authorization per accepted
idempotency key at the gateway boundary: one idempotency key returns the original receipt without a
second gateway dispatch or debit. This is not exactly-once: a remote tool's own side effect is deduplicated
only when that tool also honors the forwarded idempotency key.

## Positioning vs nearby products

Stay a **closed-loop credit and delegated-authority** system for internal AI
platform teams—not a general MCP gateway and not merchant settlement.

| Nearby | Their center of gravity | Our difference |
|--------|-------------------------|----------------|
| MCP “trust” gateways (e.g. signed receipts + replay) | Policy / evidence | Wallet debit + economic idempotency |
| MCP monetization / pay-per-tool | Payments rails | Internal budgets; no settlement claim |
| Enterprise authz for MCP | Who may call | Meter + receipt + charge-once |
| Agent reliability libraries (in-process decorators) | Retry safety inside the caller | A boundary the agent cannot route around; evidence a third party can check |

Do not pitch as production payments, compliance-grade ledger, or IAM
replacement (see [`SECURITY_LIMITATIONS.md`](SECURITY_LIMITATIONS.md)).

### Signed receipts are table stakes now (2026-08)

The 2026 competitive sweep (`docs/market-research-2026-08.md`, not included
in this repository) verified
that **signed, offline-verifiable receipts are no longer differentiating**.
At least one MCP policy proxy emits Ed25519 receipts verifiable without calling
its issuer, and it carries an IETF Internet-Draft for the format; other projects
ship hash-chained signed receipts alongside policy and HITL.

This does not change the wedge — it sharpens which half of it to lead with.
The row no surveyed project is *documented* as occupying is the **economic**
one:

> One accepted idempotency key produces **at most one** gateway dispatch **to
> the configured upstream MCP tool** and **at most one** ledger debit, linked by
> a single persisted chain, with a receipt on every path that finalizes or
> reconciles — and, on that same upstream path, a genuinely ambiguous
> outcome after the durable send claim becomes a distinct receipted state
> rather than a silent redispatch.

Two qualifications that must travel with that sentence.

**Scope it to the upstream path.** The dispatch state machine
(`prepared → dispatch_claimed → {succeeded, returned_error,
delivery_uncertain, response_rejected}`) covers the **configured upstream MCP
tool**. Immediately before the network send, one nullable
`dispatch_claim_hash` stores the process-local activation's one-shot claim. A
historical `dispatched` row is treated as already sent, never as an available
claim. Once either sent state is durable, a missing trustworthy result becomes
ambiguous and the gateway does not redispatch.

Local governed tools have no attempt row and no ambiguous state; a crash there
fails closed into manual review. The claim fence does not change local-tool
execution or reservations, per-tool call slots, quotes, human approval,
API-key/JWT authentication, or rate limiting. Say "for the configured upstream
tool" — `README.md` already does.

**Verification boundary for this release slice:** focused state-machine,
reconciliation, migration, and PostgreSQL process-kill tests cover the
claim-before-send fence. They do not establish that a deployment is current or
that any downstream effect occurred.

**The signature adds the binding, not the evidence.** For the configured
upstream tool, the receipt's Ed25519 signature covers the ledger entry, the
idempotency record, and the dispatch attempt *together* (local governed tools
have no dispatch-attempt row, so their receipts bind the first two only). That
binding — not the fact of a signature — is the part no surveyed project
documents.

State that as scope, not as exclusivity: several projects enforce budgets and
several dedupe replays, and whether any of them *binds* the two is an open
verification question (`docs/market-research-2026-08.md` §7, not included in
this repository). Say "no project we
surveyed documents this," never "nobody does."

**Re-tested 2026-08-25** (`docs/market-research-2026-08.md` §9, not included
in this repository) and the row
still holds — but the neighbourhood got more crowded, not less. Microsoft's
`agent-governance-toolkit` now **ships** offline-verifiable receipts (Ed25519
over RFC 8785 JCS, hash-chained), where §3 had recorded only a proposal;
Pipelock ships mediator-signed receipts; and an independent survey
(arXiv:2606.04193) compares the receipt protocols it names on receiver-side
signing, encryption, transparency logs and discovery — and treats coupling to
settlement as an open opportunity rather than a property any of them has. Useful
receipt-layer context; it does not itself test the debit/idempotency binding.

Treat signed offline receipts as fully commoditized from that date. This is a
re-test on a date, not a permanent result.

Lead with the debit. Cite the signature as supporting evidence, never as the
differentiator, and never as a superlative — see **What Not To Claim Yet**.

## Core User

Platform engineering or AI infrastructure teams that already run internal
agents against MCP-style tools and need a control point before those tools are
invoked.

## First Paid Use Case

Govern and meter internal agent tool calls with wallet budgets, scoped permits,
idempotent retries, signed receipts, and auditable denial.

The first design-partner motion should be one real internal tool behind the
proxy, not a broad migration of every agent workflow.

## Design Partner First Tool

Reference proof tool (local demo only): `trust-plane-echo` via
`make prove-trust-plane` / [`DEMO_SCRIPT.md`](DEMO_SCRIPT.md).

Partner swap checklist:
[`docs/partner-first-tool-runbook.md`](docs/partner-first-tool-runbook.md).

Partner motion:

1. Keep `ENABLE_PROOF_SURFACES=false` in the partner environment.
2. Configure **one** internal Streamable HTTP MCP tool through the upstream
   environment variables (do not mount AWI/media/etc.).
3. Issue a wallet-scoped permit for that tool only.
4. Walk permit → invoke → charge → receipt → replay → out-of-scope deny.
5. Stop. Do not expand surface until that loop is trusted in their stack.

## What Is Core

- Wallet-scoped API keys.
- MCP discovery and invocation.
- Signed permits for tool scope, wallet binding, key binding, budget, expiry,
  and nonce.
- A request path for authority an agent cannot mint itself: the agent states
  scope, budget, and justification; a human decides; the middleware mints the
  permit from the reviewed terms.
- Signed, single-use price quotes that the metered charge honors, so an agent
  can know a call's cost before committing to it.
- Idempotency keys for permit issuance and governed invokes.
- Ledger-backed wallet charging.
- Signed receipts for governed tool attempts.
- Tamper-evident wallet audit chains.
- Explicit denial reasons for out-of-scope or invalid governed attempts.
- A persisted remote-dispatch state that distinguishes confirmed outcomes from
  delivery uncertainty.

## What The Current Proof Shows

- A permit can bind one agent wallet to one allowed MCP tool and budget.
- A governed MCP invoke can validate that permit before the gateway dispatches
  the tool call.
- A successful governed invoke can charge the wallet and write a ledger entry.
- The response can include a signed receipt linked to permit, ledger, and audit
  identifiers.
- The audit chain can be verified after the fact.
- Replaying the same governed invoke can return the same receipt without a
  duplicate gateway dispatch or debit.
- A request outside the permit scope can be denied with a concrete reason.
- An agent with no authority can ask a human for a scoped, budgeted permit, and
  the permit that gets minted carries exactly the terms the human reviewed.
- A signed quote can fix the price of one call, and the charge honors it even
  after the tool's registered price moves.

## What Is Proof Surface

AWI, browser automation, content generation, oracle crawls, media utilities,
IoT bridges, red-team services, RTaaS, telemetry auto-PR, and sandbox demos are
proof surfaces. They may exercise the control plane, but they do not define the
product until they consume the same permit, receipt, idempotency, and audit
primitives.

**Freeze list (do not expand):** [`docs/PROOF_SURFACES.md`](docs/PROOF_SURFACES.md)
— mirrors `PROOF_SURFACE_ROUTERS` in `app/main.py`, accept/freeze stubs, and
agent rules.

## What To Freeze

- New proof-surface features and ungated HTTP demos (see
  [`docs/PROOF_SURFACES.md`](docs/PROOF_SURFACES.md)).
- Broad multi-tool migrations before one partner tool is live.
- KMS, settlement, transparency logs, and non-MCP adapters (see
  [`SECURITY_LIMITATIONS.md`](SECURITY_LIMITATIONS.md)).

Wrap-and-anchor evidence is frozen with that transparency-log line. The
intended later slice — if a named partner requires it — wraps the existing
receipt claims as an in-toto/DSSE predicate and appends the statement hash
to Rekor or a SCITT transparency service. That is anchoring/publication: an
independent timestamp for one statement in one log tree. Wrapping those
claims under a Sigstore or SPIFFE gateway identity does **not** by itself
replace `/.well-known/trust-keys.json`. The inner claims are still signed by
this origin unless the slice names an independently trusted key source
(Fulcio/TUF, a pinned SPIFFE identity, or an out-of-band pin) and the
verifier checks the receipt against that root. Unfreeze for anchoring when
a named partner needs that independent timestamp. Unfreeze for key
distribution only when that partner cannot trust this origin for keys and
the slice names that independently trusted key source. This freeze does
not specify `receipt_id` as a log subject, conflict detection for that
subject, or trusted-checkpoint consistency/witness evidence, so it does not
claim non-equivocation. A log also does not prove an unpublished action did
not occur, and it does not make a dishonest gateway honest. Lead with the
debit; cite that stack as supporting evidence, never as the product. The
ledger stays the source of truth; a log is not a second debit system.

A portable permit/audit API is frozen with the same Narrow rule. Receipts
already export (`GET /v1/receipts/{id}/portable`). Permits already return
every signed field plus `signature` and `key_id` from `GET /v1/permits/{id}`,
and `LocalPermitValidator` reconstructs and verifies those bytes without
importing the application; the signing goldens pin that reconstruction.
What is frozen is a receipt-style permit envelope around that existing
verifier, and a public export of audit-chain signed bytes plus an SDK
verifier that imports none of the application
([`docs/PROOF_MATRIX.md`](docs/PROOF_MATRIX.md) item 1). That expands the
demoted signed-evidence surface; it is not the debit wedge. Unfreeze the
permit envelope only when a named partner cannot use the shipped permit
verifier. Unfreeze the audit export only when that partner cannot trust
this origin's first-party audit verification
(`/v1/audit/verify-chain`, `GET /v1/receipts/{id}/evidence`). That slice
is not wrap-and-anchor, not out-of-band key distribution, and not an
audit-log product.

Out-of-band key distribution is frozen with the same Narrow rule. Offline
receipt verification still trusts `/.well-known/trust-keys.json` over TLS
from this origin. Wrapping origin-signed claims under Sigstore or SPIFFE
does not change that. Unfreeze only when a named partner cannot trust this
origin for keys **and** the slice names the trust root that is not this
origin (Fulcio/TUF, a pinned SPIFFE identity, or an out-of-band pin) and
the verifier checks the receipt against that root. Do not start key
pinning, TUF, or Fulcio as a side project.

Production-like deploys must keep `ENABLE_PROOF_SURFACES=false`.
Deploy SOP (single path: `railway up` from this Dockerfile):
the hosted-service deploy runbook (not in this repository).

Agent-executable remediation of known spine/discovery/deploy debt is tracked
in `docs/tech-debt-remediation-plan.md` (not included in this repository);
the current frozen-surface inventory is [`docs/PROOF_SURFACES.md`](docs/PROOF_SURFACES.md).

## What Not To Claim Yet

- Production-ready payments or settlement.
- Compliance-grade ledger storage.
- Full autonomous economic actor infrastructure.
- Universal policy enforcement across every agent framework.
- Distributed exactly-once side effects in arbitrary upstream MCP servers.
- Proof that a durable gateway dispatch claim caused any downstream effect.
- Quotes as a pricing or settlement product. A quote fixes what this gateway
  will debit from an internal wallet for one call; it is not a market price, a
  vendor commitment, or an invoice.
- Uniqueness superlatives — “the only gateway that…”, “no competitor offers…”.
  Signed receipts, offline verification, per-tool policy, and budget caps all
  exist elsewhere; a superlative that a reader can falsify in one search costs
  more credibility than the claim buys.
- Compliance mapping to the EU AI Act, Colorado AI Act, ISO 42001, or SOC 2.
  Receipts may be *one input* an operator's auditor accepts; that is the
  operator's determination, not ours. No mappings, no certifications, no
  “compliance-ready.”
- Permit requests as an approval-workflow product. The decision is delegated to
  Sentinel and the surface is one ask and one poll — it is not a replacement
  for an access-request or change-management system.
- A replacement for enterprise IAM, secrets management, or sandbox isolation.
