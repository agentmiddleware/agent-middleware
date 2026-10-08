# Break Report: gateway auth and API key routes/services

## Summary

Probed 13 adversarial angles against local gateway test setups only: revoked
keys, expired keys, wrong signing keys, tampered tokens, missing credentials,
malformed Bearer headers with a valid API key alongside, cross wallet IDOR,
garbage and hostile key input, access/refresh type confusion, expired tokens,
JWT mint refusal, use budget races under parallel load, and expiry boundary
races.

Result: 2 real breaks found, both fixed and tested. 11 angles held up with
no change needed. 0 fixed but failing. 0 blocked.

## Finding 1 (medium): non-ASCII API key crashed auth with a 500

What broke: any request carrying a non-ASCII `X-API-Key` value raised an
unhandled `TypeError` from `hmac.compare_digest` instead of being refused.
A refused credential became a 500 error.

How to reproduce: call `get_auth_context(api_key="b2a_caf\xe9key")`, or send
that value as `X-API-Key` to `GET /v1/api-keys/{wallet_id}`. Before the fix
this raised `TypeError: comparing strings with non-ASCII characters is not
supported`. Real HTTP clients can deliver such bytes, since headers decode
as latin-1.

Root cause: gateway/app/core/auth.py:238 and :257 compared attacker
controlled text with `hmac.compare_digest` on `str`, which rejects non-ASCII
input by raising.

Fix: new `_constant_time_equal` helper compares the UTF-8 bytes, so odd
input is refused (401/403) instead of crashing. Both call sites use it.
Behavior for normal keys is unchanged.

Test results: `test_garbage_api_keys_never_500` in
gateway/tests/test_gateway_auth_breakit.py fails on the old code with
`TypeError` and passes on the new code. Neighbor suites
(test_api_keys, test_rotate_api_keys, test_jwt_auth, test_jwt_authority,
test_revocation_containment, test_api_key_constant_time,
test_static_dev_api_keys, test_route_auth_inventory) all pass: 152 passed.

## Finding 2 (low): key expiring mid validation spent budget past expiry

What broke: `validate_key` checked expiry in Python, then ran the guarded
budget increment without rechecking expiry. A use budget key that timed out
between the read and the write still authenticated once and spent one use
past its expiry.

How to reproduce: scripted clock test.
`test_key_expiring_mid_validation_consumes_nothing` sets a `max_uses` key to
expire at T, answers T minus 1 minute for the liveness read and T plus 1
minute for the write, then calls `validate_key`. Old code returns the key
and increments the count. New code returns None and leaves the count at 0.

Root cause: gateway/app/services/api_key_service.py, guarded `UPDATE` in
`validate_key` had no expiry predicate, unlike its sibling
`consume_derived_key_use` which already rechecks expiry at write time.

Fix: added the same expiry predicate to the guarded `UPDATE`, with a fresh
clock read at write time. No behavior change for live keys.

Test results: the new test fails on the old code (key accepted) and passes
on the new code. Full breakit file: 13 passed.

## Angles probed with no break found

Revoked keys are rejected at HTTP and at token mint. Expired keys are
rejected at HTTP and at token mint. JWTs signed by the wrong key, tampered
tokens, expired tokens, and access/refresh type swaps are all refused with
401. Missing credentials give 401. Malformed Bearer headers never fall back
to a concurrently supplied API key, and a raw API key in a Bearer header is
refused. Cross wallet access (list, create, rotate, revoke, emergency
revoke, logs) is denied with 403 and the victim wallet is untouched. JWTs
cannot mint API keys (403). A `max_uses=5` key under 20 parallel requests
admitted exactly 5 and refused 15, so the budget guard holds.

## Open questions

1. Token scopes are minted at `/v1/auth/token` from caller supplied input
with no allowlist, and nothing in the gateway enforces scopes afterward. If
scopes are meant to restrict authority, that enforcement is missing. Left
unchanged as it needs a product decision, not a patch.
2. The unlimited key path in `validate_key` (no `max_uses`) still uses a
read then write for `last_used_at` with no write time expiry recheck, so a
request landing exactly on the expiry boundary can slip through once. Same
residual window exists in principle for the fixed path between the fresh
clock read and the commit. Both windows are now a single statement wide.
3. The legacy `verify_api_key` dependency ignores the Authorization header,
so valid JWT callers get 401 on those routes. This looks intentional
(JWT scope handling lives in `get_auth_context`), so it was left alone.
Please confirm that is the intent.
