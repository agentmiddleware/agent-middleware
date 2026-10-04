"""
Tests for Human Accessibility & Content Negotiation Layer.
Verifies that human operators can access the control deck and documentation
via browser HTML requests while agent-native machine calls receive pure JSON.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers.well_known import get_agent_first_metadata


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
async def test_root_content_negotiation_browser_html(client):
    """Browser request with Accept: text/html returns the public operator index."""
    headers = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9"}
    resp = await client.get("/", headers=headers)
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Agent Middleware API" in resp.text
    assert "Private experiment" in resp.text
    assert "No tenant records or aggregate customer counts" in resp.text


@pytest.mark.anyio
async def test_root_content_negotiation_agent_json(client):
    """Machine/Agent request with Accept: application/json returns API root JSON."""
    headers = {"Accept": "application/json"}
    resp = await client.get("/", headers=headers)
    assert resp.status_code == 200
    assert "application/json" in resp.headers["content-type"]
    data = resp.json()
    assert "agent_first" in data
    assert data["agent_first"]["design_principle"] == "agent_first"


@pytest.mark.anyio
async def test_dashboard_endpoint_returns_html(client):
    """Direct /dashboard is a public evidence index, not fake telemetry."""
    resp = await client.get("/dashboard")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Agent Middleware API" in resp.text
    assert "Private experiment" in resp.text
    assert "self-issued live gateway proof, not customer traction" in resp.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "path,headers", [("/dashboard", {}), ("/", {"Accept": "text/html"})]
)
async def test_public_operator_index_has_no_fake_data_or_browser_key_handling(
    client, path, headers
):
    resp = await client.get(path, headers=headers)
    assert resp.status_code == 200
    page = resp.text

    fabricated_markers = (
        "agent_synth_01",
        "agent_finance_bot",
        "rcpt_99a8b7",
        "pmt_8f9a2c",
        "1,428",
        "metric-permits",
        'verification_status: "VALID"',
        "Verify Ed25519 Signature",
        "Load Sample",
        "Control Plane Active",
        "INTACT",
    )
    for marker in fabricated_markers:
        assert marker not in page

    browser_secret_markers = (
        "localStorage",
        "sessionStorage",
        'type="password"',
        "fetch('/v1/permits",
        'fetch("/v1/permits',
    )
    for marker in browser_secret_markers:
        assert marker not in page

    assert "<script" not in page
    assert "railway.app" not in page
    assert "vercel.app" not in page
    assert "https://www.thisisatest.tech/proof/" in page
    for target in (
        "/health/dependencies",
        "/.well-known/trust-keys.json",
        "/.well-known/agent.json",
        "/llms.txt",
    ):
        assert f'href="{target}"' in page
    assert "https://api.thisisatest.tech/" not in page
    for resource in ("permits", "receipts", "audit/events"):
        assert f"${{API_URL}}/v1/me/{resource}" in page
    assert "Keep the key in your environment" in page
    assert "Public governed-tool catalog" not in page
    assert 'href="https://api.thisisatest.tech/mcp/tools.json"' not in page


@pytest.mark.anyio
async def test_agent_first_metadata_declares_human_observability(client):
    """get_agent_first_metadata includes human_observability URLs."""
    meta = get_agent_first_metadata()
    assert "human_observability" in meta
    assert meta["human_observability"]["human_dashboard_url"] == "/dashboard"
    assert meta["human_observability"]["interactive_docs_url"] == "/docs"
    note = meta["human_observability"]["note"]
    assert "status and evidence index" in note
    assert "active permits" not in note

    manifest_resp = await client.get("/.well-known/agent.json")
    assert manifest_resp.status_code == 200
    manifest = manifest_resp.json()
    assert manifest["documentation"]["human_dashboard"] == "/dashboard"


@pytest.mark.anyio
async def test_docs_and_llm_txt_remain_accessible(client):
    """OpenAPI and LLM documentation endpoints remain accessible."""
    resp_llm = await client.get("/llm.txt")
    assert resp_llm.status_code == 200
    assert "Agent Middleware API" in resp_llm.text

    resp_openapi = await client.get("/openapi.json")
    assert resp_openapi.status_code == 200
    assert "openapi" in resp_openapi.json()


def test_openapi_documents_human_html_and_dashboard_not_found():
    """Generated clients see both negotiated root media types and the
    dashboard file-missing response."""
    spec = app.openapi()

    root_content = spec["paths"]["/"]["get"]["responses"]["200"]["content"]
    assert "application/json" in root_content
    assert root_content["text/html"]["schema"] == {"type": "string"}

    dashboard_responses = spec["paths"]["/dashboard"]["get"]["responses"]
    assert dashboard_responses["404"]["description"] == "Dashboard not found"
