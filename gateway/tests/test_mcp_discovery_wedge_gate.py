"""Phase 2: MCP discovery honesty when ENABLE_PROOF_SURFACES=false."""

from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import app
from app.services.mcp_phase9_tools import (
    DEFAULT_MCP_STUB_SERVICE_IDS,
    PROOF_SURFACE_MCP_STUB_IDS,
    sync_proof_surface_mcp_registration,
)
from tests.test_trust_helpers import provision_agent_wallet


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def proof_surfaces_off(monkeypatch):
    """Force proof surfaces off and sync MCP stub registration.

    Clears the settings cache and rebinds module-level ``settings`` aliases so
    this stays honest after other tests call ``get_settings.cache_clear()``.
    """
    import app.main as main_mod
    import app.routers.discover as discover_mod
    import app.routers.well_known as well_known_mod

    original_flag = os.environ.get("ENABLE_PROOF_SURFACES")
    monkeypatch.setenv("ENABLE_PROOF_SURFACES", "false")
    get_settings.cache_clear()
    cfg = get_settings()
    assert cfg.ENABLE_PROOF_SURFACES is False
    monkeypatch.setattr(main_mod, "settings", cfg)
    monkeypatch.setattr(discover_mod, "settings", cfg)
    monkeypatch.setattr(well_known_mod, "settings", cfg)
    sync_proof_surface_mcp_registration()
    yield
    # Rebuild the cached settings from the flag as it stood before this
    # fixture, not from a hard-coded value: restoring "true" here left every
    # later test in the session reading ENABLE_PROOF_SURFACES=True from the
    # settings cache, after monkeypatch had already put the env var back.
    if original_flag is None:
        monkeypatch.delenv("ENABLE_PROOF_SURFACES", raising=False)
    else:
        monkeypatch.setenv("ENABLE_PROOF_SURFACES", original_flag)
    get_settings.cache_clear()
    restored = get_settings()
    monkeypatch.setattr(main_mod, "settings", restored)
    monkeypatch.setattr(discover_mod, "settings", restored)
    monkeypatch.setattr(well_known_mod, "settings", restored)
    sync_proof_surface_mcp_registration()


@pytest.mark.anyio
async def test_tools_json_omits_phase9_and_marketplace_stubs_when_proof_off(
    client, proof_surfaces_off
):
    resp = await client.get("/mcp/tools.json")
    assert resp.status_code == 200
    names = {tool["name"] for tool in resp.json()["tools"]}
    assert names.isdisjoint(PROOF_SURFACE_MCP_STUB_IDS)
    assert not any(name.startswith("awi_") for name in names)
    assert names.isdisjoint(DEFAULT_MCP_STUB_SERVICE_IDS)


@pytest.mark.anyio
async def test_well_known_tools_json_matches_gate(client, proof_surfaces_off):
    well_known = await client.get("/.well-known/mcp/tools.json")
    canonical = await client.get("/mcp/tools.json")
    assert well_known.status_code == 200
    assert canonical.status_code == 200
    wk_names = {t["name"] for t in well_known.json()["tools"]}
    can_names = {t["name"] for t in canonical.json()["tools"]}
    assert wk_names == can_names
    assert wk_names.isdisjoint(PROOF_SURFACE_MCP_STUB_IDS)


@pytest.mark.anyio
async def test_invoke_unregistered_awi_stub_not_found(
    client, proof_surfaces_off, clean_database
):
    provisioned = await provision_agent_wallet(client)
    resp = await client.post(
        "/mcp/messages",
        json={
            "jsonrpc": "2.0",
            "id": "missing-stub",
            "method": "tools/call",
            "params": {
                "name": "awi_passkey_challenge",
                "arguments": {"session_id": "s1", "action": "checkout"},
                "mcpContext": {"wallet_id": provisioned["agent_wallet_id"]},
            },
        },
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert "error" in payload
    assert "Tool not found" in payload["error"]["message"]


@pytest.mark.anyio
async def test_root_and_discover_hide_unmounted_proof_services(
    client, proof_surfaces_off
):
    root = await client.get("/")
    assert root.status_code == 200
    root_data = root.json()
    assert set(root_data["services"].keys()) == {"agent_billing", "mcp_server"}
    assert root_data["surface_boundaries"]["proof_surfaces_mounted"] is False
    assert "/.well-known/awi.json" not in root_data["agent_first"]["bootstrap_sequence"]

    # The surviving agent_billing service entry must list only routes the
    # wedge posture actually mounts — advertising an endpoint that answers
    # 404 is the drift this gate exists to prevent.
    from tests.conftest import iter_routes

    billing_endpoints = root_data["services"]["agent_billing"]["endpoints"]
    mounted_paths = {getattr(route, "path", "") for route in iter_routes(app.routes)}
    for entry in billing_endpoints:
        _method, _, path = entry.partition(" ")
        assert path in mounted_paths, f"root advertises unmounted {entry}"
    for gated in (
        "/v1/billing/wallets/child",
        "/v1/billing/wallets/{wallet_id}/swarm",
        "/v1/billing/top-up/prepare",
        "/v1/billing/arbitrage",
        "/v1/billing/alerts",
    ):
        assert not any(
            path == gated
            for _m, _, path in (e.partition(" ") for e in billing_endpoints)
        )

    discover = await client.get("/v1/discover")
    assert discover.status_code == 200
    discover_data = discover.json()
    # The wedge response model omits the field entirely — the public contract
    # carries no proof-surface vocabulary when those routes are unmounted.
    assert "awi_endpoints" not in discover_data
    # mcp_tools may include registered dogfood/partner tools, but should not
    # include proof-surface stubs (awi_*, telemetry, etc.)
    mcp_tool_names = {tool["service_id"] for tool in discover_data["mcp_tools"]}
    assert not any(name.startswith("awi_") for name in mcp_tool_names)
    assert "telemetry" not in mcp_tool_names
    assert all(c["surface"] == "product" for c in discover_data["capabilities"])


@pytest.mark.anyio
async def test_awi_manifest_404_when_proof_off(client, proof_surfaces_off):
    resp = await client.get("/.well-known/awi.json")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_llm_txt_uses_public_url_and_wedge_quickstart(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "PUBLIC_URL", "https://api.thisisatest.tech")
    resp = await client.get("/llm.txt")
    assert resp.status_code == 200
    text = resp.text
    assert "https://api.thisisatest.tech" in text
    assert "**Base URL:** http://localhost:8000" not in text
    assert "partner.notes.write" in text
    assert "POST /v1/telemetry/events" not in text
    assert "{{PUBLIC_URL}}" not in text


@pytest.mark.anyio
@pytest.mark.proof
async def test_tools_json_still_lists_awi_when_proof_on(client):
    """With proof surfaces on, the stubs the off-gate hides come back.

    The suite default is ENABLE_PROOF_SURFACES=false; the ``proof`` marker
    makes the autouse conftest fixture turn the flag on for this test only.
    """
    assert get_settings().ENABLE_PROOF_SURFACES is True
    sync_proof_surface_mcp_registration()
    resp = await client.get("/mcp/tools.json")
    assert resp.status_code == 200
    names = {tool["name"] for tool in resp.json()["tools"]}
    assert "awi_passkey_challenge" in names
    assert "data-indexer" in names
