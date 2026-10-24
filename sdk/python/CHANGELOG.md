# Changelog

## Unreleased — 0.5.0

`pyproject.toml` carries `0.5.0` from here on. `0.4.0` is published and
tagged `python-sdk-v0.4.0`, and the entries below are new surface area on
top of it, so leaving the source at `0.4.0` would have shipped something
materially different under a version already in the wild. A minor bump:
everything here is additive, with one bounded exception: `settle_402()` now
prefers `amount_usd` over the legacy `amount` alias when a requirement carries
both with different values (see below). `parse_402_response()` always sets the
two equal, so the normal path is unchanged. The tag is not cut by this
change. `python-v0.5.0` is still a release decision.

### Distribution name

The Python distribution is now `amw-sdk`. After publication, install it with
`pip install amw-sdk`.
Both `amw` and `b2a_sdk` remain available as import packages.

### Migrating from b2a-sdk

`b2a-sdk` was never published to PyPI. For a local install or the 0.4.0 wheel,
run `pip uninstall b2a-sdk` before `pip install amw-sdk`. Existing imports
remain unchanged.

### Receipt verifier

- `verify_permit_bundle()` checks the signed permit's required fields and
  rejects a signed receipt as permit evidence. `VerificationResult.permit_id`
  reports the signed permit id without changing existing positional arguments.
- An edited `alg` or `canonicalization` inside `signing_input` is `invalid`
  when the signature does not verify and the key is available. Those claims
  are `unsupported` only after the signature over the exact bytes checks out.
  Reporting them first let a tampered receipt look like a verifier limitation
  (`unsupported`), including on the public `verify_receipt` preflight, which
  then skipped the published keys.

### Audit follow-up (2026-10-01)

Security and correctness fixes from the 2026-08-27 repository audit. The only
incompatible change is `B2AEdgeClient.execute_awi_action()`, and 0.5.0 is
still unreleased.

- **Replay-safe legacy charges**: `AgentMiddlewareClient.charge()` takes a
  keyword-only `idempotency_key`, validated before anything is sent and
  forwarded as `Idempotency-Key`; a 409 on a keyed charge raises
  `IdempotencyConflictError`. `@billable` and `@combined` take an
  `idempotency_key_factory`, called with the decorated function's arguments,
  so retries of one logical call derive the same key. Calls without a key are
  unchanged and are not replay-safe.
