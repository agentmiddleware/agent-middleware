"""Tests for Redis→memory fallback observability and fail-closed posture."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.core.health import gather_dependency_report
from app.core.rate_limiter import RateLimitMiddleware
from app.core.config import get_settings
from app.core.runtime_degradation import (
    REASON_DURABLE_STATE_MEMORY_FALLBACK,
    REASON_RATE_LIMITER_MEMORY_FALLBACK,
    REASON_RATE_LIMITER_REDIS_NOT_CONFIGURED,
    get_runtime_degradation,
    mark_durable_state_fell_back,
    mark_rate_limiter_memory_fallback,
    reset_runtime_degradation,
    warn_if_rate_limiter_memory_only,
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
async def test_health_endpoint_includes_runtime_degradation_field():
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


# --- Memory-only limiter in a production-like environment (issue 286) --------

_WARNING_LOGGER = "app.core.runtime_degradation"


def _configure(monkeypatch, *, environment: str, redis_url: str) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "ENVIRONMENT", environment)
    monkeypatch.setattr(settings, "REDIS_URL", redis_url)


def _memory_only_warnings(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == _WARNING_LOGGER
        and REASON_RATE_LIMITER_REDIS_NOT_CONFIGURED in record.getMessage()
    ]


def test_production_like_without_redis_url_is_degraded_with_stable_reason(
    monkeypatch,
):
    _configure(monkeypatch, environment="production", redis_url="")

    snap = get_runtime_degradation()

    assert snap["degraded"] is True
    assert snap["reasons"] == ["rate_limiter_redis_not_configured"]
    assert snap["rate_limiter"]["memory_only_in_production_like"] is True
    assert snap["rate_limiter"]["backend"] == "memory"
    assert snap["rate_limiter"]["using_memory_fallback"] is False


@pytest.mark.parametrize("environment", ["production", "staging", "prod-eu"])
def test_every_production_like_environment_is_degraded_without_redis_url(
    monkeypatch, environment
):
    _configure(monkeypatch, environment=environment, redis_url="")

    assert get_runtime_degradation()["reasons"] == [
        REASON_RATE_LIMITER_REDIS_NOT_CONFIGURED
    ]


def test_production_like_with_redis_url_is_not_degraded(monkeypatch):
    _configure(monkeypatch, environment="production", redis_url="redis://cache:6379/0")

    snap = get_runtime_degradation()

    assert snap["degraded"] is False
    assert snap["reasons"] == []
    assert snap["rate_limiter"]["memory_only_in_production_like"] is False
    assert snap["rate_limiter"]["backend"] == "redis"


@pytest.mark.parametrize("environment", ["development", "test", "local", ""])
def test_local_environment_without_redis_url_is_not_degraded(monkeypatch, environment):
    _configure(monkeypatch, environment=environment, redis_url="")

    snap = get_runtime_degradation()

    assert snap["degraded"] is False
    assert snap["reasons"] == []
    assert snap["rate_limiter"]["memory_only_in_production_like"] is False
    assert snap["rate_limiter"]["backend"] == "memory"


def test_whitespace_redis_url_counts_as_not_configured(monkeypatch):
    _configure(monkeypatch, environment="production", redis_url="   ")

    assert get_runtime_degradation()["degraded"] is True


def test_reasons_list_every_active_cause_in_a_stable_order(monkeypatch):
    _configure(monkeypatch, environment="production", redis_url="")
    mark_rate_limiter_memory_fallback()
    mark_durable_state_fell_back("redis")

    assert get_runtime_degradation()["reasons"] == [
        REASON_RATE_LIMITER_MEMORY_FALLBACK,
        REASON_RATE_LIMITER_REDIS_NOT_CONFIGURED,
        REASON_DURABLE_STATE_MEMORY_FALLBACK,
    ]


def test_stable_reason_codes_are_not_renamed():
    assert REASON_RATE_LIMITER_MEMORY_FALLBACK == "rate_limiter_memory_fallback"
    assert (
        REASON_RATE_LIMITER_REDIS_NOT_CONFIGURED == "rate_limiter_redis_not_configured"
    )
    assert REASON_DURABLE_STATE_MEMORY_FALLBACK == "durable_state_memory_fallback"


def test_startup_warning_is_logged_once_for_a_memory_only_limiter(monkeypatch, caplog):
    _configure(monkeypatch, environment="production", redis_url="")

    with caplog.at_level("WARNING", logger=_WARNING_LOGGER):
        first = warn_if_rate_limiter_memory_only()
        second = warn_if_rate_limiter_memory_only()
        third = warn_if_rate_limiter_memory_only()

    assert (first, second, third) == (True, False, False)
    messages = _memory_only_warnings(caplog)
    assert len(messages) == 1
    assert "REDIS_URL" in messages[0]
    assert "per process" in messages[0]
    assert not any(dash in messages[0] for dash in ("\u2014", "\u2013", " - "))


def test_no_startup_warning_when_redis_url_is_set(monkeypatch, caplog):
    _configure(monkeypatch, environment="production", redis_url="redis://cache:6379/0")

    with caplog.at_level("WARNING", logger=_WARNING_LOGGER):
        logged = warn_if_rate_limiter_memory_only()

    assert logged is False
    assert _memory_only_warnings(caplog) == []


def test_no_startup_warning_in_a_local_environment(monkeypatch, caplog):
    _configure(monkeypatch, environment="development", redis_url="")

    with caplog.at_level("WARNING", logger=_WARNING_LOGGER):
        logged = warn_if_rate_limiter_memory_only()

    assert logged is False
    assert _memory_only_warnings(caplog) == []


def test_startup_warning_can_fire_again_after_a_reset(monkeypatch, caplog):
    _configure(monkeypatch, environment="production", redis_url="")

    with caplog.at_level("WARNING", logger=_WARNING_LOGGER):
        assert warn_if_rate_limiter_memory_only() is True
        reset_runtime_degradation()
        assert warn_if_rate_limiter_memory_only() is True

    assert len(_memory_only_warnings(caplog)) == 2


@pytest.mark.anyio
async def test_health_dependencies_reports_memory_only_limiter_as_degraded(
    monkeypatch,
):
    _configure(monkeypatch, environment="production", redis_url="")

    report = await gather_dependency_report()

    assert report["runtime_degradation"]["degraded"] is True
    assert report["runtime_degradation"]["reasons"] == [
        REASON_RATE_LIMITER_REDIS_NOT_CONFIGURED
    ]
    assert report["status"] == "degraded"
    assert "runtime_degradation" in report["unhealthy"]


@pytest.mark.anyio
async def test_health_dependencies_endpoint_stays_200_when_limiter_is_memory_only(
    monkeypatch,
):
    """Degrading loudly must not change which deployments serve traffic."""
    _configure(monkeypatch, environment="production", redis_url="")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        resp = await http.get("/health/dependencies")

    assert resp.status_code == 200
    body = resp.json()
    assert body["runtime_degradation"]["reasons"] == [
        REASON_RATE_LIMITER_REDIS_NOT_CONFIGURED
    ]
    assert body["status"] == "degraded"


@pytest.mark.anyio
async def test_lifespan_warns_before_signing_key_validation(monkeypatch):
    """The startup warning is wired into the app lifespan, once per boot."""
    from app import main as main_module
    from app.services.signing_keys import SigningKeyError

    calls: list[str] = []
    monkeypatch.setattr(main_module, "validate_trust_mode_guardrails", lambda _s: None)
    monkeypatch.setattr(main_module, "validate_api_key_hashing_config", lambda: None)
    monkeypatch.setattr(main_module, "warn_if_trust_mode_permissive", lambda _s: None)
    monkeypatch.setattr(
        main_module,
        "warn_if_rate_limiter_memory_only",
        lambda: calls.append("memory_only_warning") or True,
    )

    def _stop_here(_settings):
        calls.append("signing_key_validation")
        raise SigningKeyError("stop_after_warning")

    monkeypatch.setattr(main_module, "validate_signing_key_configuration", _stop_here)

    with pytest.raises(SigningKeyError, match="stop_after_warning"):
        async with main_module.lifespan(main_module.app):
            pytest.fail("lifespan must stop at the sentinel")

    assert calls == ["memory_only_warning", "signing_key_validation"]
