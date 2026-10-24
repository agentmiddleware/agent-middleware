# Break report: gateway idempotency on billing and permit callers

Summary: 5 breaks found, 5 fixed and tested, 0 fixed but failing, 0 blocked.

Local gateway tests only. Nothing was deployed, and no production database or URL was used.

## High: a second Idempotency-Key line was ignored, then charged

What broke: Charge, transfer, agent-wallet create, permit create, action-permit create, permit-request create, and x402 settle read Idempotency-Key as one string. A request with two different lines was stored under the first line only. A later call that sent only the second line was a new charge, transfer, permit, or settlement. The same routes also stored a blank key, a key longer than 128 characters, and a key with a control character. MCP and AWI already refuse those.

How to reproduce: POST `/v1/billing/charge` with two `Idempotency-Key` headers, `charge-line-a` and `charge-line-b`. Before the fix the call returned 200 and wrote a debit. POST again with only `charge-line-b` and a second debit was written. The same pattern minted a permit, approved a simulated permit request, and settled x402. A key of 129 characters, a key of three spaces, and a key containing a newline were also accepted and charged.

Root cause: scalar `Header()` parameters in `gateway/app/routers/billing.py` (agent wallet, charge, transfer), `gateway/app/routers/permits.py` (`create_permit`, `issue_action_permit`), `gateway/app/routers/permit_requests.py` (`create_permit_request`), and `gateway/app/routers/x402.py` (`settle_payment_required`). Starlette returns the first line for a scalar header. Those handlers never called `resolve_client_idempotency_key`.

Fix: `resolve_idempotency_key_lines` in `gateway/app/routers/http_idempotency.py` (line 102). The routes above declare the header as `list[str]` so every line is visible, then refuse a bad or conflicting key with HTTP 400 `invalid_idempotency_key` before any debit, mint, or settlement. Identical lines still collapse to one key. A missing required header stays HTTP 422. API key issuance still uses `begin_http_idempotency` and was not changed.

Test results: the new cases failed before the fix (charge, transfer, agent wallet, permit, permit request, and x402 each returned success and performed the side effect). They pass after the fix. See the command log at the bottom.

## High: overlapping permit requests with one key and different terms both paged a human

What broke: Two creates with the same idempotency key and different `max_credits`, sent while the first Sentinel call was still in flight, both paged a human. The second request returned 202 for its own terms. With the same terms, Sentinel was called twice. A sequential retry was already safe.

How to reproduce: In real (non-simulated) mode, hold the first `create_approval` open, then POST `/v1/permit-requests` again with the same key and `max_credits` 500. Before the fix the second call returned 202. Releasing the first call left two Sentinel creates. The same-terms overlap left `len(created) == 2`.

Root cause: `PermitRequestService.create` in `gateway/app/services/permit_requests.py` called `_create_remote` before the unique insert. `_persist_new` (the `IntegrityError` branch) returned the winning row without comparing `request_hash`. In simulated mode the loser then ran `_decide` on that winner.

Fix: The unique row is inserted before Sentinel is called (create, around the `_persist_new` call now at line 418). A different hash raises `PermitRequestConflictError` and does not page or decide the winner. A same-hash loser that finds a pending row with no Sentinel id, younger than 120 seconds, returns that row and does not page again. If Sentinel fails, the unpaged claim is deleted, so the outage retry test still sees an empty table. A claim older than 120 seconds is paged on the next retry. The Sentinel key is still derived from the stored terms.

Test results: `test_concurrent_different_terms_conflict_without_a_second_page` returned 202 before the fix and 409 after it, with one Sentinel create for the original 20 credits. `test_concurrent_same_terms_page_the_human_once` saw 2 creates before the fix and 1 after it. The existing outage, sequential conflict, and single-page tests pass.

## High: a transfer correlation id was left out of the replay hash

What broke: The same idempotency key, wallets, and amount, with a different `correlation_id`, replayed 200. Money did not move twice. The second caller was shown the first transfer, including a `transfer_id` equal to the first correlation id. `correlation_id` is written on both ledger rows and becomes `transfer_id` when it is set (`wallet_engine.transfer`).

How to reproduce: POST `/v1/billing/transfer` with key `xfer-corr-1` and `correlation_id=corr-a`, then the same URL with `correlation_id=corr-b`. Before the fix the second call was 200 and the body was the first transfer with string amounts.

Root cause: `transfer_wallets` in `gateway/app/routers/billing.py` hashed `from_wallet_id`, `to_wallet_id`, `amount`, and `description` only.

Fix: When the caller sends `correlation_id`, it is included in the hashed payload (`billing.py` line 1057). Omitting it keeps the previous hash, so older retries that never sent one still match. A different id is HTTP 409 `idempotency_key_reused`. The same id replays.

