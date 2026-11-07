"""Low-severity audit surface: defaults that must stay restrictive.

Covers the gateway half of the API-repo low fixes: CORS defaults to no
origins, the OpenAPI spec never names a domain the operator does not
control, unauthenticated health output carries no broker host or port, and
an empty permit tool allowlist admits no tool.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.core.health import check_mqtt_readiness, gather_dependency_report
from app.db.database import get_session_factory
from app.db.models import PermitModel
from app.main import app, build_openapi_servers
from app.schemas.trust import PermitCreateRequest
from app.services.permits import get_permit_service
from tests.test_trust_helpers import provision_agent_wallet


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# CORS: default deny, explicit opt-in only
# ---------------------------------------------------------------------------


def test_cors_default_is_empty():
    """The field default must not grant any origin CORS headers.

    Reads the declared default, not ``Settings()``: an ambient
    ``CORS_ORIGINS`` in the environment or ``.env`` must not decide this test.
    """
    default = Settings.model_fields["CORS_ORIGINS"].default
    assert default == ""
    origins = [o.strip() for o in default.split(",") if o.strip()]
    assert origins == []


# ---------------------------------------------------------------------------
# OpenAPI servers: no placeholder domain
# ---------------------------------------------------------------------------


def test_openapi_servers_omitted_without_public_url():
    assert build_openapi_servers("", "local") is None
    assert build_openapi_servers("   ", "production") is None


def test_openapi_servers_uses_public_url_when_set():
    servers = build_openapi_servers("https://api.example.com", "local")
    assert servers is not None
    assert servers[0]["url"] == "https://api.example.com"
    assert all("thisisatest" not in entry["url"] for entry in servers)


def test_openapi_servers_never_contains_test_domain():
    for public_url, environment in [
        ("", "local"),
        ("", "production"),
        ("https://api.example.com", "local"),
        ("https://api.example.com", "production"),
    ]:
        servers = build_openapi_servers(public_url, environment)
        assert servers is None or all(
            "thisisatest" not in entry["url"] for entry in servers
        )


def test_openapi_servers_loopback_only_beside_public_url_in_dev():
    servers = build_openapi_servers("https://api.example.com", "local")
    assert servers is not None
    assert [entry["url"] for entry in servers] == [
        "https://api.example.com",
        "http://localhost:8000",
    ]
    prod_servers = build_openapi_servers("https://api.example.com", "production")
    assert prod_servers is not None
    assert [entry["url"] for entry in prod_servers] == ["https://api.example.com"]


# ---------------------------------------------------------------------------
# Readiness: no broker host/port on the unauthenticated payload
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_ready_mqtt_reports_status_without_host_or_port(monkeypatch):
    """Even a reachable broker must not have its address published."""
    import app.core.health as health_module

    async def fake_mqtt(_sim_modes):
        return {"status": "up", "host": "broker.internal.example", "port": 1883}

    monkeypatch.setattr(health_module, "_check_mqtt", fake_mqtt)
    result = await check_mqtt_readiness()
    assert result["status"] == "up"
    rendered = json.dumps(result)
    assert "broker.internal.example" not in rendered
    assert "1883" not in rendered
    assert "host" not in result
    assert "port" not in result


@pytest.mark.anyio
async def test_ready_endpoint_publishes_no_broker_address(client):
    resp = await client.get("/health/ready")
    assert resp.status_code in (200, 503)
    body = resp.json()
    assert "host" not in body["checks"]["mqtt"]
    assert "port" not in body["checks"]["mqtt"]


@pytest.mark.anyio
async def test_full_report_mqtt_carries_no_broker_address(monkeypatch):
    """The full dependency report is also served without login on
    proof-surface hosts, so the projection applies there too."""
    import app.core.health as health_module

    async def fake_mqtt(_sim_modes):
        return {"status": "up", "host": "broker.internal.example", "port": 1883}

    monkeypatch.setattr(health_module, "_check_mqtt", fake_mqtt)
    report = await gather_dependency_report()
    mqtt = report["dependencies"]["mqtt"]
    assert mqtt["status"] == "up"
    assert "host" not in mqtt
    assert "port" not in mqtt


# ---------------------------------------------------------------------------
# Permits: empty allowlist admits no tool, even with matching scopes
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_empty_allowlist_denies_despite_matching_scope(client, clean_database):
    """The case the old `if allowed_tools and ...` guard let through.

    A permit with no tool restriction but an explicit scope for the tool
    used to pass the skipped allowlist check and authorize on scopes alone.
    It must now be denied with permit_tool_not_allowed.
    """
    provisioned = await provision_agent_wallet(client)
    service = get_permit_service()
    permit = await service.create_permit(
        PermitCreateRequest(
            issuer_wallet_id=provisioned["agent_wallet_id"],
            subject_wallet_id=provisioned["agent_wallet_id"],
            subject_key_id=provisioned["key_id"],
            allowed_tools=[],
            scopes=["tool:scoped-hole-tool:invoke", "billing:charge"],
            max_credits=Decimal("50"),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        ),
        subject_key_id=provisioned["key_id"],
    )

    validation = await service.validate_for_action(
        permit_id=permit.permit_id,
        wallet_id=provisioned["agent_wallet_id"],
        tool_name="scoped-hole-tool",
        estimated_credits=Decimal("1"),
        key_id=provisioned["key_id"],
    )
    assert validation.allowed is False
    assert validation.reason == "permit_tool_not_allowed"

    # The stored row still says what it says; only the verdict changed.
    factory = get_session_factory()
    async with factory() as session:
        model = await session.get(PermitModel, permit.permit_id)
        assert model is not None
        assert json.loads(model.allowed_tools_json) == []
