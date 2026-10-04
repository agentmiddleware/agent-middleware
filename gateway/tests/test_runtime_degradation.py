"""Tests for Redis→memory fallback observability and fail-closed posture."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.core.health import gather_dependency_report
from app.core.rate_limiter import RateLimitMiddleware
from app.core.runtime_degradation import (
    get_runtime_degradation,
    mark_durable_state_fell_back,
    mark_rate_limiter_memory_fallback,
    reset_runtime_degradation,
)
from app.main import app


@pytest.fixture(autouse=True)
def _reset_degradation():
    reset_runtime_degradation()
    yield
    reset_runtime_degradation()


def test_runtime_degradation_flags_surface_on_health_payload():
    mark_rate_limiter_memory_fallback()
    mark_durable_state_fell_back("redis")
    snap = get_runtime_degradation()
    assert snap["degraded"] is True
    assert snap["rate_limiter"]["using_memory_fallback"] is True
    assert snap["durable_state"]["fell_back_to_memory"] is True
    assert snap["durable_state"]["intended_backend"] == "redis"


@pytest.mark.anyio
async def test_health_dependencies_reports_runtime_degradation():
    mark_rate_limiter_memory_fallback()
    report = await gather_dependency_report()
    assert "runtime_degradation" in report
    assert report["runtime_degradation"]["degraded"] is True
    assert report["status"] == "degraded"
    assert "runtime_degradation" in report["unhealthy"]


@pytest.mark.anyio
async def test_health_endpoint_includes_runtime_degradation_field(client=None):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        resp = await http.get("/health/dependencies")
    assert resp.status_code == 200
    body = resp.json()
    assert "runtime_degradation" in body
    assert "degraded" in body["runtime_degradation"]


@pytest.mark.anyio
async def test_production_like_redis_outage_fails_closed(monkeypatch):
    from app.core import rate_limiter as rate_limiter_module

    monkeypatch.setattr(rate_limiter_module.settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(
        rate_limiter_module,
        "is_production_like_environment",
        lambda _env: True,
    )

    async def ok(_request):
        return PlainTextResponse("ok")

    starlette_app = Starlette(routes=[Route("/v1/ping", ok)])
    limited = RateLimitMiddleware(starlette_app, requests_per_minute=100)
    # Force the Redis-required path without relying on OS-level connection
    # refusal (CI runners vary for 127.0.0.1:1).
    limited._redis_url = "redis://127.0.0.1:1/0"

    async def _redis_unavailable():
        mark_rate_limiter_memory_fallback()
        return None

    limited._get_redis = _redis_unavailable  # type: ignore[method-assign]
    assert limited._fail_closed_on_redis_outage() is True

    async def call_next(request):
        return PlainTextResponse("ok")

    from starlette.requests import Request
    from starlette.datastructures import Headers

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/v1/ping",
        "raw_path": b"/v1/ping",
        "query_string": b"",
        "headers": Headers({"x-api-key": "prod-agent-key-1"}).raw,
        "client": ("127.0.0.1", 123),
        "server": ("test", 80),
    }
    request = Request(scope)
    response = await limited.dispatch(request, call_next)
    assert response.status_code == 503
    payload = response.body
    assert b"rate_limiter_unavailable" in payload
    assert get_runtime_degradation()["rate_limiter"]["using_memory_fallback"] is True


@pytest.mark.anyio
async def test_public_mcp_rate_bucket_ignores_untrusted_headers(monkeypatch):
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_ID", raising=False)

    async def ok(_request):
        return PlainTextResponse("ok")

    starlette_app = Starlette(routes=[Route("/mcp/public", ok, methods=["POST"])])
    limited = RateLimitMiddleware(starlette_app, requests_per_minute=2)
    transport = ASGITransport(app=limited)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        first = await http.post(
            "/mcp/public",
            headers={
                "X-API-Key": "attacker-chosen-a",
                "X-Real-IP": "198.51.100.1",
            },
        )
        second = await http.post(
            "/mcp/public",
            headers={"X-API-Key": "test-key", "X-Real-IP": "198.51.100.2"},
        )
        third = await http.post(
            "/mcp/public",
            headers={
                "X-API-Key": "attacker-chosen-b",
                "X-Real-IP": "198.51.100.3",
            },
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429
    assert len(limited._requests) == 2
    client_buckets = [key for key in limited._requests if ":client:" in key]
    assert len(client_buckets) == 1
    assert client_buckets[0].endswith(":client:127.0.0.1")


@pytest.mark.anyio
async def test_public_mcp_uses_canonical_railway_client_ip(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", "environment-test")

    async def ok(_request):
        return PlainTextResponse("ok")

    starlette_app = Starlette(routes=[Route("/mcp/public", ok, methods=["POST"])])
    limited = RateLimitMiddleware(starlette_app, requests_per_minute=2)
    transport = ASGITransport(app=limited)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        client_a = [
            await http.post(
                "/mcp/public",
                headers={"X-Real-IP": "198.51.100.11"},
            )
            for _ in range(3)
        ]
        client_b = await http.post(
            "/mcp/public",
            headers={"X-Real-IP": "2001:0db8:0:0::12"},
        )

    assert [response.status_code for response in client_a] == [200, 200, 429]
    assert client_b.status_code == 200
    client_buckets = {key for key in limited._requests if ":client:" in key}
    assert any(key.endswith(":client:198.51.100.11") for key in client_buckets)
    assert any(key.endswith(":client:2001:db8::12") for key in client_buckets)


@pytest.mark.anyio
async def test_public_mcp_rejects_ambiguous_railway_client_ip(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", "environment-test")

    async def ok(_request):
        return PlainTextResponse("ok")

    starlette_app = Starlette(routes=[Route("/mcp/public", ok, methods=["POST"])])
    limited = RateLimitMiddleware(starlette_app, requests_per_minute=2)
    transport = ASGITransport(app=limited)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        malformed = await http.post(
            "/mcp/public",
            headers={"X-Real-IP": "not-an-ip"},
        )
        comma_list = await http.post(
            "/mcp/public",
            headers={"X-Real-IP": "198.51.100.1, 198.51.100.2"},
        )
        duplicate = await http.post(
            "/mcp/public",
            headers=[
                ("X-Real-IP", "198.51.100.1"),
                ("X-Real-IP", "198.51.100.2"),
            ],
        )

    assert malformed.status_code == 200
    assert comma_list.status_code == 200
    assert duplicate.status_code == 429
    client_buckets = [key for key in limited._requests if ":client:" in key]
    assert len(client_buckets) == 1
    assert client_buckets[0].endswith(":client:127.0.0.1")


@pytest.mark.anyio
async def test_public_mcp_has_a_separate_global_backstop(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", "environment-test")

    async def ok(_request):
        return PlainTextResponse("ok")

    starlette_app = Starlette(routes=[Route("/mcp/public", ok, methods=["POST"])])
    limited = RateLimitMiddleware(starlette_app, requests_per_minute=1)
    transport = ASGITransport(app=limited)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        responses = [
            await http.post(
                "/mcp/public",
                headers={"X-Real-IP": f"198.51.100.{index}"},
            )
            for index in range(1, 12)
        ]

    assert all(response.status_code == 200 for response in responses[:10])
    assert responses[10].status_code == 429
    assert responses[10].headers["X-RateLimit-Limit"] == "10"


@pytest.mark.anyio
async def test_test_key_bypass_is_disabled_in_production_like_environments(
    monkeypatch,
):
    from app.core import rate_limiter as rate_limiter_module

    monkeypatch.setattr(rate_limiter_module.settings, "ENVIRONMENT", "production")

    async def ok(_request):
        return PlainTextResponse("ok")

    starlette_app = Starlette(routes=[Route("/v1/ping", ok)])
    limited = RateLimitMiddleware(starlette_app, requests_per_minute=1)
    transport = ASGITransport(app=limited)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        first = await http.get("/v1/ping", headers={"X-API-Key": "test-key"})
        second = await http.get("/v1/ping", headers={"X-API-Key": "test-key"})

    assert first.status_code == 200
    assert second.status_code == 429