Test results: the conflict case was 200 before the fix and 409 after it. Balances show one move of 1000. The same correlation id replays a body equal to the first response.

## Medium: a transfer replay body did not match the body the caller already got

What broke: The first successful transfer returned numeric amounts. The replay returned those amounts as strings (`"1000.0"`, `"4000.00000000"`). A 404 for a missing destination was stored as `{"error","message"}` and replayed that way, while the live response was `{"detail": "Wallet not found: ..."}`. A 402 was stored as the inner object and replayed without the `detail` wrapper the live response used. A client that rejects the replay and sends a new key can move the credits again.

How to reproduce: Transfer 1000 with one idempotency key and compare the two JSON bodies. Before the fix they differed on `amount`, `from_balance_after`, and `to_balance_after`. Repeat for a missing destination (404) and for an amount larger than the balance (402).

Root cause: `transfer_wallets` passed the engine dict, which contains `Decimal` values, to `IdempotencyService.complete`. `complete` in `gateway/app/services/idempotency.py` uses `json.dumps(..., default=str)`. Error branches stored a different object than `HTTPException` renders.

Fix: The success body is passed through `jsonable_encoder` and returned as that JSON (`billing.py` line 1094), and that same object is what `complete` stores. The 404, 402, and transfer `ValueError` branches store the `{"detail": ...}` object the live response uses.

Test results: success, 404, and 402 replays compared unequal before the fix and equal after it. Balances do not move on the error replays.

## Medium: a keyed transfer from an unknown wallet returned 500

What broke: POST `/v1/billing/transfer` with an idempotency key and a source wallet that does not exist raised `sqlite3.IntegrityError: FOREIGN KEY constraint failed` and surfaced as a server error. The idempotency row's `wallet_id` references `wallets`. The unkeyed path answers 404.

How to reproduce: POST `/v1/billing/transfer?from_wallet_id=wal-missing-source&to_wallet_id=wal-missing-dest&amount=1` with header `Idempotency-Key: xfer-missing-source`.

Root cause: `transfer_wallets` called `begin_http_idempotency` before checking that the source wallet exists. `begin_with_record` treats an `IntegrityError` as a lost race and re-raises when no row exists.

Fix: If `money.get_wallet(from_wallet_id)` is missing, the handler returns 404 with `Wallet not found: ...` before opening a record (`billing.py` line 1041). A missing destination, when the source exists, is still the engine's 404 and is replayed.

Test results: the keyed missing-source call raised `IntegrityError` before the fix and returns 404 twice after it.

## Tests

Before any production edit, from `gateway/`:

```
~/metacode/fleet/heavy .venv/bin/python -m pytest -q tests/test_idempotency_billing_permit_breaks.py tests/test_permit_request_flow.py::test_concurrent_different_terms_conflict_without_a_second_page tests/test_permit_request_flow.py::test_concurrent_same_terms_page_the_human_once --tb=line
```

Result: `17 failed, 2 passed in 2.09s`. The two passes were identical repeated charge lines (already safe, because the first line equals the second) and a permit POST with no Idempotency-Key header (already 422). The 17 failures are the breaks above: success responses and side effects where the new tests require 400 or 409, replay bodies that differ, the foreign-key 500, and the two permit-request races.

After the fix, the decimal text sent to Sentinel was `20.00000000` because the row is saved before the page and the column keeps 8 decimal places. One existing test and the new race test failed on that string only (`2 failed, 70 passed`). The page now sends `format(amount.normalize(), "f")`, which is `20` for that ask.

Final run:

```
~/metacode/fleet/heavy .venv/bin/python -m pytest -q tests/test_permit_request_flow.py tests/test_idempotency_billing_permit_breaks.py --tb=line
```

Result: `62 passed in 2.81s`.

The run that still had the decimal-string failure also passed, before that one-line change: charge retry, charge payload conflict, transfer retry, both agent-wallet idempotency tests, the top-up OpenAPI check, x402 missing-key 422, x402 atomic settlement, x402 conflicting replay, and `test_sponsor_may_issue_permit_for_its_agent`.

Ruff, from `gateway/`:

```
~/metacode/fleet/heavy .venv/bin/ruff check app/routers/http_idempotency.py app/routers/billing.py app/routers/permits.py app/routers/permit_requests.py app/routers/x402.py app/services/permit_requests.py tests/test_idempotency_billing_permit_breaks.py tests/test_permit_request_flow.py tests/conftest.py
```

Result: `All checks passed!`

The full gateway suite was not run.

## Open questions

