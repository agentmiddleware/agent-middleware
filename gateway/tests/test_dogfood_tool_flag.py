"""Opt-in dogfood tool: ENABLE_DOGFOOD_TOOL gates partner.notes.write."""

from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import app
from app.services.dogfood_tool import (
    DOGFOOD_TOOL_ID,
    sync_dogfood_tool_registration,
)
from app.services.mcp_phase9_tools import (
    DEFAULT_MCP_STUB_SERVICE_IDS,
    PROOF_SURFACE_MCP_STUB_IDS,
    sync_proof_surface_mcp_registration,
)
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _bind_settings_aliases(cfg) -> dict:
    import app.main as main_mod
    import app.routers.discover as discover_mod
    import app.routers.mcp as mcp_mod
    import app.routers.well_known as well_known_mod

    previous = {
        "main": main_mod.settings,
        "discover": discover_mod.settings,
        "well_known": well_known_mod.settings,
        "mcp": mcp_mod.settings,
    }
    main_mod.settings = cfg
    discover_mod.settings = cfg
    well_known_mod.settings = cfg
    mcp_mod.settings = cfg
    return previous


def _restore_settings_aliases(previous: dict) -> None:
    import app.main as main_mod
    import app.routers.discover as discover_mod
    import app.routers.mcp as mcp_mod
    import app.routers.well_known as well_known_mod

    main_mod.settings = previous["main"]
    discover_mod.settings = previous["discover"]
    well_known_mod.settings = previous["well_known"]
    mcp_mod.settings = previous["mcp"]


@pytest.fixture
def dogfood_tool_off(monkeypatch):
    """Force dogfood tool off; keep proof surfaces off to avoid stub pollution."""
    saved = {
        "ENABLE_DOGFOOD_TOOL": os.environ.get("ENABLE_DOGFOOD_TOOL"),
        "ENABLE_PROOF_SURFACES": os.environ.get("ENABLE_PROOF_SURFACES"),
    }
    monkeypatch.setenv("ENABLE_DOGFOOD_TOOL", "false")
    monkeypatch.setenv("ENABLE_PROOF_SURFACES", "false")
    get_settings.cache_clear()
    cfg = get_settings()
    assert cfg.ENABLE_DOGFOOD_TOOL is False
    assert cfg.ENABLE_PROOF_SURFACES is False
    previous = _bind_settings_aliases(cfg)
    sync_proof_surface_mcp_registration()
    sync_dogfood_tool_registration()
    yield cfg
    for key, old in saved.items():
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old
    get_settings.cache_clear()
    _restore_settings_aliases(previous)
    restored = get_settings()
    _bind_settings_aliases(restored)
    sync_proof_surface_mcp_registration()
    sync_dogfood_tool_registration()


@pytest.fixture
def dogfood_tool_on(monkeypatch):
    """Enable dogfood tool with proof surfaces still off."""
    saved = {
        "ENABLE_DOGFOOD_TOOL": os.environ.get("ENABLE_DOGFOOD_TOOL"),
        "ENABLE_PROOF_SURFACES": os.environ.get("ENABLE_PROOF_SURFACES"),
    }
    monkeypatch.setenv("ENABLE_DOGFOOD_TOOL", "true")
    monkeypatch.setenv("ENABLE_PROOF_SURFACES", "false")
    get_settings.cache_clear()
    cfg = get_settings()
    assert cfg.ENABLE_DOGFOOD_TOOL is True
    assert cfg.ENABLE_PROOF_SURFACES is False
    previous = _bind_settings_aliases(cfg)
    sync_proof_surface_mcp_registration()
    sync_dogfood_tool_registration()
    yield cfg
    for key, old in saved.items():
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old
    get_settings.cache_clear()
    _restore_settings_aliases(previous)
    restored = get_settings()
    _bind_settings_aliases(restored)
    sync_proof_surface_mcp_registration()
    sync_dogfood_tool_registration()


@pytest.mark.anyio
async def test_tools_json_omits_dogfood_when_flag_off(client, dogfood_tool_off):
    resp = await client.get("/mcp/tools.json")
    assert resp.status_code == 200
    names = {tool["name"] for tool in resp.json()["tools"]}
    assert DOGFOOD_TOOL_ID not in names
    assert names.isdisjoint(PROOF_SURFACE_MCP_STUB_IDS)
    assert names.isdisjoint(DEFAULT_MCP_STUB_SERVICE_IDS)


