"""Documented rate-limit threshold: 120/min, 429 on the 121st request.

A 40-request burst drawing zero 429s is the intended posture, not a missing
limiter. Auth-gated paths are counted. Discovery/docs/health are exempt.
"""

from __future__ import annotations

import asyncio
import hashlib
import time

import pytest
from fastapi import Depends, FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.core.auth import CREDENTIAL_REJECTED_HEADER, AuthContext, get_auth_context
from app.core.config import get_settings
from app.core.rate_limiter import (
    _MEMORY_BUCKET_SWEEP_THRESHOLD,
    RateLimitMiddleware,
    rate_limit_discovery,
)


@pytest.fixture
def accepted_keys(monkeypatch):
    """Env keys the real auth dependency accepts, one per simulated caller."""
    keys = [f"accepted-key-{index:03d}" for index in range(64)]
    monkeypatch.setenv("VALID_API_KEYS", ",".join(keys))
    get_settings.cache_clear()
    try:
        yield keys
    finally:
        get_settings.cache_clear()


def _governed_app() -> FastAPI:
    """Routes behind the real auth dependency, and routes with none at all."""
    api = FastAPI()

    @api.get("/v1/wallets")
    async def _wallets(auth: AuthContext = Depends(get_auth_context)):
        return {"source": auth.source}

    @api.get("/v1/wallets/denied")
    async def _denied(auth: AuthContext = Depends(get_auth_context)):
        # Authenticated, then refused on scope — the governed-loop denial.
        raise HTTPException(status_code=403, detail={"error": "insufficient_scope"})

    @api.get("/v1/discover")
    async def _discover():
        return {"public": True}

    return api


def test_discovery_payload_matches_the_documented_120_default(monkeypatch) -> None:
    from app.core import rate_limiter as rate_limiter_module

    fake = type("Settings", (), {"RATE_LIMIT_PER_MINUTE": 120})()
    monkeypatch.setattr(rate_limiter_module, "get_settings", lambda: fake)

    payload = rate_limit_discovery()
    assert payload["requests_per_minute"] == 120
    assert payload["window_seconds"] == 60
    assert payload["burst_allowance"] == 0
    assert payload["unauthenticated_scope"] == "shared_anonymous_bucket"


@pytest.mark.anyio
async def test_forty_requests_do_not_429_at_the_documented_120_limit() -> None:
    """Reviewer burst of 40 against an auth-gated path: remaining drops, no 429."""

    async def ok(_request):
        return PlainTextResponse("ok")

    starlette_app = Starlette(routes=[Route("/v1/wallets", ok)])
    limited = RateLimitMiddleware(starlette_app, requests_per_minute=120)
    transport = ASGITransport(app=limited)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        responses = [
            await http.get("/v1/wallets", headers={"X-API-Key": "reviewer-key"})
            for _ in range(40)
        ]

    assert all(response.status_code == 200 for response in responses)
    assert responses[0].headers["X-RateLimit-Limit"] == "120"
    assert responses[0].headers["X-RateLimit-Remaining"] == "119"
    assert responses[-1].headers["X-RateLimit-Remaining"] == "80"


@pytest.mark.anyio
async def test_one_hundred_twenty_first_request_returns_429() -> None:
    async def ok(_request):
        return PlainTextResponse("ok")

    starlette_app = Starlette(routes=[Route("/v1/wallets", ok)])
    limited = RateLimitMiddleware(starlette_app, requests_per_minute=120)
    transport = ASGITransport(app=limited)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        statuses = [
            (
                await http.get("/v1/wallets", headers={"X-API-Key": "reviewer-key"})
            ).status_code
            for _ in range(121)
        ]

    assert statuses[:120] == [200] * 120
    assert statuses[120] == 429


