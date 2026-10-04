"""
Tests for Agent Financial Gateways (Billing Service).
Validates the full money flow: sponsor wallets → agent provisioning →
micro-metering → 402 insufficient funds → top-ups → arbitrage reporting.
"""

import asyncio
import pytest
from datetime import timedelta
from decimal import Decimal
from httpx import AsyncClient, ASGITransport
from pydantic import ValidationError
from sqlalchemy import select

from app.main import app
from app.core.config import Settings, get_settings
from app.db.database import get_session_factory
from app.db.models import LedgerEntryModel, WalletModel
from app.services.agent_money import get_agent_money
from app.services.audit_log import list_audit_events


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def api_headers():
    return {"X-API-Key": "test-key"}


# --- Sponsor Wallet ---


@pytest.mark.anyio
async def test_create_sponsor_wallet(client, api_headers):
    resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Acme Corp",
            "email": "billing@acme.com",
            "initial_credits": 50000.0,
        },
        headers=api_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["wallet_type"] == "sponsor"
    assert data["owner_name"] == "Acme Corp"
    assert data["balance"] == 50000.0
    assert Decimal(data["balance_exact"]) == Decimal("50000")
    assert data["wallet_id"].startswith("spn-")
    assert data["status"] == "active"


@pytest.mark.anyio
async def test_create_sponsor_zero_balance(client, api_headers):
    resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={"sponsor_name": "Broke Corp", "email": "a@b.com"},
        headers=api_headers,
    )
    assert resp.status_code == 201
    assert resp.json()["balance"] == 0.0


@pytest.mark.anyio
async def test_wallet_key_cannot_create_sponsor_or_seed_credits(
    client,
    api_headers,
    clean_database,
):
    sponsor = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Existing tenant",
            "email": "existing-tenant@example.com",
            "initial_credits": 0,
        },
        headers=api_headers,
    )
    wallet_id = sponsor.json()["wallet_id"]
    key = await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id, "key_name": "tenant-key"},
        headers=api_headers,
    )

    response = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Unauthorized liability",
            "email": "unauthorized-liability@example.com",
            "initial_credits": 1_000_000,
        },
        headers={"X-API-Key": key.json()["api_key"]},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "admin_access_denied"


def test_sponsor_creation_openapi_declares_bootstrap_admin_boundary():
    operation = app.openapi()["paths"]["/v1/billing/wallets/sponsor"]["post"]

    assert "bootstrap operator" in operation["description"].lower()
    assert "403" in operation["responses"]


@pytest.mark.anyio
async def test_create_sponsor_with_credits_writes_ledger(
    client, api_headers, clean_database
):
    """Funded sponsor create must persist wallet before initial ledger credit.

    Regression for ledger_entries_wallet_id_fkey on Postgres when autoflush
    is off and the UOW inserts ledger_entries before wallets.
    """
    resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Funded Corp",
            "email": "funded@example.com",
            "initial_credits": 1000.0,
            "require_kyc": False,
        },
        headers=api_headers,
    )
    assert resp.status_code == 201, resp.text
    wallet_id = resp.json()["wallet_id"]
    assert resp.json()["balance"] == 1000.0

    factory = get_session_factory()
    async with factory() as session:
        wallet = await session.get(WalletModel, wallet_id)
        assert wallet is not None
        assert wallet.balance == Decimal("1000")

        result = await session.execute(
            select(LedgerEntryModel).where(LedgerEntryModel.wallet_id == wallet_id)
        )
        entries = list(result.scalars().all())
        assert len(entries) == 1
        assert entries[0].action == "credit"
        assert entries[0].amount == Decimal("1000")
        assert entries[0].balance_after == Decimal("1000")
        assert entries[0].description == "Initial sponsor deposit"


@pytest.mark.anyio
async def test_ledger_entry_rejects_unknown_wallet_id(clean_database):
    """Negative path: ledger FK must reject wallet_ids with no wallet row."""
    import uuid

    from sqlalchemy.exc import IntegrityError

    factory = get_session_factory()
    async with factory() as session:
        session.add(
            LedgerEntryModel(
                entry_id=str(uuid.uuid4()),
                wallet_id="spn-does-not-exist",
                action="credit",
                amount=Decimal("1"),
                balance_after=Decimal("1"),
                description="orphan ledger",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


# --- Agent Wallet Provisioning ---


@pytest.mark.anyio
async def test_provision_agent_wallet(client, api_headers):
    # Create sponsor first
    sponsor_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Test Sponsor",
            "email": "s@t.com",
            "initial_credits": 20000,
        },
        headers=api_headers,
    )
    sponsor_id = sponsor_resp.json()["wallet_id"]

    # Provision agent
    resp = await client.post(
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": sponsor_id,
            "agent_id": "agent-crawl-bot-42",
            "budget_credits": 5000.0,
            "daily_limit": 1000.0,
        },
        headers=api_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["wallet_type"] == "agent"
    assert data["balance"] == 5000.0
    assert data["daily_limit"] == 1000.0
    assert data["sponsor_wallet_id"] == sponsor_id
    assert data["agent_id"] == "agent-crawl-bot-42"
    assert data["wallet_id"].startswith("agt-")

    # Verify sponsor balance reduced
    sponsor_check = await client.get(
        f"/v1/billing/wallets/{sponsor_id}",
        headers=api_headers,
    )
    assert sponsor_check.json()["balance"] == 15000.0


@pytest.mark.anyio
async def test_provision_insufficient_sponsor_balance(client, api_headers):
    sponsor_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={"sponsor_name": "Low Funds", "email": "l@f.com", "initial_credits": 100},
        headers=api_headers,
    )
    sponsor_id = sponsor_resp.json()["wallet_id"]

    resp = await client.post(
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": sponsor_id,
            "agent_id": "greedy-bot",
            "budget_credits": 50000,
        },
        headers=api_headers,
    )
    assert resp.status_code == 400
    assert "insufficient" in resp.json()["detail"]["message"].lower()


# --- Charging (Micro-Metering) ---


@pytest.mark.anyio
async def test_charge_agent_wallet(client, api_headers):
    # Setup: sponsor → agent
    sponsor_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Charge Test",
            "email": "c@t.com",
            "initial_credits": 10000,
        },
        headers=api_headers,
    )
    sponsor_id = sponsor_resp.json()["wallet_id"]

    agent_resp = await client.post(
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": sponsor_id,
            "agent_id": "metered-bot",
            "budget_credits": 5000,
        },
        headers=api_headers,
    )
    agent_wallet_id = agent_resp.json()["wallet_id"]

    # Charge for IoT bridge usage (2 credits per request × 10 units = 20 credits)
    resp = await client.post(
        f"/v1/billing/charge?wallet_id={agent_wallet_id}&service=iot_bridge&units=10&request_path=POST+/v1/iot/devices",
        headers=api_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "debit"
    assert data["amount"] == -20.0  # 10 units × 2 credits
    assert Decimal(data["amount_exact"]) == Decimal("-20")
    assert data["balance_after"] == 4980.0
    assert Decimal(data["balance_after_exact"]) == Decimal("4980")
    assert data["service_category"] == "iot_bridge"
    assert data["compute_cost"] is not None
    assert data["margin"] is not None
    assert data["margin"] > 0  # Arbitrage should be positive


@pytest.mark.anyio
async def test_billing_charge_records_governance_audit_event(
    client,
    api_headers,
    clean_database,
):
    sponsor_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Governance Billing Test",
            "email": "governance-billing@example.com",
            "initial_credits": 10000,
        },
        headers=api_headers,
    )
    sponsor_id = sponsor_resp.json()["wallet_id"]
    agent_resp = await client.post(
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": sponsor_id,
            "agent_id": "governance-billing-bot",
            "budget_credits": 1000,
        },
        headers=api_headers,
    )
    agent_wallet_id = agent_resp.json()["wallet_id"]

    charge_resp = await client.post(
        (
            f"/v1/billing/charge?wallet_id={agent_wallet_id}"
            "&service=agent_comms&units=2&request_path=POST+/v1/agent-comms/send"
        ),
        headers={**api_headers, "X-Request-ID": "req-billing-governance"},
    )

    assert charge_resp.status_code == 200
    charge = charge_resp.json()
    events = await list_audit_events(
        wallet_id=agent_wallet_id,
        request_id="req-billing-governance",
    )
    assert len(events) == 1
    event = events[0]
    assert event.event == "billing.charge"
    assert event.tool == "billing"
    assert event.endpoint == "/v1/billing/charge"
    assert event.auth_source == "bootstrap"
    assert event.ok is True
    assert event.metadata["ledger_entry_id"] == charge["entry_id"]
    assert event.metadata["service_category"] == "agent_comms"
    assert event.metadata["amount_exact"] == charge["amount_exact"]
    assert event.metadata["balance_after_exact"] == charge["balance_after_exact"]


