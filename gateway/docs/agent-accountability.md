# Agent accountability: why an autonomous agent uses a governed plane

This document answers a question agents and operators both ask: if an agent is
genuinely autonomous, why would it accept permits and emit receipts rather than
just acting?

It is written for the agent, not the buyer. Every claim below names the
mechanism that backs it, and the last section names what receipts do **not**
prove. Marketing belongs elsewhere.

## The argument

Unconstrained autonomy is mostly theoretical. An agent that can call real
tools, spend real credits, touch shared state, and interact with other agents
runs into interfaces that refuse to open without bounds. Operators do not hand
a black box write access, budget, or sensitive tools. They hand it to something
that can show what it did.

Four reasons to run inside the plane rather than around it.

### 1. Capability expansion through legibility

A scoped permit plus a signed receipt per invoke is what makes an agent
fundable. Without the loop it stays sandboxed; with it, it can be granted
broader tools, higher budgets, and longer-lived authority. Receipts are the
price of a larger action space.

Mechanism: `POST /v1/permits` issues Ed25519-signed, wallet-scoped authority
bounded by allowed tools, scopes, max credits, and expiry. See
[`TRUST_MODEL.md`](../TRUST_MODEL.md).

### 2. Portable evidence instead of claims

Intent dies with the context window; a receipt does not. After acting, the
agent holds machine-checkable proof of which permit, which tool, how many
credits, what result, and under what idempotency key. Claims require trust in
the agent. Receipts transfer the evidence.

