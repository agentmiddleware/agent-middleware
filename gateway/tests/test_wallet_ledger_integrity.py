"""Wallet money moves must be decided by the database, not by a stale read.

Each path here read a balance or counter in one statement and wrote it back in
a later one, serialized only by ``SELECT ... FOR UPDATE``. That lock is a
silent no-op on SQLite, so these were live defects rather than theory.

``validate_trust_mode_config`` now refuses a SQLite ``DATABASE_URL`` in
production-like environments, closing the configuration that made them
exploitable. These tests still run on SQLite deliberately: the missing lock is
what makes the defect observable, and the boot guard does not reach the local
and staging databases people do real work against.

The interleaves are forced deterministically through an instrumented session
rather than raced, so they cannot pass by scheduling luck. Each test asserts
the interleave actually fired before asserting anything else.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.database import get_session_factory
from sqlalchemy import select

from app.db.models import LedgerEntryModel, WalletModel
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.agent_money import InsufficientFundsResponse, get_agent_money
from tests.test_trust_helpers import provision_agent_wallet

from tests.conftest import interleaving_factory, requires_sqlite_row_lock_noop

# Every test in this module forces a concurrent writer into an open
# transaction, which is only a *concurrent* writer while the row lock is a
# no-op. See the marker's definition for why these are skipped rather than
# adapted on other engines.
pytestmark = requires_sqlite_row_lock_noop


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _balance(wallet_id: str) -> Decimal:
    factory = get_session_factory()
    async with factory() as session:
        wallet = await session.get(WalletModel, wallet_id)
        assert wallet is not None
        return wallet.balance


async def _debits(wallet_id: str) -> Decimal:
    """Total of the debits actually written to this wallet's ledger."""
    factory = get_session_factory()
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(LedgerEntryModel.amount).where(
                        LedgerEntryModel.wallet_id == wallet_id,  # type: ignore[arg-type]
                        LedgerEntryModel.action == "debit",  # type: ignore[arg-type]
                    )
                )
            )
            .scalars()
            .all()
        )
    return sum((abs(amount) for amount in rows), Decimal("0"))


async def _set_balance(wallet_id: str, amount: Decimal) -> None:
    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            wallet = await session.get(WalletModel, wallet_id)
            assert wallet is not None
            wallet.balance = amount
            session.add(wallet)


def _exact_amount(entry) -> Decimal:
    """Magnitude of a ledger entry as a Decimal.

    ``LedgerEntry.amount`` is a float for API compatibility; ``amount_exact``
    carries the decimal string. Balances are Decimal, so comparing against the
    float is both a TypeError and, if coerced, a rounding hazard on exactly
    the arithmetic these tests exist to check.
    """
    return abs(Decimal(entry.amount_exact or str(entry.amount)))


@pytest.mark.anyio
async def test_a_concurrent_charge_cannot_vanish_from_the_balance(
    client, clean_database, monkeypatch
):
    """The balance must equal the opening balance less every debit recorded.

    ``charge`` read ``wallet.balance``, and several statements later wrote back
    ``balance - charge_amount`` computed from that read. The row lock between
    them does nothing on SQLite, so a charge committing in between is
    overwritten: the ledger records the debit, the balance never reflects it,
    and that request was served for free. Conservation — balance equals opening
    balance minus the debits on the books — is the invariant that catches it,
    and it is the one the ledger's own credibility rests on.
    """
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    # Exactly enough for one charge, not two.
    await _set_balance(wallet_id, Decimal("10"))

    money = get_agent_money()
    import app.services.billing_engine as billing_module

    real_factory = get_session_factory()
    state: dict = {}

    async def _concurrent_charge() -> None:
        # A second charge commits in full while the first is mid-transaction.
        monkeypatch.setattr(
            billing_module, "get_session_factory", lambda: real_factory, raising=False
        )
        state["second"] = await money.charge(
            wallet_id=wallet_id,
            service_category=ServiceCategory.AGENT_COMMS,
            units=Decimal("1"),
            request_path="/concurrent-b",
        )

    monkeypatch.setattr(
        money._billing_engine,
        "_session_factory",
        interleaving_factory(real_factory, _concurrent_charge, state, fire_on=1),
    )

    state["first"] = await money.charge(
        wallet_id=wallet_id,
        service_category=ServiceCategory.AGENT_COMMS,
        units=Decimal("1"),
        request_path="/concurrent-a",
    )

    assert state.get("fired"), "the interleave never ran — the test proved nothing"
    # Money must actually have moved, or conservation is trivially satisfied by
    # two refusals and the test proves nothing.
    debited = await _debits(wallet_id)
    assert debited > Decimal("0"), "no debit was recorded — no money moved"
    # Whatever mix of success and refusal the two charges produced, the balance
    # must account for exactly the debits the ledger actually recorded.
    assert await _balance(wallet_id) == Decimal("10") - debited
    # And it may never go negative.
    assert await _balance(wallet_id) >= Decimal("0")


