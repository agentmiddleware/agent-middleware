"""
Tests for Phase 9.1 Agent Discoverability
==========================================

Verifies that all discovery surfaces include Phase 9 features:
- /.well-known/agent.json
- /v1/discover
- /mcp/tools.json
- /llm.txt
"""


class TestWellKnownAgentJson:
    """Test agent.json separates trust-plane product from Phase 9 proof surfaces."""

    def test_well_known_agent_json_keeps_phase9_out_of_product_capabilities(self):
        """Phase 9 items are proof_surfaces, not product capabilities."""
        from app.routers.well_known import PRODUCT_CAPABILITIES, _build_agent_manifest

        manifest = _build_agent_manifest()
        data = manifest.model_dump()

        assert data["capabilities"] == PRODUCT_CAPABILITIES
        for cap in ("passkey_auth", "dom_bridge", "rag_memory", "awi_automation"):
            assert cap not in data["capabilities"]

        proof_ids = {entry["id"] for entry in data["proof_surfaces"]}
        for cap in ("passkey_auth", "dom_bridge", "rag_memory"):
            assert cap in proof_ids
            entry = next(e for e in data["proof_surfaces"] if e["id"] == cap)
            assert entry["status"] == "proof_surface"
            assert entry["simulation"] is True

    def test_well_known_agent_json_has_phase9_endpoints_when_proof_mounted(self):
        """When proof surfaces are enabled, Phase 9 routes appear in endpoints."""
        from app.routers.well_known import _build_agent_manifest

        manifest = _build_agent_manifest()
        data = manifest.model_dump()

        assert "endpoints" in data
        assert data["endpoints"]["permits"] == "/v1/permits"
        assert data["endpoints"]["mcp_json_rpc"] == "/mcp/messages"
        phase9_endpoints = ["awi_passkey", "awi_dom", "awi_rag"]
        for endpoint in phase9_endpoints:
            assert endpoint in data["endpoints"], (
                f"Missing Phase 9 endpoint: {endpoint}"
            )

    def test_well_known_agent_json_has_phase9_docs_when_proof_mounted(self):
        """Phase 9 doc links are present when proof surfaces are mounted."""
        from app.routers.well_known import _build_agent_manifest

        manifest = _build_agent_manifest()
        data = manifest.model_dump()

        assert "documentation" in data
        assert "wedge" in data["documentation"]
        assert "security_limitations" in data["documentation"]
        phase9_docs = ["phase9_passkey", "phase9_dom_bridge", "phase9_rag"]
        for doc in phase9_docs:
            assert doc in data["documentation"], f"Missing Phase 9 doc link: {doc}"

    def test_well_known_agent_json_declares_agent_first(self):
        """Autonomous clients: manifest exposes agent_first bootstrap metadata."""
        from app.routers.well_known import _build_agent_manifest

        data = _build_agent_manifest().model_dump()
        assert "agent_first" in data
        af = data["agent_first"]
        assert af.get("primary_audience") == "autonomous_agents"
        assert af.get("design_principle") == "agent_first"
        assert af["positioning"]["id"] == (
            "transaction_integrity_for_consequential_autonomous_actions"
        )
        assert (
            "delivery_uncertain_no_automatic_redispatch"
            in af["positioning"]["semantics"]
        )
        # Compatibility-only v1 aliases.
        assert af.get("product_wedge") == "governed_mcp_trust_plane"
        assert "invoke" in af.get("product_loop", [])
        assert "/.well-known/agent.json" in af.get("bootstrap_sequence", [])
        assert af.get("simulation_and_dependency_truth") == "/health/dependencies"
        assert "proof_surfaces_enabled" in af

    def test_agent_first_metadata_matches_manifest_field(self):
        """agent_first in manifest equals get_agent_first_metadata()."""
        from app.routers.well_known import (
            _build_agent_manifest,
            get_agent_first_metadata,
        )

        assert (
            _build_agent_manifest().model_dump()["agent_first"]
            == get_agent_first_metadata()
        )


class TestDiscoverEndpoint:
    """Test /v1/discover includes Phase 9."""

    def test_discover_has_phase9_capabilities(self):
        """Phase 9 capabilities are listed as labeled proof surfaces."""
        from app.routers.discover import _build_capabilities

        capabilities = _build_capabilities()
        by_name = {c.name: c for c in capabilities}
        phase9_caps = ["passkey", "dom_bridge", "rag_memory"]
        for cap in phase9_caps:
            assert cap in by_name, f"Missing Phase 9 capability: {cap}"
            assert by_name[cap].surface == "proof_surface"
            assert by_name[cap].simulation_default is True

        for name in ("billing", "mcp", "permits", "receipts", "audit"):
            assert by_name[name].surface == "product"

    def test_discover_has_phase9_mcp_tools(self):
        """Verify discover endpoint includes Phase 9 MCP tools when proof surfaces enabled."""
        import os

        from app.routers.discover import _build_mcp_tools

        os.environ["ENABLE_PROOF_SURFACES"] = "true"
        try:
            from app.core.config import get_settings

            get_settings.cache_clear()
            tools = _build_mcp_tools()
            tool_names = [t.name for t in tools]

            # These are the service_id-based names used by the registry
            phase9_tools = [
                "awi_passkey_challenge",
                "awi_passkey_verify",
                "awi_dom_bridge_session",
                "awi_dom_sync",
                "awi_rag_query",
            ]
            for tool in phase9_tools:
                assert tool in tool_names, f"Missing Phase 9 MCP tool: {tool}"
        finally:
            del os.environ["ENABLE_PROOF_SURFACES"]
            get_settings.cache_clear()

    def test_discover_has_phase9_awi_endpoints(self):
        """Verify discover endpoint includes Phase 9 AWI endpoints."""
        from app.routers.discover import _build_awi_endpoints

        endpoints = _build_awi_endpoints()
        endpoint_paths = [e.path for e in endpoints]

        phase9_endpoints = [
            "/v1/awi/passkey/challenge",
            "/v1/awi/dom/sync",
            "/v1/awi/rag/query",
        ]
        for endpoint in phase9_endpoints:
            assert endpoint in endpoint_paths, f"Missing Phase 9 endpoint: {endpoint}"


