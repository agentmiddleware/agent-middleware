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
| Understand retry, debit, and crash outcomes | [Failure semantics](failure-semantics.md) |
| Measure the lost-response fault against a correctly used native idempotency key | [Failure lab](failure-lab.md) |
| Run fourteen injected failures and get a signed evidence bundle | [Failure lab suite](failure-lab-suite.md) |
| Review security claims and limits | [Security limitations](../SECURITY_LIMITATIONS.md) and [security review kit](security-review-kit.md) |
| Plan a design-partner evaluation | [Design partner guide](../DESIGN_PARTNER_GUIDE.md) |
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
- No MCP registry submission runbook is shipped in this repository; do not
  submit the gateway to a registry while the deployed standard MCP endpoint
  is disabled.
- The `aegis/` and `superpowers/` planning directories are not shipped in
  this repository; they are not product documentation.
