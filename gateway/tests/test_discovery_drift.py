import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import app
from app.services import mcp_phase9_tools
from app.services.mcp_phase9_tools import MCP_PHASE9_TOOLS
from app.services.service_registry import get_service_registry
from tests.conftest import iter_routes


_DEFAULT_MCP_TOOL_IDS = {
    "data-indexer",
    "content-generator",
    "telemetry-processor",
    "semantic-search",
}


def _clear_builtin_mcp_tools_for_lazy_start():
    registry = get_service_registry()
    for tool in MCP_PHASE9_TOOLS:
        registry.unregister_local(tool["service_id"])
    for tool_id in _DEFAULT_MCP_TOOL_IDS:
        registry.unregister_local(tool_id)
    mcp_phase9_tools._registered = False
    mcp_phase9_tools._default_services_registered = False


def _assert_phase9_tool_available(tools):
    assert {tool["name"] for tool in tools} >= {"awi_passkey_challenge"}


def _tools_by_name(tools):
    return {tool["name"]: tool for tool in tools}


def _without_generated_at(manifest):
    return {key: value for key, value in manifest.items() if key != "generated_at"}


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
async def test_agent_manifest_points_to_canonical_control_plane_surfaces(client):
    response = await client.get("/.well-known/agent.json")

    assert response.status_code == 200
    data = response.json()
    endpoints = data["endpoints"]
    agent_first = data["agent_first"]

    assert endpoints["billing"] == "/v1/billing"
    assert endpoints["mcp_json_rpc"] == "/mcp/messages"
    assert endpoints["mcp_tools"] == "/mcp/tools.json"
    assert "mcp" not in endpoints
    assert endpoints["permits"] == "/v1/permits"
    assert endpoints["receipts"] == "/v1/receipts"
    assert endpoints["health"] == "/health"
    assert endpoints["agent_manifest"] == "/.well-known/agent.json"
    assert endpoints["llm_docs"] == "/llm.txt"
    assert endpoints["llms_docs"] == "/llms.txt"
    assert "/llms.txt" in agent_first["bootstrap_sequence"]
    assert "/mcp/tools.json" in agent_first["bootstrap_sequence"]
    assert agent_first["simulation_and_dependency_truth"] == "/health/dependencies"
    assert agent_first["positioning"]["schema_version"] == "1.0"
    assert agent_first["positioning"]["canonical_loop"][-1] == (
        "authoritative_external_reconciliation_required"
    )
    # Compatibility-only v1 alias.
    assert agent_first["product_wedge"] == "governed_mcp_trust_plane"
    assert "permits" in data["capabilities"]
    assert "passkey_auth" not in data["capabilities"]
    assert any(p["id"] == "passkey_auth" for p in data["proof_surfaces"])


@pytest.mark.anyio
async def test_standard_mcp_is_advertised_only_when_enabled(client, monkeypatch):
    monkeypatch.setenv("ENABLE_STANDARD_MCP_ENDPOINT", "false")
    get_settings.cache_clear()
    try:
        disabled = (await client.get("/.well-known/agent.json")).json()
        disabled_discovery = (await client.get("/v1/discover")).json()

        assert "mcp" not in disabled["endpoints"]
        assert disabled["integrations"]["mcp_json_rpc"] == "/mcp/messages"
        assert (
            disabled["integrations"]["mcp_json_rpc_status"]
            == "legacy_project_transport"
        )
        assert "preferred_integration" not in disabled["integrations"]
        assert "standard_mcp_streamable_http" not in disabled["integrations"]
        assert (
            disabled_discovery["integration_guides"]["mcp_json_rpc"] == "/mcp/messages"
        )
        assert "standard_mcp" not in disabled_discovery["integration_guides"]
        disabled_root = (await client.get("/")).json()
        assert "POST /mcp" not in disabled_root["services"]["mcp_server"]["endpoints"]

        monkeypatch.setenv("ENABLE_STANDARD_MCP_ENDPOINT", "true")
        get_settings.cache_clear()
        enabled = (await client.get("/.well-known/agent.json")).json()
        enabled_discovery = (await client.get("/v1/discover")).json()

        assert enabled["endpoints"]["mcp"] == "/mcp"
        assert enabled["integrations"]["standard_mcp_streamable_http"] == "/mcp"
        assert (
            enabled["integrations"]["preferred_integration"]
            == "standard_mcp_streamable_http"
        )
        assert enabled_discovery["integration_guides"]["standard_mcp"] == "/mcp"
        enabled_root = (await client.get("/")).json()
        assert "POST /mcp" in enabled_root["services"]["mcp_server"]["endpoints"]
    finally:
        monkeypatch.setenv("ENABLE_STANDARD_MCP_ENDPOINT", "false")
        get_settings.cache_clear()


