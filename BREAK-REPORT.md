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