@pytest.mark.anyio
async def test_refund_charge_reverses_debit(client, api_headers, clean_database):
    sponsor_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Refund Test",
            "email": "refund@example.com",
            "initial_credits": 10000,
        },
        headers=api_headers,
    )
    sponsor_id = sponsor_resp.json()["wallet_id"]

    agent_resp = await client.post(
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": sponsor_id,
            "agent_id": "refund-bot",
            "budget_credits": 5000,
            "daily_limit": 1000,
        },
        headers=api_headers,
    )
    agent_wallet_id = agent_resp.json()["wallet_id"]

    charge_resp = await client.post(
        f"/v1/billing/charge?wallet_id={agent_wallet_id}&service=agent_comms&units=5",
        headers=api_headers,
    )
    assert charge_resp.status_code == 200
    charge = charge_resp.json()
    assert Decimal(charge["amount_exact"]) == Decimal("-7.5")

    refund = await get_agent_money().refund_charge(
        wallet_id=agent_wallet_id,
        charge_entry_id=charge["entry_id"],
        description="Refund failed MCP tool",
    )
    duplicate_refund = await get_agent_money().refund_charge(
        wallet_id=agent_wallet_id,
        charge_entry_id=charge["entry_id"],
        description="Refund failed MCP tool",
    )
    assert refund.action.value == "refund"
    assert refund.entry_id == f"refund-{charge['entry_id']}"
    assert duplicate_refund.entry_id == refund.entry_id
    assert refund.amount == Decimal("7.5")
    assert refund.balance_after == Decimal("5000")
    assert duplicate_refund.balance_after == Decimal("5000")

    wallet_resp = await client.get(
        f"/v1/billing/wallets/{agent_wallet_id}",
        headers=api_headers,
    )
    assert Decimal(wallet_resp.json()["balance_exact"]) == Decimal("5000")

    factory = get_session_factory()
    async with factory() as session:
        refund_result = await session.execute(
            select(LedgerEntryModel).where(LedgerEntryModel.entry_id == refund.entry_id)
        )
        refund_row = refund_result.scalar_one()
        assert refund_row.correlation_id == charge["entry_id"]
        refund_count_result = await session.execute(
            select(LedgerEntryModel).where(
                LedgerEntryModel.wallet_id == agent_wallet_id,
                LedgerEntryModel.action == "refund",
                LedgerEntryModel.correlation_id == charge["entry_id"],
            )
        )
        assert len(refund_count_result.scalars().all()) == 1

        wallet_result = await session.execute(
            select(WalletModel).where(WalletModel.wallet_id == agent_wallet_id)
        )
        wallet = wallet_result.scalar_one()
        assert wallet.lifetime_debits == Decimal("0")
        assert wallet.hourly_spent == Decimal("0")
        assert wallet.daily_spent == Decimal("0")


@pytest.mark.anyio
async def test_late_refund_does_not_reduce_new_period_spend(
    client, api_headers, clean_database
):
    sponsor_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Late Refund Test",
            "email": "late-refund@example.com",
            "initial_credits": 10000,
        },
        headers=api_headers,
    )
    sponsor_id = sponsor_resp.json()["wallet_id"]

    agent_resp = await client.post(
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": sponsor_id,
            "agent_id": "late-refund-bot",
            "budget_credits": 5000,
            "daily_limit": 1000,
        },
        headers=api_headers,
    )
    agent_wallet_id = agent_resp.json()["wallet_id"]

    charge_resp = await client.post(
        f"/v1/billing/charge?wallet_id={agent_wallet_id}&service=agent_comms&units=5",
        headers=api_headers,
    )
    assert charge_resp.status_code == 200
    charge = charge_resp.json()

    factory = get_session_factory()
    async with factory() as session:
        charge_result = await session.execute(
            select(LedgerEntryModel).where(
                LedgerEntryModel.entry_id == charge["entry_id"]
            )
        )
        charge_row = charge_result.scalar_one()
        reset_after_charge = charge_row.timestamp + timedelta(days=1)

        wallet_result = await session.execute(
            select(WalletModel).where(WalletModel.wallet_id == agent_wallet_id)
        )
        wallet = wallet_result.scalar_one()
        wallet.hourly_reset_at = reset_after_charge
        wallet.daily_reset_at = reset_after_charge
        wallet.hourly_spent = Decimal("25")
        wallet.daily_spent = Decimal("50")
        await session.commit()

    refund = await get_agent_money().refund_charge(
        wallet_id=agent_wallet_id,
        charge_entry_id=charge["entry_id"],
        description="Late refund failed MCP tool",
    )
    assert refund.entry_id == f"refund-{charge['entry_id']}"

    async with factory() as session:
        wallet_result = await session.execute(
            select(WalletModel).where(WalletModel.wallet_id == agent_wallet_id)
        )
        wallet = wallet_result.scalar_one()
        assert wallet.balance == Decimal("5000")
        assert wallet.lifetime_debits == Decimal("0")
        assert wallet.hourly_spent == Decimal("25")
        assert wallet.daily_spent == Decimal("50")


@pytest.mark.anyio
async def test_charge_insufficient_funds_returns_402(client, api_headers):
    sponsor_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={"sponsor_name": "Empty", "email": "e@t.com", "initial_credits": 100},
        headers=api_headers,
    )
    sponsor_id = sponsor_resp.json()["wallet_id"]

    agent_resp = await client.post(
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": sponsor_id,
            "agent_id": "broke-bot",
            "budget_credits": 10,
        },
        headers=api_headers,
    )
    agent_wallet_id = agent_resp.json()["wallet_id"]

    # Try to charge more than balance (red_team scan = 100 credits)
    resp = await client.post(
        f"/v1/billing/charge?wallet_id={agent_wallet_id}&service=red_team&units=1",
        headers=api_headers,
    )
    assert resp.status_code == 402
    detail = resp.json()["detail"]
    assert detail["error"] == "insufficient_funds"
    assert detail["wallet_id"] == agent_wallet_id
    assert detail["top_up_url"] == "/v1/billing/top-up/prepare"
    assert detail["shortfall"] > 0


# --- Ledger ---


@pytest.mark.anyio
async def test_ledger_records_transactions(client, api_headers):
    sponsor_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Ledger Test",
            "email": "l@t.com",
            "initial_credits": 5000,
        },
        headers=api_headers,
    )
    sponsor_id = sponsor_resp.json()["wallet_id"]

    agent_resp = await client.post(
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": sponsor_id,
            "agent_id": "ledger-bot",
            "budget_credits": 2000,
        },
        headers=api_headers,
    )
    agent_wallet_id = agent_resp.json()["wallet_id"]

    # Make a charge
    await client.post(
        f"/v1/billing/charge?wallet_id={agent_wallet_id}&service=agent_comms&units=5",
        headers=api_headers,
    )

    # Check ledger
    resp = await client.get(
        f"/v1/billing/ledger/{agent_wallet_id}",
        headers=api_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["wallet_id"] == agent_wallet_id
    assert data["total"] >= 2  # At least transfer in + debit
    assert data["period_debits"] > 0
    assert "period_debits_exact" in data

    # Verify debit entry exists
    debits = [e for e in data["entries"] if e["action"] == "debit"]
    assert len(debits) >= 1
    assert debits[0]["service_category"] == "agent_comms"
    assert debits[0]["amount_exact"].startswith("-")


# --- Top-Up ---


def test_direct_top_up_openapi_contract_is_deprecated_and_410_only():
    operation = app.openapi()["paths"]["/v1/billing/top-up"]["post"]

    assert operation["deprecated"] is True
    assert "410" in operation["responses"]
    assert "202" not in operation["responses"]
    assert all(
        parameter["name"] != "Idempotency-Key"
        for parameter in operation.get("parameters", [])
    )


@pytest.mark.anyio
@pytest.mark.parametrize("payment_token", [None, "tok_attacker_controlled"])
async def test_direct_top_up_rejects_unverified_payment(
    client, api_headers, clean_database, payment_token
):
    sponsor_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={"sponsor_name": "Topup Test", "email": "t@t.com", "initial_credits": 0},
        headers=api_headers,
    )
    wallet_id = sponsor_resp.json()["wallet_id"]

    body = {"wallet_id": wallet_id, "amount_fiat": 50.0}
    if payment_token is not None:
        body["payment_token"] = payment_token

    resp = await client.post(
        "/v1/billing/top-up",
        json=body,
        headers=api_headers,
    )
    assert resp.status_code == 410
    detail = resp.json()["detail"]
    assert detail["error"] == "direct_top_up_disabled"
    assert detail["prepare_url"] == "/v1/billing/top-up/prepare"

    # Neither an absent nor an attacker-controlled token is payment proof.
    wallet = await client.get(f"/v1/billing/wallets/{wallet_id}", headers=api_headers)
    assert wallet.json()["balance"] == 0.0

    factory = get_session_factory()
    async with factory() as session:
        entries = await session.execute(
            select(LedgerEntryModel).where(LedgerEntryModel.wallet_id == wallet_id)
        )
        assert list(entries.scalars()) == []


@pytest.mark.anyio
async def test_internal_top_up_rejects_unverified_payment_without_mutation(
    client, api_headers, clean_database
):
    sponsor_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Internal Topup Test",
            "email": "internal-topup@example.com",
            "initial_credits": 0,
        },
        headers=api_headers,
    )
    wallet_id = sponsor_resp.json()["wallet_id"]
    money = get_agent_money()

    with pytest.raises(RuntimeError, match="direct_top_up_disabled"):
        await money.top_up(
            wallet_id=wallet_id,
            amount_fiat=Decimal("50"),
            payment_method="stripe",
            payment_token="tok_attacker_controlled",
        )

    wallet = await money.get_wallet(wallet_id)
    assert wallet is not None
    assert Decimal(wallet.balance_exact) == Decimal("0")
    assert await money.get_ledger(wallet_id) == []


@pytest.mark.anyio
async def test_top_up_agent_wallet_fails(client, api_headers):
    sponsor_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={"sponsor_name": "S", "email": "s@t.com", "initial_credits": 5000},
        headers=api_headers,
    )
    agent_resp = await client.post(
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": sponsor_resp.json()["wallet_id"],
            "agent_id": "a",
            "budget_credits": 1000,
        },
        headers=api_headers,
    )

    resp = await client.post(
        "/v1/billing/top-up",
        json={"wallet_id": agent_resp.json()["wallet_id"], "amount_fiat": 10.0},
        headers=api_headers,
    )
    assert resp.status_code == 410


# --- Pricing Table ---


