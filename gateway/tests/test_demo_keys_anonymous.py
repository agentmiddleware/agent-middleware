"""Anonymous POST /v1/demo/keys returns a partner.echo-only permit and is rate limited."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.db.database import get_session_factory
from app.db.models import PermitModel
from app.main import app
from app.services.demo_tenant import _memory_counters
from app.services.signing_keys import get_signing_key_service

RATE_LIMIT_HEADERS = ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset")


def _client(host: str) -> AsyncClient:
    transport = ASGITransport(app=app, client=(host, 443))
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
def demo_settings(monkeypatch: pytest.MonkeyPatch):
    settings = get_settings()
    signing_key = base64.b64encode(
        Ed25519PrivateKey.generate().private_bytes(
            Encoding.Raw, PrivateFormat.Raw, NoEncryption()
        )
    ).decode()
    _memory_counters.clear()
    for name, value in {
        "ENVIRONMENT": "local",
        "REDIS_URL": "",
        "ENABLE_DEMO_TENANT": True,
        "DEMO_ALLOWED_TOOLS": "partner.echo",
        "DEMO_KEY_TTL_DAYS": 1,
        "DEMO_KEY_MAX_USES": None,
        "DEMO_WALLET_CREDITS": Decimal("10"),
        "DEMO_WALLET_DAILY_LIMIT": None,
        "DEMO_MAX_PERMIT_CREDITS": Decimal("3"),
        "DEMO_MAX_PERMIT_TTL_MINUTES": 3,
        "DEMO_ISSUE_PER_IP_PER_DAY": 10,
        "DEMO_ISSUE_GLOBAL_PER_HOUR": 100,
        "DEMO_ISSUE_GLOBAL_PER_DAY": 100,
        "DEMO_MAX_LIVE_KEYS": 100,
        "TRUST_SIGNING_PRIVATE_KEY_B64": signing_key,
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(get_signing_key_service(), "_private_key", None)
    try:
        yield settings
    finally:
        _memory_counters.clear()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def _permit(permit_id: str) -> PermitModel:
    async with get_session_factory()() as session:
        permit = await session.get(PermitModel, permit_id)
    assert permit is not None
    return permit


@pytest.mark.anyio
async def test_demo_key_returns_partner_echo_permit(
    clean_database, demo_settings
) -> None:
    async with _client("203.0.113.240") as client:
        response = await client.post("/v1/demo/keys", json={})
    assert response.status_code == 201, response.text
    body = response.json()
    now = datetime.now(timezone.utc)

    # Existing fields are kept.
    for field in ("api_key", "key_id", "wallet_id", "sponsor_wallet_id", "note"):
        assert body[field]
    assert body["tenant"] == "demo"
    assert body["allowed_tools"] == ["partner.echo"]
    assert _parse(body["expires_at"]) > now

    assert body["demo_tool"] == "partner.echo"
    assert body["permit_id"]
    permit_expires_at = _parse(body["permit_expires_at"])
    assert (
        now
        < permit_expires_at
        <= now + timedelta(seconds=demo_settings.DEMO_AUTO_PERMIT_TTL_SECONDS + 5)
    )
    for header in RATE_LIMIT_HEADERS:
        assert header in response.headers

    permit = await _permit(body["permit_id"])
    assert json.loads(permit.allowed_tools_json) == ["partner.echo"]
    assert set(json.loads(permit.scopes_json)) == {
        "tool:partner.echo:invoke",
        "billing:charge",
    }
    assert permit.issuer_wallet_id == body["wallet_id"]
    assert permit.subject_wallet_id == body["wallet_id"]
    assert permit.subject_key_id == body["key_id"]
    assert permit.status == "active"
    assert Decimal("0") < permit.max_credits <= demo_settings.DEMO_MAX_PERMIT_CREDITS
    assert permit.max_credits <= demo_settings.DEMO_AUTO_PERMIT_MAX_CREDITS
    assert permit.expires_at - permit.issued_at <= timedelta(
        seconds=demo_settings.DEMO_AUTO_PERMIT_TTL_SECONDS
    )


@pytest.mark.anyio
async def test_demo_key_never_leaks_valid_api_keys_or_wider_scope(
    clean_database, demo_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Distinct client-chosen values so a leak would be visible in the body.
    admin_keys = ["amw-admin-key-not-for-demo-a", "amw-admin-key-not-for-demo-b"]
    monkeypatch.setattr(demo_settings, "VALID_API_KEYS", ",".join(admin_keys))
    async with _client("203.0.113.241") as client:
        response = await client.post("/v1/demo/keys", json={})
    assert response.status_code == 201, response.text
    for key in admin_keys:
        assert key not in response.text
        assert key not in str(response.headers)
    body = response.json()
    assert body["api_key"] not in admin_keys
    assert body["tenant"] == "demo"
    assert body["allowed_tools"] == ["partner.echo"]
    assert "scopes" not in body
    permit = await _permit(body["permit_id"])
    assert json.loads(permit.allowed_tools_json) == ["partner.echo"]
    assert not any(
        scope.startswith(("admin", "wallet:", "keys:", "permit:"))
        or (scope.startswith("tool:") and scope != "tool:partner.echo:invoke")
        for scope in json.loads(permit.scopes_json)
    )


@pytest.mark.anyio
async def test_demo_key_without_partner_echo_in_allowlist_mints_no_permit(
    clean_database, demo_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(demo_settings, "DEMO_ALLOWED_TOOLS", "partner.other")
    async with _client("203.0.113.242") as client:
        response = await client.post("/v1/demo/keys", json={})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["permit_id"] is None
    assert body["permit_expires_at"] is None
    assert body["demo_tool"] == "partner.echo"


@pytest.mark.anyio
async def test_demo_key_anonymous_per_ip_rate_limit(
    clean_database, demo_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(demo_settings, "DEMO_ISSUE_PER_IP_PER_DAY", 1)
    async with _client("203.0.113.243") as client:
        first = await client.post("/v1/demo/keys", json={})
        second = await client.post("/v1/demo/keys", json={})
    assert first.status_code == 201, first.text
    assert second.status_code == 429, second.text
    assert second.json()["detail"] == {
        "error": "demo_issuance_rate_limited",
        "scope": "ip",
    }
    assert int(second.headers["Retry-After"]) > 0
    for header in RATE_LIMIT_HEADERS:
        assert header in second.headers
    assert "api_key" not in second.text

    # The limit is per client address, not global.
    async with _client("203.0.113.244") as other:
        third = await other.post("/v1/demo/keys", json={})
    assert third.status_code == 201, third.text
