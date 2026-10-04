"""Self-serve dev key provisioning (POST /v1/dev-keys/self-provision).

The endpoint mints a wallet-scoped key with no pre-shared secret, which is
only tolerable because it is unreachable everywhere that matters: 404 until
ENABLE_DEV_KEY_SELF_PROVISION is set, 403 in production-like environments
even when set, and a boot-time guardrail that refuses the production
posture outright. The minted credential must be the ordinary DB-backed
wallet key class — never bootstrap admin — and its tenant boundary must
hold against other wallets.
"""

from decimal import Decimal

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from app.core.auth import get_auth_context
from app.core.config import get_settings
from app.core.trust_mode import (
    TrustModeGuardrailError,
    validate_trust_mode_config,
)
from app.main import app


@pytest.fixture()
def self_provision_settings():
    """Enable the endpoint locally; restore every mutated field afterwards."""
    settings = get_settings()
    saved = {
        name: getattr(settings, name)
        for name in ("ENVIRONMENT", "ENABLE_DEV_KEY_SELF_PROVISION")
    }
    settings.ENVIRONMENT = "local"
    settings.ENABLE_DEV_KEY_SELF_PROVISION = True
    try:
        yield settings
    finally:
        for name, value in saved.items():
            setattr(settings, name, value)


@pytest.fixture()
async def client():
    """Client carrying the rate-limiter bypass header.

    The endpoint itself takes no auth dependency and never reads this header;
    it exists so these tests don't drain the shared "anonymous" rate-limit
    bucket that the whole suite's unauthenticated requests count against
    (RateLimitMiddleware exempts X-API-Key: test-key — see tests/conftest.py).
    The no-credential property is proven by the one deliberately bare request
    in test_self_provision_mints_wallet_scoped_key.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"X-API-Key": "test-key"},
    ) as c:
        yield c


async def test_disabled_by_default_answers_404(client):
    settings = get_settings()
    assert settings.ENABLE_DEV_KEY_SELF_PROVISION is False
    resp = await client.post("/v1/dev-keys/self-provision", json={})
    assert resp.status_code == 404


async def test_refused_in_production_like_environment_even_when_enabled(
    client, self_provision_settings
):
    self_provision_settings.ENVIRONMENT = "production"
    resp = await client.post("/v1/dev-keys/self-provision", json={})
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "dev_key_self_provision_forbidden"


async def test_self_provision_mints_wallet_scoped_key(
    self_provision_settings, clean_database
):
    # Deliberately bare client — no headers at all — proving end to end that
    # self-provisioning needs no pre-shared secret. Exactly one such request
    # runs in the suite so the shared anonymous rate bucket is barely touched.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as bare:
        resp = await bare.post(
            "/v1/dev-keys/self-provision",
            json={"agent_id": "test-dev-agent", "budget_credits": 500},
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["agent_id"] == "test-dev-agent"
    assert body["sponsor_wallet_id"]
    assert body["wallet_id"]
    assert body["api_key"].startswith(body["key_prefix"])

    # The minted key authenticates as an ordinary wallet-scoped DB key —
    # never bootstrap admin, and bound to exactly the wallet it was minted for.
    context = await get_auth_context(api_key=body["api_key"])
    assert context.source == "db"
    assert context.is_bootstrap_admin is False
    assert context.wallet_id == body["wallet_id"]
    context.require_wallet_access(body["wallet_id"])  # own wallet: allowed
    with pytest.raises(HTTPException) as exc:
        context.require_wallet_access(body["sponsor_wallet_id"])
    assert exc.value.status_code == 403


async def test_agent_wallet_receives_requested_budget(
    client, self_provision_settings, clean_database
):
    resp = await client.post(
        "/v1/dev-keys/self-provision",
        json={"agent_id": "budget-agent", "budget_credits": 250},
    )
    assert resp.status_code == 201, resp.text
    wallet_id = resp.json()["wallet_id"]

    from app.services.agent_money import get_agent_money

    wallet = await get_agent_money().get_wallet(wallet_id)
    assert Decimal(str(wallet.balance)) == Decimal("250")


async def test_budget_is_bounded(client, self_provision_settings):
    resp = await client.post(
        "/v1/dev-keys/self-provision",
        json={"budget_credits": 10_000_000},
    )
    assert resp.status_code == 422


async def test_agent_id_is_generated_when_omitted(
    client, self_provision_settings, clean_database
):
    resp = await client.post("/v1/dev-keys/self-provision", json={})
    assert resp.status_code == 201, resp.text
    assert resp.json()["agent_id"].startswith("dev-agent-")


async def test_malformed_agent_id_rejected(client, self_provision_settings):
    resp = await client.post(
        "/v1/dev-keys/self-provision",
        json={"agent_id": "not ok/../weird"},
    )
    assert resp.status_code == 422


async def test_cross_origin_browser_request_rejected(client, self_provision_settings):
    # A browser making a cross-origin fetch sends an Origin whose host is not
    # the request host. Because this endpoint returns a live secret and runs
    # under wildcard CORS, that request must be refused before anything is
    # minted — otherwise attacker page JS reads the key cross-origin.
    resp = await client.post(
        "/v1/dev-keys/self-provision",
        json={},
        headers={"Origin": "https://evil.example"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "origin_not_allowed"


async def test_same_origin_browser_request_allowed(
    client, self_provision_settings, clean_database
):
    # An Origin whose host matches the request host is a same-origin call and
    # must pass — the guard targets foreign origins only.
    resp = await client.post(
        "/v1/dev-keys/self-provision",
        json={},
        headers={"Origin": "http://test"},
    )
    assert resp.status_code == 201, resp.text


async def test_no_origin_header_passes_through(
    client, self_provision_settings, clean_database
):
    # Real dev agents (CLIs, SDKs, curl) send no Origin and must not be
    # affected by the browser hardening.
    resp = await client.post("/v1/dev-keys/self-provision", json={})
    assert resp.status_code == 201, resp.text


async def test_agent_manifest_advertises_self_provision_only_when_enabled(
    self_provision_settings,
):
    from app.routers.well_known import _authentication_manifest

    # Enabled locally: discovery surfaces the mint endpoint so a bootstrapping
    # agent finds the credential it can actually get here.
    enabled = _authentication_manifest()
    assert "dev_self_provision" in enabled
    assert enabled["dev_self_provision"]["endpoint"] == "/v1/dev-keys/self-provision"
    assert enabled["dev_self_provision"]["requires_pre_shared_secret"] is False

    # Flag off: production-honest manifest, no self-serve advertisement.
    self_provision_settings.ENABLE_DEV_KEY_SELF_PROVISION = False
    assert "dev_self_provision" not in _authentication_manifest()

    # Flag on but production-like (a posture that can't actually boot, but the
    # manifest must never advertise it regardless).
    self_provision_settings.ENABLE_DEV_KEY_SELF_PROVISION = True
    self_provision_settings.ENVIRONMENT = "production"
    assert "dev_self_provision" not in _authentication_manifest()


async def test_missing_credentials_401_hints_self_provision_when_enabled(
    self_provision_settings,
):
    # The first "how do I tap in?" moment: an unauthenticated call should tell
    # a local agent about the self-serve mint when it's enabled, and stay
    # silent otherwise.
    with pytest.raises(HTTPException) as exc:
        await get_auth_context(api_key=None)
    assert "self_provision" in exc.value.detail

    self_provision_settings.ENABLE_DEV_KEY_SELF_PROVISION = False
    with pytest.raises(HTTPException) as exc:
        await get_auth_context(api_key=None)
    assert "self_provision" not in exc.value.detail


def test_guardrail_rejects_self_provision_in_production():
    with pytest.raises(TrustModeGuardrailError) as exc_info:
        validate_trust_mode_config(
            environment="production",
            trust_mode_enabled=True,
            signing_private_key_b64="",
            allow_legacy_unpermitted_mcp=False,
            enable_proof_surfaces=False,
            enable_dev_key_self_provision=True,
        )
    assert "ENABLE_DEV_KEY_SELF_PROVISION" in str(exc_info.value)


def test_guardrail_allows_self_provision_locally():
    validate_trust_mode_config(
        environment="local",
        trust_mode_enabled=True,
        signing_private_key_b64="",
        allow_legacy_unpermitted_mcp=True,
        enable_dev_key_self_provision=True,
    )
