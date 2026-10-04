"""
Contract tests for agent-first discovery: bootstrap URLs must stay public and
aligned with get_agent_first_metadata() so autonomous clients do not drift.
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
async def test_bootstrap_sequence_urls_are_public_ok(client):
    meta = get_agent_first_metadata()
    for path in meta["bootstrap_sequence"]:
        resp = await client.get(path)
        assert resp.status_code == 200, f"bootstrap path not OK: {path}"


@pytest.mark.anyio
async def test_simulation_truth_endpoint_has_simulation_modes(client):
    """The advertised truth endpoint reports posture on every deployment.

    `enable_proof_surfaces` is always present. The per-service
    `simulation_modes` map appears only when proof surfaces are mounted —
    with them unmounted (the posture this suite runs, and production's) the
    payload covers exactly the wedge dependencies and there is no simulated
    surface to disclose.
    """
    from app.core.config import get_settings

    path = get_agent_first_metadata()["simulation_and_dependency_truth"]
    resp = await client.get(path)
    assert resp.status_code == 200
    data = resp.json()
    assert "enable_proof_surfaces" in data
    assert data["enable_proof_surfaces"] is False
    assert "simulation_modes" not in data

    cfg = get_settings()
    previous = cfg.ENABLE_PROOF_SURFACES
    cfg.ENABLE_PROOF_SURFACES = True
    try:
        mounted = (await client.get(path)).json()
    finally:
        cfg.ENABLE_PROOF_SURFACES = previous
    assert "simulation_modes" in mounted
    assert mounted["enable_proof_surfaces"] is True


@pytest.mark.anyio
async def test_agent_first_declares_product_wedge(client):
    meta = get_agent_first_metadata()
    positioning = meta["positioning"]
    assert positioning["schema_version"] == "1.0"
    assert positioning["effective_date"] == "2026-08-28"
    assert (
        positioning["id"]
        == "transaction_integrity_for_consequential_autonomous_actions"
    )
    assert positioning["label"] == (
        "Transaction integrity for consequential autonomous actions"
    )
    assert positioning["scope"] == {
        "transaction_state_machine": "configured_upstream_mcp_tool_only",
        "local_and_dogfood_tools": ("not_covered_by_dispatch_uncertainty_semantics"),
    }
    assert positioning["supersedes"] == ["product_wedge", "product_loop"]
    assert positioning["semantics"] == [
        "logical_action_identity",
        "bounded_authority_consumption",
        "at_most_one_gateway_dispatch_and_debit",
        "delivery_uncertain_no_automatic_redispatch",
        "linked_gateway_evidence",
        "authoritative_external_reconciliation_required",
    ]
    assert positioning["canonical_loop"][-1] == (
        "authoritative_external_reconciliation_required"
    )
    assert positioning["legacy_protocol_identifiers"] == {
        "mcp_server_name": "Agent Middleware MCP Trust Plane"
    }
    assert positioning["claim_boundary"] == (
        "gateway_state_machine_not_distributed_acid_or_downstream_effect_proof"
    )

    # Deprecated v1 aliases remain for exact-match consumers during migration.
    assert meta["product_wedge"] == "governed_mcp_trust_plane"
    assert meta["product_wedge"] in positioning["legacy_aliases"]
    assert meta["product_loop"][0] == "discover"
    assert "receipt" in meta["product_loop"]
    # The suite runs with proof surfaces unmounted, so the primary manifest
    # stays wedge-only: no proof-surface note travels without its catalog.
    assert "proof_surface_note" not in meta
    resp = await client.get("/.well-known/agent.json")
    assert resp.json()["agent_first"] == meta


@pytest.mark.anyio
async def test_agent_manifest_offers_honest_credential_free_local_proof(client):
    resp = await client.get("/.well-known/agent.json")
    assert resp.status_code == 200
    data = resp.json()

    trial = data["try_it"]
    assert trial["mode"] == "local_self_hosted"
    assert trial["command"] == "make prove-trust-plane"
    assert trial["requires_live_credentials"] is False
    assert trial["live_access"] == "operator_issued"
    assert "signed_receipt" in trial["proves"]
    assert "replay_without_second_charge" in trial["proves"]
    assert data["authentication"]["public_self_serve"] is False
