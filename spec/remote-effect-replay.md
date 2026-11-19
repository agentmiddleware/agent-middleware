# Remote Effect Replay (draft RFC)

**Author:** C.Lee  
**Status:** Draft  
**Created:** 2026-11-18

## Abstract

This note explores "OmniNode-style replay": reconstructing and verifying what a remote (upstream MCP) tool actually did for a given AMW call, not just what the gateway admitted and dispatched. It describes what AMW records today, identifies gaps, evaluates candidate designs for replay verification, and recommends a smallest next step.

This is an R&D design note, not a product roadmap. No claims about future features or delivery timelines are made.

## 1. What AMW Records Today

### 1.1 Request Hash

The gateway computes a SHA-256 hash over `{tool_name, arguments, wallet_id, permit_id}`, not the wire request bytes.

**Code references:**
- `gateway/app/routers/mcp.py:1572-1581` — constructs `effective_request_payload` from these four fields, then `effective_request_hash = sha256_hex(effective_request_payload)`
- `gateway/app/services/receipts.py:171` — `receipt_model_to_response` assigns `request_hash=model.request_hash`
- `gateway/app/services/receipts.py:207` — signed payload includes `"request_hash": model.request_hash`
- `spec/receipt-v0.1.md:38` — "Lowercase hex SHA-256 of the request and response values"

The `request_hash` is signed into the receipt (line 207) and links the receipt to the logical invocation (tool name, arguments, wallet, permit), not transport framing.

### 1.2 Idempotency Key

Each governed call includes an idempotency key, which the gateway uses for replay protection.

**Code references:**
- `gateway/app/services/receipts.py:165` — `receipt_model_to_response` assigns `idempotency_record_id=model.idempotency_record_id`
- `gateway/app/services/receipts.py:225-226` — receipt signing optionally includes `idempotency_record_id` linkage (when `include_linkage` is true)
- `spec/receipt-v0.1.md:36` — "The gateway's record for the idempotency key (the raw key is not signed). Omitted unless this signature included linkage"
- `gateway/app/services/upstream_mcp.py:999-1011` — `_call_tool_once` requires `invocation_id` and `idempotency_key` before dispatch

The raw idempotency key is never signed (only its record ID is), so a verifier with only the receipt cannot prove the upstream tool received the same key.

### 1.3 Dispatch Attempt State

The gateway persists a durable state machine for each upstream dispatch attempt.

**Code references:**
- `gateway/app/services/mcp_dispatch_attempts.py:165-167` — dispatch states: `"prepared"`, `"dispatched"` (legacy), `"dispatch_claimed"`
- `gateway/app/services/mcp_dispatch_attempts.py:168-175` — terminal states: `"succeeded"`, `"returned_error"`, `"delivery_uncertain"`, `"response_rejected"`
- `gateway/app/services/mcp_dispatch_attempts.py:1650-1653` — sets `dispatch_claim_hash` when claiming dispatch (before the send)
- `gateway/app/services/mcp_dispatch_attempts.py:1998` — stores `response_hash` on terminal result
- `gateway/app/db/models.py` (line references via grep) — `McpDispatchAttemptModel` includes `dispatch_claim_hash`, `response_hash`, `result_json`, `result_size_bytes`

The `dispatch_claim_hash` is a durable one-shot boundary proving a single dispatch claim (set before the upstream send). The `result_json` field stores the actual response from the upstream tool.

### 1.4 Receipt Fields

Receipts include several fields that bind the call to its execution:

**Code references:**
- `gateway/app/services/receipts.py:161-190` — `receipt_model_to_response` shows all fields
- `gateway/app/services/receipts.py:201-217` — `_verification_payload` constructs the base signed payload
- `spec/receipt-v0.1.md:30-56` — full signed payload specification
- `gateway/app/services/receipts.py:208` — `"response_hash": model.response_hash` is signed as JSON `null` when there is no response, present otherwise

Signed fields include:
- `receipt_id`, `permit_id`, `wallet_id`, `key_id` (identity)
- `tool` (tool name)
- `idempotency_record_id`, `dispatch_attempt_id` (linkage, when included)
- `request_hash`, `response_hash` (content digests)
- `outcome` (one of `success`, `denied`, `insufficient_funds`, `failed_refunded`, `failed_unrefunded`, `delivery_uncertain`, `response_rejected`)
- `credits_authorized`, `credits_charged` (metering)
- `ledger_entry_id`, `audit_event_id` (internal linkage)
- `created_at` (timestamp)
- `alg`, `kid` (signature metadata)

