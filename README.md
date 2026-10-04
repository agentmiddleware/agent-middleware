# Agent Middleware (AMW)

> **Agents retry. Consequential tools should not run or charge twice.**

Agent Middleware is a gateway that sits in front of an agent's irreversible MCP
tools (refunds, payouts, writes to systems of record). Agents act under a
permit; a same-key retry returns the original receipt, no second call or
charge.

```text
scoped signed permit -> governed MCP call (idempotency key) -> one dispatch, one debit -> Ed25519-signed receipt
```

**Status: pre-revenue, pre-release, solo-maintained.** There are no paying
customers and no production deployments to point to. We are looking for design
partners with one real consequential tool. Nothing here is published to PyPI or
npm yet; install from source.

## See it in about a minute

Prerequisites: Python 3.11+, [`uv`](https://docs.astral.sh/uv/), and `make`.

```bash
git clone https://github.com/PetrefiedThunder/agent-middleware.git
cd agent-middleware/gateway
make demo-ambiguous-retry
```

An accounts-payable agent pays invoice `INV-4417`, $250.00. The payout
succeeds, **the response never reaches the agent**, and the agent correctly
retries. The same tool runs once without the boundary and once behind it:

```text
WITHOUT a transaction boundary        WITH Agent Middleware
  PAY-0001  INV-4417  $250.00           PAY-0001  INV-4417  $250.00
  PAY-0002  INV-4417  $250.00
  2 payouts, $500.00                    1 payout, $250.00
                                        1 debit, same receipt on both
                                        retry returned PAY-0001
```

The payout count is the number of times the tool body actually ran. The demo
then verifies the receipt offline against the published key set.

Next: [QUICKSTART.md](QUICKSTART.md) boots a local gateway, connects an MCP
client to it, and walks a permit, a governed call, a replay, and an offline
receipt check.

## What it guarantees, and what it does not

- **At most one gateway dispatch and at most one debit per accepted idempotency
  key.** A retry with the same key returns the original signed receipt. The same
  key with a different payload fails closed.
- **Authority before money.** Out-of-scope, expired, revoked, or tampered permits
  are denied before any debit.
- **Offline-verifiable receipts.** Ed25519 signatures over canonical JSON,
  checkable with no network and no account.

It is **not exactly-once.** If the gateway sent the call and no trustworthy
answer came back, the receipt says `delivery_uncertain`, the debit is kept, and
nothing is re-sent automatically; someone has to reconcile with the downstream
system. A remote side effect is deduplicated only if the remote tool also honors
the forwarded idempotency key. **An agent that restarts with a new key can
still pay twice.** It is not a payment network, an IAM system, or a compliance
product. Read [gateway/SECURITY_LIMITATIONS.md](gateway/SECURITY_LIMITATIONS.md)
before evaluating it for anything real.

## What is in this repository

| Path | What it is | License |
|---|---|---|
| [`spec/`](spec/) | Permit, receipt, and key-set format (v0.1 draft) with test vectors | Apache-2.0 |
| [`verifier/`](verifier/) | How to verify receipts offline; vector checker | Apache-2.0 |
| [`sdk/python/`](sdk/python/) | Python client SDK and the `b2a-verify-receipt` offline verifier | Apache-2.0 |
| [`sdk/typescript/`](sdk/typescript/) | TypeScript offline receipt verifier | Apache-2.0 |
| [`integrations/`](integrations/) | LangChain, CrewAI, AutoGen, OpenAI Agents wrappers; LangGraph, PydanticAI, LlamaIndex adapters | Apache-2.0 |
| [`examples/`](examples/) | Runnable examples, including an MCP client config | Apache-2.0 |
| [`gateway/`](gateway/) | The gateway itself: permits, idempotency, dispatch fence, metering, receipts, audit chain, MCP endpoints, migrations, tests, failure lab | **Fair Source: FSL-1.1-ALv2** |

The gateway is **Fair Source, not open source**. Under the Functional Source
License you can read, modify, and self-host it for your own agents, and use it
for anything except offering a competing product. **Every release converts to
Apache-2.0 two years after it is published.** The spec, verifiers, SDKs,
integrations, and examples are open source under Apache-2.0 today. Details and
plain-English FAQ: [LICENSING.md](LICENSING.md).

A hosted, managed version of the gateway is planned; its operations code is not
in this repository.

## Contributing and security

- [CONTRIBUTING.md](CONTRIBUTING.md): setup, tests, and DCO sign-off (no CLA).
- [SECURITY.md](SECURITY.md): report vulnerabilities privately, not in issues.
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md), [GOVERNANCE.md](GOVERNANCE.md).

"Agent Middleware" and "AMW" are used as project names. The licenses above
cover code and text; they do not grant rights to use the project names or logos
to imply endorsement.