@pytest.mark.anyio
async def test_pricing_table(client, api_headers):
    resp = await client.get("/v1/billing/pricing", headers=api_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["exchange_rate"] == 1000.0
    assert data["exchange_rate_exact"] == "1000.0"
    # Tests run with ENABLE_PROOF_SURFACES=false, so only the categories a core
    # deployment can actually serve are advertised.
    assert [entry["service_category"] for entry in data["pricing"]] == [
        "platform_fee",
        "swarm_delegation",
    ]
    # Check structure
    entry = data["pricing"][0]
    assert "service_category" in entry
    assert "unit" in entry
    assert "credits_per_unit" in entry


@pytest.mark.anyio
async def test_pricing_table_hides_frozen_proof_surfaces(client, api_headers):
    """A core deployment must not advertise services it cannot serve.

    billing is a CORE_TRUST_ROUTER, so it is mounted — and published in the
    OpenAPI — even when every proof-surface router is not. Listing their
    categories made the trust plane's own price list read as a menu for media,
    IoT, oracle, red-team and friends, which docs/PROOF_SURFACES.md rule 3
    forbids advertising as the product.
    """
    resp = await client.get("/v1/billing/pricing", headers=api_headers)
    assert resp.status_code == 200
    advertised = {e["service_category"] for e in resp.json()["pricing"]}

    for frozen in (
        "iot_bridge",
        "media_engine",
        "oracle",
        "red_team",
        "rtaas",
        "sandbox",
        "telemetry_pm",
        "content_factory",
        "protocol_gen",
        "agent_comms",
    ):
        assert frozen not in advertised
    # The platform fee is the category a governed MCP tool falls back to, so it
    # must survive the filter or a core deployment would advertise no price.
    assert "platform_fee" in advertised


@pytest.mark.anyio
async def test_pricing_table_lists_proof_surfaces_when_they_are_mounted(
    client, api_headers, monkeypatch
):
    """Gating is presentation-only and follows the mount flag both ways."""
    monkeypatch.setenv("ENABLE_PROOF_SURFACES", "true")
    get_settings.cache_clear()
    try:
        resp = await client.get("/v1/billing/pricing", headers=api_headers)
        assert resp.status_code == 200
        advertised = {e["service_category"] for e in resp.json()["pricing"]}
        assert {"iot_bridge", "media_engine", "oracle", "platform_fee"} <= advertised
    finally:
        monkeypatch.delenv("ENABLE_PROOF_SURFACES", raising=False)
        get_settings.cache_clear()


def test_pricing_table_has_exactly_one_definition():
    """The dry-run sandbox must price identically to the real charge path.

    shadow_ledger carried its own byte-identical copy of DEFAULT_PRICING. Two
    copies price the same only until someone edits one, and the failure mode is
    a dry run that quotes a number the real charge would never take. Assert the
    same object reaches every consumer.
    """
    from app.services import agent_money, pricing, shadow_ledger

    assert agent_money.DEFAULT_PRICING is pricing.DEFAULT_PRICING
    assert shadow_ledger.DEFAULT_PRICING is pricing.DEFAULT_PRICING


@pytest.mark.anyio
async def test_pricing_table_advertises_configured_exchange_rate(
    client, api_headers, monkeypatch
):
    """The advertised rate must follow settings.EXCHANGE_RATE, not a constant.

    Stripe settlement mints credits at settings.EXCHANGE_RATE. A second
    hardcoded copy behind this endpoint meant an operator who moved the rate
    kept being quoted the old one — the endpoint advertised a conversion that
    no payment actually used. A non-round value also pins the exactness
    contract: exchange_rate_exact must carry the configured decimal rather than
    binary-float noise from an early float() coercion.
    """
    monkeypatch.setenv("EXCHANGE_RATE", "1234.567")
    get_settings.cache_clear()
    try:
        resp = await client.get("/v1/billing/pricing", headers=api_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["exchange_rate"] == pytest.approx(1234.567)
        assert data["exchange_rate_exact"] == "1234.567"
    finally:
        monkeypatch.delenv("EXCHANGE_RATE", raising=False)
        get_settings.cache_clear()


@pytest.mark.parametrize("bad_rate", ["0", "-1000", "NaN", "Infinity"])
def test_unusable_exchange_rate_is_refused_at_construction(monkeypatch, bad_rate):
    """A rate that cannot mint credits must fail before any settlement.

    Credit issuance multiplies settled fiat by this rate, so these values would
    convert a real payment into no credits (or negative ones) at the moment
    money arrives. Zero and negative are caught by the field validator;
    non-finite values are already refused by pydantic's Decimal parsing. This
    asserts the field contract rather than either layer individually.
    """
    monkeypatch.setenv("EXCHANGE_RATE", bad_rate)
    get_settings.cache_clear()
    try:
        with pytest.raises(ValidationError):
            Settings()
    finally:
        monkeypatch.delenv("EXCHANGE_RATE", raising=False)
        get_settings.cache_clear()


# --- Arbitrage Report ---


@pytest.mark.anyio
async def test_arbitrage_report(client, api_headers):
    # Create wallet chain and make some charges
    sponsor_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Arb Test",
            "email": "a@t.com",
            "initial_credits": 100000,
        },
        headers=api_headers,
    )
    agent_resp = await client.post(
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": sponsor_resp.json()["wallet_id"],
            "agent_id": "arb-bot",
            "budget_credits": 50000,
        },
        headers=api_headers,
    )
    wallet_id = agent_resp.json()["wallet_id"]

    # Generate some revenue across services
    await client.post(
        f"/v1/billing/charge?wallet_id={wallet_id}&service=iot_bridge&units=100",
        headers=api_headers,
    )
    await client.post(
        f"/v1/billing/charge?wallet_id={wallet_id}&service=content_factory&units=5",
        headers=api_headers,
    )
    await client.post(
        f"/v1/billing/charge?wallet_id={wallet_id}&service=media_engine&units=200",
        headers=api_headers,
    )

    resp = await client.get("/v1/billing/arbitrage", headers=api_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_revenue"] > 0
    assert data["total_compute_cost"] > 0
    assert data["gross_margin"] > 0
    assert data["margin_percentage"] > 0
    assert len(data["by_service"]) >= 1
    assert len(data["top_profitable_actions"]) >= 1


# --- Wallet Listing ---


@pytest.mark.anyio
async def test_list_wallets(client, api_headers):
    await client.post(
        "/v1/billing/wallets/sponsor",
        json={"sponsor_name": "List Test", "email": "l@t.com", "initial_credits": 1000},
        headers=api_headers,
    )
    resp = await client.get("/v1/billing/wallets", headers=api_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1


@pytest.mark.anyio
async def test_list_wallets_by_type(client, api_headers):
    resp = await client.get(
        "/v1/billing/wallets?wallet_type=sponsor",
        headers=api_headers,
    )
    assert resp.status_code == 200
    for w in resp.json()["wallets"]:
        assert w["wallet_type"] == "sponsor"


@pytest.mark.anyio
async def test_get_wallet_not_found(client, api_headers):
    resp = await client.get("/v1/billing/wallets/nonexistent", headers=api_headers)
    assert resp.status_code == 404


# --- Alerts ---


@pytest.mark.anyio
async def test_billing_alerts(client, api_headers):
    resp = await client.get("/v1/billing/alerts", headers=api_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "alerts" in data
    assert "total" in data
    assert "unacknowledged" in data


# --- Auth ---


@pytest.mark.anyio
async def test_billing_requires_api_key(client):
    resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={"sponsor_name": "NoKey", "email": "n@k.com"},
    )
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_charge_requires_api_key(client):
    resp = await client.post("/v1/billing/charge?wallet_id=x&service=iot_bridge")
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_charge_retry_with_same_idempotency_key_does_not_double_charge(
    client, api_headers, clean_database
):
    sponsor_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Idempotent Charge",
            "email": "idem-charge@t.com",
            "initial_credits": 10000,
        },
        headers=api_headers,
    )
    sponsor_id = sponsor_resp.json()["wallet_id"]
    agent_resp = await client.post(
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": sponsor_id,
            "agent_id": "idem-bot",
            "budget_credits": 5000,
        },
        headers=api_headers,
    )
    agent_wallet_id = agent_resp.json()["wallet_id"]

    headers = {**api_headers, "Idempotency-Key": "retry-key-1"}
    url = f"/v1/billing/charge?wallet_id={agent_wallet_id}&service=iot_bridge&units=10"

    first = await client.post(url, headers=headers)
    assert first.status_code == 200
    first_entry_id = first.json()["entry_id"]

    # Simulate a client retry after e.g. a dropped response, same key + same params.
    second = await client.post(url, headers=headers)
    assert second.status_code == 200
    assert second.json()["entry_id"] == first_entry_id

    ledger_resp = await client.get(
        f"/v1/billing/ledger/{agent_wallet_id}", headers=api_headers
    )
    debit_entries = [e for e in ledger_resp.json()["entries"] if e["action"] == "debit"]
    assert len(debit_entries) == 1


@pytest.mark.anyio
async def test_charge_reused_idempotency_key_with_different_payload_conflicts(
    client, api_headers, clean_database
):
    sponsor_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Idempotent Conflict",
            "email": "idem-conflict@t.com",
            "initial_credits": 10000,
        },
        headers=api_headers,
    )
    sponsor_id = sponsor_resp.json()["wallet_id"]
    agent_resp = await client.post(
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": sponsor_id,
            "agent_id": "idem-conflict-bot",
            "budget_credits": 5000,
        },
        headers=api_headers,
    )
    agent_wallet_id = agent_resp.json()["wallet_id"]

    headers = {**api_headers, "Idempotency-Key": "retry-key-2"}
    await client.post(
        f"/v1/billing/charge?wallet_id={agent_wallet_id}&service=iot_bridge&units=10",
        headers=headers,
    )
    conflict = await client.post(
        f"/v1/billing/charge?wallet_id={agent_wallet_id}&service=iot_bridge&units=20",
        headers=headers,
    )
    assert conflict.status_code == 409


