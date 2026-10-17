"""CORS credentials must never be served with a wildcard/reflected origin.

Exercises both branches of ``add_cors_middleware`` directly (rather than relying
on the ambient ``CORS_ORIGINS`` env, which an allowlist in ``.env``/CI could make
the negative assertions pass vacuously):

* wildcard ``["*"]`` → ``Access-Control-Allow-Origin: *`` with credentials off,
  and an arbitrary Origin is never reflected with credentials.
* explicit allowlist → the allowed origin is reflected *with* credentials, and a
  non-allowed origin is not reflected.
"""

import base64

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.core.trust_mode import TrustModeGuardrailError, validate_trust_mode_config
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


def test_default_cors_origins_allow_no_browser_origin():
    """A fresh process must not open every browser origin.

    The field default is what boots when CORS_ORIGINS is unset. Wildcard
    here would let any website read cross-origin responses, and a later
    cookie would turn that into a credentialed read.
    """
    assert Settings.model_fields["CORS_ORIGINS"].default == ""


@pytest.mark.anyio
async def test_empty_origins_grant_no_cross_origin_access():
    """No configured origins means no CORS grant, including credentials.

    Starlette stamps Access-Control-Allow-Credentials on every Origin
    request when allow_credentials is true, even if the origin itself is
    not allowed. An empty list must not do that.
    """
    async with _client(_app_with_origins([])) as client:
        resp = await client.get("/ping", headers={"Origin": EVIL_ORIGIN})
    assert "access-control-allow-origin" not in resp.headers
    assert "access-control-allow-credentials" not in resp.headers


_SIGNING_KEY = base64.b64encode(bytes(range(32))).decode()
_POSTGRES = "postgresql+asyncpg://user:pw@db.internal:5432/trust"


def _production_cors(**overrides: str) -> dict:
    values = {
        "environment": "production",
        "trust_mode_enabled": True,
        "signing_private_key_b64": _SIGNING_KEY,
        "allow_legacy_unpermitted_mcp": False,
        "enable_proof_surfaces": False,
        "debug": False,
        "webauthn_allow_mock": False,
        "database_url": _POSTGRES,
        "cors_origins": "https://app.example.com",
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    "cors_origins",
    ["*", " * ", "*,https://app.example.com", "https://app.example.com,*"],
)
def test_production_refuses_wildcard_cors(cors_origins: str):
    with pytest.raises(TrustModeGuardrailError, match="CORS_ORIGINS"):
        validate_trust_mode_config(**_production_cors(cors_origins=cors_origins))


def test_production_accepts_explicit_cors_origins():
    validate_trust_mode_config(
        **_production_cors(
            cors_origins="https://app.example.com, https://admin.example.com"
        )
    )


def test_production_accepts_empty_cors_origins():
    """An API with no browser client is a valid explicit choice."""
    validate_trust_mode_config(**_production_cors(cors_origins=""))


def test_local_still_allows_wildcard_cors():
    validate_trust_mode_config(
        environment="local",
        trust_mode_enabled=True,
        signing_private_key_b64="",
        allow_legacy_unpermitted_mcp=True,
        cors_origins="*",
    )


@pytest.mark.anyio
async def test_lifespan_refuses_wildcard_cors_in_production(monkeypatch):
    """The boot path must refuse '*', not only the helper called in isolation."""
    import app.main as main_module

    monkeypatch.setattr(main_module.settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(main_module.settings, "CORS_ORIGINS", "*")

    with pytest.raises(TrustModeGuardrailError, match="CORS_ORIGINS"):
        async with main_module.lifespan(main_module.app):
            pytest.fail("lifespan must not start with wildcard CORS in production")
