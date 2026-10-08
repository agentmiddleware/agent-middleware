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
