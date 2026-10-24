# Break report: gateway/app/trust and sandbox services

## Summary

Probed sandbox escape flags, mid-request trust downgrades, and behavioral
sandbox false allows using local code and test setups only. Found 5 real
breaks, fixed all 5, all covered by new regression tests in
`gateway/tests/test_break_trust_sandbox.py` (15 tests). 10 of the 15 fail on
the pre-fix code (the other 5 are positive controls that pass both ways).
Fixed and tested: 5. Fixed but failing: 0. Blocked: 0.

Severity order below: high first.

## Finding 1 (high): behavioral sandbox router leaked env owner wallet id

What broke: `GET /v1/sandbox/behavioral/environments/{id}`, `DELETE` on the
same path, and `POST /v1/sandbox/behavioral/execute` answered a foreign
tenant's environment with 403 including the owner's wallet id in the
response body. Any tenant with a valid key could probe env ids and learn who
owns them. The sibling puzzle-sandbox router already maps this case to 404.

How to reproduce: wallet A creates an `mcp_sandbox` environment; wallet B
requests its state. Pre-fix: 403 with `wallet-victim...` in the body.
Post-fix: 404 identical to a missing id.

Root cause: `gateway/app/routers/sandbox_behavioral.py`, the three handlers
called `auth.require_wallet_access(owner)` directly, which raises 403
echoing the owner id.

Fix: added `_load_owned_env_id` (`sandbox_behavioral.py:44`), which maps a
failed wallet check to 404 without echoing the owner, mirroring
`_load_owned_env` in the puzzle router. All three handlers use it now.

Test results: `test_foreign_environment_is_indistinguishable_from_missing`
fails pre-fix (403, owner id in body), passes post-fix. Owner read, execute,
and delete still return 200/204.

## Finding 2 (high): pattern puzzle accepted any transform word as correct

What broke: guessing `submit_transform` with the word "rotate" solved a
puzzle whose hidden rule was "mirror". Every easy and medium puzzle was
solvable with any of five words, so generalization scores were meaningless.

How to reproduce: create an easy pattern env with seed 1 (rule is mirror),
submit `{"type": "submit_transform", "value": "rotate"}`. Pre-fix: reward
10.0, done. Post-fix: reward -1.0, not done.

Root cause: `gateway/app/services/sandbox.py`, pattern branch of
`_evaluate_action` accepted a fixed keyword list without comparing it to the
environment's own hidden rules.

Fix: added `_expected_transform_keywords` (`sandbox.py:330`), derived from
the env's hidden rules, and the guess must name every transform the rule
hides (hard rules need rotate, invert, and shift). Correct guesses still
solve, including the "color swap" spelling for the color_swap rule.

Test results: wrong-guess and partial-hard-guess tests fail pre-fix, pass
post-fix; correct-guess test passes both ways.

## Finding 3 (medium): api_mock puzzle solvable with repeated bogus calls

What broke: calling `call_endpoint` with a nonexistent path 3 times solved
an easy api_mock environment. Repeating one valid path also solved it.

How to reproduce: create an easy api_mock env, submit
`{"type": "call_endpoint", "value": "/api/v1/does-not-exist"}` three times.
Pre-fix: solved with reward 10.0. Post-fix: never solved, unknown paths get
reward 0 with guidance to discover endpoints first.

Root cause: `gateway/app/services/sandbox.py`, api_mock branch counted every
`call_endpoint` action toward discovery without checking the path or
deduplicating.

Fix: validate the path against the environment's own `mock_api` endpoint
list and count each known path once (`sandbox.py`, api_mock branch). The
endpoint list is already visible in environment state, so this asks nothing
unfair of the agent. Schema-change cadence behavior is unchanged.

Test results: bogus-repeat and valid-repeat tests fail pre-fix, pass
post-fix; calling every real endpoint still solves on both versions.

## Finding 4 (medium): outbound-URL guard trusted platform DNS for ambiguous IPs

What broke: hosts like `http://0177.0.0.1/` (dotted octal for 127.0.0.1)
and `http://0x7f.0.0.1/` failed strict IP parsing and were sent to DNS. On
this Mac they resolve to a global address and were ALLOWED, while glibc and
some browser resolvers read the same strings as loopback. The guard's own
docstring says a headless browser may resolve names differently, so
depending on one platform's getaddrinfo for these forms is a gap in the SSRF
boundary used by the behavioral sandbox HTTP proxy mode.