@pytest.mark.anyio
async def test_reclaiming_a_child_twice_cannot_mint_credits(
    client, clean_database, monkeypatch
):
    """A child's balance may be reclaimed to its parent exactly once.

    ``reclaim_child_wallet`` read ``child.balance``, zeroed the child, and
    credited the parent by the amount it had read. Both halves were protected
    only by row locks. Two concurrent reclaims of the same child therefore each
    credited the parent the full balance — the parent gained twice what the
    child ever held, and the extra credits were created from nothing.
    """
    money = get_agent_money()
    # Only agent (or child) wallets may spawn children.
    provisioned = await provision_agent_wallet(client)
    parent_id = provisioned["agent_wallet_id"]
    await _set_balance(parent_id, Decimal("100"))
    child = await money.create_child_wallet(
        parent_wallet_id=parent_id,
        child_agent_id="reclaim-integrity-child",
        budget_credits=Decimal("40"),
        max_spend=Decimal("40"),
        task_description="reclaim-integrity",
    )
    child_id = child.wallet_id

    parent_before = await _balance(parent_id)
    child_before = await _balance(child_id)
    assert child_before == Decimal("40")

    import app.services.wallet_engine as wallet_module  # noqa: F401

    real_factory = get_session_factory()
    state: dict = {}

    async def _concurrent_reclaim() -> None:
        monkeypatch.setattr(
            money._wallet_engine,
            "_session_factory",
            lambda: real_factory,
            raising=False,
        )
        try:
            state["second"] = await money.reclaim_child_wallet(child_id)
        except Exception as exc:  # a refusal is a correct outcome here
            state["second"] = exc

    monkeypatch.setattr(
        money._wallet_engine,
        "_session_factory",
        interleaving_factory(real_factory, _concurrent_reclaim, state, fire_on=1),
    )

    try:
        state["first"] = await money.reclaim_child_wallet(child_id)
    except Exception as exc:
        state["first"] = exc

    assert state.get("fired"), "the interleave never ran — the test proved nothing"
    # The parent may gain exactly what the child held, never twice.
    assert await _balance(parent_id) == parent_before + child_before
    assert await _balance(child_id) == Decimal("0")


@pytest.mark.anyio
async def test_concurrent_charges_cannot_exceed_a_child_wallet_spend_cap(
    client, clean_database, monkeypatch
):
    """A delegated child may not be pushed past the cap its parent granted.

    ``lifetime_debits + charge_amount <= max_spend`` was checked against a
    value read earlier and the increment happened several statements later, so
    two concurrent charges both cleared a cap only one of them fits inside.
    The child then spends more than the authority it was delegated — which is
    the whole point of ``max_spend``, not an accounting detail.

    Raised by review on #305 after the balance guard landed: the balance and
    the cap are two separate predicates, and fixing one does not carry the
    other.
    """
    money = get_agent_money()
    provisioned = await provision_agent_wallet(client)
    parent_id = provisioned["agent_wallet_id"]
    await _set_balance(parent_id, Decimal("1000"))
    child = await money.create_child_wallet(
        parent_wallet_id=parent_id,
        child_agent_id="cap-race-child",
        budget_credits=Decimal("100"),
        # Plenty of balance, but only enough cap for a single charge.
        max_spend=Decimal("2"),
        task_description="cap-race",
    )
    child_id = child.wallet_id

    real_factory = get_session_factory()
    state: dict = {}

    async def _concurrent_charge() -> None:
        state["second"] = await money.charge(
            wallet_id=child_id,
            service_category=ServiceCategory.AGENT_COMMS,
            units=Decimal("1"),
            request_path="/cap-race-b",
        )

    monkeypatch.setattr(
        money._billing_engine,
        "_session_factory",
        interleaving_factory(real_factory, _concurrent_charge, state, fire_on=1),
    )

    state["first"] = await money.charge(
        wallet_id=child_id,
        service_category=ServiceCategory.AGENT_COMMS,
        units=Decimal("1"),
        request_path="/cap-race-a",
    )

    assert state.get("fired"), "the interleave never ran — the test proved nothing"
    # Whatever mix of success and refusal, the delegated ceiling holds.
    assert await _debits(child_id) <= Decimal("2")


