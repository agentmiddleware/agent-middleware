"""Wallet lifecycle and hierarchy engine used by AgentMoney.

This module owns wallet creation, delegation, reclaim, transfer, locking, and
wallet reads. The public compatibility surface remains AgentMoney.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import or_, select, update as sa_update
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from ..core.config import Settings
from ..core.time import utc_now
from ..db.converters import wallet_model_to_response
from ..db.models import LedgerEntryModel, WalletModel
from ..schemas.billing import (
    KYCStatus,
    LedgerAction,
    WalletResponse,
    WalletStatus,
    WalletType,
)
from .wallet_status import SPENDABLE_WALLET_STATUSES

logger = logging.getLogger(__name__)

SessionFactoryProvider = Callable[[], async_sessionmaker[AsyncSession]]
WalletNotFoundFactory = Callable[[str], Exception]
InsufficientFundsFactory = Callable[[str, Decimal, Decimal], Exception]
MetadataSerializer = Callable[[dict | None], str | None]
MAX_CHILD_WALLET_TTL_SECONDS = 2_147_483_647
_MAX_WALLET_HIERARCHY_DEPTH = 32


class WalletExpiredError(RuntimeError):
    """Raised when an operation targets an expired wallet authority."""

    def __init__(self, wallet_id: str, expires_at: datetime):
        self.wallet_id = wallet_id
        self.expires_at = expires_at
        super().__init__(f"Wallet {wallet_id} expired at {expires_at.isoformat()}")


def _validate_child_wallet_ttl(ttl_seconds: int | None) -> None:
    """Keep service callers within the persisted PostgreSQL INTEGER range."""
    if ttl_seconds is None:
        return
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or ttl_seconds <= 0
        or ttl_seconds > MAX_CHILD_WALLET_TTL_SECONDS
    ):
        raise ValueError(
            f"ttl_seconds must be between 1 and {MAX_CHILD_WALLET_TTL_SECONDS}"
        )


def child_wallet_expires_at(wallet: WalletModel) -> datetime | None:
    """Return this row's own child-wallet expiry, if it has one."""
    if wallet.wallet_type != WalletType.CHILD.value or wallet.ttl_seconds is None:
        return None
    return wallet.created_at + timedelta(seconds=wallet.ttl_seconds)


