"""Lost-update proofs for the wallet provisioning and transfer debits.

#305 converted the charge debit, the child reclaim, refunds, and the velocity
counters to guarded updates. It left three: the sponsor provisioning debit,
the parent delegation debit, and the transfer debit, all still reading a
balance, deciding, and writing the decided value back. They rely on
``SELECT ... FOR UPDATE`` to keep a second writer out of that window, and
SQLAlchemy **silently drops** ``FOR UPDATE`` on SQLite, so on that engine the
window is wide open.

Ordinary concurrency does not reproduce it: SQLite serializes the write
transactions, so two ``asyncio.gather``-ed operations simply take turns and
the second one reads fresh data. What the dropped lock actually allows is
narrower -- a competing writer committing *between* one operation's read and
its write -- so these tests inject exactly that, by wrapping a call the
operation already awaits inside that window and committing the competing
write from it. Nothing about the assertion depends on the injection: the
operation is left to decide on its own, and a decision made from the value it
read rather than the value the row holds is what fails.

They run on SQLite, because that is where the defect is reachable: the
injection only works while the row lock is a no-op. Pointed at PostgreSQL the
injected writer would block on the lock the operation under test genuinely
holds, so they skip there. The PostgreSQL side of the same guarantee is
covered by real concurrent operations in
``tests/test_permit_postgres_concurrency.py``, and the paths #305 already
fixed are covered by ``tests/test_wallet_ledger_integrity.py``.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete as sa_delete, update as sa_update

from app.db.database import get_engine, get_session_factory
from app.db.models import LedgerEntryModel, WalletModel
from app.schemas.billing import WalletStatus
from app.services.agent_money import (
    InsufficientFundsError,
    WalletNotFoundError,
    get_agent_money,
)
from app.services.wallet_engine import WalletEngine

_SKIP_REASON = (
    "the read-to-write window these tests inject into is only open where "
    "SELECT ... FOR UPDATE is dropped; on an engine that honours it the "
    "injected writer would block on the lock the operation already holds"
)

# Checked before any database fixture runs: the PostgreSQL jobs point
# DATABASE_URL at a real server, and even reaching a fixture there would open
# connections this module has no use for.
_DATABASE_URL = os.environ.get("DATABASE_URL", "")
if _DATABASE_URL and not _DATABASE_URL.startswith("sqlite"):
    pytest.skip(_SKIP_REASON, allow_module_level=True)

pytestmark = pytest.mark.anyio


def _skip_unless_sqlite() -> None:
    """Skip where ``FOR UPDATE`` is honoured and the injection would block."""
    engine = get_engine()
    if engine is not None and engine.dialect.name != "sqlite":
        pytest.skip(_SKIP_REASON)


async def _balance(wallet_id: str) -> Decimal:
    """Read a wallet balance in its own transaction."""
    factory = get_session_factory()
    async with factory() as session:
        wallet = await session.get(WalletModel, wallet_id)
        assert wallet is not None
        return wallet.balance


async def _commit_debit(wallet_id: str, amount: Decimal) -> None:
    """Commit a debit from outside the operation under test."""
    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            await session.execute(
                sa_update(WalletModel)
                .where(WalletModel.wallet_id == wallet_id)
                .values(balance=WalletModel.balance - amount)
            )


def _interfere_once(
    monkeypatch: pytest.MonkeyPatch,
    attr: str,
    interference: Callable[[], Awaitable[None]],
    *,
    before: bool = False,
) -> None:
    """Commit a competing write inside the operation's read-to-write window.

    ``attr`` names a ``WalletEngine`` coroutine the operation under test
    already awaits after loading the wallet and before writing it. This is the
    window a working ``SELECT ... FOR UPDATE`` would hold closed and that
    SQLite leaves open; the wrapper fires once, so only the first operation in
    a test is raced. Pass ``before`` when the named call *is* the write, so
    the competing commit still lands inside the window rather than after it.
    """
    original = getattr(WalletEngine, attr)
    state = {"fired": False}

    async def wrapper(self, *args, **kwargs):
        if before and not state["fired"]:
            state["fired"] = True
            await interference()
        result = await original(self, *args, **kwargs)
        if not before and not state["fired"]:
            state["fired"] = True
            await interference()
        return result

    monkeypatch.setattr(WalletEngine, attr, wrapper)


@pytest_asyncio.fixture
async def sponsor_wallet(clean_database) -> str:
    """A sponsor wallet with a known balance."""
    _skip_unless_sqlite()
    wallet = await get_agent_money().create_sponsor_wallet(
        sponsor_name=f"Concurrency Sponsor {uuid.uuid4().hex[:8]}",
        email="concurrency@example.com",
        initial_credits=Decimal("1000"),
        require_kyc=False,
    )
    return wallet.wallet_id


async def test_delegation_refuses_a_parent_balance_that_moved_under_it(
    sponsor_wallet: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child wallet must not be funded with credits the parent no longer has."""
    money = get_agent_money()
    parent = await money.create_agent_wallet(
        sponsor_wallet_id=sponsor_wallet,
        agent_id=f"agent-{uuid.uuid4().hex[:8]}",
        budget_credits=Decimal("400"),
    )
    _interfere_once(
        monkeypatch,
        "ensure_wallet_not_expired",
        lambda: _commit_debit(parent.wallet_id, Decimal("350")),
    )

    with pytest.raises(InsufficientFundsError) as excinfo:
        await money.create_child_wallet(
            parent_wallet_id=parent.wallet_id,
            child_agent_id=f"child-{uuid.uuid4().hex[:8]}",
            budget_credits=Decimal("200"),
            max_spend=Decimal("200"),
        )

    assert "insufficient" in str(excinfo.value).lower()
    assert await _balance(parent.wallet_id) == Decimal("50")


