"""Focused coverage for the well-known discovery router.

Covers the agent bootstrap manifest, the gated AWI manifest, the public
signing-key publications (trust-keys and JWKS), and the MCP tools mirror,
including the unavailable-key and unmounted-manifest error shapes.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers import well_known
from app.routers.well_known import (
    PRODUCT_CAPABILITIES,
    _b64_to_b64url,
    build_awi_manifest,
    get_agent_first_metadata,
)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _fake_key(**overrides):
    raw = bytes(range(32))
    attrs = {
        "key_id": "test-kid-1",
        "alg": "Ed25519",
        "status": "active",
        "public_key_b64": base64.b64encode(raw).decode(),
        "created_at": None,
        "activated_at": None,
        "retired_at": None,
    }
    attrs.update(overrides)
    return SimpleNamespace(**attrs)


class _FakeKeyService:
    def __init__(self, keys=None, error=None):
        self._keys = keys
        self._error = error

    async def list_public_keys(self):
        if self._error is not None:
            raise self._error
        return self._keys


def _patch_key_service(monkeypatch, service):
    monkeypatch.setattr(well_known, "get_signing_key_service", lambda: service)


# --- agent.json ---


@pytest.mark.anyio
async def test_agent_manifest_publishes_wedge_only_by_default(client):
    resp = await client.get("/.well-known/agent.json")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert body["name"] == "agent-middleware-api"
    assert body["capabilities"] == PRODUCT_CAPABILITIES
    assert body["endpoints"]["trust_keys"] == "/.well-known/trust-keys.json"
    assert body["endpoints"]["keys"] == "/v1/signing-keys"
    # Proof surfaces are unmounted in the default test posture.
    assert body["proof_surfaces"] == []
    assert "/.well-known/awi.json" not in body["agent_first"]["bootstrap_sequence"]
    assert body["agent_first"]["proof_surfaces_enabled"] is False


def test_agent_first_metadata_bootstrap_includes_awi_when_enabled(monkeypatch):
    from app.core.config import get_settings

    cfg = get_settings()
    monkeypatch.setattr(cfg, "ENABLE_PROOF_SURFACES", True)
    metadata = get_agent_first_metadata()
    assert metadata["bootstrap_sequence"][1] == "/.well-known/awi.json"
    assert metadata["proof_surfaces_enabled"] is True
    assert "proof_surface_note" in metadata


# --- awi.json gating (edge case: unmounted by default) ---


@pytest.mark.anyio
async def test_awi_manifest_unmounted_returns_404(client):
    resp = await client.get("/.well-known/awi.json")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "awi_manifest_unmounted"
    assert "ENABLE_PROOF_SURFACES=false" in body["detail"]


def test_build_awi_manifest_shape():
    manifest = build_awi_manifest()
    assert manifest["status"] == "draft"
    assert manifest["surface"] == "proof_surface"
    assert manifest["endpoints"]["execute"] == "/v1/awi/execute"
    assert isinstance(manifest["actions"], list)
    assert "X-Permit-Id" in manifest["http_authorization"]["required_headers"]
    assert manifest["known_limitations"]


# --- key encoding helper ---


def test_b64_to_b64url_rejects_invalid_input():
    assert _b64_to_b64url("!!!not-base64!!!") is None
    assert _b64_to_b64url("") is None


def test_b64_to_b64url_round_trip_has_no_padding():
    raw = bytes(range(32))
    encoded = _b64_to_b64url(base64.b64encode(raw).decode())
    assert encoded is not None
    assert "=" not in encoded


# --- trust-keys.json ---


@pytest.mark.anyio
async def test_trust_keys_serves_jwk_mirror(client, monkeypatch):
    _patch_key_service(monkeypatch, _FakeKeyService(keys=[_fake_key()]))
    resp = await client.get("/.well-known/trust-keys.json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["alg"] == "Ed25519"
    (entry,) = body["keys"]
    assert entry["kid"] == "test-kid-1"
    assert entry["jwk"]["kty"] == "OKP"
    assert entry["jwk"]["crv"] == "Ed25519"


@pytest.mark.anyio
async def test_trust_keys_empty_store_is_503_not_empty_list(client, monkeypatch):
    _patch_key_service(monkeypatch, _FakeKeyService(keys=[]))
    resp = await client.get("/.well-known/trust-keys.json")
    assert resp.status_code == 503
    assert resp.json()["error"] == "trust_keys_unavailable"


@pytest.mark.anyio
async def test_trust_keys_backend_error_is_503(client, monkeypatch):
    from app.services.signing_keys import SigningKeyError

    _patch_key_service(monkeypatch, _FakeKeyService(error=SigningKeyError("db down")))
    resp = await client.get("/.well-known/trust-keys.json")
    assert resp.status_code == 503
    assert resp.json()["error"] == "trust_keys_unavailable"


# --- jwks.json ---


@pytest.mark.anyio
async def test_jwks_serves_key_set(client, monkeypatch):
    _patch_key_service(monkeypatch, _FakeKeyService(keys=[_fake_key()]))
    resp = await client.get("/.well-known/jwks.json")
    assert resp.status_code == 200
    (jwk,) = resp.json()["keys"]
    assert jwk["kid"] == "test-kid-1"
    assert jwk["use"] == "sig"


@pytest.mark.anyio
async def test_jwks_empty_store_is_503(client, monkeypatch):
    _patch_key_service(monkeypatch, _FakeKeyService(keys=[]))
    resp = await client.get("/.well-known/jwks.json")
    assert resp.status_code == 503
    assert resp.json()["error"] == "trust_keys_unavailable"


# --- mcp tools mirror ---


@pytest.mark.anyio
async def test_mcp_tools_mirror_is_served(client):
    resp = await client.get("/.well-known/mcp/tools.json")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