Additional fields signed only when set:
- Lines 218-224: `reason_code`, `enforcement_mode`, `gateway_build`
- Lines 231-232: `approval_id`
- Lines 235-244: `permit_digest`, `approval_digest`, `counterparty`, `sequence`, `prev_receipt_digest`
- Lines 246-250: `constraints_evaluated`

### 1.5 Audit Chain

The gateway maintains a hash-chained audit log linking each call.

**Code references:**
- `gateway/app/services/audit_chain.py:27-60` — `audit_payload` constructs signed audit events
- `gateway/app/services/audit_chain.py:142` — `model.previous_hash = previous_hash` chains each event to its predecessor
- `gateway/app/services/audit_chain.py:150-151` — `model.chain_hash = sha256_hex(...)` commits the chain link
- `gateway/app/services/receipts.py:213` — receipts include `"audit_event_id": model.audit_event_id` in the signed payload

The audit chain proves the temporal order of gateway-side events and provides tamper evidence for the gateway's own records.

### 1.6 Stored Response

The gateway stores the upstream tool's response.

**Code references:**
- `gateway/app/services/mcp_dispatch_attempts.py:1998` — `attempt.response_hash = response_hash` and `attempt.result_json` (stored earlier in the function)
- `gateway/app/services/upstream_mcp.py:989` — computes `response_hash=hashlib.sha256(encoded).hexdigest()` from the canonical response JSON
- `gateway/app/services/mcp_dispatch_reconciliation.py:871` — validates `sha256_hex(attempt.result_json) != attempt.response_hash`

The `result_json` column holds the tool's actual response. The `response_hash` is signed into the receipt, binding the receipt to a specific response without including the response body in the signature.

## 2. What Is Missing for Remote Effect Replay

A third-party verifier with only AMW's receipts and dispatch records can prove:
1. What logical invocation the gateway **recorded** (via `request_hash` over `{tool_name, arguments, wallet_id, permit_id}`)
2. What response the gateway **stored** (via `response_hash`)
3. That the gateway's internal records are consistent (via audit chain, permit/receipt digests, sequence numbers)

A verifier **cannot** prove:
1. **That the upstream tool actually executed the request**: the gateway's stored `result_json` could be fabricated, replayed from a different call, or corrupted
2. **That the response hash corresponds to the tool's actual output**: without a tool-signed attestation, a verifier trusts the gateway to report the correct `response_hash`
3. **What side effects the tool performed**: for non-idempotent tools, the stored response does not describe database writes, sent emails, or other state changes
4. **Temporal ordering at the tool**: receipts prove when the gateway recorded the call, not when the upstream system executed it
5. **Non-repudiation by the tool provider**: the tool cannot later be held accountable for the effect because it never signed anything

The core gap: **the remote tool produces no signed artifact**. The gateway is trusted to faithfully relay requests and responses, but a verifier cannot independently confirm this without cooperation from the upstream system.

## 3. Candidate Designs

### 3.1 Tool-Signed Result Attestations

**Mechanism:** Each upstream MCP tool signs its own response before returning it to the gateway. The gateway includes the tool's signature in the receipt or stores it alongside the dispatch attempt.

**What it enables:**
- A verifier can prove the tool returned a specific response
- Non-repudiation: the tool provider cannot later deny having returned that result
- Dispute resolution: if the gateway's `response_hash` disagrees with the tool's signed hash, one party is provably dishonest

**What it requires from tool providers:**
- Each tool must maintain its own signing key (key rotation, revocation, HSM integration)
- Tools must sign every response, adding latency and complexity
- Tools must define a canonical response format (ordering, whitespace, etc.)
- Tools must publish their public keys in a discoverable registry

**Trade-offs:**
- **Pro:** Strongest verifiability; shifts trust from the gateway to the tool provider's key management
- **Pro:** Enables cross-gateway comparison (two gateways calling the same tool can compare signed responses)
- **Con:** High adoption barrier — most existing MCP tools do not sign responses
- **Con:** Does not prove side effects for non-idempotent operations
- **Con:** Requires tool providers to understand signing, canonicalization, and key management

### 3.2 Request/Response Transcript Hashing in Receipt

