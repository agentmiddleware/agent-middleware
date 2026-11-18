# Receipt boundary

A design partner asked: "Where does the gateway receipt end, and where do the downstream effects begin?" This document answers that question precisely from the code.

## What a receipt attests to

A signed gateway receipt (`app/services/receipts.py:ReceiptService.create_receipt()`) is a cryptographically verifiable statement about what the gateway **admitted, authorized, charged, and dispatched**. It does not attest to what happened downstream of the gateway boundary.

### Request admitted

Every receipt records the exact request the gateway accepted:

- **`request_hash`**: SHA-256 digest of the JCS-canonical request payload (`gateway/app/services/receipts.py:568`)
- **`tool`**: the tool name the request named
- **`idempotency_record_id`**: present only when the request was bound to an idempotency record; the unique record that owns this execution (`gateway/app/services/idempotency.py:IdempotencyService.begin_with_record()`)

The receipt proves the gateway received and processed this specific request. When `idempotency_record_id` is present, it proves processing under that idempotency key.

### Authorization decision

The receipt records whether the request was **permitted or denied**:

- **`permit_id`**: the permit that governed this request (`gateway/app/services/permits.py:PermitService`)
- **`permit_digest`**: SHA-256 of the permit's signing input, binding the receipt to the permit's exact terms (`gateway/app/services/receipts.py:615-627`)
- **`approval_id` / `approval_digest`**: when human approval was required, the approval record and its digest
- **`outcome`**: one of: `success`, `denied`, `insufficient_funds`, `failed_refunded`, `failed_unrefunded`, `delivery_uncertain`, `response_rejected` (`gateway/app/services/receipts.py:48-58`)
- **`reason_code`**: for non-success outcomes, the specific reason (e.g., `permit_budget_exceeded`, `duplicate_request_new_key`)
- **`constraints_evaluated`**: snapshot of the permit constraints checked at admission time (v2 permits only; `gateway/app/services/permits.py:permit_constraints_snapshot()`)

A `denied` or `insufficient_funds` receipt proves the gateway **rejected** the request before any side effect or charge.

### Charge applied

The receipt records what was debited from the wallet:

- **`wallet_id`**: the wallet that paid for this execution
- **`ledger_entry_id`**: the ledger debit row (`gateway/app/db/models.py:LedgerEntryModel`)
- **`credits_authorized`**: the permit's authorized amount for this call
- **`credits_charged`**: the actual amount debited (zero for denials/refunds; `gateway/app/services/receipts.py:543-546`)

The presence of `ledger_entry_id` and nonzero `credits_charged` in a `success`, `delivery_uncertain`, or `response_rejected` receipt proves the gateway **charged** the wallet. Refunded failures carry zero `credits_charged`.

### Dispatch prepared

When the gateway reserved budget and prepared to dispatch, the receipt records:

- **`dispatch_attempt_id`**: the durable dispatch-attempt row (`gateway/app/services/mcp_dispatch_attempts.py:McpDispatchAttemptService`)
- **`response_hash`**: SHA-256 of the canonical response the gateway recorded, when one was received (`gateway/app/services/receipts.py:574-601`)

`dispatch_attempt_id` proves the gateway **atomically reserved permit budget** and created a `prepared` dispatch attempt (`gateway/app/services/mcp_dispatch_attempts.py:658-680`). The attempt's `state` field records what happened next:

