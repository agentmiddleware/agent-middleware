"""
Tests for discovery and documentation endpoints.
Validates the agent 'front door' — the endpoints agents hit first
to decide whether to use this API.
"""

import pytest
from httpx import AsyncClient, ASGITransport
from app.core.config import get_settings
from app.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# --- Root Discovery ---


@pytest.mark.anyio
async def test_root_returns_manifest(client):
    resp = await client.get("/")
    assert resp.status_code == 200
    data = resp.json()
    assert "services" in data
    assert "auth" in data
    assert "docs" in data
    assert "iot_bridge" in data["services"]
    assert "autonomous_pm" in data["services"]
    assert "media_engine" in data["services"]
    from app.routers.well_known import get_agent_first_metadata

    assert data["agent_first"] == get_agent_first_metadata()
    assert data["docs"].get("dependency_truth") == "/health/dependencies"
    assert data["docs"].get("capability_index") == "/v1/discover"
    assert data["docs"].get("llms_txt") == "/llms.txt"


@pytest.mark.anyio
async def test_health_check(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


# --- OpenAPI Spec ---


@pytest.mark.anyio
async def test_openapi_json_accessible(client):
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    data = resp.json()
    assert "paths" in data
    assert "components" in data
    assert len(data["paths"]) > 15  # We have ~25 endpoints now


# --- Doc Index ---


@pytest.mark.anyio
async def test_doc_index(client):
    resp = await client.get("/docs/index")
    assert resp.status_code == 200
    data = resp.json()
    assert "sections" in data
    assert "services" in data
    assert data["positioning"] == data["agent_first"]["positioning"]
    assert data["positioning"]["schema_version"] == "1.0"
    # Compatibility-only v1 alias.
    assert data["product_wedge"] == "governed_mcp_trust_plane"
    assert data["agent_first"]["design_principle"] == "agent_first"
    assert data["sections"][0]["path"] == "/.well-known/agent.json"
    services = {service["id"]: service for service in data["services"]}
    assert {
        "per-action-micro-metering",
        "daily-spend-limits",
        "bounded-credit-and-call-allowance",
        "at-most-one-debit-per-logical-action",
    } <= set(services["agent-billing"]["capabilities"])
    assert {
        "idempotent-retries",
        "signed-receipts",
        "logical-action-identity",
        "delivery-uncertain-no-automatic-redispatch",
    } <= set(services["mcp-trust-plane"]["capabilities"])
    assert services["mcp-trust-plane"]["transaction_scope"] == (
        "configured_upstream_mcp_tool_only"
    )
    assert {
        "budget-binding",
        "credit-and-call-allowance-binding",
    } <= set(services["permits"]["capabilities"])
    assert any(s["path"] == "/WEDGE.md" for s in data["sections"])
    assert any(s["path"] == "/llms.txt" for s in data["sections"])
    # Trust-plane services always; proof surfaces only when mounted.
    assert len(data["services"]) >= 5
    assert all("surface" in s for s in data["services"])
    assert any(s["id"] == "mcp-trust-plane" for s in data["services"])


@pytest.fixture
def standard_mcp_disabled(monkeypatch):
    monkeypatch.setenv("ENABLE_STANDARD_MCP_ENDPOINT", "false")
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("ENABLE_STANDARD_MCP_ENDPOINT", raising=False)
    get_settings.cache_clear()


@pytest.mark.anyio
async def test_doc_index_names_standard_mcp_as_the_governed_invoke_surface(client):
    """Standard MCP is the canonical path, so the catalog must lead with it.

    An agent reads this entry to find out how to call a tool. Pointing it at the
    project-specific JSON-RPC transport sends conforming clients down a path
    they would otherwise never need.
    """

    index = (await client.get("/docs/index")).json()
    service = next(s for s in index["services"] if s["id"] == "mcp-trust-plane")

    assert service["base_path"] == "/mcp"
    assert service["transport"] == "standard_streamable_http"
    assert service["quickstart_endpoint"] == "POST /mcp"
    # The legacy transport stays advertised rather than hidden: it is the one
    # that accepts a permit another issuer granted.
    assert service["legacy_json_rpc_endpoint"] == "/mcp/messages"


@pytest.mark.anyio
async def test_doc_index_falls_back_when_standard_mcp_is_disabled(
    client, standard_mcp_disabled
):
    """The catalog must never advertise a path the deployment does not serve.

    `/mcp` answers 404 to an authenticated caller when the endpoint is off
    (`test_standard_mcp_endpoint.py::test_endpoint_returns_404_when_disabled`
    owns that contract), so naming it as base_path here would hand every reader
    a dead path -- the class of overclaim the discovery-honesty rules exist to
    prevent.
    """

    index = (await client.get("/docs/index")).json()
    service = next(s for s in index["services"] if s["id"] == "mcp-trust-plane")

    assert service["base_path"] == "/mcp/messages"
    assert service["transport"] == "legacy_project_json_rpc"
    assert service["quickstart_endpoint"] == "POST /mcp/messages"
    assert service["standard_streamable_http_status"] == "disabled"


@pytest.mark.anyio
async def test_request_keys_guide_is_listed_and_served(client):
    index = (await client.get("/docs/index")).json()
    assert any(
        section["path"] == "/docs/concepts/request-keys"
        for section in index["sections"]
    )

    guide = await client.get("/docs/concepts/request-keys")
    assert guide.status_code == 200
    assert guide.headers["content-type"].startswith("text/markdown")
    assert guide.text.startswith("# Request keys\n")
    assert "idempotency_key_reused" in guide.text
    operation = (await client.get("/openapi.json")).json()["paths"][
        "/docs/concepts/request-keys"
    ]["get"]
    assert "text/markdown" in operation["responses"]["200"]["content"]


@pytest.mark.anyio
async def test_v1_discover_includes_agent_first(client):
    from app.routers.well_known import get_agent_first_metadata

    resp = await client.get("/v1/discover")
    assert resp.status_code == 200
    data = resp.json()
    assert "agent_first" in data
    assert data["agent_first"] == get_agent_first_metadata()
    af = data["agent_first"]
    assert af.get("primary_audience") == "autonomous_agents"
    assert af.get("design_principle") == "agent_first"
    assert (
        af["positioning"]["id"]
        == "transaction_integrity_for_consequential_autonomous_actions"
    )
    assert af.get("simulation_and_dependency_truth") == "/health/dependencies"


# --- Agent Manifest ---


@pytest.mark.anyio
async def test_well_known_agent_json(client):
    resp = await client.get("/.well-known/agent.json")
    assert resp.status_code == 200
    data = resp.json()
    assert data["schema_version"] == "1.0"
    assert "capabilities" in data
    from app.routers.well_known import _build_agent_manifest

    assert (
        data["capabilities"]
        == _build_agent_manifest().model_dump(mode="json")["capabilities"]
    )
    assert data["authentication"]["type"] == "api_key"


# --- llms.txt ---


@pytest.mark.anyio
async def test_llms_txt_and_legacy_alias_serve_identical_public_instructions(client):
    canonical = await client.get("/llms.txt")
    legacy = await client.get("/llm.txt")

    assert canonical.status_code == 200
    assert legacy.status_code == 200
    assert canonical.text == legacy.text
    assert "Agent Middleware API" in canonical.text
    assert canonical.headers["content-type"].startswith("text/plain")
