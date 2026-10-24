# Break report: gateway HTTP idempotency

Summary: 6 breaks found, 6 fixed and tested, 0 fixed but failing, 0 blocked.

Local SQLite tests only. No production calls, no migrations, no deploy.

The new tests were run on the old code first: 8 failed, 5 passed in 2.56s. After the fix the same file is 13 passed. The five that already passed are the cases that should stay: a missing header charges twice, identical repeated header lines collapse to one charge, an old transfer row still replays, a charge replay matches the first JSON, and an agent-wallet replay matches the first JSON.

## High: a blank Idempotency-Key still charged or minted

What broke: Sending `Idempotency-Key` with an empty value was treated as "no key". Each retry of a charge debited again. The same empty header on API key create minted a live key (HTTP 201). A whitespace-only header is the same class of unusable key: the billing routes now refuse it before any debit or mint. The pre-fix run stopped on the empty value, so the whitespace case was not the assertion that failed. The passing test covers both.

How to reproduce: POST `/v1/billing/charge` twice with `Idempotency-Key: ` (empty) for the same wallet and units. Old result: HTTP 200 and a new debit each time. POST `/v1/api-keys` with the same empty header. Old result: HTTP 201 and a new key.

Root cause: `gateway/app/routers/http_idempotency.py:69` (old) returned a no-op guard when `not idempotency_key`. Charge had the same check at `gateway/app/routers/billing.py:647` (old). An empty string is falsy, so the side effect ran with no replay row.

Fix: Billing charge, transfer, agent-wallet create, and API key create/rotate now pass every `Idempotency-Key` line into `resolve_optional_http_idempotency_key`. A missing header is still opt-in. A present unusable key raises HTTP 400 `invalid_idempotency_key` before `begin()` and before the side effect. Charge uses that helper instead of its own copy.

Tests: `test_blank_idempotency_key_does_not_charge` and `test_blank_key_does_not_mint_api_key_or_agent_wallet` failed before the edit (200 charge, 201 key). Both pass after it. Debit count stays 0, the key list stays empty, and the sponsor balance stays 1000.

## High: two different Idempotency-Key lines charged under the first line

What broke: If one request carried two `Idempotency-Key` lines with different values, the gateway kept the first and ignored the second. A retry that put the other value first was a second charge, not a replay and not a conflict.

How to reproduce: POST `/v1/billing/charge` with two header lines, `key-a` then `key-b`. Old result: HTTP 200 and a debit. A second POST with `key-b` then `key-a` was another debit.

Root cause: `Header(str | None)` on charge (`gateway/app/routers/billing.py:603`, old), transfer (`:1021`), agent-wallet create, and both API key routes. Starlette returns only the first line.

Fix: Those parameters are `list[str] | None`. Disagreeing lines become HTTP 400 `idempotency_key_conflict` and do not debit. Identical repeated lines still collapse to one key and replay. This matches the rule already used by agent HTTP governance.

Tests: `test_disagreeing_idempotency_key_headers_do_not_charge` failed before the edit (HTTP 200 and a debit). It passes after it, with debit count 0. `test_repeated_identical_header_replays_one_charge` passed before and after: one debit, same JSON.

## High: a transfer retry with a different correlation id looked successful

What broke: The same `Idempotency-Key` with a different `correlation_id` returned HTTP 200 and the first transfer body, including the first correlation id as `transfer_id`. Credits did not move a second time. The response claimed the new correlation had been applied.

How to reproduce: POST `/v1/billing/transfer` with key `xfer-corr-1` and `correlation_id=job-1`. Repeat with `correlation_id=job-2`. Old result: HTTP 200, `transfer_id` `job-1`, balances already moved once (5000 to 4900, 0 to 100).

Root cause: `gateway/app/routers/billing.py:1040` (old) hashed from, to, amount, and description, and left `correlation_id` out. The wallet engine still stores `correlation_id` on both ledger rows and uses it as `transfer_id` when it is set.

Fix: New transfer rows include `correlation_id` in the hash, so a different correlation is HTTP 409 `idempotency_key_reused` and does not move money. The previous four-field hash is passed only as a compatible payload for rows already stored. A brand-new key is not saved under the old hash. Rows written before this change still replay when the four old fields match, even if a later retry adds a correlation id. That is deliberate so a deploy does not turn an old retry into a second transfer.

Tests: `test_transfer_reused_key_with_different_correlation_conflicts` failed before the edit (second call HTTP 200). It passes after it: HTTP 409, balances 4900 and 100. `test_transfer_recorded_before_correlation_is_part_of_the_hash_still_replays` passed before and after: a seeded old-hash row replays and balances stay 5000 and 0.

## Medium: a keyed transfer to an unknown source wallet returned 500

What broke: Transfer with an `Idempotency-Key` and a source wallet that does not exist raised a foreign-key error from `idempotency_records` and answered 500. The same call without a key answered 404.

How to reproduce: POST `/v1/billing/transfer` with `from_wallet_id=wlt-missing-source` and any `Idempotency-Key`. Old result: `sqlite3.IntegrityError: FOREIGN KEY constraint failed` inside `begin()`.

Root cause: `gateway/app/routers/billing.py:1036` (old) opened the idempotency row before checking that the source wallet exists. `begin()` treats every integrity error as a lost insert race, looks up the row, finds none, and re-raises.

Fix: The route loads the source wallet after access control and before `begin()`. A miss is HTTP 404 `Wallet not found: {id}`, the same text the unkeyed path already used. Destination misses still go through the transfer error path, because the source row exists and the 404 can be stored.

Tests: `test_transfer_unknown_source_with_key_is_404` failed before the edit with the foreign-key error. It passes after it: both calls are 404, the bodies match, and the detail names the missing id.

