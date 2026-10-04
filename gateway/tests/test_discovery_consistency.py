"""Contract: MCP discovery manifest carries honesty metadata on every tool."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.runtime_mode import SERVICE_NAMES
from app.main import app
from app.services.mcp_integration_truth import truth_for_category
from app.services.pricing import PROOF_SURFACE_CATEGORIES

_INTEGRATION_STATUSES = frozenset({"simulated", "integrated", "platform", "postgres"})


def test_frozen_proof_surface_categories_never_annotate_as_platform():
    """
    A frozen proof surface's tools must not be advertised as platform
    functionality in /mcp/tools.json.

    mcp_phase9_tools registers data-indexer and semantic-search under
    protocol_gen behind a handler that wires no side effects. While those
    categories had no SIMULATION_MODE_* flag, truth_for_category fell through
    to ``{"simulation": False, "integration_status": "platform"}`` and the
    manifest described a preview stub as real platform functionality.

    Both assertions hold whatever the flags are set to, so this does not
    depend on test ordering.
    """
    for category in PROOF_SURFACE_CATEGORIES:
        truth = truth_for_category(category.value)
        assert truth["integration_status"] != "platform", category.value
        assert truth["runtime_service"] == category.value, category.value


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
async def test_mcp_tools_json_each_tool_has_honesty_annotations(client):
    resp = await client.get("/mcp/tools.json")
    assert resp.status_code == 200
    data = resp.json()
    tools = data.get("tools") or []
    assert tools, "expected at least one MCP tool"

    for tool in tools:
        ann = tool.get("annotations") or {}
        assert "simulation" in ann
        assert isinstance(ann["simulation"], bool)
        status = ann.get("integrationStatus")
        assert status in _INTEGRATION_STATUSES

        rs = ann.get("runtimeService")
        if status in ("simulated", "integrated", "postgres"):
            assert rs in SERVICE_NAMES
        if status == "platform":
            assert rs is None


@pytest.mark.anyio
async def test_mcp_tool_simulation_matches_runtime_for_pillars(client):
    """Pillar tools: annotation.simulation matches /health/dependencies for that service."""
    from app.core.runtime_mode import get_simulation_modes

    sim = get_simulation_modes()
    r = await client.get("/mcp/tools.json")
    assert r.status_code == 200
    for tool in r.json()["tools"]:
        ann = tool.get("annotations") or {}
        rs = ann.get("runtimeService")
        if rs in SERVICE_NAMES:
            assert ann.get("simulation") is sim[rs]
