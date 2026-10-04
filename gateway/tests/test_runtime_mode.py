"""
Tests for app/core/runtime_mode.py — the per-service simulation flag.

Covers the three public entry points (is_simulation, require_simulation,
get_simulation_modes), the unknown-service error, and the /health/dependencies
surface that consumers read the state from.
"""

import pytest
from httpx import AsyncClient, ASGITransport

from app.core.config import get_settings
from app.core.runtime_mode import (
    SERVICE_NAMES,
    UnknownServiceError,
    get_simulation_modes,
    is_simulation,
    require_simulation,
)
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.pricing import PROOF_SURFACE_CATEGORIES


# ServiceCategory values that are deliberately NOT runtime services: they are
# wallet/platform-level accounting categories, never a tool an agent invokes,
# so they carry no SIMULATION_MODE_* flag and never reach is_simulation().
# Everything else must be registered in runtime_mode._SERVICE_TO_SETTING.
NON_RUNTIME_CATEGORIES: frozenset[ServiceCategory] = frozenset(
    {
        ServiceCategory.PLATFORM_FEE,
        ServiceCategory.SWARM_DELEGATION,
    }
)


HEADERS = {"X-API-Key": "test-key"}


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """
    get_settings is lru_cached. Tests that mutate the cached instance's
    attributes must restore them afterwards; this fixture captures and
    restores each simulation field around every test.
    """
    settings = get_settings()
    fields = [
        "SIMULATION_MODE_ORACLE",
        "SIMULATION_MODE_RED_TEAM",
        "SIMULATION_MODE_RTAAS",
        "SIMULATION_MODE_MEDIA_ENGINE",
        "SIMULATION_MODE_IOT_BRIDGE",
        "SIMULATION_MODE_TELEMETRY_PM",
        "SIMULATION_MODE_AGENT_COMMS",
        "SIMULATION_MODE_CONTENT_FACTORY",
        "SIMULATION_MODE_PROTOCOL_GEN",
        "SIMULATION_MODE_SANDBOX",
    ]
    saved = {f: getattr(settings, f) for f in fields}
    yield
    for f, v in saved.items():
        setattr(settings, f, v)


def test_service_names_are_complete():
    """The SERVICE_NAMES set must stay in sync with Settings fields."""
    expected = {
        "oracle",
        "red_team",
        "rtaas",
        "media_engine",
        "iot_bridge",
        "telemetry_pm",
        "agent_comms",
        "content_factory",
        "protocol_gen",
        "sandbox",
        "human_approval",
    }
    assert SERVICE_NAMES == expected


def test_every_service_category_is_classified():
    """
    Every ServiceCategory must be a gated runtime service or an explicitly
    named non-runtime category.

    A category with neither is silently treated as running real effects:
    app/routers/mcp.py catches the UnknownServiceError and falls back to
    ``simulation=False``, so app/services/policies.py never trips a wallet
    policy's ``require_real_effects`` for it, and mcp_integration_truth
    annotates its tools ``platform`` in /mcp/tools.json. Adding a category
    without deciding which side it belongs on is therefore a silent policy
    hole, so force the decision here.
    """
    unclassified = sorted(
        c.value
        for c in ServiceCategory
        if c.value not in SERVICE_NAMES and c not in NON_RUNTIME_CATEGORIES
    )
    assert not unclassified, (
        f"ServiceCategory values with no SIMULATION_MODE_* flag: {unclassified}. "
        "Either register the service in _SERVICE_TO_SETTING "
        "(app/core/runtime_mode.py) and add the matching Settings field, or "
        "add it to NON_RUNTIME_CATEGORIES in this file if it is a "
        "wallet/platform-level category that is never invoked as a tool."
    )


def test_proof_surface_categories_are_simulation_gated():
    """
    Anything billing calls a frozen proof surface must report itself as
    simulated at runtime. pricing.PROOF_SURFACE_CATEGORIES and
    docs/PROOF_SURFACES.md are the product boundary; SERVICE_NAMES is what the
    invoke path actually consults. If they disagree, a preview stub advertises
    itself as a real effect.
    """
    ungated = sorted(
        c.value for c in PROOF_SURFACE_CATEGORIES if c.value not in SERVICE_NAMES
    )
    assert not ungated, (
        f"Frozen proof-surface categories with no SIMULATION_MODE_* flag: {ungated}. "
        "Register them in _SERVICE_TO_SETTING (app/core/runtime_mode.py), or "
        "obtain an explicit product decision to drop them from "
        "PROOF_SURFACE_CATEGORIES; see docs/PROOF_SURFACES.md."
    )


def test_default_is_simulation_true():
    """Every known service starts in simulation mode."""
    for name in SERVICE_NAMES:
        assert is_simulation(name) is True, f"{name} should default to simulation"


def test_is_simulation_reflects_settings_change():
    settings = get_settings()
    settings.SIMULATION_MODE_ORACLE = False
    assert is_simulation("oracle") is False
    assert is_simulation("red_team") is True  # others unaffected


def test_is_simulation_unknown_service():
    with pytest.raises(UnknownServiceError):
        is_simulation("nope")


def test_require_simulation_passes_when_simulating():
    """Sanity: by default, guard is a no-op."""
    require_simulation("oracle")  # should not raise


def test_require_simulation_raises_with_frozen_scope_guidance():
    settings = get_settings()
    settings.SIMULATION_MODE_TELEMETRY_PM = False
    with pytest.raises(NotImplementedError) as exc:
        require_simulation("telemetry_pm")
    msg = str(exc.value)
    assert "telemetry_pm" in msg
    assert "SIMULATION_MODE_TELEMETRY_PM" in msg
    assert "docs/PROOF_SURFACES.md" in msg


def test_get_simulation_modes_returns_all_services():
    modes = get_simulation_modes()
    assert set(modes.keys()) == SERVICE_NAMES
    assert all(v is True for v in modes.values())


def test_get_simulation_modes_reflects_toggles():
    settings = get_settings()
    settings.SIMULATION_MODE_MEDIA_ENGINE = False
    modes = get_simulation_modes()
    assert modes["media_engine"] is False
    # Everything else still True
    assert all(v for k, v in modes.items() if k != "media_engine")


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
async def test_health_dependencies_surfaces_simulation_modes(client):
    """Simulation state stays fully reported wherever proof surfaces mount.

    With them unmounted (production posture) the public payload omits the
    map — the full truth lives in the startup runtime_posture log and in
    gather_dependency_report(); see test_health_public_projection.py.
    """
    settings = get_settings()
    previous = settings.ENABLE_PROOF_SURFACES
    settings.ENABLE_PROOF_SURFACES = True
    try:
        resp = await client.get("/health/dependencies")
    finally:
        settings.ENABLE_PROOF_SURFACES = previous
    assert resp.status_code == 200
    body = resp.json()
    assert "simulation_modes" in body
    assert set(body["simulation_modes"].keys()) == SERVICE_NAMES


@pytest.mark.proof
@pytest.mark.anyio
async def test_oracle_crawl_works_when_simulation_disabled(client):
    """Durable crawl path: synthetic extractor runs; DB persistence is allowed."""
    settings = get_settings()
    settings.SIMULATION_MODE_ORACLE = False
    resp = await client.post(
        "/v1/oracle/crawl",
        json={"url": "https://api.example.com", "directory_type": "openapi"},
        headers=HEADERS,
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["url"] == "https://api.example.com"
    assert data["status"] == "indexed"
