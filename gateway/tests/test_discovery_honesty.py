"""Discovery honesty: product wedge vs labeled proof surfaces."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers.well_known import PRODUCT_CAPABILITIES, _build_agent_manifest


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
async def test_agent_json_product_capabilities_are_trust_plane_only(client):
    resp = await client.get("/.well-known/agent.json")
    assert resp.status_code == 200
    data = resp.json()
    assert data["capabilities"] == PRODUCT_CAPABILITIES
    assert all(entry["status"] == "proof_surface" for entry in data["proof_surfaces"])


@pytest.mark.anyio
async def test_discover_labels_proof_surfaces(client):
    resp = await client.get("/v1/discover")
    assert resp.status_code == 200
    data = resp.json()
    by_name = {c["name"]: c for c in data["capabilities"]}
    assert by_name["mcp"]["surface"] == "product"
    assert by_name["permits"]["surface"] == "product"
    assert by_name["awi"]["surface"] == "proof_surface"
    assert by_name["passkey"]["simulation_default"] is True


@pytest.mark.anyio
async def test_awi_manifest_declares_proof_surface(client):
    resp = await client.get("/.well-known/awi.json")
    assert resp.status_code == 200
    data = resp.json()
    assert data["surface"] == "proof_surface"
    limitations = " ".join(data["known_limitations"]).lower()
    assert "proof surface" in limitations or "permit" in limitations


def test_manifest_builder_matches_live_endpoint_shape():
    built = _build_agent_manifest().model_dump(mode="json")
    assert built["capabilities"] == PRODUCT_CAPABILITIES
    assert "permits" in built["endpoints"]
    assert built["agent_first"]["positioning"]["schema_version"] == "1.0"
    assert built["agent_first"]["positioning"]["claim_boundary"] == (
        "gateway_state_machine_not_distributed_acid_or_downstream_effect_proof"
    )
    # Compatibility-only v1 alias.
    assert built["agent_first"]["product_wedge"] == "governed_mcp_trust_plane"


def test_awi_discovery_names_only_http_routes_with_permit_guards():
    import ast
    from pathlib import Path
    from app.routers.well_known import (
        AWI_HTTP_PERMIT_ENDPOINTS,
        PROOF_SURFACE_CATALOG,
        build_awi_manifest,
    )

    root = Path(__file__).resolve().parents[1]
    guarded = set()
    for filename in ("awi.py", "awi_enhanced.py"):
        tree = ast.parse((root / "app/routers" / filename).read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "begin_awi_http_governed"
            ):
                guarded.update(
                    keyword.value.value
                    for keyword in node.keywords
                    if keyword.arg == "endpoint"
                    and isinstance(keyword.value, ast.Constant)
                )
    assert set(AWI_HTTP_PERMIT_ENDPOINTS) == guarded
    assert "POST /v1/awi/sessions" not in guarded
    manifest = build_awi_manifest()
    assert (
        manifest["http_authorization"]["permit_required_endpoints"]
        == AWI_HTTP_PERMIT_ENDPOINTS
    )
    assert manifest["http_authorization"]["required_headers"] == [
        "X-Permit-Id",
        "Idempotency-Key",
    ]
    assert manifest["http_authorization"]["additional_headers"] == {
        "POST /v1/awi/rag/query": ["X-Wallet-Id"]
    }
    assert manifest["surface"] == "proof_surface"
    assert any(
        "unresolved accounting and admission limitations" in text
        for text in manifest["known_limitations"]
    )
    awi = next(item for item in PROOF_SURFACE_CATALOG if item["id"] == "awi_automation")
    assert awi["permit_required_http_endpoints"] == AWI_HTTP_PERMIT_ENDPOINTS
    assert "governed_by_permits" not in awi  # avoid an unqualified all-routes boolean
