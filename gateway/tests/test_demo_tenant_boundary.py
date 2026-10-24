"""Demo wallet isolation checks at the money and authority boundaries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.db.database import get_session_factory
from app.db.models import WalletModel
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.agent_money import get_agent_money
from app.services.demo_tenant import _memory_counters
from app.services.demo_tenant_boundary import (
    DemoPermitDenied,
    KeyAllowlistDenied,
    check_demo_permit_bounds,
    check_key_allowlist_for_permit,
    demo_route_allowed,
    parse_allowlist,
)
from app.services.ownership import CallerIdentity
from app.services.service_registry import get_service_registry
from app.services.wallet_engine import CrossTenantTransferError
from tests.test_trust_helpers import BOOTSTRAP_HEADERS, provision_agent_wallet


@pytest.fixture
def demo_enabled():
    settings = get_settings()
    names = (
        "ENVIRONMENT",
        "REDIS_URL",
        "ENABLE_DEMO_TENANT",
        "DEMO_ALLOWED_TOOLS",
        "DEMO_ISSUE_PER_IP_PER_DAY",
        "DEMO_ISSUE_GLOBAL_PER_HOUR",
        "DEMO_ISSUE_GLOBAL_PER_DAY",
        "DEMO_MAX_LIVE_KEYS",
    )
    saved = {name: getattr(settings, name) for name in names}
    _memory_counters.clear()
    settings.ENVIRONMENT = "local"
    settings.REDIS_URL = ""
    settings.ENABLE_DEMO_TENANT = True
    settings.DEMO_ALLOWED_TOOLS = ""
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


async def _issue_demo_key(client: AsyncClient) -> dict:
    response = await client.post("/v1/demo/keys", json={})
    assert response.status_code == 201, response.text
    return response.json()


def _invoke_body(tool: str, wallet_id: str, permit_id: str, idem: str) -> dict:
    return {
        "name": tool,
        "arguments": {"message": "hello"},
        "mcp_context": {
            "wallet_id": wallet_id,
            "permit_id": permit_id,
            "idempotency_key": idem,
        },
    }


def test_corrupt_key_allowlist_fails_closed() -> None:
    assert parse_allowlist(None) is None
    assert parse_allowlist('["tool.one"]') == ("tool.one",)
    assert parse_allowlist("not-json") == ()
    assert parse_allowlist('{"tool": "one"}') == ()

    with pytest.raises(KeyAllowlistDenied):
        check_key_allowlist_for_permit(
            key_allowlist=(), allowed_tools=["tool.one"], scopes=None
        )
    with pytest.raises(KeyAllowlistDenied):
        check_key_allowlist_for_permit(
            key_allowlist=("tool.one",),
            allowed_tools=["tool.one"],
            scopes=["tool:tool.two:invoke"],
        )


def test_demo_key_route_policy_blocks_credentials_and_money() -> None:
    for path in (
        "/v1/api-keys",
        "/v1/api-keys/rotate",
        "/v1/billing/top-up",
        "/v1/admin",
    ):
        assert not demo_route_allowed("POST", path)
    assert demo_route_allowed("POST", "/v1/api-keys2")
    assert demo_route_allowed("GET", "/v1/permits")


def test_demo_permit_rejects_cross_tenant_and_caps() -> None:
    now = datetime.now(timezone.utc)
    kwargs = {
        "issuer_tenant": "demo",
        "subject_tenant": "demo",
        "caller_tenant": "demo",
        "allowed_tools": ["tool.one"],
        "max_credits": Decimal("5"),
        "expires_at": now + timedelta(minutes=10),
        "caller_key_expires_at": now + timedelta(hours=1),
        "demo_allowed_tools": ["tool.one"],
        "max_permit_credits": Decimal("5"),
        "max_permit_ttl": timedelta(minutes=30),
        "now": now,
    }
    check_demo_permit_bounds(**kwargs)
    for changed in (
        {"subject_tenant": None},
        {"issuer_tenant": None, "subject_tenant": None},
        {"allowed_tools": ["tool.two"]},
        {"max_credits": Decimal("6")},
        {"expires_at": now + timedelta(minutes=31)},
        {"expires_at": now + timedelta(hours=2)},
    ):
        with pytest.raises(DemoPermitDenied):
            check_demo_permit_bounds(**(kwargs | changed))


@pytest.mark.anyio
@pytest.mark.dormant
async def test_demo_wallets_inherit_tenant_and_cannot_transfer_to_normal(
    clean_database: None,
) -> None:
    money = get_agent_money()
    demo_sponsor = await money.create_sponsor_wallet(
        "demo", "demo@example.invalid", Decimal("20"), require_kyc=False, tenant="demo"
    )
    normal_sponsor = await money.create_sponsor_wallet(
        "normal", "normal@example.invalid", Decimal("20"), require_kyc=False
    )
    demo = await money.create_agent_wallet(
        demo_sponsor.wallet_id,
        "demo-agent",
        Decimal("10"),
        caller=CallerIdentity.for_wallet(demo_sponsor.wallet_id),
    )
    normal = await money.create_agent_wallet(
        normal_sponsor.wallet_id,
        "normal-agent",
        Decimal("10"),
        caller=CallerIdentity.for_wallet(normal_sponsor.wallet_id),
    )
    child = await money.create_child_wallet(
        demo.wallet_id,
        "demo-child",
        Decimal("2"),
        Decimal("2"),
        caller=CallerIdentity.for_wallet(demo.wallet_id),
    )
    async with get_session_factory()() as session:
        assert (await session.get(WalletModel, demo.wallet_id)).tenant == "demo"
        assert (await session.get(WalletModel, child.wallet_id)).tenant == "demo"
        assert (await session.get(WalletModel, normal.wallet_id)).tenant is None

    for source, dest in (
        (demo.wallet_id, normal.wallet_id),
        (normal.wallet_id, demo.wallet_id),
    ):
        with pytest.raises(CrossTenantTransferError):
            await money.transfer(
                source,
                dest,
                Decimal("1"),
                caller=CallerIdentity.for_wallet(source),
            )
    assert (await money.get_wallet(normal.wallet_id)).balance == Decimal("10")
    assert (await money.get_wallet(demo.wallet_id)).balance == Decimal("8")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        refused = await client.post(
            "/v1/billing/transfer",
            params={
                "from_wallet_id": demo.wallet_id,
                "to_wallet_id": normal.wallet_id,
                "amount": 1,
            },
            headers=BOOTSTRAP_HEADERS,
        )
    assert refused.status_code == 403
    assert refused.json()["detail"]["error"] == "cross_tenant_transfer_refused"
    assert (await money.get_wallet(normal.wallet_id)).balance == Decimal("10")


@pytest.mark.anyio
@pytest.mark.dormant
async def test_billing_charge_uses_bound_wallet_when_omitted(
    clean_database: None,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        agent = await provision_agent_wallet(client)
        headers = {**agent["agent_headers"], "Idempotency-Key": "billing-default-1"}
        omitted = await client.post(
            "/v1/billing/charge",
            params={"service": "iot_bridge", "units": 1},
            headers=headers,
        )
        assert omitted.status_code == 200, omitted.text
        explicit = await client.post(
            "/v1/billing/charge",
            params={
                "wallet_id": agent["agent_wallet_id"],
                "service": "iot_bridge",
                "units": 1,
            },
            headers=headers,
        )
        assert explicit.status_code == 200, explicit.text
        assert omitted.json()["entry_id"] == explicit.json()["entry_id"]

        missing = await client.post(
            "/v1/billing/charge",
            params={"service": "iot_bridge", "units": 1},
            headers=BOOTSTRAP_HEADERS,
        )
        assert missing.status_code == 400
        assert missing.json()["detail"]["error"] == "missing_wallet_id"


@pytest.mark.anyio
async def test_demo_key_cannot_touch_normal_wallet(
    clean_database: None, demo_enabled
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("203.0.113.241", 443)),
        base_url="http://test",
    ) as client:
        normal = await provision_agent_wallet(client)
        demo = await _issue_demo_key(client)
        normal_wallet = normal["agent_wallet_id"]
        headers = {"X-API-Key": demo["api_key"]}
        money = get_agent_money()
        before = await money.get_wallet(normal_wallet)
        assert before is not None

        for path in (
            f"/v1/billing/wallets/{normal_wallet}",
            f"/v1/billing/ledger/{normal_wallet}",
        ):
            response = await client.get(path, headers=headers)
            assert response.status_code == 403, (path, response.text)

        charge = await client.post(
            "/v1/billing/charge",
            params={"wallet_id": normal_wallet, "service": "iot_bridge", "units": 1},
            headers=headers,
        )
        assert charge.status_code == 403, charge.text

        permit = await client.post(
            "/v1/permits",
            json={
                "issuer_wallet_id": demo["wallet_id"],
                "subject_wallet_id": normal_wallet,
                "allowed_tools": ["demo-echo"],
                "scopes": ["tool:demo-echo:invoke", "billing:charge"],
                "max_credits": "5",
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(minutes=15)
                ).isoformat(),
            },
            headers={**headers, "Idempotency-Key": "demo-cross-tenant-permit"},
        )
        assert permit.status_code == 403, permit.text
        assert permit.json()["detail"]["error"] == "demo_permit_out_of_bounds"

        mint = await client.post(
            "/v1/api-keys", json={"wallet_id": normal_wallet}, headers=headers
        )
        assert mint.status_code == 403, mint.text
        assert mint.json()["detail"]["error"] == "demo_key_route_forbidden"
        after = await money.get_wallet(normal_wallet)
        assert after is not None and after.balance == before.balance


@pytest.mark.anyio
async def test_normal_key_cannot_touch_demo_wallet(
    clean_database: None, demo_enabled
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("203.0.113.242", 443)),
        base_url="http://test",
    ) as client:
        normal = await provision_agent_wallet(client)
        demo = await _issue_demo_key(client)
        headers = normal["agent_headers"]
        demo_wallet = demo["wallet_id"]

        for path in (
            f"/v1/billing/wallets/{demo_wallet}",
            f"/v1/billing/ledger/{demo_wallet}",
        ):
            response = await client.get(path, headers=headers)
            assert response.status_code == 403, (path, response.text)

        charge = await client.post(
            "/v1/billing/charge",
            params={"wallet_id": demo_wallet, "service": "iot_bridge", "units": 1},
            headers=headers,
        )
        assert charge.status_code == 403, charge.text

        permit = await client.post(
            "/v1/permits",
            json={
                "issuer_wallet_id": normal["agent_wallet_id"],
                "subject_wallet_id": demo_wallet,
                "allowed_tools": ["demo-echo"],
                "scopes": ["tool:demo-echo:invoke", "billing:charge"],
                "max_credits": "5",
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(minutes=15)
                ).isoformat(),
            },
            headers={**headers, "Idempotency-Key": "normal-cross-tenant-permit"},
        )
        assert permit.status_code == 403, permit.text


@pytest.mark.anyio
async def test_demo_key_permit_invoke_replay_and_receipt(
    clean_database: None, demo_enabled
) -> None:
    tool = "demo-boundary-echo"
    registry = get_service_registry()
    registry.register_local(
        service_id=tool,
        name="Demo boundary echo",
        description="In-process demo trust-loop test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=lambda message="ok": {"message": message},
        credits_per_unit=2.0,
        unit_name="call",
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("203.0.113.243", 443)),
            base_url="http://test",
        ) as client:
            demo = await _issue_demo_key(client)
            wallet_id = demo["wallet_id"]
            headers = {"X-API-Key": demo["api_key"]}
            permit = await client.post(
                "/v1/permits",
                json={
                    "issuer_wallet_id": wallet_id,
                    "subject_wallet_id": wallet_id,
                    "subject_key_id": demo["key_id"],
                    "allowed_tools": [tool],
                    "scopes": [f"tool:{tool}:invoke", "billing:charge"],
                    "max_credits": "5",
                    "expires_at": (
                        datetime.now(timezone.utc) + timedelta(minutes=15)
                    ).isoformat(),
                },
                headers={**headers, "Idempotency-Key": "demo-boundary-permit"},
            )
            assert permit.status_code == 201, permit.text

            invoke_body = _invoke_body(
                tool, wallet_id, permit.json()["permit_id"], "demo-boundary-run"
            )
            first = await client.post(
                f"/mcp/tools/{tool}/invoke", json=invoke_body, headers=headers
            )
            assert first.status_code == 200, first.text
            assert first.json()["isError"] is False
            receipt_id = first.json()["receipt"]["receipt_id"]

            replay = await client.post(
                f"/mcp/tools/{tool}/invoke", json=invoke_body, headers=headers
            )
            assert replay.status_code == 200, replay.text
            assert replay.json()["receipt"]["receipt_id"] == receipt_id

            receipt = await client.get(
                f"/v1/receipts/{receipt_id}/portable", headers=headers
            )
            assert receipt.status_code == 200, receipt.text
    finally:
        registry.unregister_local(tool)
