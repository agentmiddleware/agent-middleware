"""Tests for ENABLE_PROOF_SURFACES router mounting."""

from __future__ import annotations

from fastapi import FastAPI

from app.main import (
    CORE_TRUST_ROUTERS,
    DORMANT_TRUST_ROUTERS,
    PROOF_SURFACE_ROUTERS,
    mount_dormant_trust_surfaces,
)
from tests.conftest import iter_routes


def _paths(app: FastAPI) -> set[str]:
    return {getattr(route, "path", "") for route in iter_routes(app.routes)}


def test_core_trust_routers_include_mcp_and_permits():
    names = {mod.__name__.split(".")[-1] for mod in CORE_TRUST_ROUTERS}
    assert "mcp" in names
    assert "permits" in names
    assert "receipts" in names
    assert "audit" in names
    assert "docs" in names
    assert "static" in names


def test_proof_surface_routers_include_awi_and_media():
    names = {mod.__name__.split(".")[-1] for mod in PROOF_SURFACE_ROUTERS}
    assert "awi" in names
    assert "media" in names
    assert "oracle" in names
    assert "docs" not in names


def test_dormant_trust_routers_hold_the_second_auth_story_and_kyc():
    """auth (JWT), kyc, planner, pods, and x402 are dormant trust surfaces, not core.

    x402 settlement facilitation joined the dormant tier deliberately: it is
    a real trust surface with no active customer demand, and the settlement
    freeze in docs/settlement-rails.md keeps it unmounted (and unadvertised)
    in production posture. pods (a named group of agent keys under one
    shared budget, see docs/pods.md) joined the same way: real and tested,
    but a new core capability with no named-customer evidence yet per
    AGENTS.md.
    """
    dormant = {mod.__name__.split(".")[-1] for mod in DORMANT_TRUST_ROUTERS}
    assert dormant == {"auth", "kyc", "planner", "pods", "x402"}

    core = {mod.__name__.split(".")[-1] for mod in CORE_TRUST_ROUTERS}
    assert not (dormant & core)
    # dev_keys and webhooks left the core tuple too: dev_keys mounts with its
    # own flag deciding schema visibility; webhooks mount on Stripe config.
    assert "dev_keys" not in core
    assert "webhooks" not in core


def test_core_mount_set_excludes_dormant_paths_until_mounted():
    """Core-only mount answers the wedge; dormant mounts add the rest."""
    app = FastAPI()
    for router_module in CORE_TRUST_ROUTERS:
        app.include_router(router_module.router)

    core_only = _paths(app)
    # Wedge billing stays.
    for path in (
        "/v1/billing/wallets/sponsor",
        "/v1/billing/wallets/agent",
        "/v1/billing/ledger/{wallet_id}",
        "/v1/billing/charge",
        "/v1/billing/pricing",
    ):
        assert path in core_only
    # Dormant trust surfaces are absent from the core mount.
    for path in (
        "/v1/auth/token",
        "/v1/kyc/sessions",
        "/v1/planner/optimize",
        "/v1/billing/top-up",
        "/v1/billing/transfer",
        "/v1/billing/wallets/child",
        "/v1/billing/wallets/{wallet_id}/swarm",
        "/v1/billing/arbitrage",
        "/v1/billing/dry-run/session",
    ):
        assert path not in core_only

    mount_dormant_trust_surfaces(app)
    with_dormant = _paths(app)
    for path in (
        "/v1/auth/token",
        "/v1/kyc/sessions",
        "/v1/planner/optimize",
        "/v1/billing/top-up",
        "/v1/billing/transfer",
        "/v1/billing/wallets/child",
        "/v1/billing/wallets/{wallet_id}/swarm",
        "/v1/billing/arbitrage",
        "/v1/billing/dry-run/session",
    ):
        assert path in with_dormant


def test_proof_surfaces_can_be_omitted_from_mount_set():
    """When ENABLE_PROOF_SURFACES is false, only core trust routers mount.

    Rebuild a minimal FastAPI app the same way main.py does, without
    re-importing the process-global app. Proof-marked tests opt in explicitly;
    the ordinary test-suite import keeps ENABLE_PROOF_SURFACES=false.
    """
    app = FastAPI()
    for router_module in CORE_TRUST_ROUTERS:
        app.include_router(router_module.router)

    core_only = _paths(app)
    assert "/mcp/tools" in core_only
    assert "/v1/permits" in core_only
    assert "/v1/receipts" in core_only
    assert "/docs/index" in core_only
    assert "/WEDGE.md" in core_only
    assert not any(p.startswith("/v1/media") for p in core_only)
    assert not any(p.startswith("/v1/awi/") for p in core_only)

    for router_module in PROOF_SURFACE_ROUTERS:
        app.include_router(router_module.router)

    with_proof = _paths(app)
    assert with_proof > core_only
    assert any(p.startswith("/v1/media") for p in with_proof)
    assert any(p.startswith("/v1/awi/") for p in with_proof)
