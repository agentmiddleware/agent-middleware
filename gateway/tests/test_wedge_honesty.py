"""Docs / OpenAPI / discovery honesty for the transaction-integrity wedge."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import app
from app.routers.well_known import _build_agent_manifest
from app.services.mcp_generator import McpGenerator


PLAIN_DESCRIPTION = (
    "Agent Middleware is a gateway for an agent's configured upstream tools. "
    "For governed calls, it checks a signed permit and sends each accepted "
    "upstream request at most once per key; its signed receipts can be "
    "verified offline."
)
PRECISE_DESCRIPTION = (
    "Transaction-integrity boundary for consequential autonomous actions on the "
    "configured upstream-MCP path: one logical action binds scoped authority and "
    "configured consumption to at most one gateway dispatch and debit, records "
    "delivery_uncertain instead of automatically redispatching, and links gateway "
    "evidence for required external reconciliation."
)


def _assert_plain_description(description: str) -> None:
    assert description == PLAIN_DESCRIPTION
    assert len(description) < 300
    assert description.count(".") == 2
    assert not any(dash in description for dash in ("-", "–", "—"))
    assert not any(
        jargon in description.lower()
        for jargon in (
            "trust plane",
            "transaction boundary",
            "wedge",
            "bounded authority",
        )
    )


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
async def test_permissive_discovery_scopes_guarantees_to_governed_calls(
    client, monkeypatch
):
    cfg = get_settings()
    monkeypatch.setattr(cfg, "TRUST_MODE_ENABLED", True)
    monkeypatch.setattr(cfg, "ALLOW_LEGACY_UNPERMITTED_MCP", True)

    for path in ("/mcp/tools.json", "/.well-known/agent.json"):
        resp = await client.get(path)
        assert resp.status_code == 200
        description = resp.json()["description"]
        _assert_plain_description(description)
        assert "For governed calls," in description


@pytest.fixture
def proof_surfaces_off(monkeypatch):
    import app.main as main_mod
    import app.routers.discover as discover_mod
    import app.routers.docs as docs_mod
    import app.routers.well_known as well_known_mod

    try:
        with monkeypatch.context() as scoped:
            scoped.setenv("ENABLE_PROOF_SURFACES", "false")
            get_settings.cache_clear()
            cfg = get_settings()
            assert cfg.ENABLE_PROOF_SURFACES is False
            for mod in (main_mod, discover_mod, docs_mod, well_known_mod):
                scoped.setattr(mod, "settings", cfg, raising=False)
            yield
    finally:
        get_settings.cache_clear()


@pytest.mark.anyio
async def test_tools_json_envelope_has_versioned_transaction_positioning(client):
    resp = await client.get("/mcp/tools.json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == McpGenerator.MANIFEST_NAME
    assert "B2A Service Marketplace" not in body["name"]
    assert "B2A" not in body["name"]
    _assert_plain_description(body["description"])
    assert body["details"] == (
        PRECISE_DESCRIPTION
        + " Tools listed here are executable local, configured upstream, or "
        "dogfood endpoints — not a metadata-only service catalog."
    )
    assert body["positioning"]["schema_version"] == "1.0"
    assert body["positioning"]["id"] == (
        "transaction_integrity_for_consequential_autonomous_actions"
    )


@pytest.mark.anyio
async def test_agent_json_preserves_precise_copy_beside_plain_description(
    client, proof_surfaces_off
):
    resp = await client.get("/.well-known/agent.json")
    assert resp.status_code == 200
    body = resp.json()
    _assert_plain_description(body["description"])
    assert body["details"] == PRECISE_DESCRIPTION
    assert body["name"] == "agent-middleware-api"
    assert "agent_first" in body


def test_agent_json_preserves_precise_proof_surface_copy(monkeypatch):
    monkeypatch.setattr(get_settings(), "ENABLE_PROOF_SURFACES", True)
    body = _build_agent_manifest().model_dump(mode="json")
    _assert_plain_description(body["description"])
    assert body["details"] == (
        PRECISE_DESCRIPTION
        + " Additional routers are labeled proof surfaces, outside the "
        "transaction-integrity boundary."
    )


@pytest.mark.anyio
async def test_discover_pricing_and_guides_are_trust_plane(client, proof_surfaces_off):
    resp = await client.get("/v1/discover")
    assert resp.status_code == 200
    data = resp.json()

    feature_blob = " ".join(
        f for tier in data["pricing"] for f in tier["features"]
    ).lower()
    assert "telemetry" not in feature_blob
    assert "awi" not in feature_blob
    assert "agent messaging" not in feature_blob
    assert "ai decision" not in feature_blob

    guides = data["integration_guides"]
    assert "awi_adoption" not in guides
    assert guides["mcp_json_rpc"] == "/mcp/messages"
    assert guides["mcp_json_rpc_status"] == "legacy_project_transport"
    # The standard MCP endpoint is on by default.
    assert guides["standard_mcp"] == "/mcp"
    assert guides["wedge"] == "/WEDGE.md"
    assert "partner.notes.write" in guides["dogfood"]

    docs = data["documentation"]
    assert docs["wedge"] == "/WEDGE.md"
    assert docs["partner_guide"] == "/DESIGN_PARTNER_GUIDE.md"


@pytest.mark.anyio
async def test_agent_json_documentation_urls_resolve(client, proof_surfaces_off):
    manifest = await client.get("/.well-known/agent.json")
    assert manifest.status_code == 200
    docs = manifest.json()["documentation"]
    assert "agent_recipes" not in docs
    for key in (
        "wedge",
        "security_limitations",
        "partner_guide",
        "llm_readable",
        "llms_readable",
    ):
        path = docs[key]
        resp = await client.get(path)
        assert resp.status_code == 200, f"{key}={path} returned {resp.status_code}"
        assert len(resp.text) > 50


@pytest.mark.anyio
async def test_agent_json_sdk_integrations_are_honest(client):
    """Discovery must not advertise unpublished PyPI/npm package installs."""
    resp = await client.get("/.well-known/agent.json")
    assert resp.status_code == 200
    integrations = resp.json()["integrations"]

    python_sdk = integrations["python_sdk"]
    assert isinstance(python_sdk, dict)
    assert python_sdk["status"] == "release_artifact_only"
    # The advertised version must be the one an editable install actually
    # yields. Read it from the SDK's own pyproject rather than restating a
    # literal: a hand-copied number is exactly what drifts, and this endpoint
    # exists to be honest about what a client gets.
    sdk_pyproject = (
        Path(__file__).resolve().parents[2] / "sdk" / "python" / "pyproject.toml"
    ).read_text(encoding="utf-8")
    source_version = re.search(r'^version\s*=\s*"([^"]+)"', sdk_pyproject, re.MULTILINE)
    assert source_version is not None, "sdk/python/pyproject.toml has no version"
    assert python_sdk["version"] == source_version.group(1)
    assert python_sdk["install"] == "pip install -e ./sdk/python"
    assert "pip install amw-sdk" not in json.dumps(python_sdk)
    assert "not published" in python_sdk["note"].lower()
    # No release has been cut from this repository yet.
    assert python_sdk["latest_release_tag"] is None

    typescript_sdk = integrations["typescript_sdk"]
    assert isinstance(typescript_sdk, dict)
    assert typescript_sdk["status"] == "not_published"
    assert "npm install @b2a/sdk" not in json.dumps(typescript_sdk)

    assert integrations["mcp"] is True
    assert integrations["mcp_json_rpc"] == "/mcp/messages"
    assert integrations["mcp_json_rpc_status"] == "legacy_project_transport"
    # The standard MCP endpoint is on by default, so it is the preferred path.
    assert integrations["preferred_integration"] == "standard_mcp_streamable_http"
    assert integrations["standard_mcp_streamable_http"] == "/mcp"


@pytest.mark.anyio
async def test_llm_txt_does_not_advertise_unpublished_sdk_installs(client):
    resp = await client.get("/llm.txt")
    assert resp.status_code == 200
    text = resp.text
    assert "pip install amw-sdk" not in text
    assert "npm install @b2a/sdk" not in text
    assert "pip install -e ./sdk/python" in text
    assert "transaction-integrity boundary" in text
    assert "delivery_uncertain" in text
    assert "at most one gateway" in " ".join(text.split())
    assert "different tool input fails closed with an idempotency conflict" in " ".join(
        text.split()
    )
    assert "KYC hooks" not in text
    assert "POST /mcp/messages" in text
    assert "legacy JSON-RPC endpoint" in text
    assert "does not implement standard MCP initialization" in " ".join(text.split())
    assert "unless the deployment's discovery manifest lists it" in " ".join(
        text.split()
    )
    assert (
        "not**" in text.lower()
        or "not\npublished" in text.lower()
        or "not published" in text.lower()
    )
    assert "No published TypeScript SDK" in text


@pytest.mark.anyio
async def test_openapi_description_is_transaction_integrity_not_full_platform(client):
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    description = resp.json()["info"]["description"]
    assert "full agent middleware platform" in description.lower()
    assert "not a full agent middleware platform" in description.lower()
    assert "agent transaction integrity" in description.lower()
    assert "delivery_uncertain" in description
    assert "not proof of the downstream effect" in description.lower()


@pytest.mark.anyio
async def test_docs_index_gates_proof_services(client, proof_surfaces_off):
    resp = await client.get("/docs/index")
    assert resp.status_code == 200
    data = resp.json()
    assert data["positioning"] == data["agent_first"]["positioning"]
    # Compatibility-only v1 alias.
    assert data["product_wedge"] == "governed_mcp_trust_plane"
    paths = {s["path"] for s in data["sections"]}
    assert "/WEDGE.md" in paths
    service_ids = {s["id"] for s in data["services"]}
    assert "mcp-trust-plane" in service_ids
    assert "autonomous-pm" not in service_ids
    assert "iot-bridge" not in service_ids