- **`prepared`**: permit budget reserved, not yet claimed for dispatch.
- **`dispatch_claimed`**: the gateway acquired the durable one-shot send authority and set `dispatched_at` (`gateway/app/services/mcp_dispatch_attempts.py:1586-1653`). This timestamp is written BEFORE the actual `session.call_tool()` send (`gateway/app/services/upstream_mcp.py:1042-1054`), so `dispatch_claimed` proves the claim was acquired, not that the send completed.
- **`succeeded`**: the upstream tool returned a non-error response (one with `isError` false or absent); `response_hash` records what the gateway received. Receipt `outcome` is `success`.
- **`returned_error`**: the gateway encountered an error before or during dispatch. Receipt `outcome` is `failed_refunded` (charge refunded; `gateway/app/routers/mcp.py:3588,3954`). The signed `reason_code` distinguishes: `upstream_returned_error` when the tool returned `isError: true` (`gateway/app/routers/mcp.py:3600`), `upstream_pre_dispatch_failed` for pre-dispatch failures (`gateway/app/routers/mcp.py:3651`), or `upstream_prepare_failed` for prepare failures.
- **`delivery_uncertain`**: a transport failure occurred after the send attempt; the gateway cannot confirm whether the tool received the request. Receipt `outcome` is `delivery_uncertain` (charge retained).
- **`response_rejected`**: the tool replied but the gateway rejected the response as invalid. Receipt `outcome` is `response_rejected` (charge retained).

**Outcomes that prove the tool received the request**: `success` (the tool returned a non-error response) and `response_rejected` (the tool replied but the gateway rejected the response). **`failed_refunded` proves reception only when its signed `reason_code` is `upstream_returned_error`** (`gateway/app/routers/mcp.py:3600`, `gateway/app/services/receipts.py:751-752`). The other `failed_refunded` reason codes (`upstream_pre_dispatch_failed`, `upstream_prepare_failed`) do not prove the tool received the request (`gateway/app/services/mcp_dispatch_reconciliation.py:1243-1256`). For `denied`, `insufficient_funds`, and `delivery_uncertain` outcomes, no field proves the tool received the request. The `dispatch_claimed` state and `dispatched_at` timestamp prove only that the gateway acquired the right to send and recorded that acquisition before attempting the send.

### Signature and provenance

The receipt is signed with the gateway's Ed25519 key:

- **`signature`**: Ed25519 signature over the canonical JSON payload (`gateway/app/services/receipts.py:783-801`)
- **`signature_key_id`**: the signing key identifier
- **`enforcement_mode`**: the trust posture (`"enforce"` or `"permissive"`) the gateway was running under
- **`gateway_build`**: the exact gateway commit SHA that wrote this receipt
- **`created_at`**: when the receipt was signed

The signature is verifiable offline with the gateway's public key (`sdk/python/src/b2a_sdk/receipt_verifier.py` and `site/proof/verify_receipt.py`).

### Per-permit chain (Preview)

Opted-in receipts (`sequence` present) form an ordered, hash-chained log under one permit:

- **`sequence`**: zero-based position in the permit's receipt chain
- **`prev_receipt_digest`**: SHA-256 of the predecessor receipt's signing input (omitted for `sequence=0`)
- **`counterparty`**: the other party to the governed action (when bound)

This chain is **Preview**: not every permit uses it. When present, it proves the **order and predecessor integrity** of receipts under one permit. Detecting truncation requires a trusted chain head or expected count; the chain alone does not prove completeness (`gateway/app/services/receipts.py:437-506`, `gateway/migrations/versions/057_receipt_permit_seq_unique.py`).

## What a receipt does NOT attest to

The receipt boundary ends at the gateway's dispatch. It does **not** prove:

### 1. The remote tool's side effect actually happened

For calls that reached dispatch, the gateway signs a receipt after attempting to send the request to the upstream tool. For denied or insufficient-funds calls, the receipt is issued before dispatch. The gateway does not wait for the tool's internal side effect to complete, nor does it verify that the tool actually performed the work.

- A `success` receipt with `outcome="success"` proves the upstream tool **returned a non-error response**, not that the tool's side effect succeeded.
- The tool's response is the tool's own claim. The gateway does not verify it.
- The gateway records `response_hash`, which proves what the tool **said**, not what the tool **did**.

**Code references:**
- `gateway/app/routers/mcp.py:3500-3750`: governed invoke path: dispatch, then finalize receipt
- `gateway/app/services/upstream_mcp.py:UpstreamMcpAdapter.call_tool()`: sends the request and awaits the tool's response
- `gateway/app/services/upstream_mcp.py:1156-1157`: responses with `isError: true` raise `UpstreamMcpReturnedError`
- `gateway/app/routers/mcp.py:3588`: `UpstreamMcpReturnedError` sets dispatch attempt state to `returned_error`
- `gateway/app/routers/mcp.py:3954`: receipt `outcome` becomes `failed_refunded` (charge refunded)
- Responses without `isError` or with `isError: false` become dispatch attempt state `succeeded` and receipt `outcome="success"`