@pytest.mark.anyio
async def test_auth_gated_401_is_still_counted() -> None:
    """Failing auth does not skip the limiter; the bucket is the key value."""

    async def deny(_request):
        return PlainTextResponse("no", status_code=401)

    starlette_app = Starlette(routes=[Route("/v1/wallets", deny)])
    limited = RateLimitMiddleware(starlette_app, requests_per_minute=2)
    transport = ASGITransport(app=limited)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        first = await http.get("/v1/wallets", headers={"X-API-Key": "reviewer-key"})
        second = await http.get("/v1/wallets", headers={"X-API-Key": "reviewer-key"})
        third = await http.get("/v1/wallets", headers={"X-API-Key": "reviewer-key"})

    assert first.status_code == 401
    assert second.status_code == 401
    assert third.status_code == 429
    assert first.headers["X-RateLimit-Limit"] == "2"


def test_root_and_discover_publish_the_same_rate_limits() -> None:
    from fastapi.testclient import TestClient as FastAPITestClient

    from app.main import app

    client = FastAPITestClient(app)
    published = client.get("/").json()["rate_limits"]
    assert published == client.get("/v1/discover").json()["rate_limits"]
    assert published == rate_limit_discovery()


@pytest.mark.anyio
async def test_rotating_invalid_keys_share_one_bounded_budget() -> None:
    """A fresh invalid X-API-Key per request must not mint a fresh budget.

    The per-key bucket is chosen from a caller-supplied header before the key
    is verified, so rejected credentials are charged to one shared per-client
    bucket at ten times the per-key limit. Every request below carries a
    distinct key value and would otherwise get its own budget forever.
    """

    async def deny(_request):
        return PlainTextResponse("no", status_code=401)

    starlette_app = Starlette(routes=[Route("/v1/wallets", deny)])
    limited = RateLimitMiddleware(starlette_app, requests_per_minute=2)
    ceiling = limited.preauth_limit
    assert ceiling == 20

    transport = ASGITransport(app=limited)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        statuses = [
            (
                await http.get(
                    "/v1/wallets",
                    headers={"X-API-Key": f"invalid-key-{index}"},
                )
            ).status_code
            for index in range(ceiling + 1)
        ]

    assert statuses[:ceiling] == [401] * ceiling
    assert statuses[ceiling] == 429


@pytest.mark.anyio
async def test_accepted_credentials_never_touch_the_shared_bucket(
    accepted_keys,
) -> None:
    """The shared bucket bounds unaccepted credentials only, not real traffic.

    Acceptance is what the auth dependency says, not what the status code
    implies: these keys go through the real ``get_auth_context``.
    """

    limited = RateLimitMiddleware(_governed_app(), requests_per_minute=2)
    assert limited.preauth_limit + 5 <= len(accepted_keys)

    transport = ASGITransport(app=limited)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        statuses = [
            (
                await http.get(
                    "/v1/wallets",
                    headers={"X-API-Key": accepted_keys[index]},
                )
            ).status_code
            for index in range(limited.preauth_limit + 5)
        ]

    assert statuses == [200] * len(statuses)


@pytest.mark.anyio
async def test_accepted_credentials_hand_their_reservation_back() -> None:
    """A reservation is taken before the request and returned unless refused."""

    limited = RateLimitMiddleware(Starlette(routes=[]), requests_per_minute=2)
    bucket = "probe-bucket"
    now = time.time()

    for _ in range(limited.preauth_limit + 5):
        exhausted, _reset = await limited._reserve(bucket, now, limited.preauth_limit)
        assert exhausted is False
        await limited._release(bucket, now)

    assert limited._requests.get(bucket) in (None, [])

    for _ in range(limited.preauth_limit):
        exhausted, _reset = await limited._reserve(bucket, now, limited.preauth_limit)
        assert exhausted is False
    exhausted, reset_in = await limited._reserve(bucket, now, limited.preauth_limit)
    assert exhausted is True
    assert reset_in >= 1


