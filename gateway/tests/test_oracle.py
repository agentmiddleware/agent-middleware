"""
Tests for the Agent Oracle Infiltration endpoints.
Validates crawling, indexing, registration, visibility scoring,
and network graph generation.
"""

import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app
from tests.test_trust_helpers import provision_agent_wallet


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def api_headers():
    return {"X-API-Key": "test-key"}


# --- Crawling ---


@pytest.mark.anyio
async def test_crawl_known_api(client, api_headers):
    """Crawl a known API from the simulated directory."""
    resp = await client.post(
        "/v1/oracle/crawl",
        json={
            "url": "https://api.anthropic.com",
            "directory_type": "openapi",
            "tags": ["ai", "llm"],
        },
        headers=api_headers,
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["name"] == "Anthropic API"
    assert data["url"] == "https://api.anthropic.com"
    assert data["status"] == "indexed"
    assert len(data["capabilities"]) > 0
    assert data["compatibility_score"] >= 0
    assert data["compatibility_score"] <= 1.0
    assert data["compatibility_tier"] in [
        "native",
        "compatible",
        "bridgeable",
        "incompatible",
    ]


@pytest.mark.anyio
async def test_crawl_unknown_api(client, api_headers):
    """Crawl an unknown URL — should generate synthetic metadata."""
    resp = await client.post(
        "/v1/oracle/crawl",
        json={
            "url": "https://api.someunknown.com",
            "directory_type": "well_known",
        },
        headers=api_headers,
    )
    assert resp.status_code == 202
    data = resp.json()
    assert "api_id" in data
    assert data["status"] == "indexed"


@pytest.mark.anyio
async def test_crawl_invalid_url(client, api_headers):
    """Invalid URL should fail validation."""
    resp = await client.post(
        "/v1/oracle/crawl",
        json={"url": "not-a-url", "directory_type": "openapi"},
        headers=api_headers,
    )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_batch_crawl(client, api_headers):
    """Batch crawl multiple URLs."""
    resp = await client.post(
        "/v1/oracle/crawl/batch",
        json=[
            "https://api.openai.com",
            "https://api.stripe.com",
            "https://api.github.com",
        ],
        headers=api_headers,
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["submitted"] == 3
    assert data["crawled"] == 3
    assert len(data["apis"]) == 3
    # Check all have compatibility scores
    for api in data["apis"]:
        assert "compatibility_tier" in api
        assert "compatibility_score" in api


# --- Index ---


@pytest.mark.anyio
async def test_list_indexed_empty(client, api_headers):
    """Index should work even when empty."""
    resp = await client.get("/v1/oracle/index", headers=api_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "apis" in data
    assert "total" in data
    assert data["crawl_targets"] == []
    assert "limit" in data
    assert "offset" in data


@pytest.mark.anyio
async def test_list_indexed_after_crawl(client, api_headers):
    """Index should contain crawled APIs."""
    # Crawl first
    await client.post(
        "/v1/oracle/crawl",
        json={"url": "https://api.anthropic.com", "directory_type": "openapi"},
        headers=api_headers,
    )
    await client.post(
        "/v1/oracle/crawl",
        json={"url": "https://api.openai.com", "directory_type": "openapi"},
        headers=api_headers,
    )

    resp = await client.get("/v1/oracle/index", headers=api_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 2


@pytest.mark.anyio
async def test_get_indexed_api(client, api_headers):
    """Retrieve specific indexed API by ID."""
    crawl_resp = await client.post(
        "/v1/oracle/crawl",
        json={"url": "https://api.twilio.com", "directory_type": "openapi"},
        headers=api_headers,
    )
    api_id = crawl_resp.json()["api_id"]

    resp = await client.get(f"/v1/oracle/index/{api_id}", headers=api_headers)
    assert resp.status_code == 200
    assert resp.json()["api_id"] == api_id
    assert resp.json()["name"] == "Twilio API"


@pytest.mark.anyio
async def test_get_indexed_not_found(client, api_headers):
    resp = await client.get("/v1/oracle/index/nonexistent", headers=api_headers)
    assert resp.status_code == 404


# --- Registration ---


@pytest.mark.anyio
async def test_register_in_directories(client, api_headers):
    """Register our API in external directories."""
    resp = await client.post(
        "/v1/oracle/register",
        json={
            "targets": [
                {
                    "directory_url": "https://agentdirectory.com/api/register",
                    "directory_type": "agent_registry",
                },
                {
                    "directory_url": "https://mcp-servers.io/register",
                    "directory_type": "mcp_server",
                },
            ],
        },
        headers=api_headers,
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["total_attempted"] == 2
    assert data["total_registered"] == 2
    assert data["total_failed"] == 0
    assert len(data["results"]) == 2
    assert data["results"][0]["status"] == "registered"
    assert data["results"][0]["registration_id"] is not None


@pytest.mark.anyio
async def test_list_registrations(client, api_headers):
    """List registrations after registering."""
    await client.post(
        "/v1/oracle/register",
        json={
            "targets": [
                {
                    "directory_url": "https://example-dir.com/register",
                    "directory_type": "plugin_store",
                },
            ],
        },
        headers=api_headers,
    )

    resp = await client.get("/v1/oracle/registrations", headers=api_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1


# --- Visibility & Network ---


@pytest.mark.anyio
async def test_visibility_score(client, api_headers):
    """Visibility score should include recommendations."""
    resp = await client.get("/v1/oracle/visibility", headers=api_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert 0 <= data["overall_score"] <= 100
    assert "directories_registered" in data
    assert "directories_crawled" in data
    assert "recommendations" in data
    assert isinstance(data["recommendations"], list)


@pytest.mark.anyio
async def test_network_graph(client, api_headers):
    """Network graph should have at least the self node."""
    resp = await client.get("/v1/oracle/network", headers=api_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_nodes"] >= 1
    assert data["center_node"] == "self"
    assert any(n["node_type"] == "self" for n in data["nodes"])


@pytest.mark.anyio
async def test_network_graph_with_data(client, api_headers):
    """Network graph should grow after crawling and registering."""
    # Crawl some APIs
    await client.post(
        "/v1/oracle/crawl",
        json={"url": "https://api.cloudflare.com", "directory_type": "openapi"},
        headers=api_headers,
    )
    # Register somewhere
    await client.post(
        "/v1/oracle/register",
        json={
            "targets": [
                {
                    "directory_url": "https://agents.dev/register",
                    "directory_type": "agent_registry",
                },
            ],
        },
        headers=api_headers,
    )

    resp = await client.get("/v1/oracle/network", headers=api_headers)
    data = resp.json()
    assert data["total_nodes"] >= 3  # self + 1 indexed + 1 directory
    assert data["total_edges"] >= 2


@pytest.mark.anyio
async def test_record_discovery(client, api_headers):
    """Record inbound discovery hit."""
    resp = await client.post(
        "/v1/oracle/discovery?referrer=https://agentdirectory.com",
        headers=api_headers,
    )
    assert resp.status_code == 204


# --- Auth ---


@pytest.mark.anyio
async def test_oracle_requires_api_key(client):
    resp = await client.post(
        "/v1/oracle/crawl",
        json={"url": "https://api.example.com", "directory_type": "openapi"},
    )
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_oracle_visibility_requires_api_key(client):
    resp = await client.get("/v1/oracle/visibility")
    assert resp.status_code == 401


# --- Global-state writes ---


@pytest.mark.proof
@pytest.mark.anyio
async def test_record_discovery_requires_bootstrap_admin(
    client, api_headers, clean_database
):
    """A wallet-scoped key cannot inject inbound-discovery hits into the
    shared visibility metrics; the bootstrap key still can."""
    wallet = await provision_agent_wallet(client)

    async def discovery_hits() -> int:
        resp = await client.get("/v1/oracle/visibility", headers=api_headers)
        assert resp.status_code == 200
        return int(resp.json()["inbound_discovery_requests"])

    before = await discovery_hits()
    denied = await client.post(
        "/v1/oracle/discovery?referrer=https://wallet-injected.example",
        headers=wallet["agent_headers"],
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["error"] == "admin_access_denied"
    assert await discovery_hits() == before

    allowed = await client.post(
        "/v1/oracle/discovery?referrer=https://agentdirectory.com",
        headers=api_headers,
    )
    assert allowed.status_code == 204
    assert await discovery_hits() == before + 1


@pytest.mark.proof
@pytest.mark.anyio
async def test_batch_crawl_rejects_oversized_batch(client, api_headers):
    """An oversized batch is refused before any crawl row is written."""
    urls = [f"https://api{i}.oversized-batch.example" for i in range(26)]
    resp = await client.post("/v1/oracle/crawl/batch", json=urls, headers=api_headers)
    assert resp.status_code == 422

    rows = await client.get(
        "/v1/oracle/index?domain=oversized-batch.example", headers=api_headers
    )
    assert rows.status_code == 200
    assert rows.json()["total"] == 0


@pytest.mark.proof
@pytest.mark.anyio
async def test_batch_crawl_accepts_batch_at_cap(client, api_headers, monkeypatch):
    """A batch at the cap is forwarded whole. The crawler is stubbed: 25
    concurrent crawls would overflow the shared SQLite pool, whose wait queue
    then stays bound to this test's event loop and breaks later tests."""
    from app.services.oracle import AgentOracle

    forwarded: list[list[str]] = []

    async def fake_batch_crawl(self, urls, audit_context=None):
        forwarded.append(list(urls))
        return []

    monkeypatch.setattr(AgentOracle, "batch_crawl", fake_batch_crawl)
    urls = [f"https://api{i}.capped-batch.example" for i in range(25)]
    resp = await client.post("/v1/oracle/crawl/batch", json=urls, headers=api_headers)
    assert resp.status_code == 202
    assert resp.json()["submitted"] == 25
    assert forwarded == [urls]
