"""HEAD support on public GET routes (RFC 9110 §9.3.2).

FastAPI's APIRoute registers only declared methods, so HEAD answered 405 on
every APIRoute-backed GET while plain Starlette routes (/openapi.json)
answered 200. HeadMethodMiddleware translates HEAD to GET at the ASGI
boundary and strips the body, so monitoring probes and link checkers get
the GET's status and headers everywhere.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
@pytest.mark.parametrize(
    "path",
    [
        "/health",
        "/health/ready",
        "/health/dependencies",
        "/.well-known/agent.json",
        "/llms.txt",
        "/openapi.json",
    ],
)
async def test_head_matches_get_status_with_empty_body(client, path):
    get_resp = await client.get(path)
    head_resp = await client.head(path)
    assert head_resp.status_code == get_resp.status_code == 200
    assert head_resp.content == b""


@pytest.mark.anyio
async def test_head_carries_get_headers(client):
    get_resp = await client.get("/health")
    head_resp = await client.head("/health")
    assert head_resp.headers.get("content-type") == get_resp.headers.get("content-type")
    # Content-Length reflects the body the corresponding GET would return —
    # exactly what RFC 9110 permits for HEAD.
    assert head_resp.headers.get("content-length") == get_resp.headers.get(
        "content-length"
    )
    # Outermost placement means hardening headers still stamp HEAD responses.
    assert head_resp.headers.get("x-content-type-options") == "nosniff"


@pytest.mark.anyio
async def test_head_unknown_path_is_404_not_405(client):
    resp = await client.head("/no/such/path")
    assert resp.status_code == 404
    assert resp.content == b""


@pytest.mark.anyio
async def test_head_on_post_only_route_stays_405(client):
    # /v1/billing/charge only answers POST; HEAD translates to GET, which is
    # not offered there either.
    resp = await client.head("/v1/billing/charge")
    assert resp.status_code == 405
    assert resp.content == b""


@pytest.mark.anyio
async def test_post_and_get_behavior_unchanged(client):
    get_resp = await client.get("/health")
    assert get_resp.status_code == 200
    assert get_resp.json()["status"] == "healthy"
