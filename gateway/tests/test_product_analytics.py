"""PostHog product tracking: identify/signup/activation, fail-safe, off by default.

The PostHog HTTP send (``product_analytics._post``) is mocked throughout; no
test touches the network.
"""

import asyncio
import logging

import pytest
from httpx import ASGITransport, AsyncClient

from app.core import product_analytics
from app.core.config import get_settings
from app.main import app
from app.services.api_key_service import get_api_key_service

FAKE_KEY = "phc_test_not_a_real_key"


@pytest.fixture
def sent(monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def fake_post(host, payload):
        calls.append((host, payload))

    monkeypatch.setattr(product_analytics, "_post", fake_post)
    return calls


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("POSTHOG_API_KEY", FAKE_KEY)
    monkeypatch.setenv("POSTHOG_HOST", "https://posthog.example/")


@pytest.fixture
async def client(clean_database):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"X-API-Key": "test-key"},
    ) as c:
        yield c


@pytest.fixture
def self_provision(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ENVIRONMENT", "local")
    monkeypatch.setattr(settings, "ENABLE_DEV_KEY_SELF_PROVISION", True)


def _events(calls, distinct_id=None):
    return [
        p["event"]
        for _, p in calls
        if distinct_id is None or p["distinct_id"] == distinct_id
    ]


async def test_noop_without_api_key(monkeypatch, sent):
    monkeypatch.delenv("POSTHOG_API_KEY", raising=False)
    assert product_analytics.is_enabled() is False
    product_analytics.track_signup("w1", method="x", email="a@b.dev")
    product_analytics.track_activation("w1", key_id="k", wallet_id="w1")
    await product_analytics.drain_pending()
    assert sent == []
    assert not product_analytics._pending


async def test_blank_api_key_is_noop(monkeypatch, sent):
    monkeypatch.setenv("POSTHOG_API_KEY", "   ")
    product_analytics.capture("signup", "w1")
    await product_analytics.drain_pending()
    assert sent == []


async def test_identify_carries_email_and_signup_follows(enabled, sent):
    product_analytics.track_signup("w1", method="sponsor_wallet", email="a@b.dev")
    await product_analytics.drain_pending()
    assert [p["event"] for _, p in sent] == ["$identify", "signup"]
    host, ident = sent[0]
    assert host == "https://posthog.example"
    assert ident["distinct_id"] == "w1"
    assert ident["api_key"] == FAKE_KEY
    assert ident["properties"]["$set"] == {"email": "a@b.dev"}
    assert sent[1][1]["properties"]["method"] == "sponsor_wallet"


async def test_default_host(monkeypatch, sent):
    monkeypatch.setenv("POSTHOG_API_KEY", FAKE_KEY)
    monkeypatch.delenv("POSTHOG_HOST", raising=False)
    product_analytics.capture("signup", "w1")
    await product_analytics.drain_pending()
    assert sent[0][0] == product_analytics.DEFAULT_HOST


async def test_send_failure_is_swallowed_and_never_logs_key(
    enabled, monkeypatch, caplog
):
    async def boom(host, payload):
        raise RuntimeError(f"failed with {payload['api_key']}")

    monkeypatch.setattr(product_analytics, "_post", boom)
    caplog.set_level(logging.DEBUG, logger="app.core.product_analytics")
    product_analytics.track_signup("w1", method="x", email="a@b.dev")
    await product_analytics.drain_pending()
    assert "product_analytics_send_failed" in caplog.text
    assert FAKE_KEY not in caplog.text


async def test_slow_send_does_not_block_caller(enabled, monkeypatch):
    gate = asyncio.Event()

    async def slow(host, payload):
        await gate.wait()

    monkeypatch.setattr(product_analytics, "_post", slow)
    product_analytics.capture("signup", "w1")  # returns immediately
    assert len(product_analytics._pending) == 1
    gate.set()
    await product_analytics.drain_pending()


def test_no_running_loop_is_noop(enabled, sent):
    product_analytics.capture("signup", "w1")
    assert sent == []


async def test_sponsor_wallet_signup_identifies_with_email(client, enabled, sent):
    resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Tracking Sponsor",
            "email": "tracking-sponsor@b2a.dev",
            "initial_credits": 0,
        },
    )
    assert resp.status_code == 201, resp.text
    wallet_id = resp.json()["wallet_id"]
    await product_analytics.drain_pending()
    assert _events(sent, wallet_id) == ["$identify", "signup"]
    assert sent[0][1]["properties"]["$set"]["email"] == "tracking-sponsor@b2a.dev"


