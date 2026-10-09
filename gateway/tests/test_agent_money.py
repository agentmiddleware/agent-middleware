"""Focused coverage for the AgentMoney facade (app/services/agent_money.py).

AgentMoney is a thin compatibility facade over WalletEngine and
BillingEngine. These tests pin its stable public surface without touching
the network or any real external service: pure unit checks plus two small
SQLite-backed flows through the facade itself.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.core.config import get_settings
from app.schemas.billing import ServiceCategory, ServicePricing
from app.services import agent_money
from app.services.agent_money import (
    COMPUTE_COSTS,
    AgentMoney,
    InsufficientFundsError,
    KYCVerificationRequiredError,
    WalletNotFoundError,
    _metadata_to_json,
    get_agent_money,
)
from app.services.pricing import DEFAULT_PRICING, PROOF_SURFACE_CATEGORIES


def test_singleton_returns_same_instance():
    assert get_agent_money() is get_agent_money()


def test_default_pricing_is_reexported_not_copied():
    assert agent_money.DEFAULT_PRICING is DEFAULT_PRICING


def test_compute_costs_cover_every_category_with_margin():
    """Every priced category must have a compute cost below its price.

    The arbitrage report relies on price exceeding cost, so a new category
    added to one table but not the other (or priced below cost) fails here.
    """
    assert set(COMPUTE_COSTS) == set(ServiceCategory)
    for category in ServiceCategory:
        cost = COMPUTE_COSTS[category]
        price = DEFAULT_PRICING[category][1]
        assert cost > Decimal("0")
        assert price > cost, category


def test_get_pricing_table_entries_match_default_pricing(monkeypatch):
    money = get_agent_money()
    monkeypatch.setattr(get_settings(), "ENABLE_PROOF_SURFACES", False)
    table = money.get_pricing_table()
    assert table
    assert all(isinstance(entry, ServicePricing) for entry in table)
    by_category = {entry.service_category: entry for entry in table}
    assert ServiceCategory.PLATFORM_FEE in by_category
    assert ServiceCategory.SWARM_DELEGATION in by_category
    for category in PROOF_SURFACE_CATEGORIES:
        assert category not in by_category
    for category, entry in by_category.items():
        unit, price, _ = DEFAULT_PRICING[category]
        assert entry.unit == unit
        assert entry.credits_per_unit_exact == str(price)
        assert entry.credits_per_unit == float(price)
        assert entry.credits_per_unit > 0


def test_insufficient_funds_error_reports_shortfall():
    err = InsufficientFundsError("w-1", Decimal("30"), Decimal("100"))
    assert err.wallet_id == "w-1"
    assert err.current_balance == Decimal("30")
    assert err.required_amount == Decimal("100")
    assert err.shortfall == Decimal("70")
    assert "30" in str(err) and "100" in str(err)


def test_wallet_not_found_error_names_wallet():
    err = WalletNotFoundError("missing-wallet")
    assert err.wallet_id == "missing-wallet"
    assert "missing-wallet" in str(err)


def test_kyc_error_names_wallet_and_status():
    err = KYCVerificationRequiredError("w-2", "pending")
    assert err.wallet_id == "w-2"
    assert err.kyc_status == "pending"
    assert "w-2" in str(err) and "pending" in str(err)


def test_metadata_to_json_edge_cases():
    assert _metadata_to_json(None) is None
    assert _metadata_to_json({}) is None
    assert _metadata_to_json({"a": 1}) == '{"a": 1}'
    # Non-JSON-native values serialize through str instead of raising.
    import json

    payload = _metadata_to_json({"at": datetime(2026, 10, 8, tzinfo=timezone.utc)})
    assert payload is not None
    assert json.loads(payload) == {"at": "2026-10-08 00:00:00+00:00"}


async def test_is_wallet_or_descendant_self_needs_no_database():
    money = AgentMoney()
    assert await money.is_wallet_or_descendant("w-same", "w-same") is True


async def test_is_wallet_or_descendant_unknown_wallet_is_false(clean_database):
    money = get_agent_money()
    assert (
        await money.is_wallet_or_descendant("no-such-wallet", "no-such-ancestor")
        is False
    )


async def test_sponsor_agent_hierarchy_through_facade(clean_database):
    """Main lifecycle path: sponsor funds agent, hierarchy check follows."""
    money = get_agent_money()
    sponsor = await money.create_sponsor_wallet(
        "Facade Corp", "facade@example.test", Decimal("1000")
    )
    agent = await money.create_agent_wallet(
        sponsor.wallet_id, "facade-agent-01", Decimal("100")
    )

    assert (await money.get_wallet(sponsor.wallet_id)) is not None
    assert (await money.get_wallet(agent.wallet_id)) is not None
    assert await money.is_wallet_or_descendant(agent.wallet_id, sponsor.wallet_id)
    assert not await money.is_wallet_or_descendant(sponsor.wallet_id, agent.wallet_id)
    assert await money.get_wallet("no-such-wallet") is None


async def test_transfer_error_cases_through_facade(clean_database):
    """Edge and error cases for the transfer path."""
    money = get_agent_money()
    sponsor = await money.create_sponsor_wallet(
        "Transfer Corp", "transfer@example.test", Decimal("1000")
    )
    source = await money.create_agent_wallet(
        sponsor.wallet_id, "transfer-from", Decimal("50")
    )
    dest = await money.create_agent_wallet(
        sponsor.wallet_id, "transfer-to", Decimal("10")
    )

    with pytest.raises(ValueError, match="positive"):
        await money.transfer(source.wallet_id, dest.wallet_id, Decimal("0"))
    with pytest.raises(ValueError, match="same wallet"):
        await money.transfer(source.wallet_id, source.wallet_id, Decimal("1"))
    with pytest.raises(WalletNotFoundError):
        await money.transfer("no-such-wallet", dest.wallet_id, Decimal("1"))
    with pytest.raises(InsufficientFundsError):
        await money.transfer(source.wallet_id, dest.wallet_id, Decimal("5000"))

    receipt = await money.transfer(
        source.wallet_id, dest.wallet_id, Decimal("20"), description="facade hop"
    )
    assert receipt["from_balance_after"] == Decimal("30")
    assert receipt["to_balance_after"] == Decimal("30")
    assert receipt["status"] == "completed"
