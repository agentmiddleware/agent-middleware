# Break report: gateway/app/middleware

Summary: 4 breaks found, 4 fixed and tested, 0 fixed but failing, 0 blocked.

Local ASGI tests only. Nothing was deployed, and no production URL, database, or migration was touched.

## 1. High: a fake /health path skipped the rate limit

What broke: the limiter treated a path such as `/health?not-the-health-route` as the exempt `/health` route. The second request stayed 200. The router does not see that path as `/health`, so this was not the real health check. The same reparse made `/mcp/public?x` look like the public MCP route.

How to reproduce: call the rate limiter with an ASGI scope whose path is `/health?not-the-health-route` and `requests_per_minute=1`. On the old code both calls returned 200. A normal client can produce that path by sending `%3F`, which the server decodes into the path after it splits off the real query string.

Root cause: `gateway/app/core/rate_limiter.py` used `request.url.path`. Starlette builds that with `urlsplit`, which cuts the path at `?` or `#`. The skip is now `gateway/app/core/rate_limiter.py:672`, and the public MCP check is `gateway/app/core/rate_limiter.py:628`, both via `_routing_path` at line 171. That function reads `scope["path"]`, which is the path the router matches.

Fix: compare the routing path, not the reparsed URL path. A real `/health` path is still exempt.

Test results: `test_literal_question_mark_in_path_does_not_skip_the_limit` failed before the fix with `assert 200 == 429`. After the fix, `tests/test_rate_limit_threshold.py` passed, 21 tests.

## 2. High: the body limit copied an oversized frame before rejecting it

What broke: one ASGI message larger than the ceiling was copied into the buffer, and only then rejected. The middleware's own rule is that buffering stays within the ceiling. Under uvicorn a read is paused around 64KB, so the extra copy is bounded there, but ASGI itself does not promise a small frame. A raw ASGI caller can hand the whole body over in one message.

How to reproduce: POST one 100-byte frame through `RequestBodyLimitMiddleware` with a ceiling of 8, and make `bytearray.extend` raise if the copy would pass the ceiling. The old code raised `RuntimeError: copied 100 bytes past the ceiling`.

Root cause: `gateway/app/middleware/request_body_limit.py` used to call `buffered.extend` and then check `len(buffered)`. The check now happens first, at lines 112-119.

Fix: measure `len(chunk)` and return 413 before `extend` when the frame would pass the ceiling. Frames that fit are still copied and replayed. A body exactly at the ceiling still passes.

Test results: `test_oversized_chunk_is_rejected_without_being_copied` failed before the fix with that RuntimeError. After the fix, `tests/test_request_body_limit.py` passed (included in the 35-test run below).

## 3. Medium: the same reparsed path skipped Cache-Control: no-store

What broke: `/health?not-the-health-route` was treated as public discovery, so the response did not get `Cache-Control: no-store`. The route is not the public `/health` document. On this app that path is a 404, not a tenant payload. The header was still wrong for the path the router saw.

How to reproduce: send that path through `SecurityHeadersMiddleware`. Old code left `cache-control` unset (`assert None == 'no-store'`).

Root cause: `gateway/app/middleware/security_headers.py` used `request.url.path` for the cache decision and for CSP. The cache decision is now line 127, and CSP uses the same helper at line 154. `_request_path` is line 134.

Fix: use `scope["path"]`. Real `/health` stays cacheable. The weird path gets `no-store`.

Test results: `test_question_mark_inside_the_path_is_not_public_discovery` failed before the fix. After the fix, `tests/test_security_headers.py` passed as part of the 35-test run.

## 4. Medium: a null or non-bytes body frame crashed the body limit

What broke: an `http.request` frame with `"body": null` raised `TypeError: can't extend bytearray with NoneType`. A frame whose body was a list of strings raised `TypeError: 'str' object cannot be interpreted as an integer`. Either one became an unhandled server error instead of a response. Normal HTTP servers send bytes, so this is a bad ASGI frame, not a browser request.

How to reproduce: POST one frame with `body` set to `None`, or to `["not", "bytes"]`, through `RequestBodyLimitMiddleware`.

Root cause: `gateway/app/middleware/request_body_limit.py` passed `message.get("body", b"")` straight to `extend`. A present null is not the default. The new read is `_frame_chunk` at line 54, used at lines 108-111.

Fix: a missing or null body is an empty frame and the app still runs. Anything that is not bytes, bytearray, or memoryview gets a 400 and the app is not called.

Test results: `test_null_body_frame_is_an_empty_body` and `test_non_bytes_body_frame_is_rejected` failed before the fix with the TypeErrors above. They pass after the fix.

## Commands after the fix

From `gateway/`:

```
~/metacode/fleet/heavy .venv/bin/pytest -q tests/test_request_body_limit.py tests/test_security_headers.py tests/test_rate_limit_threshold.py::test_literal_question_mark_in_path_does_not_skip_the_limit tests/test_rate_limit_threshold.py::test_bucket_key_is_canonicalized_like_auth --tb=short
```

35 passed.

```
~/metacode/fleet/heavy .venv/bin/pytest -q tests/test_rate_limit_threshold.py --tb=line
```

21 passed.

```
~/metacode/fleet/heavy .venv/bin/ruff check app/middleware/request_body_limit.py app/middleware/security_headers.py app/core/rate_limiter.py tests/test_request_body_limit.py tests/test_security_headers.py tests/test_rate_limit_threshold.py
```

All checks passed.

The full gateway suite was not run.

## Open questions

- HEAD is rewritten to GET before the body limit runs, so a HEAD body is not measured. No current GET route reads the body, and uvicorn pauses a large unread body around 64KB. Left unchanged.
- A valid access token and the raw API key it was minted from still land in two rate-limit buckets. Joining them would need a key lookup the limiter does not do before auth. Proof surfaces, which mint tokens, stay off on production-like boots. Left unchanged.
- `/health/ready` is still counted, while `/health` is exempt. The security notes already say `/health/dependencies` is counted. Not changed.
- HSTS still trusts the leftmost `X-Forwarded-Proto`. That is covered by existing tests. Not changed.
- An oversized Content-Length is still refused before rate-limit bookkeeping. That order is documented. Not changed.
- Muse was not asked to review. This task allows network calls only for package installs.