async def test_request_succeeds_when_tracking_raises(client, enabled, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("tracking exploded")

    monkeypatch.setattr(product_analytics, "_config", boom)
    resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={"sponsor_name": "S", "email": "s@b2a.dev", "initial_credits": 0},
    )
    assert resp.status_code == 201, resp.text


async def test_activation_fires_once_per_account(client, enabled, sent, self_provision):
    resp = await client.post("/v1/dev-keys/self-provision", json={})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    sponsor = body["sponsor_wallet_id"]
    await product_analytics.drain_pending()
    assert _events(sent, sponsor) == ["$identify", "signup"]
    assert sent[1][1]["properties"]["method"] == "dev_self_provision"

    service = get_api_key_service()
    # Non-consuming preflight is not a use.
    assert await service.validate_key(body["api_key"], consume=False)
    assert await service.validate_key(body["api_key"])
    assert await service.validate_key(body["api_key"])
    await product_analytics.drain_pending()
    activations = [p for _, p in sent if p["event"] == "activation"]
    assert len(activations) == 1
    assert activations[0]["distinct_id"] == sponsor
    assert activations[0]["properties"]["key_id"] == body["key_id"]
    assert activations[0]["properties"]["wallet_id"] == body["wallet_id"]

    # A second key on the same account: its first use is not an activation.
    second = await service.create_key(wallet_id=body["wallet_id"], key_name="2nd")
    assert await service.validate_key(second["api_key"])
    await product_analytics.drain_pending()
    assert len([p for _, p in sent if p["event"] == "activation"]) == 1


async def test_concurrent_first_uses_fire_one_activation(
    client, enabled, sent, self_provision
):
    body = (await client.post("/v1/dev-keys/self-provision", json={})).json()
    service = get_api_key_service()
    results = await asyncio.gather(
        *(service.validate_key(body["api_key"]) for _ in range(5))
    )
    assert all(results)
    await product_analytics.drain_pending()
    assert len([p for _, p in sent if p["event"] == "activation"]) == 1


async def test_first_use_recorded_when_tracking_disabled(
    client, monkeypatch, sent, self_provision
):
    monkeypatch.delenv("POSTHOG_API_KEY", raising=False)
    body = (await client.post("/v1/dev-keys/self-provision", json={})).json()
    service = get_api_key_service()
    assert await service.validate_key(body["api_key"])
    key = await service.validate_key(body["api_key"], consume=False)
    assert key.first_used_at is not None
    assert sent == []


async def test_concurrent_first_uses_of_two_keys_fire_one_activation(
    client, enabled, sent, self_provision
):
    body = (await client.post("/v1/dev-keys/self-provision", json={})).json()
    service = get_api_key_service()
    second = await service.create_key(wallet_id=body["wallet_id"], key_name="2nd")
    results = await asyncio.gather(
        service.validate_key(body["api_key"]),
        service.validate_key(second["api_key"]),
        service.validate_key(body["api_key"]),
        service.validate_key(second["api_key"]),
    )
    assert all(results)
    await product_analytics.drain_pending()
    activations = [p for _, p in sent if p["event"] == "activation"]
    assert len(activations) == 1
    assert activations[0]["distinct_id"] == body["sponsor_wallet_id"]


async def test_activation_claim_recorded_on_root_even_when_tracking_off(
    client, monkeypatch, sent, self_provision
):
    from app.db.database import get_session_factory
    from app.db.models import WalletModel

    monkeypatch.delenv("POSTHOG_API_KEY", raising=False)
    body = (await client.post("/v1/dev-keys/self-provision", json={})).json()
    assert await get_api_key_service().validate_key(body["api_key"])
    async with get_session_factory()() as session:
        root = await session.get(WalletModel, body["sponsor_wallet_id"])
        assert root.product_activated_at is not None
    assert sent == []


