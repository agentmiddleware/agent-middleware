"""Focused coverage for app.core.rate_limiter.

The threshold and abuse-bucket behavior already live in
test_rate_limit_threshold.py. This file covers the module's other main
public behavior in small fast cases: the discovery payload, the timeout
helper, bucket naming, the 429 shape, header output, per-key and anonymous
budgets, and the skip paths.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from app.core.auth import CREDENTIAL_REJECTED_HEADER
from app.core.rate_limiter import (
    RateLimitMiddleware,
    _api_key_bucket,
    _credentials_were_rejected,
    _rate_limited_response,
    enforce_redis_timeouts,
    rate_limit_discovery,
)


async def _ok(_request):
    return PlainTextResponse("ok")


def _limited_app(**kwargs) -> RateLimitMiddleware:
    starlette_app = Starlette(routes=[Route("/v1/wallets", _ok)])
    return RateLimitMiddleware(starlette_app, **kwargs)


def test_discovery_payload_shape() -> None:
    payload = rate_limit_discovery()
    assert payload["window_seconds"] == 60
    assert payload["scope"] == "per_api_key"
    assert payload["burst_allowance"] == 0
    assert payload["per_partner_override"] is False
    assert payload["headers"] == [
        "X-RateLimit-Limit",
        "X-RateLimit-Remaining",
        "X-RateLimit-Reset",
    ]


def test_enforce_redis_timeouts_updates_pool_kwargs() -> None:
    class _Pool:
        def __init__(self) -> None:
            self.connection_kwargs: dict = {}

    class _Client:
        def __init__(self) -> None:
            self.connection_pool = _Pool()

    client = _Client()
    assert enforce_redis_timeouts(client) is client
    assert client.connection_pool.connection_kwargs == {
        "socket_connect_timeout": 2.0,
        "socket_timeout": 2.0,
        "health_check_interval": 15,
    }


def test_enforce_redis_timeouts_without_pool_returns_client_unchanged() -> None:
    """Edge case: a client with no pool must pass through untouched."""
    client = object()
    assert enforce_redis_timeouts(client) is client


def test_api_key_bucket_names() -> None:
    assert _api_key_bucket(None) == "anonymous"
    first = _api_key_bucket("some-key")
    assert first == _api_key_bucket("some-key")
    assert first != _api_key_bucket("other-key")
    assert "some-key" not in first


def test_rate_limited_response_shape() -> None:
    response = _rate_limited_response(5, 42, "Slow down.")
    assert response.status_code == 429
    assert response.headers["X-RateLimit-Limit"] == "5"
    assert response.headers["X-RateLimit-Remaining"] == "0"
    assert response.headers["X-RateLimit-Reset"] == "42"
    assert response.headers["Retry-After"] == "42"


def test_credentials_rejected_strips_internal_marker() -> None:
    response = Response(
        "no", status_code=403, headers={CREDENTIAL_REJECTED_HEADER: "1"}
    )
    assert _credentials_were_rejected(response) is True
    assert CREDENTIAL_REJECTED_HEADER not in response.headers


def test_credentials_rejected_plain_401_without_marker() -> None:
    """Edge case: a 401 with no marker still counts as rejected."""
    response = Response("no", status_code=401)
    assert _credentials_were_rejected(response) is True


def test_credentials_rejected_clean_200() -> None:
    response = Response("ok", status_code=200)
    assert _credentials_were_rejected(response) is False


@pytest.mark.anyio
async def test_same_key_is_limited_and_headers_count_down() -> None:
    limited = _limited_app(requests_per_minute=2)
    transport = ASGITransport(app=limited)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        first = await http.get("/v1/wallets", headers={"X-API-Key": "key-a"})
        second = await http.get("/v1/wallets", headers={"X-API-Key": "key-a"})
        third = await http.get("/v1/wallets", headers={"X-API-Key": "key-a"})

    assert (first.status_code, second.status_code, third.status_code) == (200, 200, 429)
    assert first.headers["X-RateLimit-Limit"] == "2"
    assert first.headers["X-RateLimit-Remaining"] == "1"
    assert second.headers["X-RateLimit-Remaining"] == "0"
    body = third.json()["detail"]
    assert body["error"] == "rate_limited"
    assert body["retry_after_seconds"] >= 1


@pytest.mark.anyio
async def test_distinct_keys_get_distinct_budgets() -> None:
    limited = _limited_app(requests_per_minute=1)
    transport = ASGITransport(app=limited)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        first = await http.get("/v1/wallets", headers={"X-API-Key": "key-a"})
        second = await http.get("/v1/wallets", headers={"X-API-Key": "key-a"})
        other = await http.get("/v1/wallets", headers={"X-API-Key": "key-b"})

    assert first.status_code == 200
    assert second.status_code == 429
    assert other.status_code == 200


@pytest.mark.anyio
async def test_requests_without_a_key_share_the_anonymous_bucket() -> None:
    """Edge case: no key header at all still counts against one budget."""
    limited = _limited_app(requests_per_minute=2)
    transport = ASGITransport(app=limited)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        statuses = [(await http.get("/v1/wallets")).status_code for _ in range(3)]

    assert statuses == [200, 200, 429]


@pytest.mark.anyio
async def test_skip_paths_are_never_limited() -> None:
    async def health(_request):
        return PlainTextResponse("healthy")

    starlette_app = Starlette(routes=[Route("/health", health)])
    limited = RateLimitMiddleware(starlette_app, requests_per_minute=1)
    transport = ASGITransport(app=limited)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        statuses = [(await http.get("/health")).status_code for _ in range(3)]

    assert statuses == [200, 200, 200]