@pytest.mark.anyio
async def test_rejected_credential_403_is_charged() -> None:
    """An unknown API key is refused with 403, not 401 — count it either way.

    ``get_auth_context`` answers a well-formed but unknown key with 403, which
    is exactly what a key-rotating caller sends. The app marks those as
    authentication failures; the limiter charges them and strips the marker.
    """

    async def deny(_request):
        return PlainTextResponse(
            "no",
            status_code=403,
            headers={CREDENTIAL_REJECTED_HEADER: "1"},
        )

    starlette_app = Starlette(routes=[Route("/v1/wallets", deny)])
    limited = RateLimitMiddleware(starlette_app, requests_per_minute=2)
    ceiling = limited.preauth_limit

    transport = ASGITransport(app=limited)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        responses = [
            await http.get(
                "/v1/wallets",
                headers={"X-API-Key": f"unknown-key-{index}"},
            )
            for index in range(ceiling + 1)
        ]

    assert [r.status_code for r in responses[:ceiling]] == [403] * ceiling
    assert responses[ceiling].status_code == 429
    # The marker is internal: it must not reach the caller.
    assert CREDENTIAL_REJECTED_HEADER.lower() not in responses[0].headers


@pytest.mark.anyio
async def test_authorization_403_is_not_charged(accepted_keys) -> None:
    """A denial is ordinary governed traffic, not a credential rejection.

    An authenticated caller refused on scope (``wallet_access_denied``,
    ``insufficient_scope``) carries no marker, so its denials must not spend
    the shared abuse budget that co-located callers depend on.
    """

    limited = RateLimitMiddleware(_governed_app(), requests_per_minute=2)

    transport = ASGITransport(app=limited)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        responses = [
            await http.get(
                "/v1/wallets/denied",
                headers={"X-API-Key": accepted_keys[index]},
            )
            for index in range(limited.preauth_limit + 5)
        ]

    assert [r.status_code for r in responses] == [403] * len(responses)
    assert responses[0].json()["detail"]["error"] == "insufficient_scope"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("path", "answered"),
    [
        # A public route that never authenticates.
        ("/v1/discover", 200),
        # No route at all: nothing runs that could look at the key.
        ("/v1/no-such-route", 404),
    ],
)
async def test_rotating_keys_where_nothing_authenticates_is_bounded(
    path, answered
) -> None:
    """Inventing an X-API-Key per request must not mint a budget per request.

    The per-key bucket is picked from the header before anything verifies it.
    On a route that never authenticates nothing ever refuses the key either,
    so a ceiling that only charged refused credentials let a single client
    send ``limit`` requests per invented value, without bound. Only an
    accepted credential hands its shared reservation back.
    """

    limited = RateLimitMiddleware(_governed_app(), requests_per_minute=2)
    ceiling = limited.preauth_limit

    transport = ASGITransport(app=limited)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        responses = [
            await http.get(path, headers={"X-API-Key": f"invented-key-{index}"})
            for index in range(ceiling + 1)
        ]

    assert [r.status_code for r in responses[:ceiling]] == [answered] * ceiling
    assert responses[ceiling].status_code == 429
    assert "does not reset it" in responses[ceiling].json()["detail"]["message"]


@pytest.mark.anyio
async def test_accepted_key_is_not_charged_for_unauthenticated_neighbours(
    accepted_keys,
) -> None:
    """Accepted traffic interleaved with invented keys keeps its own budget.

    The invented keys spend the client's shared bucket; the accepted key's
    requests hand theirs back, so they are only ever bounded by its own
    per-key limit.
    """

    limited = RateLimitMiddleware(_governed_app(), requests_per_minute=3)
    ceiling = limited.preauth_limit

    transport = ASGITransport(app=limited)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        accepted = []
        for index in range(ceiling - 1):
            invented = await http.get(
                "/v1/discover", headers={"X-API-Key": f"invented-key-{index}"}
            )
            assert invented.status_code == 200
            if index < 3:
                accepted.append(
                    await http.get(
                        "/v1/wallets", headers={"X-API-Key": accepted_keys[0]}
                    )
                )
        last_invented = await http.get(
            "/v1/discover", headers={"X-API-Key": "invented-key-last"}
        )
        over_budget = await http.get(
            "/v1/discover", headers={"X-API-Key": "invented-key-over"}
        )

    assert [r.status_code for r in accepted] == [200, 200, 200]
    assert last_invented.status_code == 200
    assert over_budget.status_code == 429