How to reproduce: `await check_outbound_url("http://0177.0.0.1/")` returned
None (allowed) on this machine. Post-fix it returns
`private_address_blocked` without consulting DNS.

Root cause: `gateway/app/core/url_guard.py`, no fail-closed rule for
numeric-looking hosts that strict parsing rejects.

Fix: added `_is_ambiguous_numeric_literal` (`url_guard.py:61`): all-digit
dotted forms and 0x hex forms that `ipaddress` rejects are refused as
`private_address_blocked` before DNS. Ordinary hostnames (for example a
domain with a numeric label such as 007.com) still take the DNS path, and
all previously pinned verdicts are unchanged.

Test results: new parametrized tests fail pre-fix (DNS gets consulted),
pass post-fix; a control test proves normal hostnames still use DNS.

## Finding 5 (low): navigation accepted JSON true as node 1

What broke: `{"type": "move", "value": true}` moved the agent to node 1
because Python treats True as equal to 1. A mistyped client could wander the
graph without meaning to.

How to reproduce: easy navigation env with seed 0 (node 0 neighbors node
1), submit a boolean move. Pre-fix: moved to node 1 with reward 0.5.
Post-fix: rejected with -0.5 and no state change.

Root cause: `gateway/app/services/sandbox.py`, navigation branch compared
the raw value against integer neighbor ids with no type check.

Fix: non-integer and boolean move targets are rejected before the neighbor
check. Integer moves behave exactly as before.

Test results: boolean-move test fails pre-fix, passes post-fix; integer-move
test passes both ways.

## Investigated but not broken

Sandbox escape flags: `BEHAVIORAL_SANDBOX_PYTHON_BACKEND` values
`unsafe_host` and `host` are both refused at boot in production-like
environments, near-miss spellings (for example `unsafe-host`) fall through
to the disabled path rather than to host execution, Docker failures report
`docker_unavailable` without host fallback, and `ALLOW_PRIVATE_NETWORK_TARGETS`
still blocks non-http schemes. Mid-request downgrade: backend and flag reads
come from the cached settings object per execution with no per-request
override path, and environment ownership has no update path between the
router check and engine use. The 403 versus 404 inconsistency between the
two sandbox routers (Finding 1) was the closest real instance of a trust
boundary downgrade.

## Open questions

1. Should the puzzle-sandbox `_safe_state` also stop exposing the full
   `mock_api` endpoint list, now that scoring depends on it? It is the only
   discovery mechanism today, so I left it.
2. `gateway/.venv` was already untracked in this worktree before I started;
   the runner should avoid committing it.
3. No Postgres-backed concurrency tests were run (SQLite only in this
   environment); the durable-state last-write-wins behavior under two
   writers was not probed.
# Break report: gateway permits

Summary: 2 breaks found, 2 fixed and tested, 0 fixed but still failing, 0 blocked. Expiry on a live reservation and denied-receipt tampering were probed and held.

The two breaks are in `gateway/app/services/permits.py`. Local SQLite tests only. No migration. Postgres was not part of this run.

## 1. High: a bad credit amount can mint budget, wipe it, or crash the check

What broke. `authorize_and_reserve`, `reserve_budget`, and `POST /v1/permits/verify` accepted a negative `estimated_credits`. On a live permit, reserving `-1` lowered `spent_credits` and the call was reported as allowed, which hands budget back. `NaN` did not get that far: the budget comparison raised `decimal.InvalidOperation` (pre-fix line 867). `release_budget` with a negative amount raised `spent_credits`. `release_budget` with a non-storable amount (`1000000000000`) took the clamp and set `spent_credits` to 0, which clears the reservation. Zero is still a legal amount.

How to reproduce. Create a live permit, reserve 4, then call `authorize_and_reserve` with `Decimal("-1")`. Before the fix the verdict was allowed and spent fell. The same amount on `reserve_budget` and on `POST /v1/permits/verify` (`estimated_credits` of `"-1"`) was accepted. `Decimal("NaN")` crashed the comparison. `release_budget(Decimal("-1"))` grew spent. `release_budget(Decimal("1000000000000"))` after a reserve of 2 left spent at 0.

