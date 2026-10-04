"""Direct coverage for the inbound request-body ceiling.

`tests/test_security_fuzz_battery.py` proves the limit holds on the governed
invoke path. This proves the middleware itself: what it refuses, what it lets
through untouched, and that a caller cannot talk its way past the ceiling by
lying about (or omitting) Content-Length.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.core.config import get_settings
from app.main import app as real_app
from app.middleware.request_body_limit import RequestBodyLimitMiddleware

LIMIT = 1024


async def _echo(request):
    """Read the whole body, so a replayed stream must arrive intact."""
    body = await request.body()
    return JSONResponse({"length": len(body)})


@pytest.fixture
def limited_app() -> Starlette:
    application = Starlette(
        routes=[
            Route("/echo", _echo, methods=["POST", "PUT", "PATCH", "DELETE"]),
            Route("/echo-get", _echo, methods=["GET"]),
        ]
    )
    application.add_middleware(RequestBodyLimitMiddleware, max_body_size=LIMIT)
    return application


@pytest.fixture
def limited_client(limited_app):
    return AsyncClient(transport=ASGITransport(app=limited_app), base_url="http://test")


@pytest.mark.anyio
async def test_body_at_the_limit_is_delivered_intact(limited_client):
    """A body exactly at the ceiling passes, and the app sees every byte."""
    async with limited_client as client:
        r = await client.post("/echo", content=b"x" * LIMIT)
    assert r.status_code == 200
    assert r.json()["length"] == LIMIT


@pytest.mark.anyio
async def test_body_over_the_limit_is_refused(limited_client):
    async with limited_client as client:
        r = await client.post("/echo", content=b"x" * (LIMIT + 1))
    assert r.status_code == 413
    assert r.json()["max_request_body_bytes"] == LIMIT


@pytest.mark.anyio
async def test_declared_content_length_is_refused_without_reading_the_body(
    limited_app,
):
    """An oversized Content-Length is refused before the body is consumed.

    The fast path is the one that matters under load: a caller announcing a
    1 GB body should cost the gateway a header parse, not a 1 GB read. The
    receive channel is rigged to fail the test if it is ever pulled.
    """
    pulled = False

    async def receive():
        nonlocal pulled
        pulled = True
        return {"type": "http.request", "body": b"", "more_body": False}

    sent = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/echo",
        "headers": [
            (b"content-length", str(1024**3).encode()),
            (b"host", b"test"),
        ],
        "query_string": b"",
    }
    await RequestBodyLimitMiddleware(limited_app, LIMIT)(scope, receive, send)

    assert sent[0]["status"] == 413
    assert not pulled, "oversized body was read despite a declared Content-Length"


@pytest.mark.anyio
async def test_understated_content_length_cannot_smuggle_a_large_body(limited_app):
    """A truthful Content-Length is not assumed; the real stream is measured."""
    chunks = [
        {"type": "http.request", "body": b"x" * LIMIT, "more_body": True},
        {"type": "http.request", "body": b"x" * LIMIT, "more_body": False},
    ]

    async def receive():
        return chunks.pop(0)

    sent = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/echo",
        # Claims to fit, then sends twice the ceiling.
        "headers": [(b"content-length", b"10"), (b"host", b"test")],
        "query_string": b"",
    }
    await RequestBodyLimitMiddleware(limited_app, LIMIT)(scope, receive, send)

    assert sent[0]["status"] == 413


@pytest.mark.anyio
async def test_malformed_content_length_falls_through_to_the_stream_check(
    limited_client,
):
    """A junk header is not itself proof of size; the body still decides."""
    async with limited_client as client:
        under = await client.post(
            "/echo", content=b"x" * 10, headers={"Content-Length": "not-a-number"}
        )
        over = await client.post(
            "/echo",
            content=b"x" * (LIMIT + 1),
            headers={"Content-Length": "not-a-number"},
        )
    assert under.status_code == 200
    assert over.status_code == 413


@pytest.mark.anyio
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_every_body_bearing_method_is_bounded(limited_client, method):
    async with limited_client as client:
        r = await client.request(method, "/echo", content=b"x" * (LIMIT + 1))
    assert r.status_code == 413


@pytest.mark.anyio
async def test_get_is_passed_through_unbuffered(limited_app):
    """A GET has no body to bound and must not be intercepted at all."""
    wrapped = []

    async def spy(scope, receive, send):
        wrapped.append(receive)
        await limited_app(scope, receive, send)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/echo-get",
        "headers": [(b"host", b"test")],
        "query_string": b"",
    }
    await RequestBodyLimitMiddleware(spy, LIMIT)(scope, receive, send)

    assert wrapped == [receive], "GET receive channel was wrapped"


@pytest.mark.anyio
async def test_non_http_scopes_are_passed_through(limited_app):
    """Lifespan and websocket scopes have no `method`; they must not KeyError."""
    seen = []

    async def spy(scope, receive, send):
        seen.append(scope["type"])

    async def noop():
        return {}

    async def send(message):
        pass

    for scope_type in ("lifespan", "websocket"):
        await RequestBodyLimitMiddleware(spy, LIMIT)({"type": scope_type}, noop, send)
    assert seen == ["lifespan", "websocket"]


def test_the_real_app_registers_the_limit_from_settings():
    """The ceiling is wired into the shipped app, not just available to it."""
    limits = [
        middleware
        for middleware in real_app.user_middleware
        if middleware.cls is RequestBodyLimitMiddleware
    ]
    assert len(limits) == 1
    assert limits[0].kwargs["max_body_size"] == get_settings().MAX_REQUEST_BODY_BYTES
