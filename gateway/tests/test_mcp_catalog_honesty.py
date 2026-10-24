"""Catalog honesty for MCP discovery copy (gtm-54).

Pointer tools must say they return connection details for a follow-up HTTP
call instead of claiming to perform the action, preview stubs must be
labeled as demos without wired side effects, and the root discovery block
must name one recommended invoke path while marking the older routes
legacy.
"""

import pytest
from httpx import ASGITransport, AsyncClient

POINTER_TOOL_IDS = frozenset(
    {
        "awi_passkey_verify",
        "awi_dom_bridge_session",
        "awi_dom_sync",
        "awi_dom_state",
        "awi_dom_action_preview",
        "awi_memory_index",
        "awi_rag_query",
        "awi_session_context",
    }
)

# Phrases from the old copy that claimed the tool performs the action.
RETIRED_ACTION_CLAIMS = (
    "Verify a passkey assertion",
    "Create a new Playwright bridge session for real browser automation",
    "Execute an AWI action against real browser DOM",
    "Get current DOM state as AWI representation",
    "Preview what Playwright commands will be generated",
    "Index a completed AWI session for semantic search",
    "Semantic search query over session memories",
    "Get relevant context from past sessions",
)

STUB_SERVICE_IDS = frozenset(
    {
        "data-indexer",
        "content-generator",
        "telemetry-processor",
        "semantic-search",
    }
)


def _phase9_defs():
    from app.services.mcp_phase9_tools import MCP_PHASE9_TOOLS

    return {tool["service_id"]: tool for tool in MCP_PHASE9_TOOLS}


def test_pointer_tool_descriptions_state_pointer_behavior():
    """Pointer tools advertise connection details, not performed actions."""
    defs = _phase9_defs()
    assert POINTER_TOOL_IDS <= set(defs)
    for service_id in sorted(POINTER_TOOL_IDS):
        description = defs[service_id]["description"]
        assert "follow-up HTTP call" in description, service_id
        assert "does not " in description, service_id
        assert "Always requires a signed permit." in description, service_id
        for claim in RETIRED_ACTION_CLAIMS:
            assert claim not in description, (service_id, claim)


def test_real_tool_is_not_labeled_a_pointer():
    """The one Phase 9 tool that does real work keeps its action wording."""
    defs = _phase9_defs()
    description = defs["awi_passkey_challenge"]["description"]
    assert "follow-up HTTP call" not in description
    assert "Generate a passkey challenge" in description


def test_preview_stub_tools_are_labeled_demos_without_side_effects():
    """Marketplace stubs read as demos in the buyer-visible manifest."""
    import os

    from app.core.config import get_settings
    from app.services.mcp_generator import McpGenerator
    from app.services.mcp_phase9_tools import (
        sync_proof_surface_mcp_registration,
        unregister_proof_surface_mcp_stubs,
    )

    os.environ["ENABLE_PROOF_SURFACES"] = "true"
    get_settings.cache_clear()
    try:
        sync_proof_surface_mcp_registration()
        manifest = McpGenerator().generate_tools_json()
    finally:
        unregister_proof_surface_mcp_stubs()
        del os.environ["ENABLE_PROOF_SURFACES"]
        get_settings.cache_clear()

    by_name = {tool["name"]: tool for tool in manifest["tools"]}
    for service_id in sorted(STUB_SERVICE_IDS):
        assert service_id in by_name, f"stub {service_id} missing from manifest"
        tool = by_name[service_id]
        assert tool["description"].startswith("[Preview"), service_id
        assert "not wired" in tool["description"], service_id


@pytest.mark.anyio
async def test_root_discovery_names_one_invoke_path_and_legacy_routes(clean_database):
    """Root discovery recommends POST /mcp and marks older routes legacy."""
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        response = await c.get("/")
    assert response.status_code == 200
    block = response.json()["services"]["mcp_server"]
    assert "POST /mcp" in block["description"]
    assert "legacy" in block["description"].lower()
    assert "no SSE" in block["description"]
    endpoints = block["endpoints"]
    assert "POST /mcp/messages (legacy)" in endpoints
    assert "POST /mcp/tools/{service_id}/invoke (legacy)" in endpoints
    assert "POST /mcp/messages" not in endpoints