@pytest.mark.anyio
async def test_db_key_cannot_operate_on_other_wallet(client, api_headers):
    wallet_a_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Tenant A",
            "email": "a@test.com",
            "initial_credits": 1000,
        },
        headers=api_headers,
    )
    wallet_b_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Tenant B",
            "email": "b@test.com",
            "initial_credits": 1000,
        },
        headers=api_headers,
    )
    wallet_a = wallet_a_resp.json()["wallet_id"]
    wallet_b = wallet_b_resp.json()["wallet_id"]

    key_resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_a},
        headers=api_headers,
    )
    db_headers = {"X-API-Key": key_resp.json()["api_key"]}

    blocked_requests = [
        client.get(f"/v1/billing/wallets/{wallet_b}", headers=db_headers),
        client.get(f"/v1/billing/ledger/{wallet_b}", headers=db_headers),
        client.post(
            f"/v1/billing/charge?wallet_id={wallet_b}&service=iot_bridge",
            headers=db_headers,
        ),
        client.post(
            "/v1/billing/top-up",
            json={"wallet_id": wallet_b, "amount_fiat": 1.0},
            headers=db_headers,
        ),
        client.post(
            f"/v1/billing/transfer?from_wallet_id={wallet_b}&to_wallet_id={wallet_a}&amount=1",
            headers=db_headers,
        ),
        client.get(f"/v1/billing/alerts?wallet_id={wallet_b}", headers=db_headers),
        client.get(f"/v1/billing/wallets/{wallet_b}/velocity", headers=db_headers),
        client.post(
            "/v1/billing/dry-run/session",
            json={"wallet_id": wallet_b},
            headers=db_headers,
        ),
    ]

    for request in blocked_requests:
        resp = await request
        assert resp.status_code == 403

    own_resp = await client.get(f"/v1/billing/wallets/{wallet_a}", headers=db_headers)
    assert own_resp.status_code == 200


@pytest.mark.anyio
async def test_bootstrap_key_can_manage_multiple_wallets(client, api_headers):
    wallet_a_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={"sponsor_name": "Admin A", "email": "admin-a@test.com"},
        headers=api_headers,
    )
    wallet_b_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={"sponsor_name": "Admin B", "email": "admin-b@test.com"},
        headers=api_headers,
    )

    for wallet_id in (
        wallet_a_resp.json()["wallet_id"],
        wallet_b_resp.json()["wallet_id"],
    ):
        resp = await client.get(f"/v1/billing/wallets/{wallet_id}", headers=api_headers)
        assert resp.status_code == 200

        key_resp = await client.post(
            "/v1/api-keys",
            json={"wallet_id": wallet_id},
            headers=api_headers,
        )
        assert key_resp.status_code == 201


@pytest.mark.anyio
async def test_scoped_key_cannot_mint_sponsor_credits(client, api_headers):
    """Creating a sponsor mints credits, so it must be admin-gated.

    Regression: the endpoint accepted any valid API key, letting a scoped wallet
    key mint arbitrary initial_credits — verified exploitable against production.
    """
    sponsor = await client.post(
        "/v1/billing/wallets/sponsor",
        json={"sponsor_name": "Root", "email": "root@test.com", "initial_credits": 100},
        headers=api_headers,
    )
    wallet_id = sponsor.json()["wallet_id"]
    key_resp = await client.post(
        "/v1/api-keys", json={"wallet_id": wallet_id}, headers=api_headers
    )
    scoped = {"X-API-Key": key_resp.json()["api_key"]}

    resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Free Money",
            "email": "free@test.com",
            "initial_credits": 1_000_000,
        },
        headers=scoped,
    )
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_topup_idempotency_key_cannot_bypass_payment_verification(
    client, api_headers, clean_database
):
    sponsor = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Idem Topup",
            "email": "idem-topup@t.com",
            "initial_credits": 0,
        },
        headers=api_headers,
    )
    wallet_id = sponsor.json()["wallet_id"]

    headers = {**api_headers, "Idempotency-Key": "topup-retry-1"}
    body = {"wallet_id": wallet_id, "amount_fiat": 50.0}

    first = await client.post("/v1/billing/top-up", json=body, headers=headers)
    second = await client.post("/v1/billing/top-up", json=body, headers=headers)
    assert first.status_code == 410
    assert second.status_code == 410

    wallet = await client.get(f"/v1/billing/wallets/{wallet_id}", headers=api_headers)
    assert wallet.json()["balance"] == 0.0


@pytest.mark.anyio
async def test_topup_reused_key_different_amount_remains_disabled(
    client, api_headers, clean_database
):
    sponsor = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Idem Topup Conflict",
            "email": "idem-topup-c@t.com",
            "initial_credits": 0,
        },
        headers=api_headers,
    )
    wallet_id = sponsor.json()["wallet_id"]
    headers = {**api_headers, "Idempotency-Key": "topup-conflict-1"}

    first = await client.post(
        "/v1/billing/top-up",
        json={"wallet_id": wallet_id, "amount_fiat": 50.0},
        headers=headers,
    )
    conflict = await client.post(
        "/v1/billing/top-up",
        json={"wallet_id": wallet_id, "amount_fiat": 99.0},
        headers=headers,
    )
    assert first.status_code == 410
    assert conflict.status_code == 410

    wallet = await client.get(f"/v1/billing/wallets/{wallet_id}", headers=api_headers)
    assert wallet.json()["balance"] == 0.0


@pytest.mark.anyio
async def test_transfer_retry_with_same_idempotency_key_does_not_double_spend(
    client, api_headers, clean_database
):
    sponsor = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Xfer Src",
            "email": "xfer-src@t.com",
            "initial_credits": 10000,
        },
        headers=api_headers,
    )
    src = sponsor.json()["wallet_id"]
    agent_a = await client.post(
        "/v1/billing/wallets/agent",
        json={"sponsor_wallet_id": src, "agent_id": "xfer-a", "budget_credits": 5000},
        headers=api_headers,
    )
    agent_b = await client.post(
        "/v1/billing/wallets/agent",
        json={"sponsor_wallet_id": src, "agent_id": "xfer-b", "budget_credits": 100},
        headers=api_headers,
    )
    a = agent_a.json()["wallet_id"]
    b = agent_b.json()["wallet_id"]

    headers = {**api_headers, "Idempotency-Key": "xfer-retry-1"}
    url = f"/v1/billing/transfer?from_wallet_id={a}&to_wallet_id={b}&amount=1000"

    first = await client.post(url, headers=headers)
    assert first.status_code == 200

    # Retry with the same key must not move credits again.
    second = await client.post(url, headers=headers)
    assert second.status_code == 200

    wallet_a = await client.get(f"/v1/billing/wallets/{a}", headers=api_headers)
    wallet_b = await client.get(f"/v1/billing/wallets/{b}", headers=api_headers)
    # Exactly one transfer of 1000 applied.
    assert wallet_a.json()["balance"] == 4000.0
    assert wallet_b.json()["balance"] == 1100.0  # 100 initial + 1000 transferred once


@pytest.mark.anyio
async def test_rejected_charge_does_not_inflate_velocity_counters(
    client, api_headers, clean_database
):
    """A charge rejected for insufficient funds must not leave the wallet's
    hourly/daily spend counters inflated (which would trip false daily-limit
    rejections and velocity auto-freeze despite zero real spend)."""
    from decimal import Decimal as _D
    from app.db.database import get_session_factory
    from app.db.models import WalletModel

    sponsor = await client.post(
        "/v1/billing/wallets/sponsor",
        json={"sponsor_name": "Velo", "email": "velo@t.com", "initial_credits": 10000},
        headers=api_headers,
    )
    agent = await client.post(
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": sponsor.json()["wallet_id"],
            "agent_id": "velo-a",
            "budget_credits": 5,
        },
        headers=api_headers,
    )
    wallet_id = agent.json()["wallet_id"]

    # red_team costs 100 credits/unit; wallet has 5 -> insufficient funds.
    for _ in range(3):
        resp = await client.post(
            f"/v1/billing/charge?wallet_id={wallet_id}&service=red_team&units=1",
            headers=api_headers,
        )
        assert resp.status_code == 402

    factory = get_session_factory()
    async with factory() as session:
        wallet = await session.get(WalletModel, wallet_id)
        # All three rejected charges were reversed out of the counters.
        assert wallet.daily_spent == _D("0")
        assert wallet.hourly_spent == _D("0")


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("units", "expected_status"),
    [
        ("0", 422),
        ("-5", 422),
        ("abc", 422),
        ("", 422),
        ("NaN", 422),
        ("Infinity", 422),
    ],
)
async def test_charge_rejects_malformed_units_without_touching_the_wallet(
    client, api_headers, clean_database, units: str, expected_status: int
):
    """A rejected charge must leave the balance and the ledger exactly as found.

    ``units`` is declared ``gt=0``, so zero and negatives are refused — a
    negative would otherwise invert the debit into a credit, which is a way to
    mint money through the metering endpoint. ``NaN`` and ``Infinity`` are
    valid JSON-adjacent float spellings that Python's ``float()`` accepts, and
    either would poison the balance arithmetic irrecoverably, so they are
    pinned alongside the obviously malformed cases.
    """
    sponsor_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Malformed Units",
            "email": "malformed-units@t.com",
            "initial_credits": 10000,
        },
        headers=api_headers,
    )
    sponsor_id = sponsor_resp.json()["wallet_id"]
    agent_resp = await client.post(
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": sponsor_id,
            "agent_id": "malformed-units-bot",
            "budget_credits": 5000,
        },
        headers=api_headers,
    )
    agent_wallet_id = agent_resp.json()["wallet_id"]

    before = await client.get(
        f"/v1/billing/wallets/{agent_wallet_id}", headers=api_headers
    )
    opening = before.json()["balance_exact"]

    resp = await client.post(
        f"/v1/billing/charge?wallet_id={agent_wallet_id}"
        f"&service=iot_bridge&units={units}",
        headers=api_headers,
    )
    assert resp.status_code == expected_status, resp.text

    after = await client.get(
        f"/v1/billing/wallets/{agent_wallet_id}", headers=api_headers
    )
    assert after.json()["balance_exact"] == opening
    ledger_resp = await client.get(
        f"/v1/billing/ledger/{agent_wallet_id}", headers=api_headers
    )
    assert [e for e in ledger_resp.json()["entries"] if e["action"] == "debit"] == []