class _HandlerBug(Exception):
    """An unhandled error raised by the app after the limiter let it in."""


@pytest.mark.anyio
async def test_a_request_that_raises_is_held_to_the_same_rule(accepted_keys) -> None:
    """An unhandled error must not hand an unaccepted request's budget back.

    In the real stack nothing between the routes and the limiter turns an
    unhandled exception into a response, so it propagates out of
    ``call_next``. Releasing the reservation on that exit would give a caller
    who can make a public route fail one free request per invented key.
    Accepted credentials still hand theirs back, error or not.
    """

    async def app(scope, receive, send):
        presented = Request(scope).headers.get("X-API-Key", "")
        if presented in accepted_keys:
            await get_auth_context(api_key=presented)
        raise _HandlerBug("unhandled")

    limited = RateLimitMiddleware(app, requests_per_minute=2)
    ceiling = limited.preauth_limit
    assert ceiling + 5 <= len(accepted_keys)

    transport = ASGITransport(app=limited)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        for index in range(ceiling + 5):
            with pytest.raises(_HandlerBug):
                await http.get(
                    "/v1/wallets", headers={"X-API-Key": accepted_keys[index]}
                )
        for index in range(ceiling):
            with pytest.raises(_HandlerBug):
                await http.get(
                    "/v1/discover", headers={"X-API-Key": f"invented-key-{index}"}
                )
        over_budget = await http.get(
            "/v1/discover", headers={"X-API-Key": "invented-key-over"}
        )

    assert over_budget.status_code == 429