@pytest.mark.anyio
async def test_every_advertised_endpoint_resolves_to_a_mounted_route(client):
    """No entry in the manifest may point at nothing.

    Discovery is the contract an autonomous client bootstraps from; an
    advertised path that 404s costs it a wasted call and a wrong conclusion
    about what this plane offers. `keys` pointed at `/v1/keys` while the
    router was mounted at `/v1/signing-keys`, which is exactly the drift this
    catches — generically, rather than one asserted path at a time.
    """
    response = await client.get("/.well-known/agent.json")
    assert response.status_code == 200

    mounted = {
        route.path for route in iter_routes(app.routes) if hasattr(route, "path")
    }

    unresolved = []
    for name, path in response.json()["endpoints"].items():
        if not isinstance(path, str) or not path.startswith("/"):
            continue
        # A path resolves if it is a route itself or the prefix of one, so
        # collection roots like /v1/signing-keys count via their subroutes.
        if any(
            route == path or route.startswith(path.rstrip("/") + "/")
            for route in mounted
        ):
            continue
        unresolved.append(f"{name} -> {path}")

    assert not unresolved, f"advertised endpoints with no mounted route: {unresolved}"


@pytest.mark.anyio
async def test_discover_and_agent_manifest_share_agent_first_contract(client):
    agent_response = await client.get("/.well-known/agent.json")
    discover_response = await client.get("/v1/discover")

    assert agent_response.status_code == 200
    assert discover_response.status_code == 200
    assert (
        agent_response.json()["agent_first"] == discover_response.json()["agent_first"]
    )


@pytest.mark.anyio
async def test_openapi_contains_core_control_plane_routes(client):
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/mcp/messages" in paths
    assert "/mcp/tools/{service_id}/invoke" in paths
    catalog_responses = paths["/mcp/tools.json"]["get"]["responses"]
    assert "200" in catalog_responses
    assert "401" in catalog_responses
    assert "422" in catalog_responses
    assert (
        "Missing credentials on production-like boots"
        in catalog_responses["401"]["description"]
    )
    assert "/mcp" not in paths
    legacy_mcp_description = paths["/mcp/messages"]["post"]["description"]
    assert "does not implement the standard MCP initialization lifecycle" in (
        legacy_mcp_description
    )
    assert "New integrations should use" not in legacy_mcp_description
    assert "/.well-known/agent.json" in legacy_mcp_description
    assert "/v1/billing/charge" in paths
    assert "/v1/audit/events" in paths
    assert "/v1/planner/optimize" in paths


@pytest.mark.anyio
async def test_mcp_manifest_tools_include_pricing_and_simulation_truth(client):
    response = await client.get("/mcp/tools.json")

    assert response.status_code == 200
    tools = response.json()["tools"]
    assert tools
    for tool in tools:
        annotations = tool["annotations"]
        assert "creditsPerCall" in annotations
        assert "unitName" in annotations
        assert "simulation" in annotations
        assert "integrationStatus" in annotations


@pytest.mark.anyio
async def test_well_known_mcp_manifest_matches_canonical_mcp_manifest(client):
    _clear_builtin_mcp_tools_for_lazy_start()

    well_known_response = await client.get("/.well-known/mcp/tools.json")
    canonical_response = await client.get("/mcp/tools.json")

    assert well_known_response.status_code == 200
    assert canonical_response.status_code == 200
    well_known = well_known_response.json()
    canonical = canonical_response.json()

    assert _without_generated_at(well_known) == _without_generated_at(canonical)

    well_known_tools = _tools_by_name(well_known["tools"])
    assert "awi_passkey_challenge" in well_known_tools

    annotations = well_known_tools["awi_passkey_challenge"]["annotations"]
    assert annotations["creditsPerCall"] == 1.0
    assert annotations["unitName"] == "challenge"
    assert annotations["category"] == "agent_comms"
    assert annotations["simulation"] is True
    assert annotations["integrationStatus"] == "simulated"
    assert annotations["runtimeService"] == "agent_comms"


@pytest.mark.anyio
async def test_prefixed_well_known_mcp_manifest_is_not_a_discovery_route(client):
    response = await client.get("/mcp/.well-known/mcp/tools.json")

    assert response.status_code == 404


@pytest.mark.anyio
async def test_mcp_tools_endpoint_lazily_registers_local_tools(client):
    _clear_builtin_mcp_tools_for_lazy_start()

    response = await client.get("/mcp/tools")

    assert response.status_code == 200
    _assert_phase9_tool_available(response.json()["tools"])


@pytest.mark.anyio
async def test_mcp_messages_tools_list_lazily_registers_local_tools(client):
    _clear_builtin_mcp_tools_for_lazy_start()

    response = await client.post(
        "/mcp/messages",
        json={
            "jsonrpc": "2.0",
            "id": "lazy-list",
            "method": "tools/list",
            "params": {},
        },
        headers={"X-API-Key": "test-key"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == "lazy-list"
    _assert_phase9_tool_available(payload["result"]["tools"])


@pytest.mark.anyio
async def test_mcp_get_tool_lazily_registers_local_tools(client):
    _clear_builtin_mcp_tools_for_lazy_start()

    response = await client.get("/mcp/tools/awi_passkey_challenge")

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "awi_passkey_challenge"
    assert data["annotations"]["creditsPerCall"] == 1.0