### 2. The side effect happened exactly once

The receipt proves the gateway dispatched **at most once** under this idempotency key. It does not prove the upstream tool executed exactly once:

- **Idempotency key forwarded**: The gateway forwards `io.agentmiddleware/idempotency_key` to the upstream tool (`gateway/app/services/upstream_mcp.py:1061-1062`). If the tool honors it, the side effect is deduplicated. If the tool does not, a network retry or failover may execute the side effect multiple times.
- **No exactly-once guarantee**: The gateway's at-most-one-dispatch contract does not imply exactly-once execution of the tool's side effect. The tool owns that.

**Code references:**
- `gateway/app/services/idempotency.py:IdempotencyService.begin_with_record()`: claims one idempotency record
- `gateway/app/services/mcp_dispatch_attempts.py:658-680`: `McpDispatchAttemptService.authorize_reserve_and_prepare()` atomically reserves permit budget and creates the `prepared` attempt
- `gateway/docs/failure-semantics.md`: "The gateway dispatches at most once per accepted request"

### 3. The side effect was delivered

A `delivery_uncertain` receipt means the gateway **could not confirm delivery** of the request. The send may or may not have started:

- The upstream tool may have received and executed the request, then failed to send a response.
- The upstream tool may never have received the request at all (network partition, upstream crash, or gateway crash after claiming send authority but before `session.call_tool()`).
- The gateway cannot distinguish these cases, so it retains the charge and marks the outcome `delivery_uncertain` (`gateway/app/services/mcp_dispatch_reconciliation.py:115-130`, `gateway/app/routers/mcp.py:3677-3699`).

**Code references:**
- `gateway/app/services/upstream_mcp.py:UpstreamMcpDeliveryUncertainError`: raised when the response is lost after send
- `gateway/app/services/mcp_dispatch_reconciliation.py:506-508`: stale dispatch becomes `delivery_uncertain`
- A same-key retry of a `delivery_uncertain` receipt **replays the existing receipt**, never redispatches

### 4. The tool's response, beyond what the gateway recorded

The gateway records a SHA-256 digest of the **canonical** response (`response_hash`), not the full response:

- The response may exceed the gateway's storage limits (`gateway/app/services/mcp_dispatch_attempts.py:DispatchResultTooLargeError`).
- The response may contain data the gateway rejects (`gateway/app/services/upstream_mcp.py:UpstreamMcpResponseRejectedError`).
- The gateway's `response_hash` is a digest of the **canonical form** (JCS-normalized JSON). Responses differing only in whitespace share the same hash. It does not prove the exact bytes the gateway saw.

**Code references:**
- `gateway/app/services/receipts.py:574-590`: `response_hash_override` for linked dispatch results
- `gateway/app/services/mcp_dispatch_attempts.py:342-361`: result storage bounds (`_bounded_result`)
- `outcome="response_rejected"` when the gateway cannot store the response (`gateway/app/routers/mcp.py:3720-3740`)

## Special cases and boundaries

### Same-key replay (idempotent retry)

When a client retries with the **same idempotency key**, the gateway replays the existing result without redispatching:

- The original receipt is returned unchanged.
- No new dispatch, no new charge, no new receipt.
- The replay proves the gateway **recognized** the key and **did not redispatch**.

**Code references:**
- `gateway/app/services/idempotency.py:311-326`: `_replay_from_record()` returns the existing response
- `gateway/app/services/idempotency.py:313`: raises `IdempotencyConflictError` if `request_hash` differs
- Same-key replay is free (no charge) but not instantaneous: if the original request is still in progress, replay raises `IdempotencyInProgressError` and the client must poll (`gateway/app/services/idempotency.py:325`)

### New-key retry (fresh request)