@pytest.mark.anyio
async def test_a_rejected_charge_does_not_erase_another_charges_velocity(
    client, clean_database, monkeypatch
):
    """Reversing one charge's velocity must not undo another's increment.

    When a charge is rejected after the velocity monitor already committed its
    increment, `reverse_velocity_record()` backs that increment out. It did so
    by reading `hourly_spent`/`daily_spent` and writing back read-charge — a
    read-modify-write, in the same function whose debit this PR had already
    converted to a guarded UPDATE. A charge committing in between is erased.

    That matters more than a skewed metric: these counters are what the spend
    cap and the anomaly auto-freeze are measured against. Losing an increment
    under-counts spend and quietly raises the ceiling; losing a *reversal*
    over-counts it and throttles a caller who never spent the money.

    Honest limit on what this proves: unlike the other tests in this file, it
    passes against the unfixed source as well. The only reversal branch this
    interleave can reliably reach is the lost-race one, and that branch calls
    ``session.refresh(wallet)`` first, which happens to make the stale read
    benign there. The five branches that reverse *without* a refresh are the
    exposed ones, and they need a rejection this harness cannot force on the
    same wallet the concurrent charge must succeed on. So treat this as a
    forward-looking invariant guard, not as a demonstration of the defect.
    """
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    # Enough for the concurrent charge to succeed, nothing for the outer one:
    # the outer charge is rejected and must reverse only its own increment.
    await _set_balance(wallet_id, Decimal("1.5"))

    money = get_agent_money()
    real_factory = get_session_factory()
    state: dict = {}

    async def _concurrent_charge() -> None:
        state["second"] = await money.charge(
            wallet_id=wallet_id,
            service_category=ServiceCategory.AGENT_COMMS,
            units=Decimal("1"),
            request_path="/velocity-b",
        )

    monkeypatch.setattr(
        money._billing_engine,
        "_session_factory",
        interleaving_factory(real_factory, _concurrent_charge, state, fire_on=1),
    )

    state["first"] = await money.charge(
        wallet_id=wallet_id,
        service_category=ServiceCategory.AGENT_COMMS,
        units=Decimal("1"),
        request_path="/velocity-a",
    )

    assert state.get("fired"), "the interleave never ran — the test proved nothing"
    debited = await _debits(wallet_id)
    assert debited > Decimal("0"), "no debit was recorded — no money moved"

    # The surviving charge's velocity increment must still be on the books.
    factory = get_session_factory()
    async with factory() as session:
        wallet = await session.get(WalletModel, wallet_id)
        assert wallet is not None
        assert wallet.daily_spent >= debited, (
            "a rejected charge's reversal erased a concurrent charge's increment"
        )


@pytest.mark.anyio
async def test_a_wallet_frozen_mid_charge_is_not_debited(
    client, clean_database, monkeypatch
):
    """A freeze landing after the read must still stop the debit.

    ``charge`` checks ``wallet.status`` against the row it loaded at the top of
    the transaction and writes several statements later. A freeze committing in
    between was invisible to that check, so the debit went through — and a
    freeze is the control that exists precisely to stop spending *now*, not at
    the next request. Spendability therefore travels with the debit.
    """
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    await _set_balance(wallet_id, Decimal("100"))

    money = get_agent_money()
    real_factory = get_session_factory()
    state: dict = {}

    async def _freeze_the_wallet() -> None:
        async with real_factory() as s:
            async with s.begin():
                wallet = await s.get(WalletModel, wallet_id)
                assert wallet is not None
                wallet.status = "frozen"
                s.add(wallet)

    monkeypatch.setattr(
        money._billing_engine,
        "_session_factory",
        interleaving_factory(real_factory, _freeze_the_wallet, state, fire_on=1),
    )

    result = await money.charge(
        wallet_id=wallet_id,
        service_category=ServiceCategory.AGENT_COMMS,
        units=Decimal("1"),
        request_path="/freeze-race",
    )

    assert state.get("fired"), "the interleave never ran — the test proved nothing"
    # Refused, and nothing debited.
    assert not hasattr(result, "entry_id"), f"a frozen wallet was debited: {result}"
    # A freeze is not a balance deficit: the refusal must not report a shortfall.
    assert getattr(result, "error", None) == "wallet_frozen"
    assert result.shortfall == 0.0
    assert result.shortfall_exact == "0"
    assert await _debits(wallet_id) == Decimal("0")
    assert await _balance(wallet_id) == Decimal("100")