async def test_transfer_refuses_a_source_balance_that_moved_under_it(
    sponsor_wallet: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transfer must never pay out credits it did not take from the source.

    A lost update here is worse than an overdraft: the destination is credited
    from a debit that was rolled over by the competing writer, so the credits
    exist on one side and not the other.
    """
    money = get_agent_money()
    source = await money.create_agent_wallet(
        sponsor_wallet_id=sponsor_wallet,
        agent_id=f"src-{uuid.uuid4().hex[:8]}",
        budget_credits=Decimal("300"),
    )
    dest = await money.create_agent_wallet(
        sponsor_wallet_id=sponsor_wallet,
        agent_id=f"dst-{uuid.uuid4().hex[:8]}",
        budget_credits=Decimal("100"),
    )
    _interfere_once(
        monkeypatch,
        "ensure_wallet_not_expired",
        lambda: _commit_debit(source.wallet_id, Decimal("250")),
    )

    with pytest.raises(InsufficientFundsError) as excinfo:
        await money.transfer(
            from_wallet_id=source.wallet_id,
            to_wallet_id=dest.wallet_id,
            amount=Decimal("200"),
        )

    assert "insufficient" in str(excinfo.value).lower()
    assert await _balance(source.wallet_id) == Decimal("50")
    assert await _balance(dest.wallet_id) == Decimal("100")


async def test_sponsor_provisioning_refuses_a_balance_that_moved_under_it(
    sponsor_wallet: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two provisions must not hand out the same sponsor credits twice.

    Unlike the paths above, this one awaits nothing between its read and its
    write, so the window can only be entered at the write itself. That means
    this test pins the guard's contract rather than reproducing the original
    defect against the pre-fix code, which had no such seam -- the defect is
    the same shape as the delegation debit it sits beside.
    """
    _interfere_once(
        monkeypatch,
        "_apply_balance_delta",
        lambda: _commit_debit(sponsor_wallet, Decimal("900")),
        before=True,
    )

    with pytest.raises(InsufficientFundsError) as excinfo:
        await get_agent_money().create_agent_wallet(
            sponsor_wallet_id=sponsor_wallet,
            agent_id=f"agent-{uuid.uuid4().hex[:8]}",
            budget_credits=Decimal("500"),
        )

    assert "insufficient" in str(excinfo.value).lower()
    # The competing debit stands; nothing was provisioned from credits that
    # had already been spent.
    assert await _balance(sponsor_wallet) == Decimal("100")


async def _commit_freeze(wallet_id: str) -> None:
    """Freeze a wallet from outside the operation under test."""
    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            await session.execute(
                sa_update(WalletModel)
                .where(WalletModel.wallet_id == wallet_id)
                .values(status=WalletStatus.FROZEN.value)
            )


async def test_delegation_refuses_a_parent_frozen_under_it(
    sponsor_wallet: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A freeze landing mid-operation must stop the delegation debit.

    The parent's status is checked in Python and then awaited past, so a
    freeze committed in that window is invisible to the check. Spending from
    a frozen wallet is the failure the freeze exists to prevent, which is why
    the status rides in the UPDATE alongside the balance rather than being
    trusted from the read.
    """
    money = get_agent_money()
    parent = await money.create_agent_wallet(
        sponsor_wallet_id=sponsor_wallet,
        agent_id=f"agent-{uuid.uuid4().hex[:8]}",
        budget_credits=Decimal("400"),
    )
    _interfere_once(
        monkeypatch,
        "ensure_wallet_not_expired",
        lambda: _commit_freeze(parent.wallet_id),
    )

    with pytest.raises(ValueError) as excinfo:
        await money.create_child_wallet(
            parent_wallet_id=parent.wallet_id,
            child_agent_id=f"child-{uuid.uuid4().hex[:8]}",
            budget_credits=Decimal("200"),
            max_spend=Decimal("200"),
        )

    assert "frozen" in str(excinfo.value).lower()
    # Nothing was spent out of the frozen wallet.
    assert await _balance(parent.wallet_id) == Decimal("400")


async def _delete_wallet(wallet_id: str) -> None:
    """Remove a wallet row, and the ledger rows that reference it."""
    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            await session.execute(
                sa_delete(LedgerEntryModel).where(
                    LedgerEntryModel.wallet_id == wallet_id
                )
            )
            await session.execute(
                sa_delete(WalletModel).where(WalletModel.wallet_id == wallet_id)
            )


async def test_transfer_reports_a_destination_that_vanished(
    sponsor_wallet: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A credit matching no row must surface as a plain not-found.

    The destination credit is guarded on ``wallet_id`` alone, so the only way
    it matches nothing is that the row is gone. The debit is already applied by
    then, so the transfer has to abort -- and it has to abort with the reason,
    not with whatever SQLAlchemy raises first. Refreshing an instance whose row
    no longer exists raises ``InvalidRequestError``, which would otherwise
    reach the caller in place of the real answer.
    """
    money = get_agent_money()
    source = await money.create_agent_wallet(
        sponsor_wallet_id=sponsor_wallet,
        agent_id=f"src-{uuid.uuid4().hex[:8]}",
        budget_credits=Decimal("300"),
    )
    dest = await money.create_agent_wallet(
        sponsor_wallet_id=sponsor_wallet,
        agent_id=f"dst-{uuid.uuid4().hex[:8]}",
        budget_credits=Decimal("100"),
    )
    _interfere_once(
        monkeypatch,
        "ensure_wallet_not_expired",
        lambda: _delete_wallet(dest.wallet_id),
    )

    with pytest.raises(WalletNotFoundError) as excinfo:
        await money.transfer(
            from_wallet_id=source.wallet_id,
            to_wallet_id=dest.wallet_id,
            amount=Decimal("50"),
        )

    assert dest.wallet_id in str(excinfo.value)
    # The debit rolled back with the aborted transaction.
    assert await _balance(source.wallet_id) == Decimal("300")


async def _set_child_cap(wallet_id: str, max_spend: Decimal) -> None:
    """Force a wallet into child shape with an exact lifetime cap.

    ``max_spend=0`` cannot be created through the API (the request schema
    requires ``gt=0``), so a stored zero cap is reachable only from a direct
    service call or a migration. It is still a value the column can hold, and
    the guarded UPDATE treats it as a real cap, so the Python paths must agree.
    """
    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            await session.execute(
                sa_update(WalletModel)
                .where(WalletModel.wallet_id == wallet_id)
                .values(wallet_type="child", max_spend=max_spend)
            )


async def test_zero_lifetime_cap_is_a_cap_not_an_absent_one(
    sponsor_wallet: str,
) -> None:
    """A stored cap of zero must refuse, and say it was the cap.

    The guarded UPDATE compares against the column, so a zero cap already
    refuses the debit. The Python checks used truthiness, so zero read as "no
    cap set" -- the pre-check waved the debit through and the failure path
    reported an empty balance. Two halves of the same function disagreeing
    about what zero means is the actual defect; the caller seeing the wrong
    reason is the symptom.
    """
    money = get_agent_money()
    parent = await money.create_agent_wallet(
        sponsor_wallet_id=sponsor_wallet,
        agent_id=f"agent-{uuid.uuid4().hex[:8]}",
        budget_credits=Decimal("400"),
    )
    await _set_child_cap(parent.wallet_id, Decimal("0"))

    with pytest.raises(ValueError) as excinfo:
        await money.create_child_wallet(
            parent_wallet_id=parent.wallet_id,
            child_agent_id=f"child-{uuid.uuid4().hex[:8]}",
            budget_credits=Decimal("10"),
            max_spend=Decimal("10"),
        )

    assert "cap exceeded" in str(excinfo.value).lower()
    # The balance was never the problem, and none of it moved.
    assert await _balance(parent.wallet_id) == Decimal("400")


async def test_zero_lifetime_cap_refuses_a_transfer_as_a_cap(
    sponsor_wallet: str,
) -> None:
    """The transfer path must agree with the delegation path about zero."""
    money = get_agent_money()
    source = await money.create_agent_wallet(
        sponsor_wallet_id=sponsor_wallet,
        agent_id=f"src-{uuid.uuid4().hex[:8]}",
        budget_credits=Decimal("300"),
    )
    dest = await money.create_agent_wallet(
        sponsor_wallet_id=sponsor_wallet,
        agent_id=f"dst-{uuid.uuid4().hex[:8]}",
        budget_credits=Decimal("100"),
    )
    await _set_child_cap(source.wallet_id, Decimal("0"))

    with pytest.raises(ValueError) as excinfo:
        await money.transfer(
            from_wallet_id=source.wallet_id,
            to_wallet_id=dest.wallet_id,
            amount=Decimal("10"),
        )

    assert "cap exceeded" in str(excinfo.value).lower()
    assert await _balance(source.wallet_id) == Decimal("300")
    assert await _balance(dest.wallet_id) == Decimal("100")


async def test_negative_provisioning_budget_is_refused(
    sponsor_wallet: str,
) -> None:
    """A negative budget must be refused before any wallet is touched.

    The wallet-creation schemas require ``gt=0``, so this is unreachable over
    those routes -- but the guard it would otherwise meet is
    ``balance >= budget``, which a negative budget satisfies trivially. The
    debit would then run as a credit: the sponsor gains, and the new wallet
    opens holding a negative balance that no later reclaim can unwind.

    Zero is NOT refused, and must not be: ``SelfProvisionRequest`` accepts
    ``budget_credits=0`` (``ge=0``) to open an empty wallet for later top-up,
    and refusing it there would 500 the request after its sponsor wallet had
    already been created. That is asserted separately below.
    """
    budget = Decimal("-50")
    money = get_agent_money()
    before = await _balance(sponsor_wallet)

    with pytest.raises(ValueError, match="negative"):
        await money.create_agent_wallet(
            sponsor_wallet_id=sponsor_wallet,
            agent_id=f"agent-{uuid.uuid4().hex[:8]}",
            budget_credits=budget,
        )

    assert await _balance(sponsor_wallet) == before

    parent = await money.create_agent_wallet(
        sponsor_wallet_id=sponsor_wallet,
        agent_id=f"parent-{uuid.uuid4().hex[:8]}",
        budget_credits=Decimal("200"),
    )
    with pytest.raises(ValueError, match="negative"):
        await money.create_child_wallet(
            parent_wallet_id=parent.wallet_id,
            child_agent_id=f"child-{uuid.uuid4().hex[:8]}",
            budget_credits=budget,
            max_spend=Decimal("50"),
        )

    assert await _balance(parent.wallet_id) == Decimal("200")


async def test_zero_provisioning_budget_opens_an_empty_wallet(
    sponsor_wallet: str,
) -> None:
    """Zero is a legal budget: it debits nothing and opens an empty wallet.

    ``SelfProvisionRequest.budget_credits`` is declared ``ge=0``, so a
    zero-budget provision is a supported request. Refusing it would raise
    after the caller's sponsor wallet had already been committed, leaving an
    orphan behind and answering 500 to a well-formed request.
    """
    money = get_agent_money()
    before = await _balance(sponsor_wallet)

    agent = await money.create_agent_wallet(
        sponsor_wallet_id=sponsor_wallet,
        agent_id=f"agent-{uuid.uuid4().hex[:8]}",
        budget_credits=Decimal("0"),
    )

    assert await _balance(agent.wallet_id) == Decimal("0")
    assert await _balance(sponsor_wallet) == before