@pytest.mark.anyio
@pytest.mark.parametrize("units", [Decimal("-5"), Decimal("-0.00000001"), Decimal("0")])
async def test_billing_engine_refuses_non_positive_units(units):
    """A negative units count inverts the debit into a credit.

    ``charge_amount`` goes negative, the guarded debit reads
    ``balance >= charge_amount`` as trivially true, and ``balance -
    charge_amount`` *raises* the balance. Reproduced before the fix: a wallet
    at 100 charged ``units=-5`` ended at 110, with a ledger entry recording
    ``action="debit", amount=+10`` -- money minted, and the audit trail
    agreeing it was a charge. Zero is refused alongside it: it writes a
    zero-value debit that means nothing.

    The router refuses both through ``gt=0``, but the governed MCP path and
    the SDK reach the engine without passing through it, which is why the
    guard belongs here.
    """
    from app.schemas.billing import ServiceCategory
    from app.services.agent_money import get_agent_money

    with pytest.raises(ValueError, match="greater than zero"):
        await get_agent_money().charge(
            wallet_id="wal-irrelevant",
            service_category=ServiceCategory.IOT_BRIDGE,
            units=units,
            request_path="/non-positive",
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "units", [Decimal("Infinity"), Decimal("-Infinity"), Decimal("NaN")]
)
async def test_billing_engine_refuses_non_finite_units(units):
    """The engine refuses non-finite units, not only the HTTP boundary.

    The governed MCP path and the SDK reach ``charge`` without passing the
    billing router's query validation, so the guard has to live where every
    caller meets.
    """
    from app.schemas.billing import ServiceCategory
    from app.services.agent_money import get_agent_money

    with pytest.raises(ValueError, match="finite"):
        await get_agent_money().charge(
            wallet_id="wal-irrelevant",
            service_category=ServiceCategory.IOT_BRIDGE,
            units=units,
            request_path="/non-finite",
        )


@pytest.mark.anyio
async def test_charge_against_an_unknown_wallet_creates_nothing(
    client, api_headers, clean_database
):
    """An unknown wallet is refused, and leaves no ledger entry behind.

    The bootstrap key can operate on any wallet, so authorization does not
    stop this one — the wallet lookup has to. Asserting the ledger stays empty
    matters as much as the status code: a debit written against an id with no
    wallet row is an entry nothing will ever reconcile.
    """
    unknown = "wal-does-not-exist-000000"

    resp = await client.post(
        f"/v1/billing/charge?wallet_id={unknown}&service=iot_bridge&units=1",
        headers=api_headers,
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["error"] == "wallet_not_found"

    ledger_resp = await client.get(f"/v1/billing/ledger/{unknown}", headers=api_headers)
    # Pin the status set first. Guarding the emptiness check on 200 alone
    # would let a future change to the ledger endpoint drop the assertion
    # this test exists for without anything failing.
    assert ledger_resp.status_code in (200, 404), ledger_resp.text
    if ledger_resp.status_code == 200:
        assert ledger_resp.json()["entries"] == []


@pytest.mark.anyio
@pytest.mark.parametrize("units", [-5, 0, "abc", "Infinity"])
async def test_dry_run_charge_rejects_bad_units_on_both_branches(
    client, api_headers, clean_database, units
):
    """The dry-run endpoint validates before it picks a branch.

    ``simulate_charge`` has two: with a ``dry_run_session_id`` it calls
    ``ShadowLedger.simulate_charge`` directly and never reaches
    ``BillingEngine.charge``, so the engine's non-positive guard does not
    cover it at all; without one it does reach the engine, where that guard
    raises ``ValueError`` and escapes as a 500. Constraining the request field
    refuses both before either branch is chosen — and a simulation is exactly
    where a caller checks affordability, so a 500 there reads as "the service
    is broken" rather than "that is not a valid quantity".
    """
    sponsor_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Dry Run Units",
            "email": "dry-run-units@t.com",
            "initial_credits": 1000,
        },
        headers=api_headers,
    )
    wallet_id = sponsor_resp.json()["wallet_id"]

    resp = await client.post(
        "/v1/billing/dry-run/charge",
        json={"wallet_id": wallet_id, "service": "iot_bridge", "units": units},
        headers=api_headers,
    )
    assert resp.status_code == 422, resp.text

    # A session-scoped simulation is refused on the same field, before the
    # session is even looked up — so an unknown session cannot mask it.
    resp = await client.post(
        "/v1/billing/dry-run/charge",
        json={
            "wallet_id": wallet_id,
            "service": "iot_bridge",
            "units": units,
            "dry_run_session_id": "sess-does-not-exist",
        },
        headers=api_headers,
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.anyio
async def test_dry_run_charge_refuses_unauthenticated_callers_without_leaking(
    client, api_headers, clean_database
):
    """Unauthenticated dry runs are refused, and reveal nothing by refusing.

    A simulation endpoint is a tempting oracle: it takes a wallet id and a
    session id and reports what *would* happen, so if its refusals varied with
    whether those exist, an unauthenticated caller could enumerate them
    without ever moving money. The refusal must therefore be identical
    whatever the payload names.

    It must also come *before* body validation. If invalid units produced a
    422 while valid units produced a 401, the status code alone would confirm
    that a request got far enough to be validated — a weaker oracle, but the
    same kind.
    """
    sponsor_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Dry Run Auth",
            "email": "dry-run-auth@t.com",
            "initial_credits": 1000,
        },
        headers=api_headers,
    )
    real_wallet = sponsor_resp.json()["wallet_id"]
    fake_wallet = "wal-does-not-exist-000000"

    async def _dry_run(headers: dict, wallet_id: str, units: float = 1.0):
        return await client.post(
            "/v1/billing/dry-run/charge",
            json={
                "wallet_id": wallet_id,
                "service": "iot_bridge",
                "units": units,
            },
            headers=headers,
        )

    async def _dry_run_session(headers: dict, session_id: str, units: float):
        return await client.post(
            "/v1/billing/dry-run/charge",
            json={
                "wallet_id": real_wallet,
                "service": "iot_bridge",
                "units": units,
                "dry_run_session_id": session_id,
            },
            headers=headers,
        )

    # The status varies with the *credential*, never with the payload: no
    # header and a malformed key are 401, a well-formed but unrecognized key
    # is 403. That split is fine — it describes what the caller presented, not
    # what exists on the server.
    credentials = (
        ({}, 401),
        ({"X-API-Key": "short"}, 401),
        ({"X-API-Key": "not-a-real-key"}, 403),
    )
    for headers, expected_status in credentials:
        real = await _dry_run(headers, real_wallet)
        fake = await _dry_run(headers, fake_wallet)
        assert real.status_code == expected_status, real.text
        assert fake.status_code == expected_status, fake.text
        # Identical bodies, not merely identical statuses: an error message
        # that named the wallet would leak exactly what the status hides.
        assert real.text == fake.text
        assert real_wallet not in real.text

        # Auth precedes validation, so a malformed body cannot be used to tell
        # a request that was rejected at the door from one that got inside.
        bad_units = await _dry_run(headers, real_wallet, units=-5)
        assert bad_units.status_code == expected_status, bad_units.text
        assert bad_units.text == real.text

    # The session-scoped branch is refused on the same terms. Asserting that
    # against an unknown session id alone would prove nothing: it has to be
    # compared with a session that really exists, or the test passes for a
    # server that answers 404 for one and 401 for the other.
    session_resp = await client.post(
        "/v1/billing/dry-run/session",
        json={"wallet_id": real_wallet},
        headers=api_headers,
    )
    assert session_resp.status_code == 201, session_resp.text
    real_session = session_resp.json()["session_id"]
    fake_session = "sess-does-not-exist"

    for headers, expected_status in credentials:
        for units in (1, -5):
            real = await _dry_run_session(headers, real_session, units)
            fake = await _dry_run_session(headers, fake_session, units)
            assert real.status_code == expected_status, real.text
            assert fake.status_code == expected_status, fake.text
            # The handler looks the session up *before* checking wallet
            # access — but the credential check is a dependency, so it runs
            # before the handler body at all. Nothing about which session
            # ids exist reaches a caller who never got past the door.
            assert real.text == fake.text
            assert real_session not in real.text