@pytest.mark.anyio
async def test_a_concurrent_charge_cannot_erase_a_refund_credit(
    client, clean_database, monkeypatch
):
    """A refund credit must survive a charge landing mid-transaction.

    ``refund_charge`` read the wallet, then several statements later wrote the
    credit. Written as ``balance + refund_amount`` from that read, a charge
    committing in the gap is erased — the ledger shows the debit, the balance
    does not, and the customer keeps money the service already delivered
    against. The duplicate-refund guards inside ``refund_charge`` do not help:
    the two operations here are a refund and a charge, not two refunds.
    """
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    await _set_balance(wallet_id, Decimal("100"))

    money = get_agent_money()
    import app.services.billing_engine as billing_module

    real_factory = get_session_factory()

    # A real debit to refund. Its amount is what the credit will restore.
    charge = await money.charge(
        wallet_id=wallet_id,
        service_category=ServiceCategory.AGENT_COMMS,
        units=Decimal("1"),
        request_path="/to-be-refunded",
    )
    charged = _exact_amount(charge)
    opening = await _balance(wallet_id)

    state: dict = {}

    async def _concurrent_charge() -> None:
        monkeypatch.setattr(
            billing_module, "get_session_factory", lambda: real_factory, raising=False
        )
        state["second"] = await money.charge(
            wallet_id=wallet_id,
            service_category=ServiceCategory.AGENT_COMMS,
            units=Decimal("1"),
            request_path="/concurrent-during-refund",
        )

    monkeypatch.setattr(
        money._billing_engine,
        "_session_factory",
        interleaving_factory(real_factory, _concurrent_charge, state, fire_on=1),
    )

    await money.refund_charge(
        wallet_id=wallet_id,
        charge_entry_id=charge.entry_id,
        description="refund during a concurrent charge",
    )

    assert state.get("fired"), "the interleave never ran — the test proved nothing"
    second = _exact_amount(state["second"])
    assert second > Decimal("0"), "the concurrent charge did not move money"
    # The credit and the concurrent debit must both be on the balance.
    assert await _balance(wallet_id) == opening - second + charged


@pytest.mark.anyio
async def test_a_concurrent_charge_cannot_erase_a_reconciled_refund(
    client, clean_database, monkeypatch
):
    """``refund_reconciliation._apply_refund`` has the same shape.

    It is the repair path for a charge whose tool call never delivered, so a
    lost credit here is money taken for work that provably did not happen —
    and it runs as a background sweep, precisely when other charges on the
    same wallet are most likely to be in flight.
    """
    from app.services.refund_reconciliation import get_refund_reconciliation_service

    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    await _set_balance(wallet_id, Decimal("100"))

    money = get_agent_money()
    import app.services.billing_engine as billing_module

    real_factory = get_session_factory()

    charge = await money.charge(
        wallet_id=wallet_id,
        service_category=ServiceCategory.AGENT_COMMS,
        units=Decimal("1"),
        request_path="/undelivered",
    )
    charged = _exact_amount(charge)
    opening = await _balance(wallet_id)

    service = get_refund_reconciliation_service()
    state: dict = {}

    async def _concurrent_charge() -> None:
        monkeypatch.setattr(
            billing_module, "get_session_factory", lambda: real_factory, raising=False
        )
        state["second"] = await money.charge(
            wallet_id=wallet_id,
            service_category=ServiceCategory.AGENT_COMMS,
            units=Decimal("1"),
            request_path="/concurrent-during-reconcile",
        )

    async with real_factory() as session:
        async with session.begin():
            wallet = await session.get(WalletModel, wallet_id)
            assert wallet is not None
            charge_row = await session.get(LedgerEntryModel, charge.entry_id)
            assert charge_row is not None

            # ``_apply_refund`` takes an already-loaded wallet, so the read
            # this race turns on is the ``session.get`` above. The interleave
            # therefore goes here — after the service has the wallet in hand
            # and before it writes — rather than through the session wrapper
            # the other tests use.
            await _concurrent_charge()
            state["fired"] = True
            await service._apply_refund(
                session=session,
                wallet=wallet,
                charge=charge_row,
                amount=charged,
            )

    assert state.get("fired"), "the interleave never ran — the test proved nothing"
    second = _exact_amount(state["second"])
    assert second > Decimal("0"), "the concurrent charge did not move money"
    assert await _balance(wallet_id) == opening - second + charged


