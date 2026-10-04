"""CORS credentials must never be served with a wildcard/reflected origin.

Exercises both branches of ``add_cors_middleware`` directly (rather than relying
on the ambient ``CORS_ORIGINS`` env, which an allowlist in ``.env``/CI could make
the negative assertions pass vacuously):

* wildcard ``["*"]`` → ``Access-Control-Allow-Origin: *`` with credentials off,
  and an arbitrary Origin is never reflected with credentials.
* explicit allowlist → the allowed origin is reflected *with* credentials, and a
  non-allowed origin is not reflected.
"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.main import add_cors_middleware

EVIL_ORIGIN = "https://evil.example"
GOOD_ORIGIN = "https://good.example"


def _app_with_origins(origins: list[str]) -> FastAPI:
    application = FastAPI()
    add_cors_middleware(application, origins)

    @application.get("/ping")
    async def ping():  # pragma: no cover - trivial probe route
        return {"ok": True}

    return application


def _client(application: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=application), base_url="http://test")


@pytest.mark.anyio
async def test_wildcard_serves_star_without_credentials():
    async with _client(_app_with_origins(["*"])) as client:
        resp = await client.get("/ping", headers={"Origin": EVIL_ORIGIN})
    # Under a wildcard the origin is never echoed back as the caller's, and
    # credentials must be off.
    assert resp.headers.get("access-control-allow-origin") == "*"
    assert "access-control-allow-credentials" not in resp.headers


@pytest.mark.anyio
async def test_wildcard_preflight_is_not_credentialed():
    async with _client(_app_with_origins(["*"])) as client:
        resp = await client.options(
            "/ping",
            headers={
                "Origin": EVIL_ORIGIN,
                "Access-Control-Request-Method": "GET",
            },
        )
    assert resp.headers.get("access-control-allow-origin") != EVIL_ORIGIN
    assert resp.headers.get("access-control-allow-credentials") != "true"


@pytest.mark.anyio
async def test_explicit_allowlist_reflects_allowed_origin_with_credentials():
    async with _client(_app_with_origins([GOOD_ORIGIN])) as client:
        resp = await client.get("/ping", headers={"Origin": GOOD_ORIGIN})
    assert resp.headers.get("access-control-allow-origin") == GOOD_ORIGIN
    assert resp.headers.get("access-control-allow-credentials") == "true"


@pytest.mark.anyio
async def test_explicit_allowlist_does_not_reflect_other_origins():
    async with _client(_app_with_origins([GOOD_ORIGIN])) as client:
        resp = await client.get("/ping", headers={"Origin": EVIL_ORIGIN})
    assert resp.headers.get("access-control-allow-origin") != EVIL_ORIGIN
