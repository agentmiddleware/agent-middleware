"""
Tests for Pillar 11: Protocol Generation Engine.
Validates code-to-discovery pipeline.
"""

import hashlib

import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app
from tests.test_trust_helpers import provision_agent_wallet


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


HEADERS = {"X-API-Key": "test-key"}

SAMPLE_CODE = """
from fastapi import APIRouter
router = APIRouter()

@router.get("/api/v1/widgets", summary="List widgets", description="Get all widgets")
async def list_widgets():
    return []

@router.post("/api/v1/widgets", summary="Create widget", description="Create a new widget")
async def create_widget():
    return {"id": "w1"}

@router.get("/api/v1/widgets/{widget_id}", summary="Get widget", description="Get widget by ID")
async def get_widget(widget_id: str):
    return {}
"""


@pytest.mark.anyio
async def test_generate_protocol(client):
    """Full protocol generation from source code."""
    resp = await client.post(
        "/v1/protocol/generate",
        json={
            "source_code": SAMPLE_CODE,
            "service_name": "widget-api",
            "service_version": "2.0.0",
            "base_url": "https://api.widgets.io",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()

    assert data["endpoints_parsed"] == 3
    assert "widget-api" in data["llm_txt"]
    assert data["openapi_spec"]["openapi"] == "3.1.0"
    assert data["agent_json"]["schema_version"] == "1.0"
    assert data["generation_id"].startswith("gen-")


@pytest.mark.anyio
async def test_llm_txt_contains_endpoints(client):
    """Generated llm.txt should list all parsed endpoints."""
    resp = await client.post(
        "/v1/protocol/generate",
        json={
            "source_code": SAMPLE_CODE,
            "service_name": "widget-api",
        },
        headers=HEADERS,
    )
    data = resp.json()
    llm_txt = data["llm_txt"]
    assert "GET /api/v1/widgets" in llm_txt
    assert "POST /api/v1/widgets" in llm_txt
    assert "List widgets" in llm_txt


@pytest.mark.anyio
async def test_openapi_spec_structure(client):
    """OpenAPI spec should have paths, security, and info."""
    resp = await client.post(
        "/v1/protocol/generate",
        json={
            "source_code": SAMPLE_CODE,
            "service_name": "test-api",
        },
        headers=HEADERS,
    )
    spec = resp.json()["openapi_spec"]
    assert "paths" in spec
    assert "/api/v1/widgets" in spec["paths"]
    assert "components" in spec
    assert "securitySchemes" in spec["components"]


@pytest.mark.anyio
async def test_agent_json_manifest(client):
    """agent.json should list capabilities and auth."""
    resp = await client.post(
        "/v1/protocol/generate",
        json={
            "source_code": SAMPLE_CODE,
            "service_name": "agent-test",
            "base_url": "https://api.test.com",
        },
        headers=HEADERS,
    )
    aj = resp.json()["agent_json"]
    assert aj["name"] == "agent-test"
    assert aj["base_url"] == "https://api.test.com"
    assert len(aj["capabilities"]) == 3
    assert aj["auth"]["type"] == "api_key"


@pytest.mark.anyio
async def test_empty_code_warns(client):
    """Source code with no endpoints should generate a warning."""
    resp = await client.post(
        "/v1/protocol/generate",
        json={
            "source_code": "# empty file\nprint('hello')",
            "service_name": "empty-api",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["endpoints_parsed"] == 0
    assert len(data["warnings"]) > 0


@pytest.mark.anyio
async def test_list_generations(client):
    """Historical generations should be retrievable."""
    await client.post(
        "/v1/protocol/generate",
        json={
            "source_code": SAMPLE_CODE,
            "service_name": "gen-list-test",
        },
        headers=HEADERS,
    )

    resp = await client.get("/v1/protocol/generations", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1


@pytest.mark.anyio
async def test_get_generation_by_id(client):
    """Can retrieve a specific generation by ID."""
    create = await client.post(
        "/v1/protocol/generate",
        json={
            "source_code": SAMPLE_CODE,
            "service_name": "id-test",
        },
        headers=HEADERS,
    )
    gen_id = create.json()["generation_id"]

    resp = await client.get(f"/v1/protocol/generations/{gen_id}", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["generation_id"] == gen_id


@pytest.mark.anyio
async def test_generation_not_found(client):
    resp = await client.get("/v1/protocol/generations/gen-nonexistent", headers=HEADERS)
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_protocol_requires_api_key(client):
    resp = await client.post(
        "/v1/protocol/generate",
        json={
            "source_code": SAMPLE_CODE,
            "service_name": "test",
        },
    )
    assert resp.status_code in (401, 403)


# --------------------------------------------------------------------------
# Tenant isolation: generations are owned by the generating wallet
# --------------------------------------------------------------------------


@pytest.mark.proof
@pytest.mark.anyio
async def test_generation_not_readable_across_tenants(client, clean_database):
    """Wallet B can neither fetch nor enumerate wallet A's generation."""
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)

    create = await client.post(
        "/v1/protocol/generate",
        json={
            "source_code": SAMPLE_CODE,
            "service_name": "tenant-a-secret-service",
            "base_url": "https://tenant-a.internal.example",
        },
        headers=a["agent_headers"],
    )
    assert create.status_code == 201
    a_gen_id = create.json()["generation_id"]

    b_create = await client.post(
        "/v1/protocol/generate",
        json={
            "source_code": SAMPLE_CODE,
            "service_name": "tenant-b-service",
        },
        headers=b["agent_headers"],
    )
    assert b_create.status_code == 201
    b_gen_id = b_create.json()["generation_id"]

    # B gets the same 404 as for a missing generation: no existence oracle,
    # no derived API description, and never the owner's wallet id.
    foreign = await client.get(
        f"/v1/protocol/generations/{a_gen_id}", headers=b["agent_headers"]
    )
    missing = await client.get(
        "/v1/protocol/generations/gen-nonexistent", headers=b["agent_headers"]
    )
    assert foreign.status_code == 404
    assert foreign.json() == missing.json()
    assert a["agent_wallet_id"] not in foreign.text
    assert "tenant-a" not in foreign.text

    # B's listing holds only B's own generation.
    b_list = await client.get("/v1/protocol/generations", headers=b["agent_headers"])
    assert b_list.status_code == 200
    assert {g["generation_id"] for g in b_list.json()["generations"]} == {b_gen_id}
    assert b_list.json()["total"] == 1
    assert "tenant-a" not in b_list.text
    assert a["agent_wallet_id"] not in b_list.text

    # The owner still reads and lists its own generation.
    own = await client.get(
        f"/v1/protocol/generations/{a_gen_id}", headers=a["agent_headers"]
    )
    assert own.status_code == 200
    assert own.json()["service_name"] == "tenant-a-secret-service"
    a_list = await client.get("/v1/protocol/generations", headers=a["agent_headers"])
    assert {g["generation_id"] for g in a_list.json()["generations"]} == {a_gen_id}

    # A bootstrap admin still sees every generation.
    admin = await client.get(f"/v1/protocol/generations/{a_gen_id}", headers=HEADERS)
    assert admin.status_code == 200
    admin_list = await client.get("/v1/protocol/generations", headers=HEADERS)
    admin_ids = {g["generation_id"] for g in admin_list.json()["generations"]}
    assert {a_gen_id, b_gen_id} <= admin_ids


@pytest.mark.proof
@pytest.mark.anyio
async def test_admin_generation_not_readable_by_wallet_key(client, clean_database):
    """A bootstrap-key generation has no wallet owner, so a wallet-scoped key
    can neither read nor enumerate it."""
    a = await provision_agent_wallet(client)
    create = await client.post(
        "/v1/protocol/generate",
        json={
            "source_code": SAMPLE_CODE,
            "service_name": "admin-only-service",
        },
        headers=HEADERS,
    )
    assert create.status_code == 201
    admin_gen_id = create.json()["generation_id"]

    resp = await client.get(
        f"/v1/protocol/generations/{admin_gen_id}", headers=a["agent_headers"]
    )
    assert resp.status_code == 404
    assert "admin-only-service" not in resp.text

    listing = await client.get("/v1/protocol/generations", headers=a["agent_headers"])
    assert listing.status_code == 200
    listed_ids = {g["generation_id"] for g in listing.json()["generations"]}
    assert admin_gen_id not in listed_ids
    assert "admin-only-service" not in listing.text


# --------------------------------------------------------------------------
# Oracle registration writes the shared, global Oracle index
# --------------------------------------------------------------------------


def _oracle_api_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


@pytest.mark.proof
@pytest.mark.anyio
async def test_register_in_oracle_requires_bootstrap_admin(client, clean_database):
    """A wallet-scoped key cannot push into the global Oracle index, and the
    refused request leaves neither a generation nor an index entry behind."""
    a = await provision_agent_wallet(client)
    base_url = "https://wallet-register-denied.example"

    resp = await client.post(
        "/v1/protocol/generate",
        json={
            "source_code": SAMPLE_CODE,
            "service_name": "wallet-register-denied",
            "base_url": base_url,
            "register_in_oracle": True,
        },
        headers=a["agent_headers"],
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "admin_access_denied"

    listing = await client.get("/v1/protocol/generations", headers=a["agent_headers"])
    assert listing.json()["total"] == 0
    indexed = await client.get(
        f"/v1/oracle/index/{_oracle_api_id(base_url)}", headers=HEADERS
    )
    assert indexed.status_code == 404


@pytest.mark.proof
@pytest.mark.anyio
async def test_register_in_oracle_reports_registration_id(client):
    """A registration that indexed the service reports its api_id, not a
    spurious failure warning."""
    base_url = "https://protocol-register-ok.example"
    resp = await client.post(
        "/v1/protocol/generate",
        json={
            "source_code": SAMPLE_CODE,
            "service_name": "register-ok",
            "base_url": base_url,
            "register_in_oracle": True,
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert not any("Oracle registration failed" in w for w in data["warnings"])
    assert data["oracle_registration_id"] == _oracle_api_id(base_url)

    indexed = await client.get(
        f"/v1/oracle/index/{data['oracle_registration_id']}", headers=HEADERS
    )
    assert indexed.status_code == 200
    assert indexed.json()["url"] == base_url
