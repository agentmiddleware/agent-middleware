"""Engine amount guards reject malformed creation inputs before money moves."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_session_factory
from app.db.models import LedgerEntryModel, WalletModel
from app.schemas.billing import InsufficientFundsResponse, ServiceCategory
from app.services.agent_money import get_agent_money
from app.services.ownership import CallerIdentity

pytestmark = pytest.mark.anyio

NON_FINITE = [
    pytest.param(Decimal("NaN"), id="decimal-nan"),
    pytest.param(Decimal("sNaN"), id="decimal-snan"),
    pytest.param(Decimal("Infinity"), id="decimal-positive-infinity"),
    pytest.param(Decimal("-Infinity"), id="decimal-negative-infinity"),
    pytest.param(float("nan"), id="float-nan"),
    pytest.param(float("inf"), id="float-positive-infinity"),
    pytest.param(float("-inf"), id="float-negative-infinity"),
]
NEGATIVE = [
    pytest.param(Decimal("-1"), id="decimal-negative"),
    pytest.param(-1.0, id="float-negative"),
]


def _owner(wallet_id: str) -> CallerIdentity:
    return CallerIdentity(wallet_id=wallet_id)


async def _state(session: AsyncSession):
    """Capture persisted money state, flushing any staged ORM writes first."""
    await session.flush()
    wallets = (await session.execute(select(WalletModel))).scalars().all()
    entries = (await session.execute(select(LedgerEntryModel))).scalars().all()
    return (
        sorted(
            (
                wallet.wallet_id,
                wallet.balance,
                wallet.status,
                wallet.lifetime_credits,
                wallet.lifetime_debits,
            )
            for wallet in wallets
        ),
        sorted(
            (
                entry.entry_id,
                entry.wallet_id,
                entry.action,
                entry.amount,
                entry.balance_after,
            )
            for entry in entries
        ),
    )


async def _committed_state():
    async with get_session_factory()() as session:
        return await _state(session)


async def _sponsor(credits: Decimal = Decimal("100")):
    return await get_agent_money().create_sponsor_wallet(
        sponsor_name="Amount guard sponsor",
        email="amount-guard@example.com",
        initial_credits=credits,
        require_kyc=False,
    )


async def _agent():
    sponsor = await _sponsor()
    agent = await get_agent_money().create_agent_wallet(
        sponsor_wallet_id=sponsor.wallet_id,
        agent_id="amount-guard-parent",
        budget_credits=Decimal("60"),
        caller=_owner(sponsor.wallet_id),
    )
    return sponsor, agent


@pytest.mark.parametrize("raw", NON_FINITE)
@pytest.mark.parametrize("caller_owned", [False, True])
async def test_sponsor_rejects_non_finite_before_writes(
    clean_database, raw, caller_owned
):
    money = get_agent_money()
    before = await _committed_state()
    if caller_owned:
        async with get_session_factory()() as session:
            async with session.begin():
                with pytest.raises(ValueError) as error:
                    await money.create_sponsor_wallet(
                        sponsor_name="Invalid",
                        email="invalid@example.com",
                        initial_credits=raw,
                        require_kyc=False,
                        session=session,
                    )
                assert str(error.value) == "initial_credits must be finite"
                assert session.in_transaction()
                assert await _state(session) == before
    else:
        with pytest.raises(ValueError) as error:
            await money.create_sponsor_wallet(
                sponsor_name="Invalid",
                email="invalid@example.com",
                initial_credits=raw,
                require_kyc=False,
            )
        assert str(error.value) == "initial_credits must be finite"
    assert await _committed_state() == before


@pytest.mark.parametrize("raw", NEGATIVE)
async def test_sponsor_keeps_negative_refusal(clean_database, raw):
    before = await _committed_state()
    with pytest.raises(ValueError) as error:
        await _sponsor(raw)
    assert str(error.value) == "initial_credits cannot be negative"
    assert await _committed_state() == before


@pytest.mark.parametrize("credits", [Decimal("0"), 0.0, Decimal("12.5"), 12.5])
async def test_sponsor_keeps_zero_and_finite_seed(clean_database, credits):
    wallet = await _sponsor(credits)
    state = await _committed_state()
    assert state[0][0][0] == wallet.wallet_id
    assert state[0][0][1] == Decimal(str(credits))
    assert len(state[1]) == (1 if credits else 0)


@pytest.mark.parametrize("raw", NON_FINITE)
@pytest.mark.parametrize("caller_owned", [False, True])
async def test_agent_rejects_non_finite_without_debit(
    clean_database, raw, caller_owned
):
    sponsor = await _sponsor()
    money = get_agent_money()
    before = await _committed_state()
    if caller_owned:
        async with get_session_factory()() as session:
            async with session.begin():
                with pytest.raises(ValueError) as error:
                    await money.create_agent_wallet(
                        sponsor_wallet_id=sponsor.wallet_id,
                        agent_id="invalid",
                        budget_credits=raw,
                        session=session,
                        caller=_owner(sponsor.wallet_id),
                    )
                assert str(error.value) == "budget_credits must be finite"
                assert session.in_transaction()
                assert await _state(session) == before
    else:
        with pytest.raises(ValueError) as error:
            await money.create_agent_wallet(
                sponsor_wallet_id=sponsor.wallet_id,
                agent_id="invalid",
                budget_credits=raw,
                caller=_owner(sponsor.wallet_id),
            )
        assert str(error.value) == "budget_credits must be finite"
    assert await _committed_state() == before


@pytest.mark.parametrize("raw", NEGATIVE)
async def test_agent_keeps_negative_refusal(clean_database, raw):
    sponsor = await _sponsor()
    before = await _committed_state()
    with pytest.raises(ValueError) as error:
        await get_agent_money().create_agent_wallet(
            sponsor_wallet_id=sponsor.wallet_id,
            agent_id="invalid",
            budget_credits=raw,
            caller=_owner(sponsor.wallet_id),
        )
    assert str(error.value) == "budget_credits cannot be negative"
    assert await _committed_state() == before


@pytest.mark.parametrize("budget", [Decimal("0"), 0.0, Decimal("12.5"), 12.5])
async def test_agent_keeps_zero_and_finite_budget(clean_database, budget):
    sponsor = await _sponsor()
    agent = await get_agent_money().create_agent_wallet(
        sponsor_wallet_id=sponsor.wallet_id,
        agent_id="valid",
        budget_credits=budget,
        caller=_owner(sponsor.wallet_id),
    )
    state = await _committed_state()
    balances = {wallet_id: balance for wallet_id, balance, *_ in state[0]}
    assert balances[sponsor.wallet_id] == Decimal("100") - Decimal(str(budget))
    assert balances[agent.wallet_id] == Decimal(str(budget))
    assert len(state[1]) == 3


@pytest.mark.parametrize("raw", NON_FINITE)
async def test_child_rejects_non_finite_budget_without_debit(clean_database, raw):
    _, parent = await _agent()
    before = await _committed_state()
    with pytest.raises(ValueError) as error:
        await get_agent_money().create_child_wallet(
            parent_wallet_id=parent.wallet_id,
            child_agent_id="invalid",
            budget_credits=raw,
            max_spend=Decimal("20"),
            caller=_owner(parent.wallet_id),
        )
    assert str(error.value) == "budget_credits must be finite"
    assert await _committed_state() == before


@pytest.mark.parametrize("raw", NEGATIVE)
async def test_child_keeps_negative_budget_refusal(clean_database, raw):
    _, parent = await _agent()
    before = await _committed_state()
    with pytest.raises(ValueError) as error:
        await get_agent_money().create_child_wallet(
            parent_wallet_id=parent.wallet_id,
            child_agent_id="invalid",
            budget_credits=raw,
            max_spend=Decimal("20"),
            caller=_owner(parent.wallet_id),
        )
    assert str(error.value) == "budget_credits cannot be negative"
    assert await _committed_state() == before


@pytest.mark.parametrize("budget", [Decimal("0"), 0.0, Decimal("12.5"), 12.5])
async def test_child_keeps_zero_and_finite_budget(clean_database, budget):
    _, parent = await _agent()
    child = await get_agent_money().create_child_wallet(
        parent_wallet_id=parent.wallet_id,
        child_agent_id="valid",
        budget_credits=budget,
        max_spend=Decimal("20"),
        caller=_owner(parent.wallet_id),
    )
    state = await _committed_state()
    balances = {wallet_id: balance for wallet_id, balance, *_ in state[0]}
    assert balances[parent.wallet_id] == Decimal("60") - Decimal(str(budget))
    assert balances[child.wallet_id] == Decimal(str(budget))
    assert len(state[1]) == 5


@pytest.mark.parametrize("raw", [*NON_FINITE, *NEGATIVE, Decimal("0"), 0.0])
async def test_child_rejects_invalid_spend_cap_without_debit(clean_database, raw):
    _, parent = await _agent()
    before = await _committed_state()
    with pytest.raises(ValueError) as error:
        await get_agent_money().create_child_wallet(
            parent_wallet_id=parent.wallet_id,
            child_agent_id="invalid",
            budget_credits=Decimal("10"),
            max_spend=raw,
            caller=_owner(parent.wallet_id),
        )
    assert str(error.value) == "max_spend must be a finite number greater than zero"
    assert await _committed_state() == before


@pytest.mark.parametrize("max_spend", [Decimal("20"), 20.0])
async def test_child_accepts_finite_positive_spend_cap(clean_database, max_spend):
    _, parent = await _agent()
    child = await get_agent_money().create_child_wallet(
        parent_wallet_id=parent.wallet_id,
        child_agent_id="valid",
        budget_credits=Decimal("10"),
        max_spend=max_spend,
        caller=_owner(parent.wallet_id),
    )
    async with get_session_factory()() as session:
        wallet = await session.get(WalletModel, child.wallet_id)
        assert wallet is not None
        assert wallet.max_spend == Decimal("20")


async def test_empty_wallet_charge_reports_full_shortfall(clean_database):
    wallet = await _sponsor(Decimal("0"))
    before = await _committed_state()
    result = await get_agent_money().charge(
        wallet.wallet_id,
        ServiceCategory.SANDBOX,
        Decimal("1"),
        caller=_owner(wallet.wallet_id),
    )
    assert isinstance(result, InsufficientFundsResponse)
    assert Decimal(result.shortfall_exact) == Decimal(result.required_amount_exact)
    assert Decimal(result.current_balance_exact) == Decimal("0")
    assert await _committed_state() == before


async def test_concurrent_double_refund_credits_once(clean_database):
    sponsor = await _sponsor(Decimal("10000"))
    money = get_agent_money()
    agent = await money.create_agent_wallet(
        sponsor_wallet_id=sponsor.wallet_id,
        agent_id="refund-race",
        budget_credits=Decimal("1000"),
        caller=_owner(sponsor.wallet_id),
    )
    charge = await money.charge(
        agent.wallet_id,
        ServiceCategory.SANDBOX,
        Decimal("1"),
        caller=_owner(agent.wallet_id),
    )
    assert not isinstance(charge, InsufficientFundsResponse)
    before = await _committed_state()
    before_balance = next(
        balance for wallet_id, balance, *_ in before[0] if wallet_id == agent.wallet_id
    )
    await asyncio.gather(
        money.refund_charge(
            wallet_id=agent.wallet_id,
            charge_entry_id=charge.entry_id,
            caller=_owner(agent.wallet_id),
        ),
        money.refund_charge(
            wallet_id=agent.wallet_id,
            charge_entry_id=charge.entry_id,
            caller=_owner(agent.wallet_id),
        ),
        return_exceptions=True,
    )
    after = await _committed_state()
    after_balance = next(
        balance for wallet_id, balance, *_ in after[0] if wallet_id == agent.wallet_id
    )
    assert after_balance - before_balance == Decimal("1000") - before_balance
    refunds = [
        entry
        for entry in after[1]
        if entry[1] == agent.wallet_id and entry[2] == "refund"
    ]
    assert len(refunds) == 1