class WalletEngine:
    """Internal wallet lifecycle implementation behind AgentMoney."""

    def __init__(
        self,
        *,
        session_factory: SessionFactoryProvider,
        settings: Settings,
        wallet_not_found_error: WalletNotFoundFactory,
        insufficient_funds_error: InsufficientFundsFactory,
        metadata_to_json: MetadataSerializer,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._wallet_not_found_error = wallet_not_found_error
        self._insufficient_funds_error = insufficient_funds_error
        self._metadata_to_json = metadata_to_json

    async def _effective_expiry(
        self,
        session: AsyncSession,
        wallet: WalletModel,
    ) -> datetime | None:
        """Return the earliest TTL deadline in a wallet's authority chain.

        Parent expiry constrains every descendant. This runtime walk protects
        wallets created before expiry attenuation was enforced at creation.
        Malformed cycles, missing parents, and over-deep chains fail closed.
        """
        current = wallet
        effective_expiry: datetime | None = None
        seen: set[str] = set()

        for _ in range(_MAX_WALLET_HIERARCHY_DEPTH):
            if current.wallet_id in seen:
                return datetime.min
            seen.add(current.wallet_id)

            own_expiry = child_wallet_expires_at(current)
            if own_expiry is not None and (
                effective_expiry is None or own_expiry < effective_expiry
            ):
                effective_expiry = own_expiry

            if not current.parent_wallet_id:
                return effective_expiry
            parent = await session.get(WalletModel, current.parent_wallet_id)
            if parent is None:
                return datetime.min
            current = parent

        return datetime.min

    async def ensure_wallet_not_expired(
        self,
        session: AsyncSession,
        wallet: WalletModel,
        *,
        now: datetime | None = None,
    ) -> datetime | None:
        expires_at = await self._effective_expiry(session, wallet)
        if expires_at is not None and expires_at <= (now or utc_now()):
            raise WalletExpiredError(wallet.wallet_id, expires_at)
        return expires_at

    async def wallet_is_expired(self, wallet_id: str) -> bool:
        """Return whether a wallet or any ancestor authority has expired."""
        async with self._session_factory()() as session:
            wallet = await session.get(WalletModel, wallet_id)
            if wallet is None:
                return False
            expires_at = await self._effective_expiry(session, wallet)
            return expires_at is not None and expires_at <= utc_now()

    async def _apply_balance_delta(
        self,
        session: AsyncSession,
        wallet: WalletModel,
        *,
        balance_delta: Decimal,
        lifetime_debits_delta: Decimal = Decimal("0"),
        lifetime_credits_delta: Decimal = Decimal("0"),
        require_balance: Decimal | None = None,
        require_child_cap_for: Decimal | None = None,
        require_spendable: bool = False,
    ) -> bool:
        """Move a wallet balance with a guarded UPDATE, never a write-back.

        The provisioning and transfer paths reached this module's checks the
        same way the charge debit did before #305: read the balance, decide,
        write the decided value back. ``SELECT ... FOR UPDATE`` is a silent
        no-op on SQLite, so the lock those checks lean on is not always taken,
        and the decision is made against a value another writer may already
        have moved.

        Restating each racing precondition in the WHERE clause makes the
        database re-check it in the same statement as the write. Returns
        whether the row was updated. The caller's ``wallet`` instance is
        refreshed either way: ``synchronize_session=False`` bypasses the
        identity map, and both the ``balance_after`` written onto ledger
        entries and the errors raised on a lost race must reflect the row
        rather than the stale read.
        """
        guards: list[ColumnElement[bool]] = [
            cast(ColumnElement[bool], WalletModel.wallet_id == wallet.wallet_id)
        ]
        if require_balance is not None:
            guards.append(
                cast(ColumnElement[bool], WalletModel.balance >= require_balance)
            )
        if require_child_cap_for is not None:
            # Compared against the row's own max_spend rather than a value read
            # into Python, so a cap changed concurrently is honoured too.
            # Non-child wallets and children with no cap are unconstrained.
            guards.append(
                cast(
                    ColumnElement[bool],
                    or_(
                        cast(
                            ColumnElement[bool],
                            WalletModel.wallet_type != WalletType.CHILD.value,
                        ),
                        cast(Any, WalletModel.max_spend).is_(None),
                        cast(
                            ColumnElement[bool],
                            WalletModel.lifetime_debits + require_child_cap_for
                            <= cast(Any, WalletModel.max_spend),
                        ),
                    ),
                )
            )
        if require_spendable:
            guards.append(
                cast(
                    ColumnElement[bool],
                    cast(Any, WalletModel.status).in_(tuple(SPENDABLE_WALLET_STATUSES)),
                )
            )

        values: dict[str, Any] = {"balance": WalletModel.balance + balance_delta}
        if lifetime_debits_delta:
            values["lifetime_debits"] = (
                WalletModel.lifetime_debits + lifetime_debits_delta
            )
        if lifetime_credits_delta:
            values["lifetime_credits"] = (
                WalletModel.lifetime_credits + lifetime_credits_delta
            )
        values["updated_at"] = utc_now()

        applied = await session.execute(
            sa_update(WalletModel)
            .where(*guards)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        matched = (cast(Any, applied).rowcount or 0) == 1
        if matched:
            # Only a matched row can be refreshed. Refreshing before checking
            # would raise on a row that no longer exists, which is the one case
            # the caller most needs reported as a plain miss.
            await session.refresh(wallet)
        else:
            # A miss usually means a guard failed and the row is still there, so
            # refresh to give the caller the committed values its error message
            # is built from. A miss can also mean the row is gone, and then
            # there is nothing to refresh -- keep the stale instance rather than
            # letting SQLAlchemy raise over the caller's own error.
            # ObjectDeletedError subclasses InvalidRequestError, so this catches
            # both the deleted-row and the unrefreshable-instance cases.
            try:
                await session.refresh(wallet)
            except InvalidRequestError:
                pass
        return matched

    async def _lock_wallets_in_order(
        self, session: AsyncSession, wallet_ids: list[str]
    ) -> dict[str, WalletModel]:
        """Lock each wallet row ``FOR UPDATE`` in a globally consistent order
        (sorted by wallet_id) via one statement per row.

        Every multi-wallet money operation (transfer, child reclaim) must
        acquire its locks through this helper so two operations touching the
        same pair can never deadlock by taking the locks in opposite orders.
        A single ``WHERE wallet_id IN (...)`` does NOT guarantee lock order --
        Postgres locks rows in scan order, not in the IN-list order -- which is
        why the locks are taken one at a time here.
        """
        locked: dict[str, WalletModel] = {}
        for wid in sorted(set(wallet_ids)):
            row = (
                await session.execute(
                    select(WalletModel)
                    .where(cast(ColumnElement[bool], WalletModel.wallet_id == wid))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is not None:
                locked[wid] = row
        return locked

    # --- Wallet Management ---

    async def create_sponsor_wallet(
        self,
        sponsor_name: str,
        email: str,
        initial_credits: Decimal = Decimal("0"),
        currency: str = "USD",
        metadata: dict | None = None,
        require_kyc: bool | None = None,
        session: AsyncSession | None = None,
    ) -> WalletResponse:
        """Create a human sponsor (liability sink) root wallet.

        ``session``, when given, must already be inside a transaction the
        CALLER owns end to end (an open ``async with session.begin():``
        block) — this method only ``flush()``es on it, never begins or
        commits. That lets a caller compose this with other wallet/key
        operations into one atomic unit of work instead of each call
        committing independently (see app/services/pods.py). Omit it (the
        default) for the original standalone behavior: opens, commits, and
        closes its own session.
        """
        kyc_required = (
            require_kyc
            if require_kyc is not None
            else self._settings.KYC_REQUIRED_FOR_TOPUP
        )

        if session is not None:
            return await self._create_sponsor_wallet_in(
                session,
                sponsor_name,
                email,
                initial_credits,
                currency,
                metadata,
                kyc_required,
            )

        async with self._session_factory()() as session:
            async with session.begin():
                result = await self._create_sponsor_wallet_in(
                    session,
                    sponsor_name,
                    email,
                    initial_credits,
                    currency,
                    metadata,
                    kyc_required,
                )
            await session.commit()
            logger.info(f"Created sponsor wallet {result.wallet_id} for {sponsor_name}")
            return result

    async def _create_sponsor_wallet_in(
        self,
        session: AsyncSession,
        sponsor_name: str,
        email: str,
        initial_credits: Decimal,
        currency: str,
        metadata: dict | None,
        kyc_required: bool,
    ) -> WalletResponse:
        """Core sponsor-creation logic against an already-open session.

        No BEGIN/COMMIT here — the caller's transaction owns those.
        """
        wallet_id = f"spn-{uuid.uuid4().hex[:12]}"

        wallet = WalletModel(
            wallet_id=wallet_id,
            wallet_type=WalletType.SPONSOR.value,
            owner_name=sponsor_name,
            email=email,
            balance=initial_credits,
            lifetime_credits=initial_credits,
            metadata_json=self._metadata_to_json(metadata),
            kyc_status=(
                KYCStatus.PENDING.value
                if kyc_required
                else KYCStatus.NOT_REQUIRED.value
            ),
            status="pending_kyc" if kyc_required else "active",
        )
        session.add(wallet)
        # Flush before ledger insert: with autoflush=False the UOW can
        # INSERT ledger_entries before wallets, violating
        # ledger_entries_wallet_id_fkey on Postgres.
        await session.flush()

        if initial_credits > Decimal("0"):
            entry = LedgerEntryModel(
                entry_id=str(uuid.uuid4()),
                wallet_id=wallet_id,
                action=LedgerAction.CREDIT.value,
                amount=initial_credits,
                balance_after=initial_credits,
                description="Initial sponsor deposit",
            )
            session.add(entry)

        return wallet_model_to_response(wallet)

    async def create_agent_wallet(
        self,
        sponsor_wallet_id: str,
        agent_id: str,
        budget_credits: Decimal,
        daily_limit: Decimal | None = None,
        auto_refill: bool = False,
        auto_refill_threshold: Decimal = Decimal("100.0"),
        auto_refill_amount: Decimal = Decimal("1000.0"),
        session: AsyncSession | None = None,
    ) -> WalletResponse:
        """Provision a pre-paid agent wallet from a sponsor's balance.

        ``session``, when given, must already be inside a transaction the
        CALLER owns end to end — same contract as ``create_sponsor_wallet``.
        Omit it for the original standalone behavior.
        """
        if budget_credits < Decimal("0"):
            # A negative budget inverts the debit into a credit: the guard
            # ``balance >= budget_credits`` is trivially true for a negative
            # amount, so the sponsor would be paid and the new wallet opened
            # holding a negative balance.
            #
            # Zero is deliberately allowed. It debits nothing and opens an
            # empty wallet to be topped up later, which is what
            # SelfProvisionRequest.budget_credits (ge=0) offers callers.
            raise ValueError("budget_credits cannot be negative")

        if session is not None:
            return await self._create_agent_wallet_in(
                session,
                sponsor_wallet_id,
                agent_id,
                budget_credits,
                daily_limit,
                auto_refill,
                auto_refill_threshold,
                auto_refill_amount,
            )

        async with self._session_factory()() as session:
            async with session.begin():
                result = await self._create_agent_wallet_in(
                    session,
                    sponsor_wallet_id,
                    agent_id,
                    budget_credits,
                    daily_limit,
                    auto_refill,
                    auto_refill_threshold,
                    auto_refill_amount,
                )
            await session.commit()
            logger.info(
                f"Created agent wallet {result.wallet_id} with {budget_credits} credits"
            )
            return result

    async def _create_agent_wallet_in(
        self,
        session: AsyncSession,
        sponsor_wallet_id: str,
        agent_id: str,
        budget_credits: Decimal,
        daily_limit: Decimal | None,
        auto_refill: bool,
        auto_refill_threshold: Decimal,
        auto_refill_amount: Decimal,
    ) -> WalletResponse:
        """Core agent-provisioning logic against an already-open session.

        No BEGIN/COMMIT here — the caller's transaction owns those.
        """
        # Lock sponsor wallet for update
        result = await session.execute(
            select(WalletModel)
            .where(
                cast(
                    ColumnElement[bool],
                    WalletModel.wallet_id == sponsor_wallet_id,
                )
            )
            .with_for_update()
        )
        sponsor = result.scalar_one_or_none()

        if not sponsor:
            raise self._wallet_not_found_error(sponsor_wallet_id)
        if sponsor.wallet_type != WalletType.SPONSOR.value:
            raise ValueError("Can only provision agent wallets from sponsor wallets")
        if sponsor.status not in SPENDABLE_WALLET_STATUSES:
            raise ValueError(
                f"Sponsor wallet is {sponsor.status} and cannot provision agent wallets"
            )
        # Deduct from sponsor. The balance and status checks above are
        # a fast path; the guarded UPDATE is what makes them hold, so
        # two concurrent provisions cannot hand out the same credits
        # and a freeze landing in between is not outrun.
        if not await self._apply_balance_delta(
            session,
            sponsor,
            balance_delta=-budget_credits,
            lifetime_debits_delta=budget_credits,
            require_balance=budget_credits,
            require_spendable=True,
        ):
            if sponsor.status not in SPENDABLE_WALLET_STATUSES:
                raise ValueError(
                    f"Sponsor wallet is {sponsor.status} and cannot "
                    "provision agent wallets"
                )
            raise self._insufficient_funds_error(
                sponsor_wallet_id,
                sponsor.balance,
                budget_credits,
            )

        # Create agent wallet
        agent_wallet_id = f"agt-{uuid.uuid4().hex[:12]}"
        agent_wallet = WalletModel(
            wallet_id=agent_wallet_id,
            wallet_type=WalletType.AGENT.value,
            owner_name=f"Agent: {agent_id}",
            balance=budget_credits,
            lifetime_credits=budget_credits,
            parent_wallet_id=sponsor_wallet_id,
            agent_id=agent_id,
            daily_limit=daily_limit,
            auto_refill=auto_refill,
            auto_refill_threshold=auto_refill_threshold,
            auto_refill_amount=auto_refill_amount,
        )
        session.add(agent_wallet)
        # Persist new wallet before ledger rows that FK to it.
        await session.flush()

        # Ledger entries
        session.add(
            LedgerEntryModel(
                entry_id=str(uuid.uuid4()),
                wallet_id=sponsor_wallet_id,
                action=LedgerAction.TRANSFER.value,
                amount=-budget_credits,
                balance_after=sponsor.balance,
                description=(
                    f"Provision agent wallet {agent_wallet_id} for {agent_id}"
                ),
            )
        )
        session.add(
            LedgerEntryModel(
                entry_id=str(uuid.uuid4()),
                wallet_id=agent_wallet_id,
                action=LedgerAction.TRANSFER.value,
                amount=budget_credits,
                balance_after=budget_credits,
                description=f"Provisioned from sponsor {sponsor_wallet_id}",
            )
        )

        return wallet_model_to_response(agent_wallet)

    # --- Child Wallet Management (Swarm Delegation) ---

    async def create_child_wallet(
        self,
        parent_wallet_id: str,
        child_agent_id: str,
        budget_credits: Decimal,
        max_spend: Decimal,
        task_description: str = "",
        ttl_seconds: int | None = None,
        auto_reclaim: bool = True,
    ) -> WalletResponse:
        """Spawn a child sub-agent wallet from a parent agent's balance."""
        _validate_child_wallet_ttl(ttl_seconds)
        if budget_credits < Decimal("0"):
            # Same inversion as the sponsor path, and worse: a child opened
            # with a negative balance cannot be unwound by a later reclaim,
            # which only credits the parent back a positive reclaim_amount.
            # Zero is allowed here too, for the same reason.
            raise ValueError("budget_credits cannot be negative")
        async with self._session_factory()() as session:
            async with session.begin():
                # Lock parent wallet
                result = await session.execute(
                    select(WalletModel)
                    .where(
                        cast(
                            ColumnElement[bool],
                            WalletModel.wallet_id == parent_wallet_id,
                        )
                    )
                    .with_for_update()
                )
                parent = result.scalar_one_or_none()

                if not parent:
                    raise self._wallet_not_found_error(parent_wallet_id)
                if parent.wallet_type not in (
                    WalletType.AGENT.value,
                    WalletType.CHILD.value,
                ):
                    raise ValueError(
                        "Only agent or child wallets can spawn child wallets"
                    )
                if parent.status not in SPENDABLE_WALLET_STATUSES:
                    raise ValueError(
                        f"Parent wallet is {parent.status} and cannot spawn child wallets"
                    )
                now = utc_now()
                parent_expires_at = await self.ensure_wallet_not_expired(
                    session,
                    parent,
                    now=now,
                )
                if (
                    parent.wallet_type == WalletType.CHILD.value
                    and parent.max_spend is not None
                    and parent.lifetime_debits + budget_credits > parent.max_spend
                ):
                    raise ValueError("Child wallet lifetime spend cap exceeded")

                # Deduct from parent. Balance, spendable status, and the child
                # lifetime cap all travel with the debit: two concurrent
                # delegations must not both clear a limit only one of them
                # fits inside.
                if not await self._apply_balance_delta(
                    session,
                    parent,
                    balance_delta=-budget_credits,
                    lifetime_debits_delta=budget_credits,
                    require_balance=budget_credits,
                    require_spendable=True,
                    require_child_cap_for=budget_credits,
                ):
                    if parent.status not in SPENDABLE_WALLET_STATUSES:
                        raise ValueError(
                            f"Parent wallet is {parent.status} and cannot spawn "
                            "child wallets"
                        )
                    if (
                        parent.wallet_type == WalletType.CHILD.value
                        and parent.max_spend is not None
                        and parent.lifetime_debits + budget_credits > parent.max_spend
                    ):
                        raise ValueError("Child wallet lifetime spend cap exceeded")
                    raise self._insufficient_funds_error(
                        parent_wallet_id,
                        parent.balance,
                        budget_credits,
                    )

                # Create child wallet
                child_wallet_id = f"chd-{uuid.uuid4().hex[:12]}"
                effective_ttl_seconds = ttl_seconds
                if parent_expires_at is not None:
                    remaining_seconds = int((parent_expires_at - now).total_seconds())
                    if remaining_seconds <= 0:
                        raise WalletExpiredError(parent.wallet_id, parent_expires_at)
                    if (
                        effective_ttl_seconds is None
                        or effective_ttl_seconds > remaining_seconds
                    ):
                        effective_ttl_seconds = remaining_seconds
                child = WalletModel(
                    wallet_id=child_wallet_id,
                    wallet_type=WalletType.CHILD.value,
                    owner_name=f"Child: {child_agent_id}",
                    balance=budget_credits,
                    lifetime_credits=budget_credits,
                    parent_wallet_id=parent_wallet_id,
                    child_agent_id=child_agent_id,
                    max_spend=max_spend,
                    task_description=task_description,
                    ttl_seconds=effective_ttl_seconds,
                    created_at=now,
                    updated_at=now,
                )
                session.add(child)
                # Persist new wallet before ledger rows that FK to it.
                await session.flush()

                # Ledger entries
                session.add(
                    LedgerEntryModel(
                        entry_id=str(uuid.uuid4()),
                        wallet_id=parent_wallet_id,
                        action=LedgerAction.TRANSFER.value,
                        amount=-budget_credits,
                        balance_after=parent.balance,
                        description=(
                            f"Delegate to child wallet {child_wallet_id} "
                            f"({child_agent_id})"
                        ),
                    )
                )
                session.add(
                    LedgerEntryModel(
                        entry_id=str(uuid.uuid4()),
                        wallet_id=child_wallet_id,
                        action=LedgerAction.TRANSFER.value,
                        amount=budget_credits,
                        balance_after=budget_credits,
                        description=f"Provisioned from parent {parent_wallet_id}",
                    )
                )

            await session.commit()
            logger.info(f"Spawned child wallet {child_wallet_id} for {child_agent_id}")
            return wallet_model_to_response(child)

    async def reclaim_child_wallet(self, child_wallet_id: str) -> dict:
        """Reclaim unspent credits from a child wallet back to its parent."""
        async with self._session_factory()() as session:
            async with session.begin():
                # Discover the (immutable) parent id with an unlocked read, so
                # both rows can then be locked in the same globally consistent
                # sorted order that transfer() uses -- otherwise reclaim would
                # lock child-then-parent while a concurrent transfer locks the
                # sorted order, and the two could deadlock.
                child_peek = (
                    await session.execute(
                        select(WalletModel).where(
                            cast(
                                ColumnElement[bool],
                                WalletModel.wallet_id == child_wallet_id,
                            )
                        )
                    )
                ).scalar_one_or_none()
                if not child_peek:
                    raise self._wallet_not_found_error(child_wallet_id)
                if not child_peek.parent_wallet_id:
                    raise ValueError("Child wallet has no parent")
                parent_wallet_id = child_peek.parent_wallet_id

                locked = await self._lock_wallets_in_order(
                    session, [child_wallet_id, parent_wallet_id]
                )
                child = locked.get(child_wallet_id)
                parent = locked.get(parent_wallet_id)

                if not child:
                    raise self._wallet_not_found_error(child_wallet_id)
                if child.wallet_type != WalletType.CHILD.value:
                    raise ValueError("Can only reclaim from child wallets")
                if child.status == WalletStatus.CLOSED.value:
                    raise ValueError("Wallet already closed")
                if not parent:
                    raise self._wallet_not_found_error(parent_wallet_id)

                reclaim_amount = child.balance

                # Claim the child's balance with a guarded UPDATE before any of
                # it reaches the parent. Reading ``child.balance`` and then
                # writing both wallets is a read-modify-write serialized only by
                # the row locks taken above, and ``SELECT ... FOR UPDATE`` is a
                # silent no-op on SQLite: two concurrent reclaims of the same
                # child both read the full balance, both zero it, and both
                # credit the parent -- minting credits that never existed. The
                # database now admits exactly one reclaim, because only one
                # transaction can move this row off the balance it was observed
                # holding while it is still open.
                claimed = await session.execute(
                    sa_update(WalletModel)
                    .where(
                        cast(
                            ColumnElement[bool],
                            WalletModel.wallet_id == child_wallet_id,
                        ),
                        cast(
                            ColumnElement[bool],
                            WalletModel.status != WalletStatus.CLOSED.value,
                        ),
                        cast(
                            ColumnElement[bool],
                            WalletModel.balance == reclaim_amount,
                        ),
                    )
                    .values(
                        balance=Decimal("0"),
                        status=WalletStatus.CLOSED.value,
                        updated_at=utc_now(),
                    )
                    .execution_options(synchronize_session=False)
                )
                if (cast(Any, claimed).rowcount or 0) != 1:
                    # Someone else reclaimed this child, or its balance moved
                    # under us. Either way no credits may be created here.
                    await session.refresh(child)
                    if child.status == WalletStatus.CLOSED.value:
                        raise ValueError("Wallet already closed")
                    raise ValueError("Child wallet balance changed during reclaim")

                if reclaim_amount > Decimal("0"):
                    # Credit the parent relatively, so a concurrent credit to
                    # the same parent is not lost.
                    await session.execute(
                        sa_update(WalletModel)
                        .where(
                            cast(
                                ColumnElement[bool],
                                WalletModel.wallet_id == parent_wallet_id,
                            )
                        )
                        .values(
                            balance=WalletModel.balance + reclaim_amount,
                            lifetime_credits=WalletModel.lifetime_credits
                            + reclaim_amount,
                            updated_at=utc_now(),
                        )
                        .execution_options(synchronize_session=False)
                    )
                    # The parent's ledger entry records the balance this credit
                    # produced, so it must be read after the write.
                    await session.refresh(parent)

                    # Ledger entries
                    session.add(
                        LedgerEntryModel(
                            entry_id=str(uuid.uuid4()),
                            wallet_id=child_wallet_id,
                            action=LedgerAction.TRANSFER.value,
                            amount=-reclaim_amount,
                            balance_after=Decimal("0"),
                            description=f"Reclaimed to parent {parent.wallet_id}",
                        )
                    )
                    session.add(
                        LedgerEntryModel(
                            entry_id=str(uuid.uuid4()),
                            wallet_id=parent.wallet_id,
                            action=LedgerAction.TRANSFER.value,
                            amount=reclaim_amount,
                            balance_after=parent.balance,
                            description=f"Reclaimed from child {child_wallet_id}",
                        )
                    )

                # The guarded claim above already closed the child and zeroed
                # its balance; re-reading keeps the returned view honest.
                await session.refresh(child)

            await session.commit()

            return {
                "child_wallet_id": child_wallet_id,
                "parent_wallet_id": parent.wallet_id,
                "credits_reclaimed": reclaim_amount,
                "parent_balance_after": parent.balance,
                "child_status": WalletStatus(child.status),
            }

    # --- Agent-to-Agent Transfers ---

    async def transfer(
        self,
        from_wallet_id: str,
        to_wallet_id: str,
        amount: Decimal,
        description: str = "",
        correlation_id: str | None = None,
    ) -> dict:
        """
        Transfer credits between two wallets (agent-to-agent handoff).

        This enables agents to pay each other for completed tasks.
        Both wallets are locked during the transaction for ACID compliance.

        Args:
            from_wallet_id: Source wallet (must have sufficient balance)
            to_wallet_id: Destination wallet
            amount: Credits to transfer (must be > 0)
            description: Optional description of the transfer
            correlation_id: Optional ID to link related transfers

        Returns:
            Transfer receipt with both wallet balances

        Raises:
            WalletNotFoundError: If either wallet doesn't exist
            InsufficientFundsError: If source has insufficient balance
            ValueError: If amount <= 0 or same wallet IDs
        """
        if amount <= Decimal("0"):
            raise ValueError("Transfer amount must be positive")

        if from_wallet_id == to_wallet_id:
            raise ValueError("Cannot transfer to the same wallet")

        async with self._session_factory()() as session:
            async with session.begin():
                # Lock both wallets in a globally consistent (sorted) order via
                # one FOR UPDATE per row, so a concurrent transfer/reclaim on
                # the same pair cannot deadlock.
                wallets = await self._lock_wallets_in_order(
                    session, [from_wallet_id, to_wallet_id]
                )

                source = wallets.get(from_wallet_id)
                dest = wallets.get(to_wallet_id)

                if not source:
                    raise self._wallet_not_found_error(from_wallet_id)
                if not dest:
                    raise self._wallet_not_found_error(to_wallet_id)

                if source.status not in SPENDABLE_WALLET_STATUSES:
                    raise ValueError(
                        f"Source wallet is {source.status} and cannot transfer credits"
                    )
                await self.ensure_wallet_not_expired(session, source)
                if (
                    source.wallet_type == WalletType.CHILD.value
                    and source.max_spend is not None
                    and source.lifetime_debits + amount > source.max_spend
                ):
                    raise ValueError("Child wallet lifetime spend cap exceeded")

                # Execute transfer. The debit is guarded on everything a
                # concurrent writer could move, so the credit below only ever
                # pays out credits this statement actually took from the
                # source -- a lost update here would mint credits, landing
                # them on the destination without removing them anywhere.
                if not await self._apply_balance_delta(
                    session,
                    source,
                    balance_delta=-amount,
                    lifetime_debits_delta=amount,
                    require_balance=amount,
                    require_spendable=True,
                    require_child_cap_for=amount,
                ):
                    if source.status not in SPENDABLE_WALLET_STATUSES:
                        raise ValueError(
                            f"Source wallet is {source.status} and cannot "
                            "transfer credits"
                        )
                    if (
                        source.wallet_type == WalletType.CHILD.value
                        and source.max_spend is not None
                        and source.lifetime_debits + amount > source.max_spend
                    ):
                        raise ValueError("Child wallet lifetime spend cap exceeded")
                    raise self._insufficient_funds_error(
                        from_wallet_id, source.balance, amount
                    )
                # The debit is already applied in this transaction, so a credit
                # that matches no row would leave the transfer reporting success
                # with the credits on neither side. Only ``wallet_id`` guards
                # this write, so a miss means the destination row is gone --
                # abort rather than commit half a transfer.
                if not await self._apply_balance_delta(
                    session,
                    dest,
                    balance_delta=amount,
                    lifetime_credits_delta=amount,
                ):
                    raise self._wallet_not_found_error(to_wallet_id)

                # Create ledger entries on both sides
                session.add(
                    LedgerEntryModel(
                        entry_id=str(uuid.uuid4()),
                        wallet_id=from_wallet_id,
                        action=LedgerAction.TRANSFER.value,
                        amount=-amount,
                        balance_after=source.balance,
                        description=description or f"Transfer to {to_wallet_id}",
                        correlation_id=correlation_id,
                    )
                )
                session.add(
                    LedgerEntryModel(
                        entry_id=str(uuid.uuid4()),
                        wallet_id=to_wallet_id,
                        action=LedgerAction.TRANSFER.value,
                        amount=amount,
                        balance_after=dest.balance,
                        description=description or f"Transfer from {from_wallet_id}",
                        correlation_id=correlation_id,
                    )
                )

            await session.commit()

            logger.info(
                f"Transfer {amount} credits from {from_wallet_id} to {to_wallet_id}"
            )

            return {
                "transfer_id": correlation_id or str(uuid.uuid4()),
                "from_wallet_id": from_wallet_id,
                "to_wallet_id": to_wallet_id,
                "amount": amount,
                "from_balance_after": source.balance,
                "to_balance_after": dest.balance,
                "status": "completed",
            }

    async def get_swarm_budget(self, parent_wallet_id: str) -> dict:
        """Get hierarchical budget summary for an agent's child swarm."""
        async with self._session_factory()() as session:
            # Get parent
            parent_result = await session.execute(
                select(WalletModel).where(
                    cast(ColumnElement[bool], WalletModel.wallet_id == parent_wallet_id)
                )
            )
            parent = parent_result.scalar_one_or_none()

            if not parent:
                raise self._wallet_not_found_error(parent_wallet_id)

            # Get children
            children_result = await session.execute(
                select(WalletModel).where(
                    cast(
                        ColumnElement[bool],
                        WalletModel.parent_wallet_id == parent_wallet_id,
                    )
                )
            )
            children = list(children_result.scalars().all())

            total_delegated = sum(w.lifetime_credits for w in children)
            total_reclaimed = sum(
                w.lifetime_credits - w.balance - w.lifetime_debits
                for w in children
                if w.status == WalletStatus.CLOSED.value
            )
            active = [w for w in children if w.status == WalletStatus.ACTIVE.value]
            completed = [w for w in children if w.status == WalletStatus.CLOSED.value]
            frozen = [w for w in children if w.status == WalletStatus.FROZEN.value]

            return {
                "parent_wallet_id": parent_wallet_id,
                "parent_balance": parent.balance,
                "total_delegated": total_delegated,
                "total_reclaimed": total_reclaimed,
                "active_children": len(active),
                "completed_children": len(completed),
                "frozen_children": len(frozen),
                "children": [wallet_model_to_response(w) for w in children],
            }

    # --- Wallet Queries ---

    async def get_wallet(self, wallet_id: str) -> WalletResponse | None:
        """Get a wallet by ID."""
        async with self._session_factory()() as session:
            result = await session.execute(
                select(WalletModel).where(
                    cast(ColumnElement[bool], WalletModel.wallet_id == wallet_id)
                )
            )
            wallet = result.scalar_one_or_none()
            if wallet:
                return wallet_model_to_response(wallet)
            return None

    async def get_daily_spend(self, wallet_id: str) -> Decimal:
        """Return the wallet's spend in the current daily window.

        Read-only and reset-aware: if the daily window has rolled over since the
        last reset (mirroring the velocity monitor's rule), the effective spend
        is 0 even though it hasn't been persisted yet. Returns 0 for unknown
        wallets so callers can treat it as "no spend recorded".
        """
        async with self._session_factory()() as session:
            result = await session.execute(
                select(WalletModel).where(
                    cast(ColumnElement[bool], WalletModel.wallet_id == wallet_id)
                )
            )
            wallet = result.scalar_one_or_none()
            if not wallet:
                return Decimal("0")
            reset_at = wallet.daily_reset_at
            if reset_at is None:
                return Decimal("0")
            if reset_at.tzinfo is None:
                reset_at = reset_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - reset_at >= timedelta(days=1):
                return Decimal("0")
            return wallet.daily_spent

    async def list_wallets(
        self,
        wallet_type: str | None = None,
    ) -> list[WalletResponse]:
        """List wallets with optional filtering."""
        async with self._session_factory()() as session:
            query = select(WalletModel)
            if wallet_type:
                enum_val = WalletType(wallet_type).value if wallet_type else None
                if enum_val:
                    query = query.where(
                        cast(ColumnElement[bool], WalletModel.wallet_type == enum_val)
                    )
            result = await session.execute(query)
            wallets = list(result.scalars().all())
            return [wallet_model_to_response(w) for w in wallets]