Root cause. `gateway/app/services/permits.py`, the budget comparison inside `_validate_model_for_action`, and the unguarded `spent_credits + amount` / `spent_credits - amount` writes in `authorize_and_reserve`, `reserve_budget`, and `release_budget`. Creation already rejected a non-storable cap. Reservation and release did not. Tool price registration already refused a negative price for this reason. The permit service was still the boundary.

Fix. `_storable_reservation_amount` (line 115) rejects anything that is not a `Decimal` or that fails `credit_amount_fits_storage` (negative, NaN, Infinity, above the storage limit, bad scale). The read path returns `permit_credits_invalid` before lifecycle checks (line 850), so verify and authorize share it. `reserve_budget` (line 1112) and `release_budget` (line 1268) raise `PermitError("permit_credits_invalid")` before any write. Zero still passes. A storable release larger than the current reservation still clamps to 0.

Tests. `tests/test_permit_break_it.py`: `test_negative_estimated_credits_cannot_mint_budget`, `test_non_finite_estimated_credits_cannot_reserve`, `test_verify_rejects_negative_estimated_credits`, `test_release_budget_rejects_amounts_that_grow_or_wipe_spend`. All four failed on the pre-fix run and pass now.

## 2. High: a released receipt stops counting against the aggregate cap

What broke. `aggregate_value_cap` is a bound on cumulative value. After a positive receipt, `release_budget` puts `spent_credits` back to 0 and leaves the receipt in place. The next reservation checked `max(spent, receipt total) + amount` but stored only `spent + amount`. Once spent numerically caught the old receipt, the floor dropped to 0 and further reservations were bounded by spent alone. Worked case: cap 5, receipt 2, spent released to 0. The first new reservation of 2 was stored as spent 2, not 4. The second reservation of 2 was allowed. Cumulative value was the historical 2 plus 4 of new authority, which is 6, over the cap of 5. Six parallel reservations of 2 admitted 2. The same hole is on `reserve_budget` (the AWI path). A separate case, max credits 3 and cap 100 with the same orphan of 2, allowed a new reservation of 2 because the write never tried to store the orphan.

How to reproduce. Create a permit with `aggregate_value_cap` 5 and `max_credits` 100. `reserve_budget` 2, write a receipt with `credits_charged` 2 and outcome `failed_unrefunded`, then `release_budget` 2. Spent is 0. Call `authorize_and_reserve` twice for 2. Before the fix both calls were allowed and the first left spent at 2. The concurrent form is six parallel reservations of 2: before the fix, 2 were allowed.

Root cause. `gateway/app/services/permits.py`, the reservation UPDATE in `authorize_and_reserve` and `reserve_budget`. The cap predicate added a Python floor constant. The SET clause was only `spent_credits + amount`, so a successful reservation did not keep the orphan in the counter. The service comment described that later check as the same arithmetic as a receipt that was never released. It was not: a never-released charge of 2 plus one new reservation of 2 leaves spent at 4 and denies the next 2.

Fix. `_reservation_charge_expr` (line 1056) puts the receipt sum inside the UPDATE. When a cap is set, the statement adds `max(receipt sum - spent, 0)` and uses that same total in the cap predicate and the `max_credits` predicate. A successful reservation of 2 on the example above stores 4. The next reservation of 2 is denied, reason `permit_aggregate_value_cap_exceeded`, and spent stays 4. The read path counts the same floor against both the cap and `max_credits`, so verify does not allow a call the write must refuse. After a 0-row update the floor is computed again from the refreshed row, so a denial is cap or budget rather than a retry loop. In-flight release with no receipt still frees the cap: the floor is 0 and the write matches the old `spent + amount` form. Alert thresholds on `reserve_budget`, when a cap is set, use the spent value observed before the write, because the new counter can rise by more than `amount`.

Tests. `tests/test_permit_break_it.py`: `test_released_receipt_history_is_absorbed_on_the_next_reservation`, `test_reserve_budget_absorbs_released_receipt_history`, `test_concurrent_reservations_cannot_refill_a_released_receipt`, `test_absorption_cannot_push_spent_past_max_credits`. All four failed on the pre-fix run (spent stayed 2, the second reserve was allowed, the race admitted 2, the tight max credits case was allowed). They pass now. `tests/test_permit_v2_constraints.py` had locked the loose result (a second reserve allowed, and the race allowed 1 or 2). Those two tests now expect one admission and spent 4. The no-receipt cap tests in that file were not loosened and still pass, including release-then-reserve and the parallel cap with no receipt history.