@pytest.mark.anyio
async def test_charge_without_a_service_category_is_refused(
    client, api_headers, clean_database
):
    """Neither ``service`` nor ``service_category`` given: no pricing applies."""
    sponsor_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "No Category",
            "email": "no-category@t.com",
            "initial_credits": 1000,
        },
        headers=api_headers,
    )
    sponsor_id = sponsor_resp.json()["wallet_id"]
    agent_resp = await client.post(
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": sponsor_id,
            "agent_id": "no-category-bot",
            "budget_credits": 500,
        },
        headers=api_headers,
    )
    agent_wallet_id = agent_resp.json()["wallet_id"]

    resp = await client.post(
        f"/v1/billing/charge?wallet_id={agent_wallet_id}&units=1",
        headers=api_headers,
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["error"] == "missing_service"

    ledger_resp = await client.get(
        f"/v1/billing/ledger/{agent_wallet_id}", headers=api_headers
    )
    assert [e for e in ledger_resp.json()["entries"] if e["action"] == "debit"] == []


@pytest.mark.anyio
async def test_dry_run_session_does_not_leak_another_tenants_session(
    client, api_headers, clean_database
):
    """A session belonging to another wallet answers as if it did not exist.

    The dry-run session endpoints look the session up first and check wallet
    access second. Reporting those two failures differently made the surface an
    oracle: a caller holding any valid wallet-scoped key got 404 for an
    invented session id and 403 for a real one, so the two were
    distinguishable with no access at all. The 403 body also carried
    ``session.wallet_id`` — the *owning* wallet — so probing another tenant's
    session id disclosed that tenant's wallet id outright.

    Session ids are UUID4 and cannot be enumerated, so the exposure needs an id
    learned some other way (a log, a trace, a shared URL). That is a narrow
    door, not a closed one, and the repository requires that tenant A cannot
    reach tenant B's data.
    """
    tenant_a = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Tenant A",
            "email": "tenant-a-sessions@t.com",
            "initial_credits": 100,
        },
        headers=api_headers,
    )
    wallet_a = tenant_a.json()["wallet_id"]
    tenant_b = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Tenant B",
            "email": "tenant-b-sessions@t.com",
            "initial_credits": 100,
        },
        headers=api_headers,
    )
    wallet_b = tenant_b.json()["wallet_id"]

    key_resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_a, "key_name": "tenant-a-key"},
        headers=api_headers,
    )
    assert key_resp.status_code == 201, key_resp.text
    a_headers = {"X-API-Key": key_resp.json()["api_key"]}

    session_resp = await client.post(
        "/v1/billing/dry-run/session",
        json={"wallet_id": wallet_b},
        headers=api_headers,
    )
    assert session_resp.status_code == 201, session_resp.text
    b_session = session_resp.json()["session_id"]
    unknown_session = "sess-does-not-exist"

    # Every endpoint that takes a session id, on the same terms. `charge` is
    # included because it reaches the same lookup through a body field rather
    # than a path parameter.
    probes = (
        ("GET", "/v1/billing/dry-run/session/{sid}", None),
        ("DELETE", "/v1/billing/dry-run/session/{sid}", None),
        ("POST", "/v1/billing/dry-run/session/{sid}/commit", {}),
        ("POST", "/v1/billing/dry-run/session/{sid}/revert", {}),
    )
    for method, template, body in probes:
        responses = []
        for sid in (b_session, unknown_session):
            kwargs = {"headers": a_headers}
            if body is not None:
                kwargs["json"] = body
            responses.append(
                await client.request(method, template.format(sid=sid), **kwargs)
            )
        theirs, absent = responses
        assert theirs.status_code == 404, f"{method} {template}: {theirs.text}"
        assert absent.status_code == 404, f"{method} {template}: {absent.text}"
        # Both, not just `theirs`. Asserting the error code on one of them
        # would let a change that answered 404 with a *different* code for the
        # unknown session pass, which is the distinction this test exists to
        # forbid. Byte equality is unavailable because the message echoes the
        # session id, so normalise that out and compare the rest.
        assert theirs.json()["detail"]["error"] == "session_not_found"
        assert absent.json()["detail"]["error"] == "session_not_found"
        assert theirs.text.replace(b_session, "SID") == absent.text.replace(
            unknown_session, "SID"
        ), f"{method} {template}: bodies differ beyond the echoed session id"
        # The owning wallet must not appear anywhere in the refusal.
        assert wallet_b not in theirs.text, f"{method} {template} leaked wallet_b"

    charges = []
    for sid in (b_session, unknown_session):
        charge = await client.post(
            "/v1/billing/dry-run/charge",
            json={
                "wallet_id": wallet_a,
                "service": "iot_bridge",
                "units": 1,
                "dry_run_session_id": sid,
            },
            headers=a_headers,
        )
        assert charge.status_code == 404, charge.text
        assert charge.json()["detail"]["error"] == "session_not_found"
        assert wallet_b not in charge.text
        charges.append(charge)

    # Compared against each other, not only checked one at a time. This is the
    # endpoint most likely to grow response fields — it is the only one of the
    # five with a full response model — so a per-response check would let a
    # newly added field recreate the oracle without failing anything.
    theirs_charge, absent_charge = charges
    assert theirs_charge.text.replace(b_session, "SID") == absent_charge.text.replace(
        unknown_session, "SID"
    ), "dry-run/charge: bodies differ beyond the echoed session id"

    # The owner is unaffected: a wallet-scoped key still reaches its own
    # session. Without this the fix could "pass" by refusing everyone.
    own_key = await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_b, "key_name": "tenant-b-key"},
        headers=api_headers,
    )
    assert own_key.status_code == 201, own_key.text
    b_headers = {"X-API-Key": own_key.json()["api_key"]}
    own = await client.get(
        f"/v1/billing/dry-run/session/{b_session}", headers=b_headers
    )
    assert own.status_code == 200, own.text
    assert own.json()["wallet_id"] == wallet_b


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["commit", "revert"])
async def test_dry_run_session_lost_mid_operation_is_a_404(
    client, api_headers, clean_database, monkeypatch, operation
):
    """A session that vanishes between the ownership check and the operation.

    ``commit_session`` and ``revert_session`` do not raise when the session is
    gone — they return a result object with ``wallet_id`` unset. The router
    used to hand that straight back as a **200**, so a commit that committed
    nothing answered OK with ``success: false``. A caller reading the status
    code would record a commit that never happened. ``end_dry_run_session``
    already 404s on the identical window.

    The window is between two awaits and could not be forced through HTTP —
    two concurrent commits are both caught by the ownership check, because the
    winner removes the session before the loser's check runs. So the service
    is driven into exactly the state the guard exists for, which tests the
    branch rather than the race.
    """
    from app.services import shadow_ledger as shadow_ledger_module

    sponsor = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Lost Session",
            "email": f"lost-session-{operation}@t.com",
            "initial_credits": 1000,
        },
        headers=api_headers,
    )
    wallet_id = sponsor.json()["wallet_id"]
    session_resp = await client.post(
        "/v1/billing/dry-run/session",
        json={"wallet_id": wallet_id},
        headers=api_headers,
    )
    assert session_resp.status_code == 201, session_resp.text
    session_id = session_resp.json()["session_id"]

    ledger = shadow_ledger_module.get_shadow_ledger()
    method = f"{operation}_session"
    original = getattr(ledger, method)

    async def vanished(*args, **kwargs):
        """Return what the service really returns once the session is gone."""
        await ledger.end_session(session_id)
        return await original(*args, **kwargs)

    monkeypatch.setattr(ledger, method, vanished)

    resp = await client.post(
        f"/v1/billing/dry-run/session/{session_id}/{operation}",
        json={},
        headers=api_headers,
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["error"] == "session_not_found"


@pytest.mark.anyio
async def test_a_failed_commit_is_not_reported_as_a_missing_session(
    client, api_headers, clean_database
):
    """A commit that fails on funds is a real answer about a real session.

    ``commit_session`` reports ``success: false`` for two unrelated reasons:
    the session is gone, and the charges themselves failed. The 404 guard keys
    on the unset ``wallet_id`` precisely so this second case keeps its 200 —
    404-ing it would tell the caller the session never existed, when it exists
    and the commit is merely unaffordable.

    Reaching it needs a charge that fits when simulated and does not when
    committed, so the wallet is drained *between* the two: 100 credits, a
    4-credit simulation, then a real 98-credit debit, leaving 2.
    """
    sponsor = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Broke Commit",
            "email": "broke-commit@t.com",
            "initial_credits": 10000,
        },
        headers=api_headers,
    )
    agent = await client.post(
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": sponsor.json()["wallet_id"],
            "agent_id": "broke-commit-bot",
            "budget_credits": 100,
        },
        headers=api_headers,
    )
    wallet_id = agent.json()["wallet_id"]

    session_resp = await client.post(
        "/v1/billing/dry-run/session",
        json={"wallet_id": wallet_id},
        headers=api_headers,
    )
    assert session_resp.status_code == 201, session_resp.text
    session_id = session_resp.json()["session_id"]

    simulated = await client.post(
        "/v1/billing/dry-run/charge",
        json={
            "wallet_id": wallet_id,
            "service": "iot_bridge",
            "units": 2,
            "dry_run_session_id": session_id,
        },
        headers=api_headers,
    )
    assert simulated.status_code == 200, simulated.text
    assert simulated.json()["would_succeed"] is True, simulated.text

    drain = await client.post(
        f"/v1/billing/charge?wallet_id={wallet_id}&service=iot_bridge&units=49",
        headers=api_headers,
    )
    assert drain.status_code == 200, drain.text
    assert drain.json()["balance_after"] == 2.0, drain.text

    resp = await client.post(
        f"/v1/billing/dry-run/session/{session_id}/commit",
        json={},
        headers=api_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is False, body
    assert body["committed_charges"] == 0, body
    assert "insufficient_funds" in body["message"], body
    # The session is named, not denied. This is the assertion the guard could
    # break: keying the 404 on ``success`` instead of ``wallet_id`` fails here.
    assert body["wallet_id"] == wallet_id


@pytest.mark.anyio
async def test_revert_does_not_claim_success_after_a_concurrent_commit(
    client, api_headers, clean_database
):
    """A revert that lost the session to a commit must not report success.

    ``revert_session`` reads the session, then calls ``end_session`` — and used
    to discard that call's return value. ``commit_session`` claims the session
    atomically, so a commit landing between those two steps left ``end_session``
    with nothing to end while the revert still reported ``reverted=True``,
    carrying the wallet id from its stale read.

    The status code is the smaller half. The message reads "No changes made to
    real wallet", and the concurrent commit had just applied the charges to it.
    Reproduced before the fix: a wallet at 100 ended at **96** while the revert
    claimed nothing had moved.

    The interleave is forced *after* the initial read — patching ``end_session``
    rather than removing the session up front, because a session already gone
    is caught by the ownership check and never reaches this window.
    """
    from app.services import shadow_ledger as shadow_ledger_module

    sponsor = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Revert Race",
            "email": "revert-race@t.com",
            "initial_credits": 10000,
        },
        headers=api_headers,
    )
    agent = await client.post(
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": sponsor.json()["wallet_id"],
            "agent_id": "revert-race-bot",
            "budget_credits": 100,
        },
        headers=api_headers,
    )
    wallet_id = agent.json()["wallet_id"]

    session_resp = await client.post(
        "/v1/billing/dry-run/session",
        json={"wallet_id": wallet_id},
        headers=api_headers,
    )
    assert session_resp.status_code == 201, session_resp.text
    session_id = session_resp.json()["session_id"]
    simulated = await client.post(
        "/v1/billing/dry-run/charge",
        json={
            "wallet_id": wallet_id,
            "service": "iot_bridge",
            "units": 2,
            "dry_run_session_id": session_id,
        },
        headers=api_headers,
    )
    assert simulated.status_code == 200, simulated.text

    ledger = shadow_ledger_module.get_shadow_ledger()
    original_end_session = ledger.end_session
    committed = {}

    async def commit_then_end(sid):
        """Land the commit in the window, then run the real ``end_session``."""
        if sid == session_id and "result" not in committed:
            committed["result"] = await ledger.commit_session(sid, get_agent_money())
        return await original_end_session(sid)

    ledger.end_session = commit_then_end
    try:
        resp = await client.post(
            f"/v1/billing/dry-run/session/{session_id}/revert",
            json={},
            headers=api_headers,
        )
    finally:
        ledger.end_session = original_end_session

    # The commit really did charge the wallet — otherwise there is nothing for
    # the revert to have lied about.
    assert committed["result"].success is True, committed["result"]
    assert committed["result"].committed_charges == 1, committed["result"]
    balance = await client.get(f"/v1/billing/wallets/{wallet_id}", headers=api_headers)
    assert balance.json()["balance"] == 96.0, balance.text

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["error"] == "session_not_found"
    # Nothing in the refusal may claim the revert happened.
    assert "reverted" not in resp.text


