"""Phase 7: canonical_api + partner key bootstrap discovery honesty."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import app
from app.routers.well_known import _build_agent_manifest


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
async def test_agent_json_canonical_api_from_public_url(client, monkeypatch):
    public = "https://api.thisisatest.tech"
    monkeypatch.setenv("PUBLIC_URL", public)
    get_settings.cache_clear()
    try:
        built = _build_agent_manifest().model_dump(mode="json")
        assert built["canonical_api"] == public
        resp = await client.get("/.well-known/agent.json")
        assert resp.status_code == 200
        data = resp.json()
        assert data["canonical_api"] == public
        assert data["authentication"]["public_self_serve"] is False
        assert (
            data["authentication"]["bootstrap_docs"]
            == "/docs/partner-api-key-bootstrap.md"
        )
        assert (
            data["documentation"]["partner_api_key_bootstrap"]
            == "/docs/partner-api-key-bootstrap.md"
        )
    finally:
        get_settings.cache_clear()


def test_canonical_api_empty_when_public_url_unset(monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "")
    get_settings.cache_clear()
    try:
        built = _build_agent_manifest().model_dump(mode="json")
        assert built["canonical_api"] == ""
    finally:
        get_settings.cache_clear()


@pytest.mark.anyio
async def test_partner_api_key_bootstrap_doc_is_served(client):
    resp = await client.get("/docs/partner-api-key-bootstrap.md")
    assert resp.status_code == 200
    assert "public self-serve" in resp.text.lower()
    assert "partner_api_key_bootstrap.py" in resp.text
    assert "DELETE" in resp.text
    assert "/v1/api-keys/" in resp.text


def test_partner_bootstrap_rejects_cleartext_remote_url():
    from scripts.partner_api_key_bootstrap import _require_safe_api_url

    assert _require_safe_api_url("https://api.example.com") == "https://api.example.com"
    assert _require_safe_api_url("http://127.0.0.1:8000") == "http://127.0.0.1:8000"
    try:
        _require_safe_api_url("http://api.example.com")
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "https://" in str(exc)