class TestMcpToolsManifest:
    """Test MCP generator includes Phase 9 tools."""

    def test_mcp_generator_has_phase9_tools(self):
        """Verify MCP generator includes Phase 9 tools."""
        from app.services.mcp_phase9_tools import (
            ensure_phase9_registered,
            MCP_PHASE9_TOOLS,
        )

        ensure_phase9_registered()

        phase9_tool_ids = [t["service_id"] for t in MCP_PHASE9_TOOLS]
        expected = [
            "awi_passkey_challenge",
            "awi_passkey_verify",
            "awi_dom_bridge_session",
            "awi_dom_sync",
            "awi_dom_state",
            "awi_dom_action_preview",
            "awi_memory_index",
            "awi_rag_query",
            "awi_session_context",
        ]
        for tool in expected:
            assert tool in phase9_tool_ids, f"Missing Phase 9 tool: {tool}"

    def test_mcp_phase9_tools_have_pricing(self):
        """Verify Phase 9 MCP tools have proper pricing annotations."""
        from app.services.mcp_phase9_tools import MCP_PHASE9_TOOLS

        for tool in MCP_PHASE9_TOOLS:
            assert "credits_per_unit" in tool
            assert "unit_name" in tool
            assert tool["credits_per_unit"] > 0


class TestLlmTxt:
    """Test /llm.txt documents the trust-plane wedge (Phase 2 honesty)."""

    def test_llm_txt_has_trust_plane_quickstart(self):
        """Verify llm.txt points at permit → invoke → receipt, not proof stubs."""
        with open("static/llm.txt", "r") as f:
            content = f.read()

        assert "Agent-first" in content, "Missing agent-first preamble"
        assert "{{PUBLIC_URL}}" in content, "Missing PUBLIC_URL placeholder"
        assert "**Base URL:** http://localhost:8000" not in content
        assert "partner.notes.write" in content
        assert "POST /v1/telemetry/events" not in content

    def test_llm_txt_stays_wedge_only(self):
        """The primary agent doc carries no proof-surface inventory.

        Unmounted workloads are not discovery: an agent bootstrapping from
        llm.txt should meet only the governed permit → invoke → receipt loop.
        The proof-surface catalog lives in docs/PROOF_SURFACES.md.
        """
        with open("static/llm.txt", "r") as f:
            content = f.read()

        assert "/v1/permits" in content
        assert "ENABLE_PROOF_SURFACES" not in content
        assert "awi_" not in content
        assert "proof surface" not in content.lower()
        assert "proof-surface" not in content.lower()
        assert "simulation_modes" not in content


class TestPhase9ToolsRegistration:
    """Test Phase 9 tools are properly registered."""

    def test_phase9_tools_registered_in_service_registry(self):
        """Verify Phase 9 tools appear in the service registry."""
        from app.services.mcp_phase9_tools import ensure_phase9_registered
        from app.services.service_registry import get_service_registry

        ensure_phase9_registered()
        registry = get_service_registry()

        expected_tools = [
            "awi_passkey_challenge",
            "awi_passkey_verify",
            "awi_dom_bridge_session",
            "awi_dom_sync",
            "awi_dom_state",
            "awi_dom_action_preview",
            "awi_memory_index",
            "awi_rag_query",
            "awi_session_context",
        ]

        for tool_id in expected_tools:
            service = registry.get_local(tool_id)
            assert service is not None, f"Phase 9 tool not registered: {tool_id}"

    def test_phase9_tools_in_mcp_manifest(self):
        """Verify Phase 9 tools appear in MCP tools manifest."""
        from app.services.mcp_phase9_tools import ensure_phase9_registered
        from app.services.mcp_generator import get_mcp_generator

        ensure_phase9_registered()
        generator = get_mcp_generator()

        manifest = generator.generate_tools_json(include_persistent=False)
        tool_names = [t["name"] for t in manifest["tools"]]

        expected_tools = [
            "awi_passkey_challenge",
            "awi_passkey_verify",
            "awi_dom_bridge_session",
            "awi_dom_sync",
            "awi_memory_index",
            "awi_rag_query",
        ]
        for tool in expected_tools:
            assert tool in tool_names, f"Phase 9 tool not in manifest: {tool}"
