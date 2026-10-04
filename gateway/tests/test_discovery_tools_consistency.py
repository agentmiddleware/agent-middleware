"""Discovery consistency: /v1/discover and /mcp/tools.json must agree on tool lists.

When ENABLE_PROOF_SURFACES=false, both endpoints should show the same set of
registered tools (dogfood/partner tools). When true, both should include those
plus proof-surface tools.

The bug: /v1/discover returned mcp_tools=[] while /mcp/tools.json advertised
partner.echo — a lie that breaks autonomous discovery.
"""

from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import app
from app.services.mcp_phase9_tools import sync_proof_surface_mcp_registration
from app.services.dogfood_tool import sync_dogfood_tool_registration


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def proof_surfaces_off(monkeypatch):
    """Force proof surfaces off and sync MCP stub registration."""
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


@pytest.fixture
def dogfood_on(monkeypatch):
    """Enable dogfood tool."""
    import app.routers.mcp as mcp_mod

    monkeypatch.setenv("ENABLE_DOGFOOD_TOOL", "true")
    get_settings.cache_clear()
    cfg = get_settings()
    assert cfg.ENABLE_DOGFOOD_TOOL is True
    monkeypatch.setattr(mcp_mod, "settings", cfg)
    sync_dogfood_tool_registration()
    yield
    monkeypatch.setenv("ENABLE_DOGFOOD_TOOL", "false")
    get_settings.cache_clear()
    restored = get_settings()
    monkeypatch.setattr(mcp_mod, "settings", restored)
    sync_dogfood_tool_registration()


@pytest.mark.anyio
async def test_discover_and_tools_json_agree_when_proof_surfaces_off(
    client, proof_surfaces_off, dogfood_on
):
    """When proof surfaces are off, /v1/discover and /mcp/tools.json must agree.

    Both should show dogfood tools (partner.notes.write) but not proof-surface
    stubs (awi_*, telemetry, etc.).
    """
    discover_resp = await client.get("/v1/discover")
    tools_json_resp = await client.get("/mcp/tools.json")

    assert discover_resp.status_code == 200
    assert tools_json_resp.status_code == 200

    discover_data = discover_resp.json()
    tools_json_data = tools_json_resp.json()

    # Extract tool names from both endpoints
    discover_tools = {tool["service_id"] for tool in discover_data["mcp_tools"]}
    tools_json_tools = {tool["name"] for tool in tools_json_data["tools"]}

    # They must agree
    assert discover_tools == tools_json_tools, (
        f"Discovery tools mismatch: /v1/discover has {discover_tools}, "
        f"/mcp/tools.json has {tools_json_tools}"
    )

    # When dogfood is on and proof surfaces are off, we should see the dogfood tool
    assert "partner.notes.write" in tools_json_tools
    assert "partner.notes.write" in discover_tools

    # But not proof-surface stubs
    assert not any(name.startswith("awi_") for name in tools_json_tools)
    assert not any(name.startswith("awi_") for name in discover_tools)
    assert "telemetry" not in tools_json_tools
    assert "telemetry" not in discover_tools


@pytest.mark.anyio
@pytest.mark.proof
async def test_discover_and_tools_json_agree_when_proof_surfaces_on(client):
    """When proof surfaces are on, /v1/discover and /mcp/tools.json must agree.

    Both should show proof-surface tools plus any registered dogfood tools.
    The suite default is ENABLE_PROOF_SURFACES=false; the ``proof`` marker
    makes the autouse conftest fixture turn the flag on, mount the proof
    routers and register the proof-surface MCP tools for this test only.
    """
    assert get_settings().ENABLE_PROOF_SURFACES is True

    sync_proof_surface_mcp_registration()

    discover_resp = await client.get("/v1/discover")
    tools_json_resp = await client.get("/mcp/tools.json")

    assert discover_resp.status_code == 200
    assert tools_json_resp.status_code == 200

    discover_data = discover_resp.json()
    tools_json_data = tools_json_resp.json()

    # Extract tool names
    discover_tools = {tool["service_id"] for tool in discover_data["mcp_tools"]}
    tools_json_tools = {tool["name"] for tool in tools_json_data["tools"]}

    # They must agree
    assert discover_tools == tools_json_tools, (
        f"Discovery tools mismatch: /v1/discover has {discover_tools}, "
        f"/mcp/tools.json has {tools_json_tools}"
    )

    # Proof surfaces should include AWI tools
    assert any(name.startswith("awi_") for name in tools_json_tools)
    assert any(name.startswith("awi_") for name in discover_tools)


@pytest.mark.anyio
async def test_discover_and_tools_json_both_empty_when_no_tools_registered(
    client, proof_surfaces_off
):
    """When no tools are registered and proof surfaces are off, both should agree.

    Note: This fixture has proof_surfaces_off but NOT dogfood_on.
    The registry may have leftover tools from other tests, but both endpoints
    must still agree on what they return.
    """
    discover_resp = await client.get("/v1/discover")
    tools_json_resp = await client.get("/mcp/tools.json")

    assert discover_resp.status_code == 200
    assert tools_json_resp.status_code == 200

    discover_data = discover_resp.json()
    tools_json_data = tools_json_resp.json()

    discover_tools = {tool["service_id"] for tool in discover_data["mcp_tools"]}
    tools_json_tools = {tool["name"] for tool in tools_json_data["tools"]}

    # Both must agree, even if the registry has leftover tools from other tests
    assert discover_tools == tools_json_tools, (
        f"Discovery tools mismatch: /v1/discover has {discover_tools}, "
        f"/mcp/tools.json has {tools_json_tools}"
    )

    # With proof surfaces off and no dogfood flag, we shouldn't see proof-surface stubs
    assert not any(name.startswith("awi_") for name in tools_json_tools)
    assert not any(name.startswith("awi_") for name in discover_tools)


@pytest.mark.anyio
async def test_llms_txt_discovery_auth_claim_matches_mcp_messages_requirement(client):
    """llms.txt must not say MCP auth is optional if POST /mcp/messages requires a key.

    The endpoint requires authentication (via get_auth_context dependency), so
    llms.txt must reflect that. Checked line by line rather than by proximity:
    the endpoints table row for /mcp/messages must start with "Required", and no line
    that names /mcp/messages or the X-API-Key header may call anything
    optional, wherever in the document it appears.
    """
    unauthenticated = await client.post(
        "/mcp/messages",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert unauthenticated.status_code == 401, unauthenticated.text

    resp = await client.get("/llms.txt")
    assert resp.status_code == 200
    lines = resp.text.splitlines()

    table_rows = [
        line
        for line in lines
        if line.lstrip().startswith("|") and "/mcp/messages" in line
    ]
    assert len(table_rows) == 1, (
        f"expected one endpoints-table row for /mcp/messages, got {table_rows}"
    )
    auth_cell = table_rows[0].strip().strip("|").split("|")[-1].strip()
    assert auth_cell.startswith("Required"), (
        f"llms.txt lists /mcp/messages auth as {auth_cell!r}: {table_rows[0]}"
    )

    optional_auth_lines = [
        line
        for line in lines
        if ("/mcp/messages" in line.lower() or "x-api-key" in line.lower())
        and "optional" in line.lower()
    ]
    assert not optional_auth_lines, (
        "llms.txt must not claim MCP auth is optional when /mcp/messages "
        f"requires it: {optional_auth_lines}"
    )