- **`create_agent_wallet(daily_limit=0)`** now sends the zero cap ("spend
  nothing"). A truthiness test used to drop it, which the server reads as
  "no cap".
- **`@monitored`** no longer sends the exception text or a stack trace to
  telemetry by default; pass `capture_traceback=True` to include them. Its
  sync wrapper no longer raises `RuntimeError` when called with no running
  event loop: the telemetry event is dropped and the wrapped function's own
  result or exception comes back.
- **`api_key` is a read-only property** on `AgentMiddlewareClient` and
  `X402Client`, read from the transport's `X-API-Key` header, so `vars()`
  dumps no longer contain the key. Assigning to it raises `AttributeError`;
  assignment never changed the key actually sent.
- **`B2AEdgeClient.execute_awi_action()`** requires keyword `permit_id` and
  `idempotency_key` and sends them as `X-Permit-Id` and `Idempotency-Key`.
  The server already required both, so the old call always failed with 403
  or 400. Blank values raise `ValueError` before any request is sent.

### Added

- **Legacy MCP `serve` result format**: Successful served-tool calls now pass
  through the gateway's MCP `content` and `structuredContent` directly. Older
  clients that parse the full gateway JSON response from `content[0].text` can
  start `python -m b2a_sdk.mcp serve --legacy-result-format` to retain that
  envelope. Permit denials and pending human approvals return actionable MCP
  results with retry guidance.
- **ACP (Agentic Commerce Protocol) checkout**: Add `ACPLineItem`,
  `ACPCheckoutRequest`, `ACPCheckoutResponse` models and
  `AgentMiddlewareClient.acp_checkout()` method for POST /v1/billing/acp/checkout.
  The server mints the permit; this is not invoke_tool. Proof-only: the
  endpoint is on the server's dormant `billing.expansion_router` and is
  mounted only when `ENABLE_PROOF_SURFACES=true`, so deployments with proof
  surfaces off return 404. `ACPLineItem.sku` defaults to `None`, so a line
  item without a SKU is built by omitting it. `sponsor_wallet_id` and
  `agent_wallet_id` are required and sent as query params, matching the server
  route. `ACPCheckoutRequest.spt_token` is declared `repr=False`, so the
  delegated payment credential does not surface in logs or tracebacks that
  render the request object. It is still sent on the wire by `to_payload()`,
  which callers must not log.
- **IGA bearer authentication**: `AgentMiddlewareClient.__init__()` accepts an
  optional `bearer_token` parameter. When set, the client sends
  `Authorization: Bearer <token>` alongside X-API-Key for IGA-governed flows
  (the governed invoke endpoint reads the bearer when `IGA_TRUSTED_ISSUERS` is
  set). The client never mints tokens itself.
- **x402 parse endpoint**: Add `X402Client.parse_402()` method for POST
  /v1/x402/parse. It returns a validated requirement dict — not a typed model —
  with `amount_usd`, `pay_to` and `network` required and `asset` optional; a 2xx
  body missing a required field raises `APIError` rather than reaching
  settlement. The client-side `parse_402_response()`
  helper now returns `amount_usd` to match, alongside the legacy `amount`
  key (same value) so existing callers keep working. `settle_402()` reads
  `amount_usd` first and falls back to `amount` only when it is absent, so the
  canonical field decides the settled amount.
- **In-process permit validation**: Document `LocalPermitValidator` and
  `GovernedEdgeSession` in README. These classes, already shipped in 0.5.0,
  verify a permit's Ed25519 signature locally and mirror the server's per-call
  permit checks in-process (expiry, tool scope, per-tool call caps, and budget
  when `estimated_credits` is supplied) with no server round trip for
  locally-denied calls. Local signature verification needs the optional
  `verify` extra (`pip install './sdk/python[verify]'`); without it,
  `GovernedEdgeSession.open()` raises
  `PermitDeniedError("permit_verification_unavailable")`.
- **`mcp` extra**: `pip install './sdk/python[mcp]'` installs the MCP SDK for
  `python -m b2a_sdk.mcp serve` and the generated standalone server, bounded
  to `>=1.29.0,<2` to match the application. mcp 2.x renamed `FastMCP` to
  `MCPServer`, which both paths import. The import-error hints now name the
  bound (or the extra) instead of an unbounded `pip install mcp`.

### Fixed

- **x402 HTTP 402 handling**: The x402 module's `parse_402()` and `settle_402()`
  methods do not collapse an HTTP 402 status into `InsufficientFundsError`. A
  402 response with valid x402 headers is parsed as a payment requirement, not
  a billing error. `InsufficientFundsError` is raised only when the response
  body signals `insufficient_funds`.

### Deprecations

- **Deprecated `B2AEdgeClient.call_mcp_tool()`**: This method bypasses the
  trust-plane loop (no permit, no idempotency key, no signed receipt, no replay
  protection). Each call dispatches and charges independently, making retry a
  double-charge. Use `AgentMiddlewareClient.invoke_tool()` instead, which
  requires a permit and idempotency key for governed invocations with replay
  protection and signed receipts.
- Updated framework wrapper READMEs (langchain, crewai, autogen) to document
  that direct tool calls bypass the trust plane and lack replay protection.

- Add `b2a_sdk.receipt_verifier` for offline verification of portable trust
  receipts. It imports nothing from the middleware application and needs no
  network access: given a bundle and a published key set, it checks the
  Ed25519 signature over the exact signed bytes and reports only fields read
  from inside them.
- Distinguish a failed verification (`INVALID`) from an undetermined one
  (`UNKNOWN_KEY`, `MALFORMED`, `UNSUPPORTED`), so callers do not mistake a
  stale key cache for tampering. A bundle that is internally inconsistent —
  or that disagrees with a caller-supplied `expected_issuer` — is `MISMATCH`:
  a finding about the bundle, distinct from both families.
- Read `kid`, `alg`, and `canonicalization` from the **signed** payload, never
  from the surrounding envelope. The envelope is unauthenticated, so letting it
  select the key or gate the capability checks would let one edited byte
  downgrade a genuine receipt to `UNSUPPORTED` — an attacker-chosen "your
  verifier is too old" that reads as a verifier problem rather than a bundle
  problem. Envelope values are cross-checked *after* the signature verifies,
  and any disagreement is reported as `MISMATCH`.
- Add `VerificationResult.is_rejected`, true for `INVALID` and `MISMATCH` —
  the property callers should branch on for "do not trust this bundle".
  `is_tampered` stays narrow (`INVALID` only), because `MISMATCH` also covers
  an `expected_issuer` disagreement, which is not a claim about tampering.
- Add the `b2a-verify-receipt` CLI, with exit codes `0` verified, `1` rejected
  (`INVALID` or `MISMATCH`), `2` undetermined.
- Add the `verify` extra (`pip install "b2a-sdk[verify]"`) for the
  `cryptography` dependency. The base install is unchanged; importing
  `b2a_sdk` without the extra still works, and only signature checking
  requires it.
- Require `cryptography>=50.0.0` in the `verify` and `dev` extras, matching
  the application's own floor: every 42.x–49.x release carries at least one
  published advisory, and a receipt *verifier* must not itself depend on a
  known-vulnerable cryptography build.

## 0.4.0

- Add the typed async `AgentMiddlewareClient` trust-loop API.
- Require caller-provided idempotency keys for permit creation and tool calls.
- Expose typed permits, receipts, verification results, and evidence bundles.
- Raise typed authentication, authorization, permit, billing, idempotency,
  delivery-uncertainty, API, and transport errors.
- Retain `B2AClient` as a deprecated compatibility name.
- Build installable wheel and source artifacts from the standard `src/` layout.