Production path checked, not changed. The MCP refund path that calls `release_budget` then signs a receipt uses `credits_charged` 0 (`_finalize_governed_denial`). The upstream refund release followed by that same denial receipt is also 0. Those paths do not leave a positive orphan. The service itself still accepts the state, and the cap tests build it with `release_budget` after a positive receipt. I did not find a current caller that does that in production code. The break is still in the permit service the cap claims to enforce.

## Probed, not a break: expired permit

`authorize_and_reserve` and `reserve_budget` already put `expires_at > now` on the reservation UPDATE and classify `permit_expired` before budget. `test_expired_permit_cannot_be_reserved` backdates `expires_at` by 5 minutes, leaves status `active`, and reserves 1. Both paths return `permit_expired` and spent stays 0. That part passed before the fix. The same test then sends `Decimal("-1")` on the expired permit. Before the fix the reason was `permit_expired` and spent did not move (the deadline check returned before the write). After the fix the reason is `permit_credits_invalid`, and spent still does not move. Replay access still ignores expiry, revocation, and remaining budget on purpose. That was not changed.

## Probed, not a break: denied receipt signing

`test_denied_receipt_signature_rejects_outcome_and_charge_tamper` signs a denial with `credits_charged` 0, `credits_authorized` 1.10, and reason `permit_expired`. Verify returns valid. Flipping the stored outcome to `success` makes verify return `receipt_signature_invalid`. Restoring the outcome and setting `credits_charged` to 5 does the same. This passed before any code change. Existing coverage in `tests/test_receipts.py` already checks reason-code tamper. `_finalize_governed_denial` in `gateway/app/routers/mcp.py` still omits `constraints_evaluated` on denial receipts. That is an evidence gap, not a signature that accepts a rewritten outcome. MCP was not edited.

## Tests run

Pre-fix, before any edit to `permits.py`:

```
cd gateway && ~/metacode/fleet/heavy .venv/bin/pytest -q tests/test_permit_break_it.py --tb=line
```

9 failed, 2 passed in 1.44s. The two that passed were the zero-credit reservation and the denied-receipt tamper. The nine failures are the breaks above, plus the expired-permit test's negative amount (it wanted `permit_credits_invalid` and got `permit_expired`).

After the fix, final tree:

```
cd gateway && ~/metacode/fleet/heavy .venv/bin/pytest -q tests/test_permit_break_it.py tests/test_permit_v2_constraints.py tests/test_permit_budget_integrity.py --tb=line
```

40 passed in 3.04s.

```
cd gateway && ~/metacode/fleet/heavy bash -c '.venv/bin/ruff check app/services/permits.py tests/test_permit_break_it.py tests/test_permit_v2_constraints.py && .venv/bin/pytest -q tests/test_permits.py tests/test_denial_details_and_self_service.py tests/test_receipts.py tests/test_permit_budget_drift_visibility.py tests/test_permit_verification_sessions.py tests/test_quickstart_path.py --tb=line -x'
```

Ruff: all checks passed. Pytest: 74 passed in 10.42s.

The full gateway suite was not run.

## Open questions

- Should a denial receipt from `_finalize_governed_denial` include `constraints_evaluated` the way success receipts do? Verify still holds without it. Left unchanged.
- Is any caller outside MCP, x402, and AWI releasing a reservation after a receipt with a positive `credits_charged`? Those three paths that were read write 0 on the refund receipt, or they do not release once a receipt exists. The service fix covers the state anyway.
- Postgres was not used. The suite here is SQLite. The new cap write is a correlated sum inside the UPDATE. On SQLite that sum is a float. Amounts in these tests were integers and matched. A sum of many fractional credits could disagree with the Decimal read and surface as `permit_write_contended` instead of a clean cap denial. Not reproduced here.
- Zero-credit authorize still succeeds and does not change spent. A free call can still consume a `max_calls_per_tool` slot.
- A storable `release_budget` larger than current spent still clamps to 0. That is the existing drift behavior for a real refund amount.

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
