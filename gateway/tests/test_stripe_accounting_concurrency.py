"""Force distinct Stripe events to overlap at their vulnerable read boundaries."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.database import get_session_factory
from app.db.models import WalletModel, LedgerEntryModel
from app.services.stripe_integration import StripeIntegration
from tests.conftest import interleaving_factory, requires_sqlite_row_lock_noop

pytestmark = requires_sqlite_row_lock_noop


async def seed_wallet():
    async with get_session_factory()() as session:
        session.add(WalletModel(wallet_id="qa-stripe-sponsor", wallet_type="sponsor"))
        await session.commit()
    return "qa-stripe-sponsor"


@pytest.mark.anyio
async def test_distinct_settled_topups_must_preserve_both_credits(
    clean_database, monkeypatch
):
    wallet_id = await seed_wallet()
    outer, inner = StripeIntegration(), StripeIntegration()
    factory = get_session_factory()
    state = {}

    async def concurrent_mint():
        await inner._mint_credits(wallet_id, Decimal("20"), "pi_qa_inner", "synthetic")

    monkeypatch.setattr(
        outer,
        "_session_factory",
        interleaving_factory(factory, concurrent_mint, state, fire_on=1),
    )
    await outer._mint_credits(wallet_id, Decimal("30"), "pi_qa_outer", "synthetic")
    async with factory() as session:
        wallet = await session.get(WalletModel, wallet_id)
        entries = (await session.execute(select(LedgerEntryModel))).scalars().all()
    assert state["fired"]
    assert sum(entry.amount for entry in entries) == Decimal("50")
    assert sorted(entry.balance_after for entry in entries) == [
        Decimal("20"),
        Decimal("50"),
    ]
    assert wallet.balance == Decimal("50")
    assert wallet.lifetime_credits == Decimal("50")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "outer_refunded, expected_refund",
    [(1000, "10000"), (2000, "20000"), (500, "10000")],
)
async def test_concurrent_cumulative_refunds_must_apply_only_new_delta(
    clean_database, monkeypatch, outer_refunded, expected_refund
):
    wallet_id = await seed_wallet()
    outer, inner = StripeIntegration(), StripeIntegration()
    await inner._mint_credits(wallet_id, Decimal("50000"), "pi_qa_refund", "synthetic")
    charge = {
        "payment_intent": "pi_qa_refund",
        "amount": 5000,
        "amount_refunded": 1000,
        "currency": "usd",
        "id": "ch_qa_refund",
    }
    factory = get_session_factory()
    state = {}

    async def concurrent_refund():
        await inner._handle_refund(charge, "evt_qa_inner")

    monkeypatch.setattr(
        outer,
        "_session_factory",
        interleaving_factory(factory, concurrent_refund, state, fire_on=3),
    )
    await outer._handle_refund(
        {**charge, "amount_refunded": outer_refunded}, "evt_qa_outer"
    )
    async with factory() as session:
        wallet = await session.get(WalletModel, wallet_id)
        entries = (await session.execute(select(LedgerEntryModel))).scalars().all()
    refunds = [entry for entry in entries if entry.action == "refund"]
    assert state["fired"]
    assert wallet.lifetime_debits == Decimal(expected_refund)
    assert -sum(entry.amount for entry in refunds) == Decimal(expected_refund)
    assert wallet.balance == Decimal("50000") - Decimal(expected_refund)
    assert len(refunds) == (2 if outer_refunded == 2000 else 1)