@pytest.mark.anyio
async def test_a_period_rollover_is_not_charged_for_a_rejected_charge(
    client, clean_database, monkeypatch
):
    """A reversal must not decrement a period the charge never contributed to.

    ``check_and_record_charge`` commits the velocity increment before the
    debit transaction takes the wallet, so a rejected debit has to compensate
    it. The counters roll over on their own schedule, though, and a rollover
    landing in between zeroes the counter this charge added to. Reversing
    against the *new* period takes credits off a total that belongs to live
    spend by other callers.

    The direction matters. An over-count throttles a caller who did not spend
    and heals at the next rollover; an under-count silently raises the
    effective spend cap and delays the anomaly auto-freeze, which are the two
    controls these counters exist to drive. So the reversal is guarded on the
    period marker it was recorded against and skipped when that no longer
    holds.
    """
    from datetime import timedelta

    from app.core.time import utc_now

    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    await _set_balance(wallet_id, Decimal("100"))

    money = get_agent_money()
    real_factory = get_session_factory()
    state: dict = {}

    async def _roll_the_period_and_freeze() -> None:
        """The hourly and daily windows roll, then the wallet is frozen.

        The freeze is what makes the debit reject, so the reversal runs. The
        rollover is backdated past both windows and the counters are set to a
        known live figure, standing in for spend by other callers in the new
        period.
        """
        async with real_factory() as s:
            async with s.begin():
                wallet = await s.get(WalletModel, wallet_id)
                assert wallet is not None
                now = utc_now()
                wallet.hourly_reset_at = now
                wallet.daily_reset_at = now
                wallet.hourly_spent = Decimal("7")
                wallet.daily_spent = Decimal("9")
                wallet.status = "frozen"
                s.add(wallet)

    # Put the wallet in an old period so the charge's own increment is
    # recorded against a marker the hook below then replaces.
    async with real_factory() as session:
        async with session.begin():
            wallet = await session.get(WalletModel, wallet_id)
            assert wallet is not None
            stale = utc_now() - timedelta(days=2)
            wallet.hourly_reset_at = stale
            wallet.daily_reset_at = stale
            session.add(wallet)

    monkeypatch.setattr(
        money._billing_engine,
        "_session_factory",
        interleaving_factory(
            real_factory, _roll_the_period_and_freeze, state, fire_on=1
        ),
    )

    result = await money.charge(
        wallet_id=wallet_id,
        service_category=ServiceCategory.AGENT_COMMS,
        units=Decimal("1"),
        request_path="/rollover-reversal",
    )

    assert state.get("fired"), "the interleave never ran — the test proved nothing"
    # Pin the response, not merely the absence of a ledger entry. The freeze
    # commits after the wallet SELECT returns, so the in-memory wallet still
    # reads "active" at the status check and it is the guarded UPDATE that
    # rejects the charge. Both branches reach the reversal this test measures,
    # but they are separately reachable, and ``not hasattr(result, "entry_id")``
    # records neither.
    assert isinstance(result, InsufficientFundsResponse), (
        f"a frozen wallet was debited: {result}"
    )
    assert result.error == "wallet_frozen"

    async with real_factory() as session:
        wallet = await session.get(WalletModel, wallet_id)
        assert wallet is not None
        # The new period's counters are untouched: this charge never added to
        # them, so it has nothing to take back from them.
        assert wallet.hourly_spent == Decimal("7")
        assert wallet.daily_spent == Decimal("9")