## Medium: a successful transfer retry did not match the first JSON

What broke: The first transfer response sent numeric amounts and balances. The stored replay turned those Decimals into strings (`"1000.0"`, `"4000.00000000"`). A client that compared the retry to the original saw a different body for the same transfer.

How to reproduce: POST `/v1/billing/transfer` with an `Idempotency-Key`, then POST the same request again. Old result: HTTP 200 both times, one money movement, but `amount` and the two balances were strings on the retry and numbers on the first response.

Root cause: `gateway/app/services/idempotency.py:799` (old) stored `json.dumps(response_json, default=str)`. `str(Decimal)` is a JSON string. FastAPI's own encoder turns an integral Decimal into a number and other Decimals into floats, which is what the first response used.

Fix: `complete()` now stores `json.dumps(jsonable_encoder(response_json), default=str)` when the body is non-empty. An empty dict is still stored as no body, same as before. This is the shared writer, so every replay path uses the same encoder the live response uses.

Tests: `test_transfer_replay_returns_the_original_json` failed before the edit on `amount` and both balances. It passes after it, and the balances are 4000 and 1000. Charge and agent-wallet full-body equality passed before and after.

## Medium: a failed transfer retry did not match the first error body

What broke: The first 402, 404, or 400 from transfer wrapped the error under `detail`. The stored replay returned the inner object at the top level, or a different object than the live body. A retry looked like a different error.

How to reproduce: With one `Idempotency-Key`, transfer more than the balance (402), transfer to a missing destination (404), or transfer a wallet to itself (400). Repeat each call. Old 402 replay was `{error, wallet_id, shortfall}` while the first body was `{"detail": {those fields}}`.

Root cause: `gateway/app/routers/billing.py:1088`, `:1108`, and `:1153` (old) stored a body that was not the `HTTPException.detail` the client received. The expired-wallet 403 path already stored `{"detail": ...}` and was left as it was.

Fix: Those three completes now store `{"detail": <the same value passed to HTTPException>}`.

Tests: `test_transfer_error_replays_keep_the_original_body` failed before the edit on the 402 body. It passes after it. Each replay equals the first body, the only top-level key is `detail`, and the sender balance stays 50.

## Tests run

Pre-fix, from `gateway/`:

`~/metacode/fleet/heavy .venv/bin/pytest -q tests/test_http_idempotency_breaks.py --tb=line`

Result: `8 failed, 5 passed in 2.56s`. Failures: `test_blank_idempotency_key_does_not_charge`, `test_overlong_and_control_keys_do_not_charge`, `test_disagreeing_idempotency_key_headers_do_not_charge`, `test_blank_key_does_not_mint_api_key_or_agent_wallet`, `test_transfer_reused_key_with_different_correlation_conflicts`, `test_transfer_replay_returns_the_original_json`, `test_transfer_error_replays_keep_the_original_body`, `test_transfer_unknown_source_with_key_is_404`.

After the fix, same command: `13 passed in 1.29s`.

Related suites, from `gateway/`:

`~/metacode/fleet/heavy .venv/bin/pytest -q tests/test_http_idempotency_breaks.py tests/test_billing.py tests/test_api_keys.py tests/test_transfers.py tests/test_wallet_status_enforcement.py tests/test_idempotency.py tests/test_idempotency_completion_contention.py tests/test_mcp_idempotency_key_validation.py tests/test_quickstart_invalid_idempotency_key.py --tb=line -x`

Result: `242 passed in 12.85s`.

Encoder spot check, from `gateway/`:

`~/metacode/fleet/heavy .venv/bin/pytest -q tests/test_awi_http_governance.py tests/test_x402.py::test_x402_settle_conflicting_replay_is_409 tests/test_mcp_upstream_governed.py::test_governed_upstream_success_replays_without_second_debit_or_dispatch --tb=line -x`

Result: `23 passed in 3.09s`.

Lint, from `gateway/`: `.venv/bin/ruff check` on the four source files and the new test. Result: `All checks passed!` (`gateway/ruff.toml` selects F only).

## Open questions

Permits (`gateway/app/routers/permits.py:103`) and x402 settle (`gateway/app/routers/x402.py:152`) still take a required string header. They do not run the shared unusable-key check. An empty string can be stored as a real key. Two disagreeing lines still follow the first line. An overlong key can fail inside the database insert on Postgres. Not changed here. No failing test was added for those routes.

Billing and API key routes do not reinterpret header bytes as UTF-8. MCP and agent HTTP governance do. Changing the billing identity now would make a retry miss an old row and charge again. Left as the latin-1 string the app already stored.

A missing header is still unprotected on purpose. `test_charge_without_header_is_a_separate_operation` locks that: two calls, two debits.

A transfer row written before `correlation_id` was part of the hash still replays if only the correlation id changes. New rows conflict.

Clients that already succeeded with a whitespace-only key, a control character, or a key longer than 128 characters (SQLite accepts the long key; Postgres rejected the insert before the charge) will get HTTP 400 on retry. They should not switch to a fresh key for that same operation, or the gateway will treat it as a new charge or mint.

`begin()` still treats a non-unique integrity error as a lost race. A source wallet deleted after the new pre-check and before the insert can still 500.

`complete()` now runs every stored body through FastAPI's JSON encoder. The suites above passed. The full MCP upstream file was not run. A body that the encoder cannot handle would now fail the response write after the side effect, where `default=str` used to store a string.

Reading every header line changes the OpenAPI type of `Idempotency-Key` on these routes from a string to an array of strings. One header on the wire behaves as before. Agent HTTP governance already published the array form. Direct top-up stays 410 and still ignores the header.

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