- Charge replay bodies were not changed. A charge `LedgerEntry` includes a timestamp, and a strict compare can fail on timestamp text even when the debit is the same. The new charge tests compare `entry_id` and the debit count.
- A crash after `money.charge` commits and before `complete` still leaves a billing idempotency row in progress. `reconcile_stuck_records` does not repair a generic billing row. A retry is 409, and a new key charges again. HTTP charge does not pass `operation_key` into the billing engine.
- `IdempotencyService.complete` still lets the last writer overwrite the stored body.
- A permit-request worker that dies after the insert and before the Sentinel id is saved leaves a pending row. A retry younger than 120 seconds returns that row and does not page. A poll in that window is 503, because `advance` still requires a Sentinel id. After 120 seconds the next create pages. Real Sentinel dedupes that page by the derived key. The test double does not.
- A transfer that was stored before this change, and that included a `correlation_id`, will 409 on retry because the old hash did not contain it. Money does not move on that 409. A client that then picks a new key would transfer again. Retries that never sent a correlation id still match.
- These headers are now `list[str]` in the route signature, so generated OpenAPI may describe Idempotency-Key as an array. One header value still works. The top-up route was not given this parameter.
- API key create and rotate were left on the scalar header path.

---

# Break Report: gateway/failure_lab (Oct 8, 2026)

## Summary

Probed the fail-closed surfaces of `gateway/failure_lab` with 8 adversarial
probes (bad observation counts, mismatched observations, malformed operation
identifiers, oversized bodies, type coercions). Found 5 breaks. Fixed 4 with
root-cause patches plus regression tests, all passing. Fixed but failing: 0.
Blocked / left open: 1 (observation binding without identifiers, needs a
contract decision, see finding 5).

All work is local only. No production config, secrets, migrations, or network
calls were involved. No database needed: every fix is in pure functions
covered by unit tests.

## Findings (by severity)

### 1. High: a boolean `True` observation established downstream execution

What broke: `verify()` with
`downstream_observation={"executions": True, ...}` reported
`DOWNSTREAM_EXECUTION_ESTABLISHED`. In Python `bool` is a subclass of `int`,
so the `isinstance(executions, int)` guard accepted it and `True > 0`
counted as one execution.

How to reproduce: sign any receipt, call
`verify(bundle, keys, downstream_observation={"executions": True, ...})`,
read the downstream claim.

Root cause: `gateway/failure_lab/verifier.py`, `_downstream_claim`
(execution-count validation, formerly around line 378).

Fix: reject booleans explicitly (`isinstance(executions, bool)` fails the
observation as unusable, status NOT_ESTABLISHED). Same for any non-int type.

Test results: new test `test_a_boolean_is_not_an_execution_count`
(parametrized True/False) in
`gateway/tests/test_failure_lab_verifier.py`. Confirmed it fails on pre-fix
code (old code returned ESTABLISHED for True) via a read-only
`git show HEAD:` comparison script. Passes on fixed code.

### 2. Medium: an impossible negative execution count was reported as consistent

What broke: `{"executions": -4}` with a non-success outcome returned
NOT_ESTABLISHED with reason text calling the observation "consistent with"
the outcome. Minus four executions cannot happen, so blessing it as
consistent is a false clean bill for the instrument.

How to reproduce: `verify()` with `executions=-4`, outcome
`failed_refunded` or `delivery_uncertain`, read the reason string.

Root cause: same function as finding 1, no lower-bound validation.

Fix: negative counts return NOT_ESTABLISHED with reason "the supplied
observation reported an impossible execution count". An impossible reading
is an instrument error, not evidence for or against execution.

Test results: new test
`test_a_negative_count_is_an_unusable_observation_not_consistency`.
Confirmed old code emitted "consistent" wording; passes on fixed code.

### 3. Medium: an observation naming a different receipt established execution

What broke: `verify()` never compared any identifier between the
observation and the signed payload, so an observation collected for receipt
B (with executions > 0) marked receipt A as executed.

How to reproduce: verify receipt A with an observation carrying
`receipt_id` of receipt B and `executions=1`. Old result: ESTABLISHED.

Root cause: same function; the documented observation shape carries
`operation_id` but the signed payload has no operation field to compare, so
nothing was ever cross-checked.

Fix: when the observation carries a non-empty string `receipt_id` and the
signed payload names a different one, the claim FAILS with a reason naming
both receipts. Matching or absent identifiers behave exactly as before, so
no existing caller (including scenario T10, whose observation carries no
receipt_id) changes status. The `verify()` docstring now documents the
binding rule and recommends callers always bind observations.

Test results: new tests `test_an_observation_of_another_receipt_fails_the_claim`
and `test_an_observation_bound_to_this_receipt_still_establishes`.
Confirmed old code returned ESTABLISHED for the mismatch; both pass fixed.

### 4. Medium: non-string operation ids were coerced into plausible identifiers

