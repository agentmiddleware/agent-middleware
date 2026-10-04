# sample_app — the unintegrated refund agent

`agent.py` is the whole application. It issues refunds by POSTing straight at
the refund tool with the tool's own bearer token, and it retries whenever an
attempt does not come back.

```python
agent = UnprotectedRefundAgent(client, bearer_token)
await agent.issue_refund_with_retries(
    RefundIntent(
        operation_id="refund:pay_1041",
        customer_id="cus_1041",
        payment_id="pay_1041",
        amount_minor_units=5000,
    )
)
```

## What is missing, and why each absence matters

| Missing | Consequence |
| --- | --- |
| Scoped authority | The agent holds the tool credential. Nothing bounds which payments it may refund or how much in total. |
| Replay key | `POST /refunds` carries no idempotency key, so the tool cannot tell a retry from a second refund. |
| Receipt | Six weeks later the only evidence that this refund was authorized is a log line the agent wrote about itself. |
| An outcome vocabulary | `issue_refund_with_retries` reads "no response" as "did not happen". A response lost *after* the tool committed becomes a second refund. |

That last row is the measured failure. The refund tool in this lab executes
and commits before the fault layer drops the response, and the independent
effect ledger records both executions as separate rows. Nothing in this file
prevents the duplicate; nothing in this file even notices it.

## The exercise

Put the gateway in front of this agent. Do not edit the gateway, do not turn
any of its checks off, and do not replace the independent verifier with the
gateway's own. The prompt is in [`../PROMPT.md`](../PROMPT.md); the judge is
`failure_lab/integration_check/judge.py` and decides from the effect ledger,
the fault layer and the gateway's tables — never from what the integration
reports about itself.

## What this sample app is not

It is not a benchmark, a reference architecture, or an argument that direct
tool calls are wrong. A correctly built downstream that honors `operation_id`
as a durable idempotency key removes the duplicate with no gateway at all —
that is configuration `B_direct_native_idempotency` in the main suite, and
when it handles a scenario the lab says so. This file models the integration
that has not done that yet, because that is the one the exercise starts from.
