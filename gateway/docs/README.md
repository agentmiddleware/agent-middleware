# Documentation guide

This is the entry point for the supported Agent Middleware API path: one
consequential MCP tool behind a governed boundary. Start with the
[repository overview](../README.md) for the product summary and local proof.

## Start with your goal

| Goal | Start here |
|---|---|
| Decide whether the boundary fits your problem | [Product wedge](../WEDGE.md) |
| Run the core loop locally | [Quickstart](quickstart.md) |
| Configure one real upstream MCP tool | [Partner first-tool runbook](partner-first-tool-runbook.md) |
| Use the typed Python SDK and offline verifier | [Python SDK](../../sdk/python/README.md) |
| Make stable keys for governed retries | [Request keys](concepts/request-keys.md) |
| Understand retry, debit, and crash outcomes | [Failure semantics](failure-semantics.md) |
| Measure the lost-response fault against a correctly used native idempotency key | [Failure lab](failure-lab.md) |
| Run fourteen injected failures and get a signed evidence bundle | [Failure lab suite](failure-lab-suite.md) |
| Review security claims and limits | [Security limitations](../SECURITY_LIMITATIONS.md) and [security review kit](security-review-kit.md) |
| Rotate or revoke API keys | [API key rotation](api-key-rotation.md) |
| Configure API-key hashing (pepper) | [API key hashing](api-key-hashing.md) |
| Plan a design-partner evaluation | [Design partner guide](../DESIGN_PARTNER_GUIDE.md) |
| Tell plain pytest, Postgres, and live runs apart | [Test tiers](TESTING_TIERS.md) |
| Contribute to the repository | [Contributing guide](../../CONTRIBUTING.md) |

## Agent and API discovery

Autonomous clients begin with
`GET /.well-known/agent.json`, then `GET /llms.txt`,
`GET /mcp/tools.json`, and `GET /openapi.json`. Read the
[agent bootstrap section](../README.md#agent-bootstrap) for the current
transport, authentication, and dependency-readiness rules.

## Supported pilot boundary

The design-partner deployment governs one operator-configured Streamable HTTP
MCP tool. A scoped permit and accepted idempotency key allow at most one gateway
dispatch and wallet debit; an identical replay returns the prior result and
receipt. This is not exactly-once: a remote side effect is deduplicated only if the upstream tool honors
the forwarded idempotency key. Signed receipts are evidence for this boundary,
not a replacement for settlement, compliance, or IAM.

## Source-only and historical material

The repository also contains frozen proof surfaces and historical work. Do not
use them as product onboarding or a public capability list:

- [Proof-surface inventory](PROOF_SURFACES.md) records the explicitly frozen
  AWI, browser, media, IoT, and related workloads.
- [`agent-recipes.md`](agent-recipes.md) and
  [`human-onboarding.md`](human-onboarding.md) are legacy proof-surface
  material, not the supported one-tool pilot.
- [Product analytics](product-analytics.md) documents the optional PostHog
  signup, identify and activation events (`POSTHOG_API_KEY`, `POSTHOG_HOST`).
- [Pods](pods.md) are a dormant trust feature, not part of the supported pilot.
- `server.json` at the repository root is the official MCP registry
  manifest. It advertises the standard MCP endpoint
  (`https://api.agentmiddleware.dev/mcp`). To connect,
  [get an API key](https://www.agentmiddleware.dev/demo-key/).
  `ENABLE_STANDARD_MCP_ENDPOINT`
  defaults to on; a host that sets it false answers 404 after authentication.
  Before requesting owner approval to publish, send an authenticated MCP
  `initialize` request to that exact host with a valid wallet-scoped API key.
  Require HTTP 200 and a JSON-RPC `result` containing `protocolVersion` and
  `serverInfo`. An unauthenticated 401 does not prove the endpoint is enabled:
  authentication runs before the enable check, so a disabled host also returns
  401. Run `mcp-publisher publish` only after owner approval.