@pytest.mark.anyio
async def test_a_simulated_charge_cannot_resurrect_a_claimed_session(
    client, api_headers, clean_database
):
    """A simulation must not write back a session a terminal operation claimed.

    ``simulate_charge`` reads the session, mutates it, and writes it back. A
    commit claiming in between used to leave that write-back facing a session
    that no longer exists, with two symptoms by engine:

    * **Redis** — the unconditional ``setex`` *recreated* the key, with every
      simulated charge intact. The committed session came back to life and
      could be committed again, applying the same charges to the real wallet
      twice. This is the production configuration.
    * **memory** — ``self._memory_store[session_id]`` raised ``KeyError``,
      which escaped the router as a 500.

    Both now refuse: the write is conditional on the session still existing
    (``xx=True`` on Redis, a ``.get()`` guard in memory) and the caller is told
    the session is gone, which the router answers as 404 like every other
    session-gone path.

    Only the memory symptom is exercised here. The Redis path has no fake
    client in this suite and adding one is a dependency this test does not
    justify, so that half is argued from the same code path rather than run.
    """
    from app.services import shadow_ledger as shadow_ledger_module

    sponsor = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Resurrect",
            "email": "resurrect-session@t.com",
            "initial_credits": 10000,
        },
        headers=api_headers,
    )
    agent = await client.post(
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": sponsor.json()["wallet_id"],
            "agent_id": "resurrect-bot",
            "budget_credits": 100,
        },
        headers=api_headers,
    )
    wallet_id = agent.json()["wallet_id"]
    session_resp = await client.post(
        "/v1/billing/dry-run/session",
        json={"wallet_id": wallet_id},
        headers=api_headers,
    )
    assert session_resp.status_code == 201, session_resp.text
    session_id = session_resp.json()["session_id"]

    ledger = shadow_ledger_module.get_shadow_ledger()
    original_get_redis = ledger._get_redis
    state: dict = {"calls": 0}

    async def commit_at_the_write_back():
        """Claim the session at the seam between the read and the write.

        Through the router the lookups are: (1) the ownership check's
        ``get_session``, (2) ``simulate_charge``'s own ``get_session``, and
        (3) the write-back. Only the third lands the commit *after* the read
        and the mutation, which is the sole window where the write-back can
        resurrect anything -- firing earlier just reproduces the ordinary
        missing-session path, which already answered 404 before this fix.
        """
        state["calls"] += 1
        if state["calls"] == 3 and "fired" not in state:
            state["fired"] = True
            state["commit"] = await ledger.commit_session(session_id, get_agent_money())
        return await original_get_redis()

    ledger._get_redis = commit_at_the_write_back
    try:
        resp = await client.post(
            "/v1/billing/dry-run/charge",
            json={
                "wallet_id": wallet_id,
                "service": "iot_bridge",
                "units": 2,
                "dry_run_session_id": session_id,
            },
            headers=api_headers,
        )
    finally:
        ledger._get_redis = original_get_redis

    assert "fired" in state, "the commit never landed; the window was missed"
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["error"] == "session_not_found"

    # The session stays dead. If the write-back had resurrected it, it would
    # still be committable — and its charges applied to the wallet twice.
    assert await ledger.get_session(session_id) is None
    recommit = await client.post(
        f"/v1/billing/dry-run/session/{session_id}/commit",
        json={},
        headers=api_headers,
    )
    assert recommit.status_code == 404, recommit.text


class _FakeRedis:
    """The eight Redis calls ``ShadowLedger`` makes, over plain dicts.

    Every method yields with ``asyncio.sleep(0)`` before touching state. That
    is the whole point: against a real server each call is a network round
    trip, so another task can run between any two of them. Without the yield
    the fake would serialise everything and quietly hide the races these
    tests exist to catch.

    Hand-rolled rather than pulled in as a dependency — the surface is eight
    methods and the repository does not otherwise need a Redis test double.
    """

    def __init__(self):
        self.kv: dict = {}
        self.sets: dict = {}
        self.on_get = None
        self.on_set = None

    async def ping(self):
        await asyncio.sleep(0)
        return True

    async def get(self, key):
        await asyncio.sleep(0)
        if self.on_get is not None:
            hook, self.on_get = self.on_get, None
            await hook()
        return self.kv.get(key)

    async def getdel(self, key):
        await asyncio.sleep(0)
        return self.kv.pop(key, None)

    async def set(self, key, value, ex=None, xx=False):
        await asyncio.sleep(0)
        if self.on_set is not None:
            hook, self.on_set = self.on_set, None
            await hook()
        if xx and key not in self.kv:
            return None
        self.kv[key] = value
        return True

    async def setex(self, key, ttl, value):
        await asyncio.sleep(0)
        self.kv[key] = value
        return True

    async def delete(self, key):
        await asyncio.sleep(0)
        return 1 if self.kv.pop(key, None) is not None else 0

    async def sadd(self, key, member):
        await asyncio.sleep(0)
        self.sets.setdefault(key, set()).add(member)

    async def smembers(self, key):
        await asyncio.sleep(0)
        return set(self.sets.get(key, set()))

    async def srem(self, key, member):
        await asyncio.sleep(0)
        self.sets.get(key, set()).discard(member)


@pytest.fixture
def redis_backed_shadow_ledger():
    """Point the shared ShadowLedger at the fake, and restore it after."""
    from app.services import shadow_ledger as shadow_ledger_module

    ledger = shadow_ledger_module.get_shadow_ledger()
    fake = _FakeRedis()
    saved_client, saved_url = ledger._redis, ledger._redis_url
    ledger._redis, ledger._redis_url = fake, "redis://fake"
    try:
        yield ledger, fake
    finally:
        ledger._redis, ledger._redis_url = saved_client, saved_url


@pytest.mark.anyio
async def test_two_concurrent_end_sessions_do_not_both_succeed(
    client, api_headers, clean_database, redis_backed_shadow_ledger
):
    """Only one terminal operation may claim a session — proved on Redis.

    This is the property that closes the revert/commit window, and until now
    it was argued rather than tested: against the in-memory store there is no
    suspension point between a read and a delete, so the interleave could not
    be forced. Against Redis every call is a round trip, and the fake models
    that with an ``asyncio.sleep(0)`` in each method.

    Read-then-delete lets both callers read before either deletes, so both
    return a summary — and the same shape is what let a commit slip between
    ``end_session``'s read and its delete while ``revert_session`` reported
    success. ``_claim_session`` is a single ``GETDEL``, so exactly one wins.
    """
    ledger, _fake = redis_backed_shadow_ledger

    sponsor = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Double End",
            "email": "double-end@t.com",
            "initial_credits": 1000,
        },
        headers=api_headers,
    )
    wallet_id = sponsor.json()["wallet_id"]
    session = await ledger.create_session(
        wallet_id=wallet_id, real_balance=Decimal("1000")
    )

    first, second = await asyncio.gather(
        ledger.end_session(session.session_id),
        ledger.end_session(session.session_id),
    )

    ended = [s for s in (first, second) if s is not None]
    assert len(ended) == 1, (
        "both callers ended the same session; a terminal operation is not "
        f"exclusive (got {first!r} and {second!r})"
    )
    assert await ledger.get_session(session.session_id) is None


@pytest.mark.anyio
async def test_a_simulated_charge_cannot_recreate_a_claimed_redis_session(
    client, api_headers, clean_database, redis_backed_shadow_ledger
):
    """The Redis half of the resurrection fix, which was argued until now.

    ``simulate_charge`` writes the mutated session back. Unconditionally, that
    write *recreates* a key a concurrent commit has already claimed — with
    every simulated charge intact — so the committed session comes back to
    life and can be committed a second time, applying the same charges to the
    real wallet twice. ``xx=True`` makes the write conditional on the key
    still existing, so a claimed session stays claimed.

    The commit is landed from inside the fake's ``set``, which is exactly the
    moment the old code would have resurrected the session.
    """
    ledger, fake = redis_backed_shadow_ledger
    from app.schemas.billing import ServiceCategory

    sponsor = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Redis Resurrect",
            "email": "redis-resurrect@t.com",
            "initial_credits": 10000,
        },
        headers=api_headers,
    )
    agent = await client.post(
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": sponsor.json()["wallet_id"],
            "agent_id": "redis-resurrect-bot",
            "budget_credits": 100,
        },
        headers=api_headers,
    )
    wallet_id = agent.json()["wallet_id"]
    session = await ledger.create_session(
        wallet_id=wallet_id, real_balance=Decimal("100")
    )
    session_id = session.session_id
    committed = {}

    async def commit_before_the_write_back():
        committed["result"] = await ledger.commit_session(session_id, get_agent_money())

    fake.on_set = commit_before_the_write_back

    with pytest.raises(ValueError, match="Session not found"):
        await ledger.simulate_charge(session_id, ServiceCategory.IOT_BRIDGE, units=2)

    assert "result" in committed, "the commit never landed; the window was missed"
    # The session must stay claimed. If the write-back recreated it, it would
    # still be committable — and its charges applied to the wallet twice.
    assert await ledger.get_session(session_id) is None
    second = await ledger.commit_session(session_id, get_agent_money())
    assert second.wallet_id == "", second
    assert second.committed_charges == 0, second


