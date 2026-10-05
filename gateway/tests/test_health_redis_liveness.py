"""/health reports Redis reachability, and the limiter recovers from a dead socket.

Regression for the production outage where Redis hung after a platform restart:
/health kept answering 200 while every /v1 request returned 503
rate_limiter_unavailable, and the limiter kept reusing its dead cached client.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.core import health as health_module
from app.core import rate_limiter as rate_limiter_module
from app.core.config import get_settings
from app.core.rate_limiter import RateLimitMiddleware

_SECRET_URL = "redis://default:s3cr3t-pass@redis.internal.example:6379/0"


class _FakeRedis:
    def __init__(self, *, fail: BaseException | None = None, hang: bool = False):
        self.fail = fail
        self.hang = hang
        self.pings = 0
        self.closed = False
        self.counts: dict[str, int] = {}

    async def _maybe_fail(self) -> None:
        if self.hang:
            await asyncio.sleep(30)
        if self.fail is not None:
            raise self.fail

    async def ping(self) -> bool:
        self.pings += 1
        await self._maybe_fail()
        return True

    async def incr(self, key: str) -> int:
        await self._maybe_fail()
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key: str, _seconds: int) -> bool:
        await self._maybe_fail()
        return True

    async def decr(self, key: str) -> int:
        await self._maybe_fail()
        self.counts[key] = self.counts.get(key, 0) - 1
        return self.counts[key]

    async def delete(self, key: str) -> int:
        self.counts.pop(key, None)
        return 1

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def redis_url(monkeypatch):
    monkeypatch.setenv("REDIS_URL", _SECRET_URL)
    get_settings.cache_clear()
    health_module.reset_liveness_redis_cache()
    monkeypatch.setattr(health_module, "_liveness_client", None)
    monkeypatch.setattr(health_module, "_liveness_client_url", None)
    try:
        yield _SECRET_URL
    finally:
        health_module.reset_liveness_redis_cache()
        get_settings.cache_clear()


@pytest.fixture
def liveness_clients(monkeypatch, redis_url):
    """Every client /health creates, in order; tests set the next behaviour."""
    created: list[_FakeRedis] = []
    behaviour: dict[str, object] = {"fail": None, "hang": False}

    def _factory(url: str) -> _FakeRedis:
        assert url == redis_url
        client = _FakeRedis(fail=behaviour["fail"], hang=bool(behaviour["hang"]))
        created.append(client)
        return client

    monkeypatch.setattr(health_module, "_liveness_redis_client", _factory)
    return created, behaviour


async def _get_health():
    from app.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        return await http.get("/health")


@pytest.mark.anyio
async def test_health_without_redis_configured_stays_healthy(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "")
    get_settings.cache_clear()
    health_module.reset_liveness_redis_cache()
    try:
        resp = await _get_health()
    finally:
        get_settings.cache_clear()
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"
    assert body["checks"] == {"redis": "not_configured"}


@pytest.mark.anyio
async def test_health_reports_redis_up(liveness_clients):
    created, _ = liveness_clients
    resp = await _get_health()
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"
    assert resp.json()["checks"] == {"redis": "up"}
    assert created and created[0].pings == 1


@pytest.mark.anyio
async def test_health_is_503_degraded_when_redis_fails(liveness_clients):
    created, behaviour = liveness_clients
    behaviour["fail"] = ConnectionError(
        "Error 111 connecting to redis.internal.example:6379. Connection refused."
    )
    resp = await _get_health()
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"] == {"redis": "down"}
    # Unauthenticated endpoint: no hostnames, credentials, or driver text.
    raw = resp.text
    for leak in ("redis.internal.example", "s3cr3t", "6379", "Connection refused"):
        assert leak not in raw
    # The dead client is dropped so the next probe reconnects.
    assert created[0].closed
    assert health_module._liveness_client is None


@pytest.mark.anyio
async def test_health_times_out_fast_when_redis_hangs(monkeypatch, liveness_clients):
    _, behaviour = liveness_clients
    behaviour["hang"] = True
    monkeypatch.setattr(health_module, "LIVENESS_REDIS_TIMEOUT_SECONDS", 0.05)
    loop = asyncio.get_running_loop()
    start = loop.time()
    resp = await _get_health()
    assert loop.time() - start < 2.0
    assert resp.status_code == 503
    assert resp.json()["checks"] == {"redis": "down"}


@pytest.mark.anyio
async def test_health_caches_verdict_and_recovers(monkeypatch, liveness_clients):
    created, behaviour = liveness_clients
    behaviour["fail"] = ConnectionError("down")
    assert await health_module.check_redis_liveness() == "down"
    # Within the cache window no new PING is sent.
    assert await health_module.check_redis_liveness() == "down"
    assert len(created) == 1 and created[0].pings == 1

    behaviour["fail"] = None
    health_module.reset_liveness_redis_cache()
    assert await health_module.check_redis_liveness() == "up"
    # A fresh client replaced the one that failed.
    assert len(created) == 2


@pytest.mark.anyio
async def test_cached_down_verdict_expires(monkeypatch, liveness_clients):
    created, behaviour = liveness_clients
    behaviour["fail"] = ConnectionError("down")
    assert await health_module.check_redis_liveness() == "down"
    behaviour["fail"] = None
    # Age the cached verdict past the window instead of clearing it.
    at, status = health_module._liveness_cache
    monkeypatch.setattr(
        health_module,
        "_liveness_cache",
        (at - health_module.LIVENESS_REDIS_CACHE_SECONDS - 0.1, status),
    )
    assert await health_module.check_redis_liveness() == "up"
    assert len(created) == 2


@pytest.mark.anyio
async def test_health_answers_while_limiter_redis_is_down(
    monkeypatch, liveness_clients
):
    """/health bypasses the limiter, so it reports degraded instead of hanging."""
    _, behaviour = liveness_clients
    behaviour["fail"] = ConnectionError("down")
    limiter = _limited_app(monkeypatch, production_like=True)

    async def _no_redis():
        return None

    limiter._get_redis = _no_redis  # type: ignore[method-assign]

    async def health_route(_request):
        status = await health_module.check_redis_liveness()
        return PlainTextResponse(status, status_code=200 if status != "down" else 503)

    limiter.app.router.routes.append(Route("/health", health_route))
    async with AsyncClient(
        transport=ASGITransport(app=limiter), base_url="http://test"
    ) as http:
        v1 = await http.get("/v1/wallets")
        health = await http.get("/health")
    assert v1.status_code == 503
    assert health.status_code == 503
    assert health.text == "down"


# ---------------------------------------------------------------------------
# Rate limiter: a failing command on an existing connection resets the client
# ---------------------------------------------------------------------------


def _limited_app(monkeypatch, *, production_like: bool) -> RateLimitMiddleware:
    async def ok(_request):
        return PlainTextResponse("ok")

    monkeypatch.setattr(
        rate_limiter_module,
        "is_production_like_environment",
        lambda _env: production_like,
    )
    app = Starlette(routes=[Route("/v1/wallets", ok)])
    limiter = RateLimitMiddleware(app, requests_per_minute=100)
    limiter._redis_url = _SECRET_URL
    return limiter


@pytest.mark.anyio
async def test_redis_command_failure_resets_cached_client_and_logs(monkeypatch, caplog):
    limiter = _limited_app(monkeypatch, production_like=True)
    dead = _FakeRedis(fail=ConnectionError("Connection reset by peer"))
    limiter._redis = dead  # established earlier, then Redis restarted

    healthy = _FakeRedis()
    created: list[_FakeRedis] = []

    def _from_url(url, **kwargs):
        created.append(healthy)
        return healthy

    monkeypatch.setattr(rate_limiter_module.redis, "from_url", _from_url)

    async with AsyncClient(
        transport=ASGITransport(app=limiter), base_url="http://test"
    ) as http:
        with caplog.at_level(logging.ERROR, logger=rate_limiter_module.__name__):
            first = await http.get("/v1/wallets")
        assert first.status_code == 503
        assert first.json()["detail"]["error"] == "rate_limiter_unavailable"
        assert limiter._redis is None
        assert dead.closed
        assert any(
            "reset the cached connection" in record.getMessage()
            for record in caplog.records
        )

        # Redis is back: the next request reconnects instead of reusing the
        # dead socket until the process restarts.
        second = await http.get("/v1/wallets")
    assert second.status_code == 200
    assert created == [healthy]
    assert limiter._redis is healthy


@pytest.mark.anyio
async def test_redis_command_failure_outside_production_falls_back_and_resets(
    monkeypatch,
):
    limiter = _limited_app(monkeypatch, production_like=False)
    dead = _FakeRedis(fail=TimeoutError("Timeout reading from socket"))
    limiter._redis = dead
    monkeypatch.setattr(
        rate_limiter_module.redis,
        "from_url",
        lambda url, **kwargs: _FakeRedis(fail=ConnectionError("still down")),
    )
    async with AsyncClient(
        transport=ASGITransport(app=limiter), base_url="http://test"
    ) as http:
        resp = await http.get("/v1/wallets")
    assert resp.status_code == 200
    assert limiter._redis is None
    assert dead.closed


@pytest.mark.anyio
async def test_redis_client_is_built_with_timeouts(monkeypatch):
    limiter = _limited_app(monkeypatch, production_like=True)
    seen: dict = {}

    def _from_url(url, **kwargs):
        seen.update(kwargs)
        return _FakeRedis()

    monkeypatch.setattr(rate_limiter_module.redis, "from_url", _from_url)
    assert await limiter._get_redis() is not None
    assert seen["socket_connect_timeout"] == (
        rate_limiter_module.REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS
    )
    assert seen["socket_timeout"] == rate_limiter_module.REDIS_SOCKET_TIMEOUT_SECONDS
    assert seen["health_check_interval"] == (
        rate_limiter_module.REDIS_HEALTH_CHECK_INTERVAL_SECONDS
    )
    assert 0 < seen["socket_connect_timeout"] <= 5
    assert 0 < seen["socket_timeout"] <= 5


@pytest.mark.anyio
async def test_stale_failure_does_not_drop_a_newer_healthy_client(monkeypatch):
    """A late failure from the old client must not close a reconnected one."""
    limiter = _limited_app(monkeypatch, production_like=True)
    old = _FakeRedis(fail=ConnectionError("old socket"))
    fresh = _FakeRedis()
    limiter._redis = fresh  # another request already reconnected
    with pytest.raises(ConnectionError):
        await limiter._incr_window(old, "rate_limit:k:0")
    assert limiter._redis is fresh
    assert not fresh.closed
    assert old.closed


@pytest.mark.anyio
async def test_concurrent_health_cache_miss_sends_one_ping(liveness_clients):
    created, behaviour = liveness_clients
    behaviour["hang"] = False
    results = await asyncio.gather(
        *(health_module.check_redis_liveness() for _ in range(20))
    )
    assert set(results) == {"up"}
    assert len(created) == 1 and created[0].pings == 1


def test_health_declares_its_503_in_openapi():
    from app.main import app

    responses = app.openapi()["paths"]["/health"]["get"]["responses"]
    assert "503" in responses


def test_url_query_cannot_lift_the_timeouts():
    """redis-py lets URL query params override from_url kwargs; we re-apply ours."""
    import redis.asyncio as redis

    url = "redis://localhost:6390/0?socket_timeout=120&socket_connect_timeout=120"
    client = rate_limiter_module.enforce_redis_timeouts(
        redis.from_url(url, socket_timeout=2.0, socket_connect_timeout=2.0)
    )
    kwargs = client.connection_pool.connection_kwargs
    assert kwargs["socket_timeout"] == rate_limiter_module.REDIS_SOCKET_TIMEOUT_SECONDS
    assert kwargs["socket_connect_timeout"] == (
        rate_limiter_module.REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS
    )
    assert kwargs["health_check_interval"] == (
        rate_limiter_module.REDIS_HEALTH_CHECK_INTERVAL_SECONDS
    )
    live = health_module._liveness_redis_client(url)
    live_kwargs = live.connection_pool.connection_kwargs
    assert live_kwargs["socket_timeout"] == health_module.LIVENESS_REDIS_TIMEOUT_SECONDS


@pytest.mark.anyio
async def test_redis_url_change_drops_client_and_verdict(monkeypatch, liveness_clients):
    created, behaviour = liveness_clients
    assert await health_module.check_redis_liveness() == "up"
    other = "redis://default:other@redis-2.internal.example:6379/0"
    monkeypatch.setenv("REDIS_URL", other)
    get_settings.cache_clear()
    monkeypatch.setattr(
        health_module,
        "_liveness_redis_client",
        lambda url: (
            created.append(_FakeRedis(fail=ConnectionError("new down"))) or created[-1]
        ),
    )
    # Within the cache window, but the verdict belonged to the old URL.
    assert await health_module.check_redis_liveness() == "down"
    assert created[0].closed
    assert len(created) == 2
