# Break Report: gateway policy services

Target: deny bypass, empty policy, risk tier. Local tests only, no production calls.

## Summary

Probed the wallet policy evaluator (`evaluate_wallet_policy`), the tenant
decision helpers, the policy CRUD routes, the planner and billing callers,
the enterprise IGA bridge, and the JEV advisory path with adversarial
inputs (NaN and infinite money values, None costs, empty allowlists,
tier mismatches, multi-bundle combinations).

Breaks found: 1 (with two symptoms). Fixed and tested: 1. Fixed but
failing: 0. Blocked: 0.

The remaining probed areas held up: risk tier mismatch is recorded and
does not deny (documented in `gateway/docs/POLICY_ENFORCEMENT.md`), empty
allowlists deny everything, corrupt allowlist columns deny closed, policy
CRUD is bootstrap admin only, and every HTTP boundary that feeds money
values into the evaluator already refuses non-finite numbers. Details for
each verified non-break are under Finding 2 so a reviewer can see what was
covered.

## Finding 1 (medium): non-finite money inputs crashed or bypassed the monetary gates

What broke: `evaluate_wallet_policy` normalized incoming
`estimated_cost` and `daily_spend_used` into `Decimal` without checking
finiteness. A NaN value made the Decimal ordering comparisons raise
`decimal.InvalidOperation`, which escaped the evaluator as an
unclassified 500 with no denial recorded and no audit event carrying a
policy reason. A negative-infinity cost compared smaller than every cap,
so it silently passed both `max_cost_per_action` and `daily_spend_limit`
with reason "allowed".

How to reproduce: create a wallet with a policy bundle carrying
`max_cost_per_action` and `daily_spend_limit`, then call
`evaluate_wallet_policy` directly with `estimated_cost=float("nan")`
(raises) or `estimated_cost=float("-inf")` (returns allowed). Today's HTTP
callers validate finiteness before calling, so this was reachable through
direct service use, a misbehaving registry price, or any future caller,
not through current HTTP requests.

Root cause: `gateway/app/services/policies.py:352` (`est >
policy.max_cost_per_action`) and `:364` (`daily + est >
policy.daily_spend_limit`) assumed finite Decimals; `Decimal("NaN")`
ordering raises instead of answering False, and `-Infinity` answers
False to every greater-than check.

Fix: after normalizing the two money inputs, the evaluator now denies
with reason `policy_cost_invalid` when either is non-finite
(`gateway/app/services/policies.py:263-278`). `None` keeps its existing
meaning ("unknown, nothing to check") and stays allowed, and a wallet
with no bundles keeps the documented no-bundles-allow default. The new
reason code is recorded in `gateway/docs/POLICY_ENFORCEMENT.md` (Layer C
table and reason-code reference).

Test results: new file `gateway/tests/test_policy_nonfinite_cost.py`, 9
tests. With the guard temporarily disabled, 5 fail (3 NaN variants raise
`InvalidOperation`, negative infinity allows, positive infinity denies
with the old reason) and 4 pass (finite allow/deny, None semantics,
no-bundle default, empty-allowlist pin). With the fix, all 9 pass.
Related suites all pass: `test_policy_bundles.py`,
`test_policy_decisions.py`, `test_iga_policy.py` (115 passed combined
with the new file), `test_billing.py` (76 passed). `ruff check`,
`ruff format --check`, and `mypy` on the changed service file are clean.

## Finding 2 (no break): areas probed that held up

Risk tier: a policy `risk_tier` that differs from the requested tier is
recorded on the decision (`requested_risk_tier`) and does not deny. This
is the documented design, confirmed in code and docs, so no change was
made. Callers that pass a tier (planner) and callers that do not (MCP,
billing) behave as documented.

Empty policy: a bundle with every constraint unset allows, consistent
with the documented no-bundles-allow default; an explicit empty
allowlist (`allowed_tools: []`) denies every tool; NULL remains the only
unrestricted value. Pinned by a test in the new file.

Deny path integrity: multiple active bundles combine with AND semantics
(every bundle must pass), corrupt allowlist columns deny with
`policy_constraint_corrupt`, the IGA bridge reads corrupt columns as
deny-all, policy create/patch/list/get all require bootstrap admin, and
`tool_name=None` against a restrictive allowlist denies.

Input validation already present, left untouched: billing `units`
(`gt=0, allow_inf_nan=False`), optimizer state and `task_context`
(finite-float validation), policy limit fields (`allow_inf_nan=False`,
storage bounds), registered tool prices (non-finite raises
`tool_price_invalid` before policy runs).

## Open questions

1. Should `policy_cost_invalid` denials attribute a policy bundle id
instead of None? The denial names no bundle because the request input
itself was malformed, and all denial consumers handle a None policy id,
but a reviewer may prefer attributing the first bundle for audit
convenience.
2. `evaluate_governed_action` accepts an explicit `allowed=True` that
skips its auth check. Both production callers (planner, billing audit
recording) pass internal decisions, not attacker input, so this is an
audit-labeling helper rather than a gate, and it was left unchanged.
If it is ever used as a gate, the explicit override needs an auth
conjunction.
3. There is a natural check-to-dispatch gap: a policy deactivated after
the last pre-dispatch evaluation still permits the in-flight call. This
is standard time-of-check timing and out of scope for this fix, but a
re-check inside the reservation transaction would close it.