**Mechanism:** The gateway signs a more detailed digest into the receipt, such as:
- A Merkle tree root over (request, response, timestamp, dispatch\_attempt\_id)
- A hash of the full request and response bodies (not just their SHA-256s)
- A "transcript commitment" that binds together the gateway's view of the request/response pair

**What it enables:**
- A verifier can prove the gateway's internal view is consistent (the receipt's signed `request_hash` and `response_hash` match the stored `result_json`)
- Lightweight audit: instead of storing full transcripts, store hash commitments and reconstruct on demand

**What it requires from tool providers:**
- Nothing — this is a gateway-only mechanism

**Trade-offs:**
- **Pro:** No changes needed from tool providers
- **Pro:** Low latency impact (one additional hash computation)
- **Con:** Still trusts the gateway to faithfully report what the tool returned
- **Con:** Does not enable non-repudiation by the tool
- **Con:** Does not prove side effects

### 3.3 Verifier-Side Replay Harness

**Mechanism:** A third-party verifier re-executes the tool call with the same request and compares the response. The gateway publishes its `request_hash` and `response_hash`, and the verifier independently calls the upstream tool to check if the response matches.

**What it enables:**
- Verifies that the tool is **currently** returning the same response for the same request
- Can detect if the gateway fabricated a response or if the tool's behavior has changed
- Requires no changes to the tool's signing infrastructure

**What it requires from tool providers:**
- Tools must be idempotent (or the verifier must tolerate side effects)
- Tools must be accessible to the verifier (authentication, network access)
- Tools must return consistent responses for the same input (no timestamp in response, etc.)

**Trade-offs:**
- **Pro:** Works with existing tools (no signing required)
- **Pro:** Detects retroactive tampering by the gateway
- **Con:** Cannot verify non-idempotent side effects (emails sent, database rows deleted)
- **Con:** Requires the verifier to have credentials and network access to the upstream tool
- **Con:** Does not prove what happened **at the time**: only what the tool returns **now**
- **Con:** High cost: every verification is a live tool invocation (rate limits, billing, side effects)

## 4. Recommended Next Step and Open Questions

### Recommended Next Step

**Extend receipts to include a "replay hints" object** that helps future replay efforts without requiring tool-side changes today. Specifically:

1. Add an optional `replay_metadata` field to receipts (not signed, or signed as an optional extension):
   ```json
   {
     "upstream_tool_endpoint": "<tool-endpoint-url>",
     "request_canonical_json": "<canonical-request-json>",
     "response_canonical_json": "<canonical-response-json>",
     "dispatch_timestamp_utc": "<ISO-8601-dispatch-time>",
     "tool_reported_timestamp": "<ISO-8601-tool-time>"
   }
   ```
   (Illustrative shape with placeholders; actual recorded values would be populated by the gateway)
2. Store the full request/response transcripts alongside the receipt, indexed by `dispatch_attempt_id`
3. Publish a "replay verification spec" that describes how a verifier can call the tool and compare responses

This enables verifier-side replay for idempotent tools **without requiring tool changes now**, while leaving the door open for tool-signed attestations later.

**Why this first:**
- It is implementable entirely within AMW
- It provides immediate value for debugging and audit
- It does not force tool providers to adopt signing before they are ready
- It establishes the transcript format that a future signing mechanism can adopt

### Open Questions

1. **Scope of replay:** Should replay apply to all tools, or only a subset marked as "replay-safe"?
2. **Idempotency verification:** How does a verifier determine if a tool is safe to re-invoke?
3. **Credential handling:** If the verifier calls the upstream tool, does it use its own credentials or replay the original agent's credentials?
4. **Side effect boundaries:** For non-idempotent tools (e.g., sending an email), what does "verification" even mean? Is observing the sent email sufficient?
5. **Tool provider incentives:** Why would a tool provider adopt signing? What liability or compliance requirement makes the effort worthwhile?
6. **Cross-gateway federation:** If multiple gateways call the same tool, can they share verification artifacts? Does this require a "tool response notary" service?
7. **Canonicalization drift:** If the gateway and the tool use different JSON canonicalization rules, hashes will not match even for identical logical responses. How is this resolved?
8. **Timestamp skew:** If a tool includes a server-side timestamp in its response, replay will never match. Should timestamps be excluded from the signed payload?

These questions do not block the recommended next step, but they shape the longer-term design.

---

**This is a design note only.** No implementation timeline, roadmap commitment, or product feature is implied. C.Lee is the sole maintainer.