async def test_activation_claim_failure_does_not_fail_auth(
    client, enabled, sent, self_provision, monkeypatch
):
    from app.services.api_key_service import APIKeyService

    async def boom(session, wallet_id):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(APIKeyService, "_root_wallet_id", staticmethod(boom))
    body = (await client.post("/v1/dev-keys/self-provision", json={})).json()
    service = get_api_key_service()
    assert await service.validate_key(body["api_key"]) is not None
    reread = await service.validate_key(body["api_key"], consume=False)
    assert reread.first_used_at is not None
    await product_analytics.drain_pending()
    assert "activation" not in _events(sent)


async def test_root_wallet_walk_handles_nesting_and_cycles(clean_database):
    from app.db.database import get_session_factory
    from app.db.models import WalletModel
    from app.services.api_key_service import APIKeyService

    async with get_session_factory()() as session:
        session.add(WalletModel(wallet_id="w-root", wallet_type="sponsor"))
        await session.commit()
        session.add(
            WalletModel(
                wallet_id="w-mid", wallet_type="agent", parent_wallet_id="w-root"
            )
        )
        await session.commit()
        session.add(
            WalletModel(
                wallet_id="w-leaf", wallet_type="agent", parent_wallet_id="w-mid"
            )
        )
        await session.commit()
        assert await APIKeyService._root_wallet_id(session, "w-leaf") == "w-root"
        assert await APIKeyService._root_wallet_id(session, "w-root") == "w-root"
        assert await APIKeyService._root_wallet_id(session, "missing") == "missing"


def test_posthog_settings_load_from_dotenv_fields(monkeypatch):
    from app.core.config import Settings

    monkeypatch.delenv("POSTHOG_API_KEY", raising=False)
    monkeypatch.delenv("POSTHOG_HOST", raising=False)
    s = Settings(
        _env_file=None,
        POSTHOG_API_KEY=FAKE_KEY,
        POSTHOG_HOST="https://eu.i.posthog.com",
    )
    assert s.POSTHOG_API_KEY.get_secret_value() == FAKE_KEY
    assert FAKE_KEY not in repr(s)
    monkeypatch.setattr(get_settings(), "POSTHOG_API_KEY", s.POSTHOG_API_KEY)
    monkeypatch.setattr(get_settings(), "POSTHOG_HOST", s.POSTHOG_HOST)
    assert product_analytics._config() == (FAKE_KEY, "https://eu.i.posthog.com")


def test_settings_accepts_posthog_entries_in_env_file(tmp_path, monkeypatch):
    from app.core.config import Settings

    env = tmp_path / ".env"
    env.write_text(
        f"POSTHOG_API_KEY={FAKE_KEY}\nPOSTHOG_HOST=https://eu.i.posthog.com\n"
    )
    monkeypatch.delenv("POSTHOG_API_KEY", raising=False)
    monkeypatch.delenv("POSTHOG_HOST", raising=False)
    s = Settings(_env_file=str(env))
    assert s.POSTHOG_HOST == "https://eu.i.posthog.com"


async def test_pending_sends_are_capped(enabled, monkeypatch):
    gate = asyncio.Event()

    async def slow(host, payload):
        await gate.wait()

    monkeypatch.setattr(product_analytics, "_post", slow)
    monkeypatch.setattr(product_analytics, "MAX_PENDING", 3)
    for _ in range(10):
        product_analytics.capture("signup", "w1")
    assert len(product_analytics._pending) == 3
    gate.set()
    await product_analytics.drain_pending()


async def test_shutdown_drains_and_closes_shared_client(enabled, monkeypatch):
    client = product_analytics._get_client()
    assert product_analytics._get_client() is client  # reused
    await product_analytics.shutdown()
    assert client.is_closed
    assert product_analytics._client is None
