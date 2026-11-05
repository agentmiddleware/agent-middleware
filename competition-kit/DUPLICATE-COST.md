# The duplicate cost problem

## The story

An agent calls a paid tool, for example a refund. The tool does the work, but
the response is lost: a timeout, a dropped connection, a crashed worker. The
agent cannot tell "it failed" from "it worked and I never heard back", so it
retries. Without idempotency, the action could run twice: two refunds, two
payouts, two deploys, and two charges for the tool call.

AMW sits between the agent and the tool. Each call carries a request key (the
`Idempotency-Key` header). The gateway's behavior, as stated in the
[repository README](../README.md):

- a retry with the same key returns the original receipt instead of
  dispatching again;
- a new key is a new request;
- reusing a key with a different request fails with a conflict before
  dispatch;
- a `delivery_uncertain` receipt means no trustworthy answer came back. The
  debit is kept, and the operator must reconcile with the downstream tool. The
  gateway does not resend automatically.

The last point is the honest middle case. If the timeout happens after the
gateway has dispatched, the gateway cannot know whether the remote action
happened. It says so in a signed receipt rather than guessing or resending.

## What AMW does not do here

- The gateway limits dispatch and debit per key. Whether a remote effect
  happens once depends on the tool honoring the forwarded key.
- A receipt signs hashes of the request and response, not raw tool content,
  and cannot prove a remote effect happened.
- This is not exactly once delivery.

Payment APIs such as Stripe already support idempotency keys themselves. A
correctly built direct integration that persists its key across the retry is
already protected against a duplicate refund. The
[failure lab](../gateway/docs/failure-lab.md) includes that native baseline and
reports that it handles the lost response fault. What AMW adds in front of such
a tool is a permit that bounds what the agent may call and spend, one gateway
debit per key, conflict refusal for a changed request under the same key, and a
signed receipt a third party can verify offline. For tools that have no native
idempotency, the gateway still limits its own dispatch per key, but cannot make
the remote side effect idempotent.

## What exists today

These run locally with stubs. No money moves and no provider is called.

- [examples/retry-safe-refund/](../examples/retry-safe-refund/): a payout
  against an in memory stub. The response is dropped, the retry with the same
  key returns the original receipt, and the script asserts one provider
  dispatch and rejection of a changed payout under the same key.
- `make failure-lab` in `gateway/`: the lost response scenario across three
  integrations of a simulated payment rail. See
  [failure-lab.md](../gateway/docs/failure-lab.md) and
  [failure-lab-suite.md](../gateway/docs/failure-lab-suite.md).
- The local quickstart retry in [DEMO-SCRIPT.md](DEMO-SCRIPT.md), step 4.

This kit does not quote results from these runs. Run them and read the output.

## Planned demo: Stripe test mode refund (T-03)

**TODO(T-03): not built yet, no results exist.**

Test mode only. No real money. No live Stripe keys.

Planned flow:

1. Issue a permit scoped to one refund tool with a credit budget.
2. The agent calls the tool, which calls Stripe `refunds.create` in test mode
   for $1.00, forwarding the request key as the Stripe `Idempotency-Key`.
3. The response is dropped to simulate a timeout.
4. The agent retries with the same key.
5. Expected: the same `receipt_id` on both calls, one gateway debit, and the
   tool's second dispatch not called.

The $1.00 is a Stripe test mode amount. The gateway debit is in credits, which
are internal metering units, not money. They are separate units.

### Results

**TODO(T-03): not built yet, no results exist.**

No receipt IDs, debit counts, Stripe object IDs, timings, or logs exist for
this demo. Do not present any.

### Verification

**TODO(T-03): not built yet, no results exist.**

The planned check is offline receipt verification as in
[DEMO-SCRIPT.md](DEMO-SCRIPT.md), step 5, plus the Stripe test mode dashboard
showing one refund for the key. Neither has been run.

### Open questions for T-03

- **TODO(T-03):** how the timeout is injected (before or after the Stripe call
  returns), and whether the demo also shows the `delivery_uncertain` case.
- **TODO(T-03):** where the Stripe test mode secret is held. It must never
  appear in this repository, the kit, or a recording.
