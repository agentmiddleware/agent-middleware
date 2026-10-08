"""Adversarial engine-level tests for billing and wallet money paths.

The HTTP layer validates amounts with ``gt/ge`` plus ``allow_inf_nan=False``
and ``lt=MAX_STORABLE_AMOUNT``, but the SDK and composed server-side callers
(pods, dev keys, governed MCP) reach ``WalletEngine`` directly, bypassing all
of that. These tests pin the engine as the choke point: negative, zero, NaN
and Infinite amounts must be refused with a clean ``ValueError`` before any
row is written, empty-wallet charges must report the full shortfall without
writing ledger rows, and concurrent transfers and refunds must conserve
credits under real parallel execution.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.db.database import get_session_factory
from app.db.models import LedgerEntryModel, WalletModel
from app.schemas.billing import InsufficientFundsResponse, ServiceCategory
from app.services.agent_money import get_agent_money

pytestmark = pytest.mark.anyio


async def _make_sponsor(initial: Decimal = Decimal("1000")):
    money = get_agent_money()
    return await money.create_sponsor_wallet(
        sponsor_name="Adversarial Sponsor",
        email="adversarial@example.com",
        initial_credits=initial,
        require_kyc=False,
    )


async def _balance(wallet_id: str) -> Decimal:
    factory = get_session_factory()
    async with factory() as session:
        wallet = await session.get(WalletModel, wallet_id)
        assert wallet is not None
        return wallet.balance


async def _wallet_count() -> int:
    factory = get_session_factory()
    async with factory() as session:
        return (
            await session.execute(select(func.count()).select_from(WalletModel))
        ).scalar_one()


async def _ledger_count(wallet_id: str) -> int:
    factory = get_session_factory()
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(LedgerEntryModel).where(
                        LedgerEntryModel.wallet_id == wallet_id
                    )
                )
            )
            .scalars()
            .all()
        )
        return len(rows)


# --- Transfer amount validation ---


@pytest.mark.parametrize("raw", [Decimal("-1"), Decimal("0")])
async def test_transfer_rejects_non_positive_amount(clean_database, raw):
    sender = await _make_sponsor(Decimal("100"))
    receiver = await _make_sponsor(Decimal("0"))
    money = get_agent_money()
    with pytest.raises(ValueError):
        await money.transfer(sender.wallet_id, receiver.wallet_id, raw)
    assert await _balance(sender.wallet_id) == Decimal("100")
    assert await _balance(receiver.wallet_id) == Decimal("0")


@pytest.mark.parametrize(
    "raw", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")]
)
async def test_transfer_rejects_non_finite_amount(clean_database, raw):
    """NaN must not leak decimal.InvalidOperation; Infinity must not escape
    into an InsufficientFundsError carrying infinite amounts."""
    sender = await _make_sponsor(Decimal("100"))
    receiver = await _make_sponsor(Decimal("0"))
    before = await _ledger_count(sender.wallet_id)
    money = get_agent_money()
    with pytest.raises(ValueError):
        await money.transfer(sender.wallet_id, receiver.wallet_id, raw)
    assert await _balance(sender.wallet_id) == Decimal("100")
    assert await _balance(receiver.wallet_id) == Decimal("0")
    assert await _ledger_count(sender.wallet_id) == before


# --- Wallet creation amount validation ---


@pytest.mark.parametrize(
    "raw",
    [Decimal("-500"), Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")],
)
async def test_sponsor_rejects_negative_and_non_finite_initial(clean_database, raw):
    """A negative opening balance books an unrecorded liability; a non-finite
    one stores NaN/Infinity in a Numeric column (or leaks IntegrityError)."""
    before = await _wallet_count()
    money = get_agent_money()
    with pytest.raises(ValueError):
        await money.create_sponsor_wallet(
            sponsor_name="Bad",
            email="bad@example.com",
            initial_credits=raw,
            require_kyc=False,
        )
    assert await _wallet_count() == before


async def test_sponsor_zero_initial_still_allowed(clean_database):
    wallet = await _make_sponsor(Decimal("0"))
    assert await _balance(wallet.wallet_id) == Decimal("0")


@pytest.mark.parametrize(
    "raw", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")]
)
async def test_agent_provision_rejects_non_finite_budget(clean_database, raw):
    sponsor = await _make_sponsor(Decimal("1000"))
    money = get_agent_money()
    with pytest.raises(ValueError):
        await money.create_agent_wallet(
            sponsor_wallet_id=sponsor.wallet_id,
            agent_id="bad-agent",
            budget_credits=raw,
        )
    assert await _balance(sponsor.wallet_id) == Decimal("1000")


@pytest.mark.parametrize(
    "raw", [Decimal("-5"), Decimal("0"), Decimal("NaN"), Decimal("Infinity")]
)
async def test_child_rejects_non_positive_or_non_finite_max_spend(clean_database, raw):
    """Zero/negative caps brick the wallet at birth; NaN poisons every later
    cap comparison; Infinity silently disables the cap the parent set."""
    sponsor = await _make_sponsor(Decimal("1000"))
    money = get_agent_money()
    agent = await money.create_agent_wallet(
        sponsor_wallet_id=sponsor.wallet_id,
        agent_id="parent",
        budget_credits=Decimal("500"),
    )
    with pytest.raises(ValueError):
        await money.create_child_wallet(
            parent_wallet_id=agent.wallet_id,
            child_agent_id="child",
            budget_credits=Decimal("10"),
            max_spend=raw,
        )
    assert await _balance(agent.wallet_id) == Decimal("500")


@pytest.mark.parametrize("raw", [Decimal("NaN"), Decimal("Infinity"), Decimal("-1")])
async def test_child_rejects_bad_budget(clean_database, raw):
    sponsor = await _make_sponsor(Decimal("1000"))
    money = get_agent_money()
    agent = await money.create_agent_wallet(
        sponsor_wallet_id=sponsor.wallet_id,
        agent_id="parent",
        budget_credits=Decimal("500"),
    )
    with pytest.raises(ValueError):
        await money.create_child_wallet(
            parent_wallet_id=agent.wallet_id,
            child_agent_id="child",
            budget_credits=raw,
            max_spend=Decimal("100"),
        )
    assert await _balance(agent.wallet_id) == Decimal("500")


# --- Empty wallet charge ---


async def test_empty_wallet_charge_reports_full_shortfall(clean_database):
    wallet = await _make_sponsor(Decimal("0"))
    money = get_agent_money()
    result = await money.charge(wallet.wallet_id, ServiceCategory.SANDBOX, Decimal("1"))
    assert isinstance(result, InsufficientFundsResponse)
    assert Decimal(result.shortfall_exact) == Decimal(result.required_amount_exact)
    assert Decimal(result.current_balance_exact) == Decimal("0")
    assert await _ledger_count(wallet.wallet_id) == 0
    assert await _balance(wallet.wallet_id) == Decimal("0")


# --- Concurrency: conservation under real parallel execution ---


async def test_concurrent_transfers_conserve_credits(clean_database):
    """Five transfers of 60 out of a 100-credit wallet: exactly one wins and
    no credits are created or destroyed."""
    money = get_agent_money()
    sender = await money.create_sponsor_wallet(
        sponsor_name="Sender",
        email="sender@example.com",
        initial_credits=Decimal("100"),
        require_kyc=False,
    )
    receiver = await money.create_sponsor_wallet(
        sponsor_name="Receiver",
        email="receiver@example.com",
        initial_credits=Decimal("0"),
        require_kyc=False,
    )
    results = await asyncio.gather(
        *[
            money.transfer(sender.wallet_id, receiver.wallet_id, Decimal("60"))
            for _ in range(5)
        ],
        return_exceptions=True,
    )
    winners = [r for r in results if isinstance(r, dict)]
    assert len(winners) == 1
    source_balance = await _balance(sender.wallet_id)
    dest_balance = await _balance(receiver.wallet_id)
    assert source_balance + dest_balance == Decimal("100")
    assert source_balance == Decimal("40")
    assert dest_balance == Decimal("60")


async def test_concurrent_double_refund_credits_once(clean_database):
    """Two parallel refunds of one charge: the wallet is credited exactly
    once, so a double-click or a retried webhook cannot mint credits."""
    money = get_agent_money()
    sponsor = await money.create_sponsor_wallet(
        sponsor_name="Sponsor",
        email="sponsor@example.com",
        initial_credits=Decimal("10000"),
        require_kyc=False,
    )
    agent = await money.create_agent_wallet(
        sponsor_wallet_id=sponsor.wallet_id,
        agent_id="agent",
        budget_credits=Decimal("1000"),
    )
    charge = await money.charge(agent.wallet_id, ServiceCategory.SANDBOX, Decimal("1"))
    assert not isinstance(charge, InsufficientFundsResponse)
    before = await _balance(agent.wallet_id)
    await asyncio.gather(
        money.refund_charge(wallet_id=agent.wallet_id, charge_entry_id=charge.entry_id),
        money.refund_charge(wallet_id=agent.wallet_id, charge_entry_id=charge.entry_id),
        return_exceptions=True,
    )
    after = await _balance(agent.wallet_id)
    charged = Decimal("1000") - before
    assert after - before == charged