@pytest.mark.anyio
async def test_tools_json_lists_dogfood_when_flag_on(client, dogfood_tool_on):
    resp = await client.get("/mcp/tools.json")
    assert resp.status_code == 200
    tools = resp.json()["tools"]
    names = {tool["name"] for tool in tools}
    assert DOGFOOD_TOOL_ID in names
    assert names.isdisjoint(PROOF_SURFACE_MCP_STUB_IDS)
    dogfood = next(t for t in tools if t["name"] == DOGFOOD_TOOL_ID)
    annotations = dogfood.get("annotations") or {}
    assert annotations.get("requirePermit") is True


@pytest.mark.anyio
async def test_well_known_tools_json_matches_dogfood_gate(client, dogfood_tool_on):
    well_known = await client.get("/.well-known/mcp/tools.json")
    canonical = await client.get("/mcp/tools.json")
    assert well_known.status_code == 200
    assert canonical.status_code == 200
    wk_names = {t["name"] for t in well_known.json()["tools"]}
    can_names = {t["name"] for t in canonical.json()["tools"]}
    assert wk_names == can_names
    assert DOGFOOD_TOOL_ID in can_names


@pytest.mark.anyio
async def test_health_reports_dogfood_flag(client, dogfood_tool_on):
    """Operator truth keeps the flag; the public wedge payload omits it.

    /mcp/tools.json remains the authoritative public truth for whether the
    dogfood tool exists — the health flag is operator telemetry.
    """
    from app.core.health import gather_dependency_report

    report = await gather_dependency_report()
    assert report["enable_dogfood_tool"] is True
    assert report["enable_proof_surfaces"] is False

    resp = await client.get("/health/dependencies")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enable_proof_surfaces"] is False
    assert "enable_dogfood_tool" not in body


@pytest.mark.anyio
async def test_dogfood_invoke_completes_trust_loop(
    client, dogfood_tool_on, clean_database, tmp_path, monkeypatch
):
    """Flag on → permit → invoke → receipt for partner.notes.write."""
    import app.services.dogfood_tool as dogfood_mod

    notes_path = tmp_path / "dogfood_partner_notes.jsonl"
    monkeypatch.setattr(dogfood_mod, "DOGFOOD_NOTES_PATH", notes_path)
    monkeypatch.setattr(dogfood_mod, "_note_count", None)

    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    permit = await create_tool_permit(
        client,
        wallet_id=wallet_id,
        key_id=provisioned["key_id"],
        tool_name=DOGFOOD_TOOL_ID,
        idem_key="dogfood-flag-permit-1",
    )

    invoke = await client.post(
        "/mcp/messages",
        headers=provisioned["agent_headers"],
        json={
            "jsonrpc": "2.0",
            "id": "dogfood-flag-1",
            "method": "tools/call",
            "params": {
                "name": DOGFOOD_TOOL_ID,
                "arguments": {"text": "flag-gated dogfood note"},
                "mcpContext": {
                    "wallet_id": wallet_id,
                    "permit_id": permit["permit_id"],
                    "idempotency_key": "dogfood-flag-invoke-1",
                },
            },
        },
    )
    assert invoke.status_code == 200, invoke.text
    payload = invoke.json()
    assert "result" in payload, payload
    result = payload["result"]
    assert result.get("isError") is False
    receipt = result["receipt"]
    assert receipt["permit_id"] == permit["permit_id"]
    assert receipt["outcome"] == "success"
    assert receipt["ledger_entry_id"]

    assert notes_path.exists()
    lines = [ln for ln in notes_path.read_text(encoding="utf-8").splitlines() if ln]
    assert len(lines) == 1
    assert "flag-gated dogfood note" in lines[0]


@pytest.mark.anyio
async def test_dogfood_not_invokable_when_flag_off(
    client, dogfood_tool_off, clean_database
):
    provisioned = await provision_agent_wallet(client)
    resp = await client.post(
        "/mcp/messages",
        json={
            "jsonrpc": "2.0",
            "id": "missing-dogfood",
            "method": "tools/call",
            "params": {
                "name": DOGFOOD_TOOL_ID,
                "arguments": {"text": "should not run"},
                "mcpContext": {"wallet_id": provisioned["agent_wallet_id"]},
            },
        },
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert "error" in payload
    assert "Tool not found" in payload["error"]["message"]
