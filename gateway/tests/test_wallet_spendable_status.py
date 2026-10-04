"""Every wallet-funded debit shares the billing spendability boundary."""

from decimal import Decimal

import pytest
from sqlalchemy import func, select, update

from app.db.database import get_session_factory
from app.db.models import LedgerEntryModel, WalletModel
from app.services.agent_money import get_agent_money


@pytest.fixture
async def funded_wallets(clean_database):
    money = get_agent_money()
    sponsor = await money.create_sponsor_wallet(
        "Status boundary", "status@example.test", Decimal("100")
    )
    agent = await money.create_agent_wallet(sponsor.wallet_id, "source", Decimal("50"))
    receiver = await money.create_agent_wallet(
        sponsor.wallet_id, "receiver", Decimal("10")
    )
    return sponsor.wallet_id, agent.wallet_id, receiver.wallet_id


async def _snapshot():
    async with get_session_factory()() as session:
        wallets = (await session.execute(select(WalletModel))).scalars().all()
        return {
            "wallets": {
                wallet.wallet_id: (
                    wallet.balance,
                    wallet.lifetime_debits,
                    wallet.lifetime_credits,
                    wallet.hourly_spent,
                    wallet.daily_spent,
                    wallet.status,
                )
                for wallet in wallets
            },
            "ledger_count": await session.scalar(
                select(func.count()).select_from(LedgerEntryModel)
            ),
        }


async def _debit(operation, source_id, receiver_id):
    money = get_agent_money()
    if operation == "transfer":
        return await money.transfer(source_id, receiver_id, Decimal("10"))
    if operation == "agent":
        return await money.create_agent_wallet(source_id, "new", Decimal("10"))
    assert operation == "child"
    return await money.create_child_wallet(
        source_id, "new-child", Decimal("10"), Decimal("10")
    )


@pytest.mark.parametrize("operation", ["transfer", "agent", "child"])
@pytest.mark.parametrize("status", ["frozen", "suspended", "closed", "operator_hold"])
async def test_non_spendable_status_blocks_every_wallet_debit(
    funded_wallets, operation, status
):
    sponsor_id, agent_id, receiver_id = funded_wallets
    source_id = sponsor_id if operation == "agent" else agent_id
    async with get_session_factory()() as session:
        wallet = await session.get(WalletModel, source_id)
        wallet.status = status
        await session.commit()
    before = await _snapshot()

    with pytest.raises(ValueError, match=status):
        await _debit(operation, source_id, receiver_id)

    # Denial cannot debit the source, credit another wallet, mint a child,
    # alter spend counters, or append a transfer ledger entry.
    assert await _snapshot() == before


@pytest.mark.parametrize("operation", ["transfer", "agent", "child"])
@pytest.mark.parametrize("status", ["active", "pending_kyc"])
async def test_spendable_status_preserves_existing_wallet_debits(
    funded_wallets, operation, status
):
    sponsor_id, agent_id, receiver_id = funded_wallets
    source_id = sponsor_id if operation == "agent" else agent_id
    async with get_session_factory()() as session:
        wallet = await session.get(WalletModel, source_id)
        wallet.status = status
        await session.commit()
    before = await _snapshot()

    await _debit(operation, source_id, receiver_id)

    after = await _snapshot()
    source_before = before["wallets"][source_id]
    source_after = after["wallets"][source_id]
    assert source_after[0] == source_before[0] - Decimal("10")
    assert source_after[1] == source_before[1] + Decimal("10")
    assert source_after[2:] == source_before[2:]
    assert after["ledger_count"] == before["ledger_count"] + 2
    assert len(after["wallets"]) == len(before["wallets"]) + (operation != "transfer")
    assert sum(row[0] for row in after["wallets"].values()) == sum(
        row[0] for row in before["wallets"].values()
    )


@pytest.mark.parametrize("status", ["frozen", "suspended", "closed", "operator_hold"])
async def test_guarded_wallet_debit_rechecks_status_after_stale_read(
    funded_wallets, status
):
    _, source_id, _ = funded_wallets
    before = await _snapshot()
    async with get_session_factory()() as session:
        wallet = await session.get(WalletModel, source_id)
        assert wallet.status == "active"
        # Preserve the loaded active object while changing the row: this
        # deterministically exercises the SQL guard, not the Python precheck.
        await session.execute(
            update(WalletModel)
            .where(WalletModel.wallet_id == source_id)
            .values(status=status)
            .execution_options(synchronize_session=False)
        )
        assert wallet.status == "active"

        applied = await get_agent_money()._wallet_engine._apply_balance_delta(
            session,
            wallet,
            balance_delta=Decimal("-10"),
            lifetime_debits_delta=Decimal("10"),
            require_balance=Decimal("10"),
            require_spendable=True,
        )

        assert applied is False
        assert wallet.status == status
        await session.commit()
    before["wallets"][source_id] = (*before["wallets"][source_id][:-1], status)
    assert await _snapshot() == before
