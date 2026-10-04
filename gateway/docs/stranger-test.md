# The stranger test

The stranger test is the milestone check for the trust plane: **can a person
who has never seen this repository drive the whole governed loop and
independently verify the five claims, using only the published discovery
contract and an off-the-shelf MCP client, without asking the maintainers a
single question?**

It is deliberately not automated. Its value is the fresh pair of eyes. The two
credentialed HTTP batteries (`scripts/adversarial_battery.py`,
`scripts/red_team_trust_plane.py`) and the in-process pass
(`tests/test_adversarial_five_claims.py`) prove the claims *from inside*. The
stranger proves the claims are *reachable and checkable from the documentation
alone*. If the stranger gets stuck, the gap is real product surface — usually a
doc, an error message, or a discovery field — not a test.

This is a usability and independent-verification test, not customer validation.
A local tool, self-issued public proof, or friendly evaluator cannot satisfy the
partner-owned pilot gate in
`docs/30-day-customer-validation.md`.

## Ground rules

- **Public docs only.** The stranger may read the published discovery contract
  and the offline verifier's README. No internal chat, no maintainer, no
  reading application source to figure out an argument.
- **Off-the-shelf client.** A standard MCP client, `curl`, or the published
  `b2a_sdk` — nothing bespoke to this run.
- **Deliberate misuse is part of the test.** The stranger *tries* to retry, to
  overspend, and to trust a receipt they did not sign.
- **Zero questions asked.** The moment the stranger has to ask "what does this
  field mean?" or "why did this fail?", stop and record it as a finding.

## What the stranger starts from

Either target works; pick one and stay on it.

- **Live:** the published origin is a **private experiment** after the
  Narrow lockdown. Anonymous callers cannot list tools. A stranger with no
  key cannot bootstrap from `GET /mcp/tools.json`. Use a key C.Lee issued,
  then `GET /.well-known/agent.json` → `GET /health/dependencies` before
  assuming real side effects. Receipt keys at
  `/.well-known/trust-keys.json` stay unauthenticated. The stranger test as a
  *no-key discovery* exercise is **local only**.
- **Local:** clone the repo and run `make quickstart`, then follow
  [docs/quickstart.md](quickstart.md) — it boots a real server with
  self-serve key minting and one invokable governed tool, so every step
  below is reachable with no operator-issued key. (`make prove-trust-plane`
  remains the no-server option: it proves the same loop in-process, but you
  watch it rather than drive it.)

## The run — each step maps to one claim

Record PASS/FAIL and, for any FAIL, the exact point the docs stopped being
enough.

1. **Discover and authorize.** From the discovery contract alone, find the one
   invokable tool, learn that a call needs a signed permit + idempotency key,
   and make one governed call succeed. Capture the receipt.
   → gate for everything below.

2. **Deliberate retry (Claim 1 — charge-once).** Send the *same* call with the
   *same* idempotency key again. Expect the identical receipt id and no second
   charge. Then send the same key with a *changed* payload and expect a
   fail-closed `idempotency_key_reused`, not a second execution.

3. **Deliberate budget overrun (Claim 2 — over-spend containment).** Keep
   invoking under a permit until the spend would exceed its cap. Expect
   `permit_budget_exceeded` on the call that would cross the cap, with no debit
   for the denied call. The stranger should be able to predict *which* call
   gets denied from the permit's `max_credits` and the tool's per-call cost.

4. **Interrupted invocation (Claim 3 — accounting).** This one usually needs the
   local target, where the stranger can point the gateway at an upstream that
   times out. Expect a `delivery_uncertain` result that **stays charged** and
   is **not** silently retried or refunded on replay. Confirm from the docs
   (`docs/failure-semantics.md`) that this is the intended terminal state, not a
   bug — the stranger should reach that conclusion without asking.

5. **Receipt verification (Claim 4 — offline-verifiable).** As a stranger who
   does not trust this plane, fetch the portable receipt
   (`GET /v1/receipts/{id}/portable`) and the unauthenticated key document
   (`GET /.well-known/trust-keys.json`), and verify the receipt **offline** with
   `python -m b2a_sdk.verify_cli --bundle bundle.json --keys trust-keys.json`.
   Expect `VERIFIED`. Then flip one byte of the signed input and expect the
   verifier to report the receipt as forged (exit 1), distinctly from a missing
   key (exit 2).

6. **Denied by authority (Claim 5 — authority before money).** Ask for a tool
   the permit does not allow. Expect a specific denial reason and a signed
   denial receipt that carries no ledger linkage — evidence the stranger can
   verify the same way as a success receipt.

## Passing the milestone

The stranger test passes when all six steps are PASS and the stranger asked
**zero** questions. A single question is a finding: capture it, fix the doc or
the surface it points at, and re-run with a new stranger. Do not fix it by
telling the next stranger the answer.

The honest boundary still holds: a receipt proves what happened, never what did
not, and offline verification trusts the issuing origin for key distribution
(`SECURITY_LIMITATIONS.md`, `docs/PROOF_MATRIX.md`). The stranger test checks
that the *claimed* guarantees are reachable and checkable — not that the
un-claimed ones exist.