When a client retries with a **different idempotency key**, the gateway treats it as a new request:

- A new permit check runs.
- If permitted, a new dispatch and charge occur.
- A new receipt is signed.

**This is not a replay.** The new request is governed independently of the original.

**Code reference:**
- `gateway/app/services/idempotency.py:IdempotencyService.begin_with_record()`: creates a new record for a new key

### `max_calls_per_tool` cap (per-tool dispatch bound)

When a permit has `max_calls_per_tool` set, the gateway enforces **at most N dispatches per tool name**, where N is the configured cap. For example, `{"stripe_refunds": 1}` allows one dispatch:

- Calls up to the configured cap are permitted and dispatched.
- With a cap of 1, a second call to `stripe_refunds` (even with a different idempotency key) is **denied** with `permit_max_calls_exceeded` before dispatch.
- The cap bounds **gateway dispatches**, not remote executions. If the upstream tool retries internally or the network duplicates the request, the tool may execute more than once.

**Code references:**
- `gateway/app/services/mcp_dispatch_attempts.py:658-680`: `McpDispatchAttemptService.authorize_reserve_and_prepare()` atomically reserves permit budget and creates the `prepared` attempt
- `gateway/app/services/permits.py:777`: denial reason `permit_max_calls_exceeded`
- `gateway/tests/test_permit_v2_constraints.py:163`: per-tool cap enforcement test

**Unverified:** Whether a `max_calls_per_tool=1` permit guarantees at-most-one **remote execution** depends on whether the upstream tool honors the forwarded idempotency key. The receipt does not verify this.

### Delivery-uncertain outcomes

A `delivery_uncertain` receipt means:

- The gateway acquired send authority for the request (`dispatch_attempt_id` present, state `delivery_uncertain`). The send may not have started, for example after a crash between the claim and `session.call_tool()`.
- The gateway **could not confirm** whether the upstream tool received and processed the request.
- The charge is **retained** (not refunded), because the tool may have executed.
- A same-key retry **replays the same `delivery_uncertain` receipt**, never redispatches.

**Code references:**
- `gateway/app/services/mcp_dispatch_reconciliation.py:506-508`: stale claim becomes `delivery_uncertain`
- `gateway/app/routers/mcp.py:3677-3699`: finalize as `delivery_uncertain` when response is lost
- `gateway/failure_lab/scenarios/t03_execute_then_lose_response.py`: failure-lab test demonstrating the outcome

### In-flight permit revocation

When a permit is **revoked** while a call is in flight:

- The dispatch that was already sent **continues**.
- The receipt is signed with the permit that was **valid at dispatch time**.
- The charge is retained (the call was authorized when it started).
- A retry under a **new** idempotency key with the revoked permit is **denied** before dispatch.
- A same-key replay of a completed call still returns the stored result. See [Same-key replay](#same-key-replay-idempotent-retry).

**Unverified:** The exact revocation-race behavior. Permits are checked during atomic reservation (`gateway/app/services/mcp_dispatch_attempts.py:658-680`), but whether an in-flight dispatch is interrupted or allowed to complete after revocation is not explicitly tested in the files read.

**Code references:**
- `gateway/app/services/mcp_dispatch_attempts.py:658-680`: `McpDispatchAttemptService.authorize_reserve_and_prepare()` checks permit during atomic reservation
- `gateway/tests/test_permit_postgres_concurrency.py`: concurrency tests, but explicit revocation-during-dispatch not found
- `gateway/failure_lab/scenarios/t08_permit_revocation_race.py`: exists but not read for this document

## Diagram: Receipt boundary

```
┌─────────────────────────────────────────────────────────────────┐
│                          RECEIPT ATTESTS TO                     │
│                                                                  │
│  ┌────────────┐   ┌─────────────┐   ┌─────────┐   ┌──────────┐│
│  │  Request   │──▶│   Permit    │──▶│  Debit  │──▶│ Dispatch ││
│  │  admitted  │   │   granted/  │   │ applied │   │ prepared ││
│  │            │   │   denied    │   │         │   │ (budget  ││
│  │            │   │             │   │         │   │ reserved)││
│  └────────────┘   └─────────────┘   └─────────┘   └──────────┘│
│       ▲                  ▲                ▲              ▲      │
│       │                  │                │              │      │
│   request_hash      permit_digest   ledger_entry_id  dispatch_ │
│   idempotency_      outcome/reason  credits_charged  attempt_id│
│   record_id         constraints                       + state  │
│                                                                  │
│  dispatch_attempt.state tells what happened:                    │
│    prepared → budget reserved, not yet claimed                  │
│    dispatch_claimed → claim acquired, dispatched_at written     │
│       (timestamp set BEFORE actual send, per upstream_mcp.py)   │
│    succeeded/returned_error/delivery_uncertain/response_rejected│
│      → terminal (attempt reached an outcome)                    │
│                                                                  │
│  NO FIELD PROVES THE UPSTREAM TOOL RECEIVED THE REQUEST        │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
                                │
                                │  Gateway boundary
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                    RECEIPT DOES NOT ATTEST TO                   │
│                                                                  │
│  ┌──────────────┐   ┌──────────────┐   ┌────────────────────┐ │
│  │ Upstream tool│   │ Side effect  │   │  Exactly-once      │ │
│  │   received   │   │   happened   │   │  execution         │ │
│  │   request    │   │              │   │  (tool's own       │ │
│  │              │   │              │   │   responsibility)  │ │
│  └──────────────┘   └──────────────┘   └────────────────────┘ │
│                                                                  │
│  delivery_uncertain = delivery unconfirmed (send may not have  │
│                       started)                                   │
│  response_rejected  = tool replied, gateway rejected response   │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

## Summary

The receipt boundary is:

1. **Request admitted**: proved by `request_hash`, `idempotency_record_id`
2. **Authorization decision**: proved by `permit_id`, `outcome`, `reason_code`
3. **Charge applied**: proved by `ledger_entry_id`, `credits_charged`
4. **Dispatch attempt created**: proved by `dispatch_attempt_id`. Check the attempt's `state` field: `prepared` means budget reserved but not yet claimed; `dispatch_claimed` means the send authority was acquired and `dispatched_at` timestamp written (but this happens before the actual send); terminal states mean the attempt reached an outcome.
5. **Gateway provenance**: proved by Ed25519 `signature`, `gateway_build`, `created_at`

The receipt does **not** prove:

1. The upstream tool received the request for `denied`, `insufficient_funds`, `delivery_uncertain`, or `failed_refunded` outcomes with `reason_code` other than `upstream_returned_error`. Only `success` and `response_rejected` always prove the tool received the request. `failed_refunded` proves reception only when its signed `reason_code` is `upstream_returned_error`. The other `failed_refunded` reason codes (`upstream_pre_dispatch_failed`, `upstream_prepare_failed`) do not prove it.
2. The tool's side effect happened
3. The side effect happened exactly once (tool must honor forwarded idempotency key)
4. The tool's full response (only `response_hash` is recorded)

The gateway's contract is **at-most-one dispatch per idempotency key**, not exactly-once remote execution.

---

**File references:**
- Receipt signing: `gateway/app/services/receipts.py:ReceiptService.create_receipt()`
- Idempotency: `gateway/app/services/idempotency.py:IdempotencyService`
- Dispatch attempts: `gateway/app/services/mcp_dispatch_attempts.py:McpDispatchAttemptService`
- Permits: `gateway/app/services/permits.py:PermitService`
- Upstream adapter: `gateway/app/services/upstream_mcp.py:UpstreamMcpAdapter`
- Reconciliation: `gateway/app/services/mcp_dispatch_reconciliation.py`
- Governed invoke: `gateway/app/routers/mcp.py` (lines 3500-3750)
- Receipt verification: `sdk/python/src/b2a_sdk/receipt_verifier.py`, `site/proof/verify_receipt.py`
- Failure semantics: `gateway/docs/failure-semantics.md`
- Failure lab: `gateway/failure_lab/scenarios/`