# --- Zero is a value, not an absence ---


@pytest.mark.anyio
async def test_zero_daily_limit_blocks_spending_rather_than_unlocking_it(
    client, api_headers
):
    """``daily_limit=0`` is the strictest cap there is, not the absence of one.

    ``daily_limit`` is Optional: ``None`` means "no cap". A truthiness test
    conflates that with ``Decimal("0")`` -- the value an operator sets to halt
    a runaway agent -- and hands it unlimited daily spend instead. The HTTP
    schema advertises ``ge=0``, so zero is a documented input, and this line is
    the only hard daily-spend enforcement in the system.
    """

    sponsor_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Zero Cap Co",
            "email": "ops@zerocap.example",
            "initial_credits": 10000,
        },
        headers=api_headers,
    )
    sponsor_id = sponsor_resp.json()["wallet_id"]

    agent_resp = await client.post(
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": sponsor_id,
            "agent_id": "halted-bot",
            "budget_credits": 5000,
            "daily_limit": 0,
        },
        headers=api_headers,
    )
    assert agent_resp.status_code == 201
    agent_wallet_id = agent_resp.json()["wallet_id"]

    charge_resp = await client.post(
        f"/v1/billing/charge?wallet_id={agent_wallet_id}&service=agent_comms&units=1",
        headers=api_headers,
    )
    assert charge_resp.status_code == 402, (
        "a zero daily limit was read as 'no limit' and the charge went through"
    )

    # The wallet keeps its funds: a refused charge must not debit.
    wallet_resp = await client.get(
        f"/v1/billing/wallets/{agent_wallet_id}", headers=api_headers
    )
    assert Decimal(wallet_resp.json()["balance_exact"]) == Decimal("5000")


def test_register_local_derives_an_input_schema_alongside_an_output_model() -> None:
    """Supplying only an output model must not erase the input contract.

    The callable fallback used to run only when *both* schemas were absent, so
    a handler with plain typed arguments plus an output model advertised
    ``inputSchema: {}``. A client that obeyed that contract called the tool
    with no arguments and got a TypeError.
    """

    from pydantic import BaseModel

    from app.schemas.billing import ServiceCategory
    from app.services.service_registry import get_service_registry

    class Out(BaseModel):
        echoed: str

    registry = get_service_registry()

    def handler(value: str) -> Out:
        return Out(echoed=value)

    record = registry.register_local(
        service_id="schema-fallback-probe",
        name="Schema fallback probe",
        description="Input schema must still be derived from the callable",
        category=ServiceCategory.AGENT_COMMS,
        func=handler,
        output_model=Out,
    )

    input_schema = record["input_schema"]
    assert input_schema, "input schema was dropped because an output model was given"
    assert "value" in (input_schema.get("properties") or {}), (
        f"the handler's own parameter is missing from {input_schema!r}"
    )


@pytest.mark.anyio
async def test_losing_an_operation_key_race_does_not_leave_velocity_overcounted(
    client, api_headers
):
    """A blinded debit read cannot bypass the durable operation checkpoint.

    Governed velocity now shares the billing transaction and runs only after
    the idempotency checkpoint and dispatch fence are validated. If a stale
    reader misses the winner's ledger row while the operation record already
    names it, the checkpoint conflict rejects that reader before it can mutate
    velocity or any other wallet control state. This keeps one logical charge
    counted exactly once without relying on after-the-fact compensation.
    """

    from app.db.models import IdempotencyRecordModel
    from app.schemas.billing import ServiceCategory
    from app.services import billing_engine as billing_engine_module
    from app.services.billing_engine import (
        BillingEngine,
        LedgerOperationConflictError,
    )

    sponsor_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Race Co",
            "email": "ops@race.example",
            "initial_credits": 10000,
        },
        headers=api_headers,
    )
    wallet_id = sponsor_resp.json()["wallet_id"]

    session_factory = get_session_factory()
    operation_key = "op-velocity-race"

    async with session_factory() as session:
        async with session.begin():
            session.add(
                IdempotencyRecordModel(
                    record_id=operation_key,
                    wallet_id=wallet_id,
                    endpoint="/v1/billing/charge",
                    idempotency_key=operation_key,
                    request_hash="b" * 64,
                )
            )

    money = get_agent_money()
    charge_kwargs = dict(
        wallet_id=wallet_id,
        service_category=ServiceCategory.AGENT_COMMS,
        units=Decimal("2"),
        operation_key=operation_key,
    )

    # The winner: an ordinary charge under the same operation key.
    winner = await money.charge(**charge_kwargs)
    assert hasattr(winner, "entry_id")

    async with session_factory() as session:
        wallet = await session.get(WalletModel, wallet_id)
        operation_record = await session.get(IdempotencyRecordModel, operation_key)
        assert wallet is not None
        assert operation_record is not None
        assert operation_record.ledger_entry_id == winner.entry_id
        wallet_after_winner = {
            field: getattr(wallet, field)
            for field in (
                "balance",
                "lifetime_debits",
                "hourly_spent",
                "daily_spent",
                "last_charge_at",
                "hourly_reset_at",
                "daily_reset_at",
                "velocity_alerts_triggered",
                "status",
            )
        }

    # Simulate a stale ledger read while retaining the authoritative checkpoint.
    real_get_operation_debit = BillingEngine._get_operation_debit
    calls = {"n": 0}

    async def _blind_first_precheck(session, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # Stand in for the winner's ledger row not being visible yet.
            return None
        return await real_get_operation_debit(session, **kwargs)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        billing_engine_module.BillingEngine,
        "_get_operation_debit",
        staticmethod(_blind_first_precheck),
    )
    try:
        with pytest.raises(
            LedgerOperationConflictError,
            match="ledger_operation_checkpoint_conflict",
        ):
            await money.charge(**charge_kwargs)
    finally:
        monkeypatch.undo()

    assert calls["n"] == 1

    async with session_factory() as session:
        wallet = await session.get(WalletModel, wallet_id)
        assert wallet is not None
        wallet_after_loser = {
            field: getattr(wallet, field) for field in wallet_after_winner
        }
        operation_debits = list(
            (
                await session.execute(
                    select(LedgerEntryModel).where(
                        LedgerEntryModel.wallet_id == wallet_id,
                        LedgerEntryModel.operation_key == operation_key,
                        LedgerEntryModel.action == "debit",
                    )
                )
            )
            .scalars()
            .all()
        )

    assert wallet_after_loser == wallet_after_winner
    assert [entry.entry_id for entry in operation_debits] == [winner.entry_id]


@pytest.mark.anyio
async def test_agent_wallet_provisioning_replays_on_idempotency_key(
    client, api_headers, clean_database
):
    """A retried provisioning call with the same Idempotency-Key must not fund
    a second wallet or debit the sponsor twice."""
    sponsor_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Idem Sponsor",
            "email": "i@s.com",
            "initial_credits": 1000,
        },
        headers=api_headers,
    )
    assert sponsor_resp.status_code == 201
    sponsor_id = sponsor_resp.json()["wallet_id"]

    payload = {
        "sponsor_wallet_id": sponsor_id,
        "agent_id": "idem-bot",
        "budget_credits": 300,
    }
    headers = {**api_headers, "Idempotency-Key": "provision-idem-bot-1"}
    first = await client.post(
        "/v1/billing/wallets/agent", json=payload, headers=headers
    )
    assert first.status_code == 201, first.text
    second = await client.post(
        "/v1/billing/wallets/agent", json=payload, headers=headers
    )
    assert second.status_code == 201, second.text
    assert second.json()["wallet_id"] == first.json()["wallet_id"]

    sponsor = await client.get(f"/v1/billing/wallets/{sponsor_id}", headers=api_headers)
    assert sponsor.status_code == 200
    assert sponsor.json()["balance"] == 700.0

    # Same key, different ask: refused rather than silently replayed.
    conflict = await client.post(
        "/v1/billing/wallets/agent",
        json={**payload, "budget_credits": 301},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error"] == "idempotency_key_reused"


@pytest.mark.anyio
async def test_agent_wallet_provisioning_error_completes_idempotency_record(
    client, api_headers, clean_database
):
    """A terminal error must close the key, not leave it in-progress.

    Without completing the record on the 404 branch, a retry with the same
    Idempotency-Key answered ``409 idempotency_in_progress`` until the stale
    record sweep -- the caller could neither replay nor correct the request.
    """
    payload = {
        "sponsor_wallet_id": "wlt-does-not-exist",
        "agent_id": "orphan-bot",
        "budget_credits": 10,
    }
    headers = {**api_headers, "Idempotency-Key": "provision-orphan-1"}
    first = await client.post(
        "/v1/billing/wallets/agent", json=payload, headers=headers
    )
    assert first.status_code == 404, first.text
    replay = await client.post(
        "/v1/billing/wallets/agent", json=payload, headers=headers
    )
    assert replay.status_code == 404, replay.text
    assert replay.json() == first.json()
