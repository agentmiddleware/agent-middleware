"""Public demo key issuance and its browser, storage, and auth boundaries."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from itertools import count

import pytest
from alembic import command
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, func, inspect, select, text

from app.core.config import Settings, get_settings
from app.core.jwt import get_jwt_service
from app.db.database import get_session_factory
from app.db.models import APIKeyModel, WalletModel
from app.main import app
from app.services.api_key_service import get_api_key_service
from app.services.demo_tenant import _memory_counters
from tests.test_trust_helpers import BOOTSTRAP_HEADERS, provision_agent_wallet


_KEYS_PATH = "/v1/demo/keys"
_FUNNEL_PATH = "/v1/admin/demo-keys/funnel"
_ORIGINS = (
    "https://www.thisisatest.tech",
    "https://thisisatest.tech",
    "https://agentmiddleware.dev",
    "https://www.agentmiddleware.dev",
)
_CLIENT_NUMBERS = count(1)
_DEMO_SETTINGS = (
    "ENVIRONMENT",
    "REDIS_URL",
    "ENABLE_DEMO_TENANT",
    "DEMO_ALLOWED_ORIGINS",
    "DEMO_KEY_MAX_USES",
    "DEMO_ISSUE_PER_IP_PER_DAY",
    "DEMO_ISSUE_GLOBAL_PER_HOUR",
    "DEMO_ISSUE_GLOBAL_PER_DAY",
    "DEMO_MAX_LIVE_KEYS",
)


@pytest.fixture
async def client():
    # Each test gets its own client IP so the real per-IP limiter has no
    # order-dependent state. The test suite uses 127.0.0.1 elsewhere.
    ip = f"203.0.113.{next(_CLIENT_NUMBERS)}"
    transport = ASGITransport(app=app, client=(ip, 443))
    async with AsyncClient(transport=transport, base_url="http://test") as value:
        yield value


@pytest.fixture
def demo_enabled():
    settings = get_settings()
    saved = {name: getattr(settings, name) for name in _DEMO_SETTINGS}
    _memory_counters.clear()
    settings.ENVIRONMENT = "local"
    settings.REDIS_URL = ""
    settings.ENABLE_DEMO_TENANT = True
    settings.DEMO_ALLOWED_ORIGINS = ",".join(_ORIGINS)
    settings.DEMO_ISSUE_PER_IP_PER_DAY = 10
    settings.DEMO_ISSUE_GLOBAL_PER_HOUR = 100
    settings.DEMO_ISSUE_GLOBAL_PER_DAY = 100
    settings.DEMO_MAX_LIVE_KEYS = 100
    try:
        yield settings
    finally:
        for name, value in saved.items():
            setattr(settings, name, value)
        _memory_counters.clear()


async def _issue(client: AsyncClient, **attribution: str) -> dict:
    response = await client.post(_KEYS_PATH, json=attribution)
    assert response.status_code == 201
    return response.json()


async def _key_row(key_id: str) -> APIKeyModel:
    async with get_session_factory()() as session:
        row = await session.get(APIKeyModel, key_id)
        assert row is not None
        session.expunge(row)
        return row


@pytest.mark.anyio
async def test_issued_key_shape_wallets_and_hash(
    client: AsyncClient, clean_database: None, demo_enabled
) -> None:
    body = await _issue(client)
    assert set(body) == {
        "api_key",
        "key_id",
        "key_prefix",
        "wallet_id",
        "sponsor_wallet_id",
        "tenant",
        "allowed_tools",
        "expires_at",
        "max_uses",
        "budget_credits",
        "base_url",
        "note",
    }
    assert body["api_key"].startswith("b2a_")
    assert body["key_prefix"] == body["api_key"][:8]
    assert body["tenant"] == "demo"
    assert body["allowed_tools"] is None
    assert body["expires_at"] is None
    assert body["max_uses"] is None
    assert Decimal(str(body["budget_credits"])) == Decimal("1000")
    assert "shown" in body["note"].lower()

    key = await _key_row(body["key_id"])
    assert key.wallet_id == body["wallet_id"]
    assert key.tenant == "demo"
    assert key.key_hash.startswith("scrypt$")
    assert body["api_key"] not in key.key_hash
    assert key.key_lookup == body["api_key"][:16]
    matched = await get_api_key_service().validate_key(body["api_key"], consume=False)
    assert matched is not None and matched.key_id == key.key_id
    after_lookup = await _key_row(body["key_id"])
    assert after_lookup.use_count == 0
    assert after_lookup.first_used_at is None

    async with get_session_factory()() as session:
        agent = await session.get(WalletModel, body["wallet_id"])
        sponsor = await session.get(WalletModel, body["sponsor_wallet_id"])
    assert agent is not None and sponsor is not None
    assert agent.tenant == sponsor.tenant == "demo"
    assert agent.parent_wallet_id == sponsor.wallet_id
    assert agent.balance == Decimal("1000")


@pytest.mark.anyio
async def test_optional_attribution_is_normalized_and_stored_only(
    client: AsyncClient, clean_database: None, demo_enabled
) -> None:
    body = await _issue(
        client,
        utm_source="  newsletter  ",
        utm_medium="email",
        utm_campaign="launch",
        utm_content="hero",
        utm_term="term",
        referrer=" https://news.example/item ",
    )
    assert not {"utm_source", "utm_medium", "utm_campaign", "referrer"} & set(body)
    key = await _key_row(body["key_id"])
    assert (
        key.utm_source,
        key.utm_medium,
        key.utm_campaign,
        key.utm_content,
        key.utm_term,
        key.referrer,
    ) == (
        "newsletter",
        "email",
        "launch",
        "hero",
        "term",
        "https://news.example/item",
    )


@pytest.mark.anyio
async def test_attribution_validation_and_omitted_body(
    client: AsyncClient, clean_database: None, demo_enabled
) -> None:
    too_long = await client.post(_KEYS_PATH, json={"utm_campaign": "x" * 129})
    assert too_long.status_code == 422
    wrong_type = await client.post(_KEYS_PATH, json={"referrer": ["wrong"]})
    assert wrong_type.status_code == 422
    async with get_session_factory()() as session:
        before = await session.scalar(select(func.count()).select_from(APIKeyModel))
    assert before == 0

    omitted = await client.post(_KEYS_PATH)
    assert omitted.status_code == 201
    blank = await _issue(client, utm_source="  ", referrer="  ")
    long_referrer = "https://example.com/" + "a" * 600
    clipped = await _issue(client, referrer=long_referrer)
    for body in (omitted.json(), blank):
        row = await _key_row(body["key_id"])
        assert row.utm_source is None and row.referrer is None
    assert (await _key_row(clipped["key_id"])).referrer == long_referrer[:512]


@pytest.mark.anyio
@pytest.mark.parametrize("max_uses", [None, 10])
async def test_first_authenticated_use_is_recorded_only_once(
    client: AsyncClient, clean_database: None, demo_enabled, max_uses: int | None
) -> None:
    demo_enabled.DEMO_KEY_MAX_USES = max_uses
    body = await _issue(client)
    assert (await _key_row(body["key_id"])).first_used_at is None

    for _ in range(3):
        response = await client.get(
            "/v1/me/authority", headers={"X-API-Key": body["api_key"]}
        )
        assert response.status_code == 200
        row = await _key_row(body["key_id"])
        assert row.first_used_at is not None
        if _ == 0:
            first_used_at = row.first_used_at
        else:
            assert row.first_used_at == first_used_at
    assert row.last_used_at is not None and row.last_used_at >= first_used_at


@pytest.mark.anyio
@pytest.mark.parametrize("credential", ["api_key", "jwt"])
@pytest.mark.parametrize("legacy_wallet_label", [False, True])
async def test_denied_demo_auth_does_not_consume_capped_key(
    client: AsyncClient,
    clean_database: None,
    demo_enabled,
    monkeypatch: pytest.MonkeyPatch,
    credential: str,
    legacy_wallet_label: bool,
) -> None:
    demo_enabled.DEMO_KEY_MAX_USES = 1
    body = await _issue(client)
    if legacy_wallet_label:
        async with get_session_factory()() as session:
            key = await session.get(APIKeyModel, body["key_id"])
            assert key is not None
            key.tenant = None
            await session.commit()

    if credential == "jwt":
        monkeypatch.setattr(
            demo_enabled,
            "TRUST_SIGNING_PRIVATE_KEY_B64",
            base64.b64encode(bytes(range(32))).decode(),
        )
        token = get_jwt_service().create_access_token(
            wallet_id=body["wallet_id"], key_id=body["key_id"], scopes=[]
        )
        headers = {"Authorization": f"Bearer {token}"}
    else:
        headers = {"X-API-Key": body["api_key"]}

    forbidden = await client.get(_FUNNEL_PATH, headers=headers)
    assert forbidden.status_code == 403
    assert forbidden.json()["detail"]["error"] == "demo_key_route_forbidden"
    row = await _key_row(body["key_id"])
    assert (row.first_used_at, row.last_used_at, row.use_count) == (None, None, 0)

    demo_enabled.ENABLE_DEMO_TENANT = False
    disabled = await client.get("/v1/me/authority", headers=headers)
    assert disabled.status_code == 403
    assert disabled.json()["detail"]["error"] == "demo_tenant_disabled"
    row = await _key_row(body["key_id"])
    assert (row.first_used_at, row.last_used_at, row.use_count) == (None, None, 0)

    demo_enabled.ENABLE_DEMO_TENANT = True
    allowed = await client.get("/v1/me/authority", headers=headers)
    assert allowed.status_code == 200
    row = await _key_row(body["key_id"])
    assert row.first_used_at is not None
    assert row.last_used_at is not None
    assert row.use_count == 1

    expected_error = "no_active_api_key" if credential == "jwt" else "invalid_api_key"
    expected_status = 401 if credential == "jwt" else 403
    exhausted = await client.get(_FUNNEL_PATH, headers=headers)
    assert exhausted.status_code == expected_status
    assert exhausted.json()["detail"]["error"] == expected_error
    demo_enabled.ENABLE_DEMO_TENANT = False
    disabled_exhausted = await client.get("/v1/me/authority", headers=headers)
    assert disabled_exhausted.status_code == expected_status
    assert disabled_exhausted.json()["detail"]["error"] == expected_error
    after = await _key_row(body["key_id"])
    assert (after.first_used_at, after.last_used_at, after.use_count) == (
        row.first_used_at,
        row.last_used_at,
        1,
    )


@pytest.mark.anyio
async def test_disabled_issuance_hides_body_validation_and_disables_existing_key(
    client: AsyncClient, clean_database: None, demo_enabled
) -> None:
    body = await _issue(client)
    demo_enabled.ENABLE_DEMO_TENANT = False
    response = await client.post(_KEYS_PATH, json={"utm_source": ["invalid"]})
    assert response.status_code == 404
    disabled_key = await client.get(
        "/v1/me/authority", headers={"X-API-Key": body["api_key"]}
    )
    assert disabled_key.status_code == 403
    assert disabled_key.json()["detail"]["error"] == "demo_tenant_disabled"
    demo_enabled.ENABLE_DEMO_TENANT = True
    restored = await client.get(
        "/v1/me/authority", headers={"X-API-Key": body["api_key"]}
    )
    assert restored.status_code == 200


def test_default_browser_origins_include_both_domains() -> None:
    configured = Settings.model_fields["DEMO_ALLOWED_ORIGINS"].default
    assert set(configured.split(",")) >= set(_ORIGINS)


@pytest.mark.anyio
@pytest.mark.parametrize("origin", _ORIGINS)
async def test_allowed_origin_preflight_and_post(
    origin: str, client: AsyncClient, clean_database: None, demo_enabled
) -> None:
    preflight = await client.options(
        _KEYS_PATH,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers.get("access-control-allow-origin") == origin
    assert "POST" in preflight.headers.get("access-control-allow-methods", "")
    assert (
        "content-type"
        in preflight.headers.get("access-control-allow-headers", "").lower()
    )

    response = await client.post(_KEYS_PATH, json={}, headers={"Origin": origin})
    assert response.status_code == 201
    assert response.headers.get("access-control-allow-origin") == origin


@pytest.mark.anyio
async def test_unlisted_origin_cannot_mint_or_read_cross_origin(
    client: AsyncClient, clean_database: None, demo_enabled
) -> None:
    response = await client.post(
        _KEYS_PATH, json={}, headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "origin_not_allowed"
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.anyio
async def test_per_ip_issuance_limit_returns_structured_429(
    client: AsyncClient, clean_database: None, demo_enabled
) -> None:
    demo_enabled.DEMO_ISSUE_PER_IP_PER_DAY = 2
    await _issue(client)
    await _issue(client)
    limited = await client.post(_KEYS_PATH, json={})
    assert limited.status_code == 429
    assert limited.json()["detail"] == {
        "error": "demo_issuance_rate_limited",
        "scope": "ip",
    }
    assert limited.headers["retry-after"] == "86400"
    async with get_session_factory()() as session:
        count_keys = await session.scalar(select(func.count()).select_from(APIKeyModel))
    assert count_keys == 2


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("setting", "scope"),
    [
        ("DEMO_ISSUE_GLOBAL_PER_HOUR", "global_hour"),
        ("DEMO_ISSUE_GLOBAL_PER_DAY", "global_day"),
        ("DEMO_MAX_LIVE_KEYS", "live_keys"),
    ],
)
async def test_global_and_live_key_limits_stop_additional_issuance(
    client: AsyncClient,
    clean_database: None,
    demo_enabled,
    setting: str,
    scope: str,
) -> None:
    setattr(demo_enabled, setting, 1)
    await _issue(client)
    limited = await client.post(_KEYS_PATH, json={})
    assert limited.status_code == 429
    assert limited.json()["detail"] == {
        "error": "demo_issuance_rate_limited",
        "scope": scope,
    }
    assert limited.headers["retry-after"] == "3600"
    async with get_session_factory()() as session:
        count_keys = await session.scalar(select(func.count()).select_from(APIKeyModel))
    assert count_keys == 1


@pytest.mark.anyio
async def test_get_keys_returns_json_post_instructions_with_issuance_disabled(
    client: AsyncClient, demo_enabled
) -> None:
    demo_enabled.ENABLE_DEMO_TENANT = False
    response = await client.get(_KEYS_PATH)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert isinstance(body, dict)
    instructions = json.dumps(body).lower()
    for expected in (
        "demo",
        "key",
        "post",
        "application/json",
        "utm_source",
        "referrer",
        "curl",
        "docs",
    ):
        assert expected in instructions


@pytest.mark.anyio
async def test_demo_key_cannot_mint_another_key(
    client: AsyncClient, clean_database: None, demo_enabled
) -> None:
    body = await _issue(client)
    response = await client.post(
        "/v1/api-keys",
        json={"wallet_id": body["wallet_id"]},
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "demo_key_route_forbidden"


@pytest.mark.anyio
async def test_admin_funnel_counts_issued_and_used_keys_by_source_and_referrer_host(
    client: AsyncClient, clean_database: None, demo_enabled
) -> None:
    used_source = await _issue(
        client, utm_source="producthunt", utm_medium="launch", utm_campaign="ph1"
    )
    await _issue(
        client, utm_source="producthunt", utm_medium="launch", utm_campaign="ph1"
    )
    used_referrer = await _issue(
        client, referrer="https://news.ycombinator.com/item?id=1"
    )
    await _issue(client, referrer="https://news.ycombinator.com/newest")
    await _issue(client)
    await provision_agent_wallet(client)

    for key in (used_source, used_source, used_referrer):
        response = await client.get(
            "/v1/me/authority", headers={"X-API-Key": key["api_key"]}
        )
        assert response.status_code == 200

    report = await client.get(_FUNNEL_PATH, headers=BOOTSTRAP_HEADERS)
    assert report.status_code == 200
    rows = {
        (row["utm_source"], row["utm_campaign"], row["referrer_host"]): row
        for row in report.json()["rows"]
    }
    assert set(rows) == {
        ("producthunt", "ph1", None),
        ("(none)", None, "news.ycombinator.com"),
        ("(none)", None, None),
    }
    source = rows[("producthunt", "ph1", None)]
    assert (source["keys_issued"], source["keys_used"], source["utm_medium"]) == (
        2,
        1,
        "launch",
    )
    referrer = rows[("(none)", None, "news.ycombinator.com")]
    assert (referrer["keys_issued"], referrer["keys_used"]) == (2, 1)
    unattributed = rows[("(none)", None, None)]
    assert (unattributed["keys_issued"], unattributed["keys_used"]) == (1, 0)
    assert source["first_issued_at"] <= source["last_issued_at"]


@pytest.mark.anyio
async def test_admin_funnel_filters_issuance_time(
    client: AsyncClient, clean_database: None, demo_enabled
) -> None:
    await _issue(client, utm_source="newsletter")
    now = datetime.now(timezone.utc)
    past = (now - timedelta(hours=1)).isoformat()
    future = (now + timedelta(hours=1)).isoformat()

    later = await client.get(
        _FUNNEL_PATH, params={"since": future}, headers=BOOTSTRAP_HEADERS
    )
    assert later.status_code == 200
    assert later.json()["rows"] == []

    window = await client.get(
        _FUNNEL_PATH,
        params={"since": past, "until": future},
        headers=BOOTSTRAP_HEADERS,
    )
    assert window.status_code == 200
    assert [
        (row["utm_source"], row["keys_issued"]) for row in window.json()["rows"]
    ] == [("newsletter", 1)]

    earlier = await client.get(
        _FUNNEL_PATH, params={"until": past}, headers=BOOTSTRAP_HEADERS
    )
    assert earlier.status_code == 200
    assert earlier.json()["rows"] == []


@pytest.mark.anyio
async def test_admin_funnel_requires_bootstrap_key(
    client: AsyncClient, clean_database: None, demo_enabled
) -> None:
    demo = await _issue(client)
    normal = await provision_agent_wallet(client)

    assert (await client.get(_FUNNEL_PATH)).status_code == 401
    unknown = await client.get(_FUNNEL_PATH, headers={"X-API-Key": "not-a-real-key"})
    assert unknown.status_code == 403
    assert unknown.json()["detail"]["error"] == "invalid_api_key"

    wallet = await client.get(_FUNNEL_PATH, headers=normal["agent_headers"])
    assert wallet.status_code == 403
    assert wallet.json()["detail"]["error"] == "admin_access_denied"

    denied = await client.get(_FUNNEL_PATH, headers={"X-API-Key": demo["api_key"]})
    assert denied.status_code == 403
    assert denied.json()["detail"]["error"] == "demo_key_route_forbidden"


@pytest.mark.anyio
async def test_admin_funnel_stays_available_with_issuance_disabled(
    client: AsyncClient, clean_database: None, demo_enabled
) -> None:
    await _issue(client, utm_source="newsletter")
    demo_enabled.ENABLE_DEMO_TENANT = False
    report = await client.get(_FUNNEL_PATH, headers=BOOTSTRAP_HEADERS)
    assert report.status_code == 200
    assert [
        (row["utm_source"], row["keys_issued"]) for row in report.json()["rows"]
    ] == [("newsletter", 1)]


def test_demo_key_migration_adds_nullable_fields_backfills_once_and_is_repeatable(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "demo-key-migration.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    config = Config("alembic.ini")
    command.upgrade(config, "042_permit_action_binding")
    asyncio.set_event_loop(asyncio.new_event_loop())

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO wallets (wallet_id, wallet_type, owner_key, balance, "
                "lifetime_credits, lifetime_debits, daily_spent, auto_refill, status) "
                "VALUES ('legacy-wallet', 'sponsor', '', 0, 0, 0, 0, 0, 'active')"
            )
        )
        insert = text(
            "INSERT INTO api_keys (key_id, wallet_id, key_hash, key_prefix, "
            "status, rotation_count, created_at, use_count, last_used_at) "
            "VALUES (:key_id, 'legacy-wallet', :key_id, 'b2a_xxxx', 'active', "
            "0, '2026-10-01 00:00:00', 0, :last_used_at)"
        )
        for key_id, last_used_at in (
            ("k-used", "2026-10-10 00:00:00"),
            ("k-unused", None),
            ("k-normal", "2026-10-11 00:00:00"),
        ):
            connection.execute(insert, {"key_id": key_id, "last_used_at": last_used_at})
    engine.dispose()

    command.upgrade(config, "043_demo_key_fields")
    asyncio.set_event_loop(asyncio.new_event_loop())
    engine = create_engine(f"sqlite:///{db_path}")
    wallet_columns = {
        column["name"]: column for column in inspect(engine).get_columns("wallets")
    }
    key_columns = {
        column["name"]: column for column in inspect(engine).get_columns("api_keys")
    }
    assert wallet_columns["tenant"]["nullable"]
    for name in (
        "tenant",
        "allowed_tools_json",
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_content",
        "utm_term",
        "referrer",
        "first_used_at",
    ):
        assert key_columns[name]["nullable"]
    assert "demo_key_funnel_by_source" in inspect(engine).get_view_names()

    with engine.begin() as connection:
        first_used = dict(
            connection.execute(text("SELECT key_id, first_used_at FROM api_keys")).all()
        )
        assert first_used["k-used"] is not None
        assert first_used["k-normal"] is not None
        assert first_used["k-unused"] is None
        connection.execute(
            text(
                "UPDATE api_keys SET tenant = 'demo', utm_source = 'ph' "
                "WHERE key_id IN ('k-used', 'k-unused')"
            )
        )
        connection.execute(
            text(
                "UPDATE api_keys SET first_used_at = '2026-10-09 00:00:00', "
                "last_used_at = '2026-10-12 00:00:00' WHERE key_id = 'k-used'"
            )
        )
        connection.execute(
            text("UPDATE api_keys SET first_used_at = NULL WHERE key_id = 'k-normal'")
        )

    revision = ScriptDirectory.from_config(config).get_revision("043_demo_key_fields")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            revision.module.upgrade()
        first_used = dict(
            connection.execute(text("SELECT key_id, first_used_at FROM api_keys")).all()
        )
        assert str(first_used["k-used"]).startswith("2026-10-09")
        assert str(first_used["k-normal"]).startswith("2026-10-11")
        view_rows = connection.execute(
            text(
                "SELECT utm_source, keys_issued, keys_used "
                "FROM demo_key_funnel_by_source"
            )
        ).all()
    assert [tuple(row) for row in view_rows] == [("ph", 2, 1)]
    engine.dispose()