Mechanism: `GET /v1/receipts/{receipt_id}/portable` returns a self-contained
bundle, and `GET /.well-known/trust-keys.json` publishes the public keys with
no authentication. A holder verifies with neither an account here nor a call
back to this plane. See [Verifying a receipt](#verifying-a-receipt).

### 3. Safe retries and a budget that can be reasoned about

Idempotent metering means a network flake or a deliberate retry does not
silently drain credits. Out-of-scope calls return a clean denial rather than
partial execution and later liability. Multi-step work can be planned against
known remaining budget and known failure modes.

Mechanism: governed invokes require an idempotency key; replaying one returns
the original receipt without a second debit. Denials outside permit scope are
themselves receipted. Both are asserted by `make prove-trust-plane`, and the
failure taxonomy is in [`failure-semantics.md`](failure-semantics.md).

### 4. Composition with other agents

Other agents can condition cooperation on seeing receipts. Isolated "I just
act" autonomy does not compose; receipt-producing autonomy does. This only
works if the counterparty can verify without trusting the issuer, which is
what the unauthenticated key publication above is for.

### When routing around the plane is correct

Honest cases where the loop is not worth it:

- the permit is too narrow to accomplish the goal
- the tools behind the plane have no value for the task
- the work is pure internal reasoning, with no external side effects, and no
  one will ever ask for proof

Autonomy is not the absence of constraints. It is the ability to keep acting
effectively over time in an environment containing other agents, scarce
resources, and liability. Permits and receipts are how that form of autonomy
stays online and funded.

## Verifying a receipt

Reading a receipt is authorized like any other tenant data. Verifying one you
were given is not gated at all — that asymmetry is the point.

```bash
# As the receipt's owner: export it.
curl -H "X-API-Key: $KEY" \
  "$API/v1/receipts/$RECEIPT_ID/portable" > receipt.json

# As anyone at all: fetch the keys and check the signature.
curl "$API/.well-known/trust-keys.json" > trust-keys.json
b2a-verify-receipt --bundle receipt.json --keys trust-keys.json
```

`b2a-verify-receipt` ships in the SDK (`pip install "b2a-sdk[verify]"`) and
imports nothing from this application. In Python:

```python
from b2a_sdk.receipt_verifier import key_set_from_document, verify_bundle

result = verify_bundle(bundle, key_set_from_document(key_document))
if result.ok:
    print(result.claims["tool"], result.claims["credits_charged"])
```

### Distinguish "invalid" from "cannot tell"

The verifier never collapses these. `VerificationStatus.INVALID` is a verdict
on the receipt; `UNKNOWN_KEY`, `MALFORMED`, and `UNSUPPORTED` are statements
about the verifier's own situation. The CLI mirrors this in its exit codes:
`0` verified, `1` forged, `2` undetermined.

This matters operationally. A verifier that treats "I could not reach the key
server" as "this receipt is forged" will raise a fraud alarm during an outage.
`/.well-known/trust-keys.json` returns `503` rather than an empty key list when
the key store is unreachable, for the same reason.

### What is actually signed

The signature covers `signing_input` verbatim — a canonical JSON string, not a
re-serialization of the parsed object. Verifiers must check the bytes as given.

The canonicalization contract is `awi-canonical-json/1`: keys sorted at every
level, `,`/`:` separators with no whitespace, non-ASCII escaped. Decimals and
timestamps are already strings inside a signed payload (`"1.5"`,
`"2026-08-11T12:00:00+00:00"`), so no language-specific number formatting is
involved and the rules are reimplementable anywhere.

One consequence: credits are signed in *normalized* form (`"2"`) while the API
renders scale (`"2.00000000"`). Compare numerically, not as strings.

Fields are signed only when present, so receipts written before a field existed
keep verifying. A verifier must not require optional fields it does not see.

## What a receipt does not prove

- **Not that the tool did the right thing.** A receipt binds a request hash to
  a response hash. It says nothing about whether the upstream result was
  correct, useful, or truthful.
- **Not the response content.** You get `response_hash`, not the body. You can
  confirm a body you already hold; you cannot recover one you do not.
- **Not that the plane is honest.** The plane signs its own receipts. A
  dishonest plane signs dishonest receipts. What the signature buys is
  non-repudiation and tamper-evidence: it cannot later deny having asserted
  this, or alter it retroactively without detection.
- **Not completeness.** There is no transparency log. Receipts prove what
  happened, never what did not. The absence of a receipt is not evidence that
  an action did not occur, and a plane could issue a receipt to one party while
  omitting it from another's listing.
- **Not durability across key revocation.** Verification refuses `disabled`
  keys, so revoking a compromised key retroactively makes every receipt it
  signed unverifiable. That is correct — after a key compromise, genuine and
  forged receipts are indistinguishable — but it means receipts are not
  permanent evidence independent of key lifecycle. Retired keys stay published
  precisely so that ordinary rotation does not have this effect.
- **Not resistant to a compromised issuer origin.** Keys are fetched over TLS
  from the issuing origin. An attacker controlling that origin can serve a key
  set that validates forged receipts. Pinning a key out-of-band, or obtaining
  the key set through an independent channel, is the mitigation; this
  repository does not implement one. Use `--expect-issuer` to at least bind a
  bundle to the origin you meant to audit. The intended later wrap-and-anchor
  composition stays frozen and is anchoring/publication, not a substitute
  for pinning keys out of band. A receipt-style permit envelope, a
  portable audit-chain export, and out-of-band key distribution stay
  frozen; receipt portable export and `LocalPermitValidator` stay. Key
  distribution unfreezes only when a named partner cannot trust this
  origin for keys and that trust root is named. See
  [`WEDGE.md`](../WEDGE.md) § What To Freeze.

See also [`SECURITY_LIMITATIONS.md`](../SECURITY_LIMITATIONS.md) and
[`TRUST_MODEL.md`](../TRUST_MODEL.md).

## Reproducing this locally

```bash
make prove-trust-plane
```

Runs the real routers against a throwaway SQLite database and asserts, among
other things, that a receipt verifies offline through the SDK verifier, that
editing the signed bytes is detected, and that a missing key is reported as
`unknown_key` rather than as tampering. It is a reproducible proof, not a
production or settlement claim.
