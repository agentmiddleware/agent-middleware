"""
Test that /v1/discover does not advertise KYC when Stripe is not configured.

This verifies that the discover endpoint uses the same truth as
/health/dependencies: when stripe.status=not_configured, KYC must be
absent from the capabilities list.
"""

import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.core.config import get_settings


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def proof_surfaces_on():
    """KYC routes mount only with proof surfaces; capability honesty follows.

    The KYC router is a dormant trust surface now, so /v1/discover may only
    advertise the capability when the routes are actually mounted. These
    tests flip the flag to exercise the stripe-configured half of the truth;
    test_discover_omits_kyc_when_proof_surfaces_off covers the other half.
    """
    cfg = get_settings()
    previous = cfg.ENABLE_PROOF_SURFACES
    cfg.ENABLE_PROOF_SURFACES = True
    yield cfg
    cfg.ENABLE_PROOF_SURFACES = previous


@pytest.mark.anyio
async def test_discover_omits_kyc_when_stripe_not_configured(
    client, monkeypatch, proof_surfaces_on
):
    """When Stripe is not configured, /v1/discover must not list KYC capability."""
    import app.routers.discover as discover_mod

    monkeypatch.setenv("STRIPE_SECRET_KEY", "")
    get_settings.cache_clear()
    cfg = get_settings()
    cfg.ENABLE_PROOF_SURFACES = True
    assert not cfg.STRIPE_SECRET_KEY
    monkeypatch.setattr(discover_mod, "settings", cfg, raising=False)

    health_resp = await client.get("/health/dependencies")
    assert health_resp.status_code == 200
    health_data = health_resp.json()
    assert health_data["dependencies"]["stripe"]["status"] == "not_configured"

    discover_resp = await client.get("/v1/discover")
    assert discover_resp.status_code == 200
    discover_data = discover_resp.json()

    capability_names = {cap["name"] for cap in discover_data["capabilities"]}
    assert "kyc" not in capability_names, (
        "KYC capability must not be present when Stripe is not_configured"
    )

    get_settings.cache_clear()


@pytest.mark.anyio
async def test_discover_includes_kyc_when_stripe_configured(
    client, monkeypatch, proof_surfaces_on
):
    """When Stripe is configured, /v1/discover must list KYC capability."""
    import app.routers.discover as discover_mod

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake_key_for_testing")
    get_settings.cache_clear()
    cfg = get_settings()
    cfg.ENABLE_PROOF_SURFACES = True
    assert cfg.STRIPE_SECRET_KEY
    monkeypatch.setattr(discover_mod, "settings", cfg, raising=False)

    discover_resp = await client.get("/v1/discover")
    assert discover_resp.status_code == 200
    discover_data = discover_resp.json()

    capability_names = {cap["name"] for cap in discover_data["capabilities"]}
    assert "kyc" in capability_names, (
        "KYC capability must be present when Stripe is configured"
    )

    kyc_cap = next(cap for cap in discover_data["capabilities"] if cap["name"] == "kyc")
    assert kyc_cap["surface"] == "product"
    assert kyc_cap["category"] == "compliance"
    assert "stripe" in kyc_cap["description"].lower()

    get_settings.cache_clear()


@pytest.mark.anyio
async def test_discover_and_health_stripe_truth_match(
    client, monkeypatch, proof_surfaces_on
):
    """Discover's KYC presence must match health's stripe.status truth."""
    import app.routers.discover as discover_mod

    for stripe_key, expected_health_status, should_have_kyc in [
        ("", "not_configured", False),
        ("sk_test_fake", "not_configured", True),
    ]:
        monkeypatch.setenv("STRIPE_SECRET_KEY", stripe_key)
        get_settings.cache_clear()
        cfg = get_settings()
        cfg.ENABLE_PROOF_SURFACES = True
        monkeypatch.setattr(discover_mod, "settings", cfg, raising=False)

        health_resp = await client.get("/health/dependencies")
        assert health_resp.status_code == 200
        health_data = health_resp.json()
        stripe_status = health_data["dependencies"]["stripe"]["status"]

        discover_resp = await client.get("/v1/discover")
        assert discover_resp.status_code == 200
        discover_data = discover_resp.json()
        capability_names = {cap["name"] for cap in discover_data["capabilities"]}
        has_kyc = "kyc" in capability_names

        if stripe_status == "not_configured":
            assert not has_kyc, (
                f"When health reports stripe={stripe_status}, "
                "discover must not list KYC"
            )
        else:
            assert has_kyc, (
                f"When health reports stripe={stripe_status}, discover must list KYC"
            )

    get_settings.cache_clear()


@pytest.mark.anyio
async def test_discover_omits_kyc_when_proof_surfaces_off(client, monkeypatch):
    """Stripe configured but proof surfaces unmounted: KYC routes answer 404,
    so discover must not advertise the capability."""
    import app.routers.discover as discover_mod

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake_key_for_testing")
    get_settings.cache_clear()
    cfg = get_settings()
    cfg.ENABLE_PROOF_SURFACES = False
    monkeypatch.setattr(discover_mod, "settings", cfg, raising=False)

    discover_resp = await client.get("/v1/discover")
    assert discover_resp.status_code == 200
    capability_names = {cap["name"] for cap in discover_resp.json()["capabilities"]}
    assert "kyc" not in capability_names

    get_settings.cache_clear()
