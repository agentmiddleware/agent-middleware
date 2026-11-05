# Agent Middleware competition kit

This kit is for judges and evaluators. It explains what Agent Middleware (AMW)
is, gives a 5 minute path to see it work, and states plainly what is built,
what is planned, and what we do not claim.

## What AMW is

From the [repository README](../README.md):

> Agent Middleware is a gateway you put in front of an agent's risky tools
> (refunds, payouts, deploys) so each action needs a signed permit, runs at
> most once per request key, and leaves a signed receipt anyone can check
> offline.

The "at most once per request key" wording refers to gateway dispatch and
debit. It is not exactly once delivery. See [CLAIMS.md](CLAIMS.md).

Status, also from the README: pre revenue, pre release, solo maintained. There
are no paying customers or production deployments to point to.

### Concepts

- **Permit:** a signed grant that limits an agent to specified tools, a budget,
  and an expiry.
- **Call:** a governed MCP tool invocation under a permit.
- **Request key:** the `Idempotency-Key` header that identifies a request for
  retry handling.
- **Receipt:** a signed record of a call's outcome and hashes of its request
  and response, with a debit link when applicable. Receipts sign hashes, not
  raw tool content.
- **Credits:** internal metering units, not money.

## 5 minute judge path

1. **Read the problem (1 minute).** [DUPLICATE-COST.md](DUPLICATE-COST.md)
   explains the failure AMW targets: an agent retries a paid tool call after a
   timeout, and without idempotency the action could run twice.
2. **Check the hosted gateway is up (30 seconds).**
   Run `curl -s https://api.agentmiddleware.dev/health/dependencies`. Per
   `gateway/app/core/health.py` (`build_public_dependency_report`), the
   response should include a dependency report, `status`, and `version`; run
   the curl to confirm on the hosted gateway. Details in
   [DEMO-SCRIPT.md](DEMO-SCRIPT.md), step 1.
3. **Get a demo key (30 seconds).** `POST /v1/demo/keys`
   (`gateway/app/routers/demo_keys.py`) on the hosted gateway, or use the
   [demo key page](https://www.agentmiddleware.dev/demo-key/) (source in
   `site/demo-key/index.html`).
   [DEMO-SCRIPT.md](DEMO-SCRIPT.md), step 2.
4. **See a retry return the same receipt (2 minutes).** Either the hosted
   [amw-demo](https://github.com/agentmiddleware/amw-demo) client, or a local
   gateway with `make quickstart`. [DEMO-SCRIPT.md](DEMO-SCRIPT.md), steps 3
   and 4.
5. **Read what we claim and do not claim (1 minute).** [CLAIMS.md](CLAIMS.md).

Install path today is `git clone`. The `agent-middleware` 0.5.0 package is not
on PyPI yet: TODO(T-01).

## Kit contents

| File | What it covers |
| --- | --- |
| [README.md](README.md) | This page |
| [DEMO-SCRIPT.md](DEMO-SCRIPT.md) | Step by step demo against endpoints that exist in this repo |
| [DUPLICATE-COST.md](DUPLICATE-COST.md) | The duplicate cost narrative, what exists today, and the planned Stripe test mode demo (T-03, not built) |
| [CLAIMS.md](CLAIMS.md) | What we claim and what we explicitly do not claim |

## Further reading in this repo

- [QUICKSTART.md](../QUICKSTART.md): run the gateway locally.
- [gateway/SECURITY_LIMITATIONS.md](../gateway/SECURITY_LIMITATIONS.md): known
  limits and what is deferred.
- [gateway/docs/failure-lab.md](../gateway/docs/failure-lab.md): the lost
  response failure lab, including a native idempotency baseline.
- [examples/retry-safe-refund/](../examples/retry-safe-refund/): local payout
  retry demo against an in memory stub.
- [verifier/](../verifier/): offline receipt verification.
- [spec/](../spec/): draft permit, receipt, and key formats.
