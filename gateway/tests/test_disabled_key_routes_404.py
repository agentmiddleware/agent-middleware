"""Disabled dev-key and demo-key routes look exactly like unknown paths (#357).

A handler or dependency check runs after FastAPI has read the body. So with
``ENABLE_DEV_KEY_SELF_PROVISION=false`` an invalid or malformed body still got
a 422 that spelled out the request schema. With ``ENABLE_DEMO_TENANT=false``,
``GET /v1/demo/keys`` still returned POST instructions and a curl example.
Both routes now fail to match while their flag is off, so every request gets
the router's own 404, the same one an unknown path gets.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import app

UNKNOWN_PATH = "/v1/route-that-never-existed"

DEV_KEY_PATH = "/v1/dev-keys/self-provision"
DEMO_KEYS_PATH = "/v1/demo/keys"

# (method, json body, raw content). Each one is a request that leaked
# the route before: a schema-invalid body, malformed JSON, a valid body, and
# methods the route does not define (which used to answer 405).
_REQUESTS = [
    ("POST", {"agent_id": "bad id!"}, None),
    ("POST", {"budget_credits": -1}, None),
    ("POST", {"utm_source": ["not", "a", "string"]}, None),
    ("POST", None, b"{not json"),
    ("POST", {}, None),
    ("GET", None, None),
    ("PUT", {}, None),
    ("DELETE", None, None),
]


@pytest.fixture
async def client(clean_database):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"X-API-Key": "test-key"},
    ) as c:
        yield c


@pytest.fixture
def flags_off():
    settings = get_settings()
    saved = {
        name: getattr(settings, name)
        for name in ("ENABLE_DEV_KEY_SELF_PROVISION", "ENABLE_DEMO_TENANT")
    }
    settings.ENABLE_DEV_KEY_SELF_PROVISION = False
    settings.ENABLE_DEMO_TENANT = False
    try:
        yield settings
    finally:
        for name, value in saved.items():
            setattr(settings, name, value)


async def _send(client: AsyncClient, method: str, path: str, body, content):
    kwargs: dict[str, object] = {}
    if content is not None:
        kwargs["content"] = content
        kwargs["headers"] = {"Content-Type": "application/json"}
    elif body is not None:
        kwargs["json"] = body
    return await client.request(method, path, **kwargs)


@pytest.mark.anyio
@pytest.mark.parametrize("path", [DEV_KEY_PATH, DEMO_KEYS_PATH])
@pytest.mark.parametrize(("method", "body", "content"), _REQUESTS)
async def test_disabled_route_answers_like_an_unknown_path(
    client: AsyncClient, flags_off, path, method, body, content
):
    disabled = await _send(client, method, path, body, content)
    unknown = await _send(client, method, UNKNOWN_PATH, body, content)

    assert disabled.status_code == 404
    assert unknown.status_code == 404
    assert disabled.json() == unknown.json()
    assert disabled.headers.get("content-type") == unknown.headers.get("content-type")
    assert "allow" not in disabled.headers


@pytest.mark.anyio
async def test_disabled_demo_route_skips_its_cors_policy(
    client: AsyncClient, flags_off
):
    origin = get_settings().DEMO_ALLOWED_ORIGINS.split(",")[0].strip()
    if "://" not in origin:
        origin = "https://" + origin
    headers = {
        "Origin": origin,
        "Access-Control-Request-Method": "POST",
    }
    disabled = await client.options(DEMO_KEYS_PATH, headers=headers)
    unknown = await client.options(UNKNOWN_PATH, headers=headers)
    assert disabled.status_code == unknown.status_code
    assert disabled.headers.get("access-control-allow-methods") == (
        unknown.headers.get("access-control-allow-methods")
    )


@pytest.mark.anyio
async def test_enabled_routes_still_match(client: AsyncClient, flags_off):
    flags_off.ENABLE_DEV_KEY_SELF_PROVISION = True
    flags_off.ENABLE_DEMO_TENANT = True
    flags_off_env = flags_off.ENVIRONMENT
    flags_off.ENVIRONMENT = "local"
    try:
        invalid = await client.post(DEV_KEY_PATH, json={"agent_id": "bad id!"})
        assert invalid.status_code == 422
        help_resp = await client.get(DEMO_KEYS_PATH)
        assert help_resp.status_code == 200
        assert help_resp.json()["post"]["path"] == DEMO_KEYS_PATH
    finally:
        flags_off.ENVIRONMENT = flags_off_env


def test_disabled_routes_are_not_in_openapi():
    """Both flags default off, so the app imported for the suite does not
    advertise either issuance route. The admin funnel stays documented."""
    settings = get_settings()
    assert settings.ENABLE_DEV_KEY_SELF_PROVISION is False
    assert settings.ENABLE_DEMO_TENANT is False
    paths = app.openapi()["paths"]
    assert DEV_KEY_PATH not in paths
    assert DEMO_KEYS_PATH not in paths
    assert "/v1/admin/demo-keys/funnel" in paths
