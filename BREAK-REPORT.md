# BREAK REPORT: gateway receipts service

Date: Oct 8, 2026. Scope: tamper, wrong signature, denied receipt shape. Local tests only (SQLite test DB, no network, no production).

## Summary

Probed receipt creation, signature verification, tamper handling, idempotent replay, concurrency, auth checks, and denied receipt shapes with adversarial inputs.

Result: 3 breaks found, 3 fixed and tested, 0 fixed but failing, 0 blocked.

Tamper resistance, wrong signature handling, key revocation handling, and concurrent duplicate writes all held up. Each is listed below with the probe that confirmed it.

## Finding 1 (medium-high): any outcome string was accepted, signed, and verified

What broke: `ReceiptService.create_receipt` accepted any outcome string (`bogus`, `SUCCESS`, empty string, 33 chars, `denied;DROP`) and signed it into a receipt that verified True. Nothing defines what those outcomes mean to budget release, refund, or evidence checks, so a signed receipt could make a claim no consumer understands.

How to reproduce: call `create_receipt` with `outcome="bogus"` and valid credit amounts, then call `verify_receipt`. Before the fix it returned `(True, None)`.

Root cause: `gateway/app/services/receipts.py`, `create_receipt` (previously around line 295): outcome was a free-form string with no allowlist check, unlike `reason_code` which is pattern checked.

Fix: added a `RECEIPT_OUTCOMES` allowlist (success, denied, insufficient_funds, failed_refunded, failed_unrefunded, delivery_uncertain, response_rejected; each attested by a governed caller) and reject anything else with `ReceiptError("receipt_outcome_invalid")`.

Test results: new `test_receipt_rejects_unknown_outcome` fails without the fix (receipts created), passes with it. Related suites pass: test_receipts, signing input snapshot, portability, wallet linkage, write contention (74 passed), plus evidence, denial details, jev guard, permit v2, refund, mcp trust, trust negative, constant loop signature, acta interop (163 passed). Ruff check and format clean.

## Finding 2 (medium): denied receipts could claim a nonzero charge and still verify

What broke: a receipt with `outcome="denied"` (or `insufficient_funds`) and `credits_charged=5` was accepted and verified True. Every governed denial path records zero charge, so a denied receipt that claims money moved contradicts the ledger and has no defined consumer. With a matching ledger id set, even the evidence checks could pass on this contradictory shape.

How to reproduce: call `create_receipt` with `outcome="denied"`, `credits_charged=Decimal("5")`, no ledger entry. Before the fix it created and verified True.

Root cause: `gateway/app/services/receipts.py`, `create_receipt`: no rule tied denial outcomes to zero charge. The charged-failure shapes (`failed_refunded`, `failed_unrefunded`) are the defined way to record money on a failed call.

Fix: reject nonzero `credits_charged` for `denied` and `insufficient_funds` with `ReceiptError("receipt_denied_charge_invalid")`. Charged failure shapes are untouched.

Test results: new `test_receipt_denied_with_charge_rejected` fails without the fix, passes with it. Same related suites pass as Finding 1.

## Finding 3 (low): idempotent replay with different constraints evidence was silently adopted

What broke: `_assert_idempotent_match` took a `constraints_evaluated` argument but never compared it. Reusing one idempotency record with different `constraints_evaluated` silently returned the first receipt, so the caller got back a receipt whose signed constraints differ from what it computed, with no error.

How to reproduce: create a receipt with an idempotency record and `constraints_evaluated={"a": 1}`, then create again on the same record with `{"a": 2}`. Before the fix the second call returned the first receipt silently.

Root cause: `gateway/app/services/receipts.py`, `_assert_idempotent_match` (previously around line 230): the parameter was accepted and ignored.

Fix: compare normalized stored constraints against the replayed value and raise `ReceiptError("receipt_idempotency_conflict")` on mismatch, matching how every other signed field is treated.

Test results: new `test_receipt_idempotency_conflict_on_constraints_divergence` fails without the fix (silent adopt), passes with it. Same related suites pass. The jev guard metadata merge happens before the replay check, and it is deterministic per audit event, so legitimate replays still match.

## Areas probed that held up (no break)

Tamper sweep: changed each signed field in the stored row (tool, request and response hashes, both credit amounts, outcome, reason code, ledger, audit, approval, key, permit, wallet, constraints, signature, key id). Every change made `verify_receipt` return invalid and `signing_input` return None. Cross-receipt signature transplant also failed closed.

Wrong signature shapes: garbage, short, wrong-bytes, empty, and unknown-key signatures all verify False with no exception. The verify endpoint returns 200 with valid False (no 500), and the portable endpoint refuses with 409. Disabling the signing key also fails closed across all three paths.

Concurrency: two concurrent `create_receipt` calls on one idempotency record returned the same receipt id with one stored row.

Denied shape otherwise: a clean denied receipt (zero charge, reason code, no ledger) verifies True and exports as portable evidence. Its detailed evidence bundle reports valid False only because it carries no audit event link, which is expected for receipts created outside the governed path since governed denials always carry an audit event id.

## Open questions

1. The verify endpoint returns different 403 errors for a missing receipt id (`admin_access_denied`) versus another wallet's receipt (`wallet_access_denied`), which lets an agent key holder test whether a receipt id exists. Receipt ids are unguessable random strings, so this is very low risk. Unifying the errors would change API behavior, so no change was made. Christopher should decide whether to unify.
2. No migration was added. All fixes are additive validation in existing code paths with new error reasons. Callers that passed only the seven known outcomes are unaffected.