@pytest.mark.anyio
async def test_raw_api_key_never_names_a_limiter_bucket() -> None:
    """Bucket names become Redis key names, so a live key must not be one.

    Anyone who can list the limiter's Redis keys (``KEYS rate_limit:*``)
    would otherwise read every API key that called in the last minute. A
    digest still gives each distinct value its own bucket.
    """

    secret = "b2a_live-secret-that-must-never-reach-redis"
    seen: list[str] = []

    class _RecordingRedis:
        def __init__(self) -> None:
            self.counts: dict[str, int] = {}

        async def incr(self, key: str) -> int:
            seen.append(key)
            self.counts[key] = self.counts.get(key, 0) + 1
            return self.counts[key]

        async def expire(self, key: str, _seconds: int) -> bool:
            return True

        async def decr(self, key: str) -> int:
            self.counts[key] = self.counts.get(key, 0) - 1
            return self.counts[key]

        async def delete(self, key: str) -> int:
            self.counts.pop(key, None)
            return 1

    fake = _RecordingRedis()

    async def _fake_redis():
        return fake

    async def ok(_request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/v1/wallets", ok)])
    shared = RateLimitMiddleware(app, requests_per_minute=2)
    shared._get_redis = _fake_redis  # type: ignore[method-assign]
    in_memory = RateLimitMiddleware(app, requests_per_minute=2)
    in_memory._redis_url = ""

    for limited in (shared, in_memory):
        transport = ASGITransport(app=limited)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            statuses = [
                (
                    await http.get("/v1/wallets", headers={"X-API-Key": secret})
                ).status_code
                for _ in range(3)
            ]
            other = await http.get(
                "/v1/wallets", headers={"X-API-Key": f"{secret}-other"}
            )
        # Still one bucket per distinct key value.
        assert statuses == [200, 200, 429]
        assert other.status_code == 200

    assert seen
    assert not any(secret in key for key in seen)
    assert not any(secret in key for key in in_memory._requests)
    # Nor the key table's own lookup hash for that key.
    key_hash = hashlib.sha256(secret.encode()).hexdigest()
    assert not any(key_hash in key for key in seen)


@pytest.mark.anyio
async def test_the_real_unknown_key_path_is_bounded() -> None:
    """End to end through the actual dependency, not a stub that returns 401."""

    from fastapi import Depends, FastAPI

    from app.core.auth import get_auth_context

    api = FastAPI()

    @api.get("/v1/wallets")
    async def _wallets(auth=Depends(get_auth_context)):  # pragma: no cover - denied
        return {"ok": True}

    limited = RateLimitMiddleware(api, requests_per_minute=2)
    ceiling = limited.preauth_limit

    transport = ASGITransport(app=limited)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        responses = [
            await http.get(
                "/v1/wallets",
                headers={"X-API-Key": f"unknown-but-well-formed-{index}"},
            )
            for index in range(ceiling + 1)
        ]

    assert responses[0].status_code == 403
    assert responses[0].json()["detail"]["error"] == "invalid_api_key"
    assert CREDENTIAL_REJECTED_HEADER.lower() not in responses[0].headers
    assert responses[ceiling].status_code == 429


@pytest.mark.anyio
async def test_concurrent_rejections_cannot_exceed_the_ceiling() -> None:
    """The budget is reserved before the request, not charged after it.

    Reading the bucket and charging it after the response would let every
    request already in flight pass the same read, so a caller with enough
    concurrency would walk straight past the ceiling.
    """

    entered = 0
    gate = asyncio.Event()

    async def slow_deny(_request):
        nonlocal entered
        entered += 1
        await gate.wait()
        return PlainTextResponse("no", status_code=401)

    starlette_app = Starlette(routes=[Route("/v1/wallets", slow_deny)])
    limited = RateLimitMiddleware(starlette_app, requests_per_minute=1)
    ceiling = limited.preauth_limit
    attempts = ceiling + 5

    transport = ASGITransport(app=limited)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        tasks = [
            asyncio.create_task(
                http.get("/v1/wallets", headers={"X-API-Key": f"distinct-key-{index}"})
            )
            for index in range(attempts)
        ]
        for _ in range(500):
            await asyncio.sleep(0)
            if entered + sum(task.done() for task in tasks) >= attempts:
                break
        gate.set()
        responses = await asyncio.gather(*tasks)

    assert entered == ceiling
    assert sum(r.status_code == 429 for r in responses) == attempts - ceiling


@pytest.mark.anyio
async def test_expired_in_memory_buckets_are_swept() -> None:
    """Rotating key values must not leave a list behind per value forever."""

    async def ok(_request):
        return PlainTextResponse("ok")

    limited = RateLimitMiddleware(Starlette(routes=[Route("/v1/w", ok)]))
    stale = time.time() - 3600
    for index in range(_MEMORY_BUCKET_SWEEP_THRESHOLD + 1):
        limited._requests[f"rotated-key-{index}"] = [stale]

    await limited._check_limit_in_memory("live-key", time.time())

    assert list(limited._requests) == ["live-key"]


def test_published_window_does_not_claim_one_backend_s_algorithm() -> None:
    """Redis counts a fixed window; the in-memory fallback counts a rolling one."""

    payload = rate_limit_discovery()
    assert payload["window_seconds"] == 60
    assert payload["window_accounting"] == (
        "fixed_window_shared_rolling_window_in_memory"
    )
    assert payload["rejected_credentials_scope"] == "shared_per_client_bucket"


@pytest.mark.anyio
async def test_bucket_key_is_canonicalized_like_auth() -> None:
    """``"key"`` and ``"key "`` are one credential to auth, so one bucket here.

    The limiter used the raw header as the bucket key while auth strips it, so
    a caller could sidestep the per-key limit by varying trailing whitespace.
    """

    async def ok(_request):
        return PlainTextResponse("ok")

    starlette_app = Starlette(routes=[Route("/v1/wallets", ok)])
    limited = RateLimitMiddleware(starlette_app, requests_per_minute=2)

    transport = ASGITransport(app=limited)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        first = await http.get("/v1/wallets", headers={"X-API-Key": "same-key"})
        second = await http.get("/v1/wallets", headers={"X-API-Key": "same-key "})
        third = await http.get("/v1/wallets", headers={"X-API-Key": " same-key"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429