What broke: `classify_request()` applied `str()` to whatever the request
carried as `operation_id`, so a dict, number, or bool became a
plausible-looking string (for example `"{'nested': True}"`). That value
matches no fault plan, so an injected fault would silently never fire while
the crossing record reads as normally observed.

How to reproduce: feed a `tools/call` (or `/refunds`) body with
`"operation_id": {"nested": true}` to `classify_request`. Old result:
`(kind, "{'nested': True}")`.

Root cause: `gateway/failure_lab/faults.py`, `classify_request`, both the
MCP and REST branches.

Fix: accept the value only when it is already a string, else record `None`
(visibly unidentified). The real gateway only ever sends strings
(`StrictStr` in the refund tool), so no legitimate traffic changes.

Test results: new `gateway/tests/test_failure_lab_failclosed.py` with
parametrized bad-type cases for both paths plus a positive control.
Confirmed old code coerced; passes fixed.

### 5. Low (open, needs a decision): observations without identifiers are taken on trust

What did not break but is weak by design: an observation carrying no
`receipt_id` (the documented shape has only executions, operation_id,
source) is assumed to describe the receipt being verified. Finding 3 closes
the case where binding info is present but ignored; it cannot close the
case where no binding info exists, because the signed payload carries no
operation identifier to compare against (only receipt_id, request_hash, and
dispatch linkage). Closing it fully means adding `receipt_id` (or
`request_hash`) to the observation contract and threading it through every
scenario that builds one, starting with T10.

No code changed for this. Left as an open question for C.Lee below.

### 6. Low: oversized fault-layer bodies raised instead of refusing cleanly

What broke: a POST body over the 1 MiB buffer raised raw `ValueError` out
of `FaultInjectionMiddleware`, surfacing to the gateway as an
unclassifiable transport error instead of a clean refusal. The tool was
never touched (fail-closed in effect), but the failure mode was a crash,
not a refusal, and nothing recorded the refusal.

How to reproduce: POST more than `_MAX_BODY_BYTES` to `/mcp` through
`FaultInjectionMiddleware`. Old behavior: `ValueError` propagates.

Root cause: `gateway/failure_lab/faults.py`,
`FaultInjectionMiddleware.__call__` called `_read_body` unguarded.

Fix: catch `ValueError`, record a crossing (`fault="oversized_body_rejected"`,
`reached_tool=False`, `delivered=True`, kind `unclassified` so it never
pollutes dispatch counts), and answer 413 with a JSON error. This mirrors
the diagnostic server, which already refuses oversized submissions with 413
(see `test_an_oversized_submission_is_refused`).

Test results: new test
`test_oversized_body_is_refused_as_413_without_reaching_the_tool` in the new
file. Confirmed old code raised `ValueError`; passes fixed.

## What was probed and held

- Float (`1.0`) and string (`"1"`) execution counts: already rejected.
- `False` count on a success receipt: already FAILED the claim correctly.
- Unsigned envelope edits: still verify, and are reported as disagreements.
- Envelope `kid` relabelling: cannot steer key selection (signed bytes win).
- Unknown signed `kid`, disabled keys, key-length checks: all fail closed.
- Redaction and secret plumbing (`secret_values` carries admin key, bearer,
  control token plus environment secrets): complete, no change needed.
- Retired signing keys staying published: deliberate (historical receipts
  stay verifiable), not a bug.
- Scenario verdict logic (T02, T09, T10): guards against empty probe loops
  and ledger reconciliation already present, no change needed.

## Test summary

Commands run from `gateway/` through the slot queue:

- `python -m pytest tests/test_failure_lab_verifier.py tests/test_failure_lab_failclosed.py -q`: 27 passed
- `python -m pytest ...verifier ...failclosed ...evidence ...report ...claims ...harness ...portable_keys -q`: 90 passed
- `python -m pytest ...verifier ...failclosed ...scenarios -q`: 86 passed
- `ruff check failure_lab/ tests/test_failure_lab_failclosed.py tests/test_failure_lab_verifier.py`: all checks passed
- `ruff format` applied to `failure_lab/faults.py`; all touched files format-clean
- No-fix confirmation: new assertions executed against pre-fix modules
  extracted read-only via `git show HEAD:`; all 5 failed there and pass
  with the fix.

Full gateway suite was not run (time and shared-machine load); scenario
integration runs needing the app and databases were not run beyond the
`test_failure_lab_scenarios` file, which passed.

## Open questions

1. Should the downstream-observation contract require `receipt_id`
   (finding 5), with T10 and any other builders updated to supply it?
   Recommend yes, as a follow-up task.
2. No migration, no config, no API changes in this work. Nothing needs a
   decision to land it.
