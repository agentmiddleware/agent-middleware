"""Billing, metering, ledger, and reporting engine for AgentMoney.

The HTTP and Python compatibility surface remains app.services.agent_money.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import or_, select, update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from ..core.config import Settings, get_settings
from ..core.resilience import run_with_write_conflict_retry
from ..core.time import utc_now
from ..db.converters import billing_alert_model_to_schema, ledger_entry_model_to_schema
from ..db.sql_expressions import clamped_decrement
from ..db.models import (
    BillingAlertModel,
    IdempotencyRecordModel,
    LedgerEntryModel,
    McpDispatchAttemptModel,
    ServiceRegistryModel,
    WalletModel,
)
from ..schemas.billing import (
    AlertSeverity,
    AlertType,
    ArbitrageReport,
    BillingAlert,
    InsufficientFundsResponse,
    LedgerAction,
    LedgerEntry,
    ServiceCategory,
    ServicePricing,
    ServiceRegistration,
    TopUpResponse,
    WalletStatus,
    WalletType,
)
from .pricing import PROOF_SURFACE_CATEGORIES
from .shadow_ledger import SimulatedChargeResult, get_shadow_ledger
from .velocity_monitor import get_velocity_monitor
from .wallet_engine import (
    SessionFactoryProvider,
    WalletEngine,
    WalletExpiredError,
    WalletNotFoundFactory,
)
from .wallet_status import SPENDABLE_WALLET_STATUSES

KYCRequiredFactory = Callable[[str, str], Exception]

logger = logging.getLogger(__name__)


class DirectTopUpDisabledError(RuntimeError):
    """Raised when code attempts to mint credits without provider settlement."""


class LedgerWriteContendedError(RuntimeError):
    """The governed debit transaction lost repeated SQLite write conflicts.

    Nothing was charged and nothing was dispatched: every attempt rolled back
    whole. The caller may retry the same idempotency key, which is why this is
    distinct from an unclassified failure -- a client that cannot tell the two
    apart has to assume its money may have moved.
    """

    reason = "ledger_write_contended"


class LedgerOperationConflictError(RuntimeError):
    """Raised when one governed operation key describes different debits."""


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp_in_current_period(
    timestamp: datetime,
    period_start: datetime | None,
) -> bool:
    if period_start is None:
        return True
    return _as_utc(timestamp) >= _as_utc(period_start)


async def _reverse_velocity_counters(
    session: Any,
    *,
    wallet_id: str,
    amount: Decimal,
    hourly_reset_at: datetime | None,
    daily_reset_at: datetime | None,
) -> None:
    """Undo one velocity increment that the monitor already committed.

    The velocity monitor records a charge in its own transaction, before the
    billing transaction has taken the wallet lock. Any path that then declines
    or loses that charge owes this reversal, and owes it in a session that is
    still alive -- a reversal issued on the transaction that just rolled back
    goes nowhere.

    Relative, in one statement, for the same reason the increment is: reading a
    counter and writing back read-charge is a read-modify-write, and these
    counters are what the spend cap and the anomaly auto-freeze are measured
    against. A lost reversal leaves the wallet permanently over-counted, which
    throttles -- and eventually freezes -- a caller that never spent the money.

    Each counter is reversed under its own period marker, because the two roll
    over on independent schedules. If a period rolled between the increment and
    this reversal, the counter this charge added to has already been zeroed and
    the current one belongs to a period the charge never touched; decrementing
    it would under-count live spend, which is the worse direction: an
    over-count throttles a caller who did not spend, an under-count silently
    raises the cap and delays the auto-freeze. A predicate covering both
    markers at once would be wrong for the same reason -- a rolled daily period
    would suppress an hourly reversal that is still owed.
    """

    reversals = (
        (
            "hourly_spent",
            WalletModel.hourly_reset_at,
            hourly_reset_at,
            WalletModel.hourly_spent,
        ),
        (
            "daily_spent",
            WalletModel.daily_reset_at,
            daily_reset_at,
            WalletModel.daily_spent,
        ),
    )
    for column_name, marker_column, recorded_marker, counter in reversals:
        predicates: list[Any] = [
            cast(ColumnElement[bool], WalletModel.wallet_id == wallet_id)
        ]
        if recorded_marker is not None:
            predicates.append(
                cast(ColumnElement[bool], marker_column == recorded_marker)
            )
        await session.execute(
            sa_update(WalletModel)
            .where(*predicates)
            .values(**{column_name: clamped_decrement(counter, amount)})
            .execution_options(synchronize_session=False)
        )


class BillingEngine:
    """Internal billing and ledger implementation behind AgentMoney."""

    def __init__(
        self,
        *,
        session_factory: SessionFactoryProvider,
        settings: Settings,
        wallet_engine: WalletEngine,
        default_pricing: dict[ServiceCategory, tuple[str, Decimal, str]],
        compute_costs: dict[ServiceCategory, Decimal],
        wallet_not_found_error: WalletNotFoundFactory,
        kyc_required_error: KYCRequiredFactory,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._wallet_engine = wallet_engine
        self._default_pricing = default_pricing
        self._compute_costs = compute_costs
        self._wallet_not_found_error = wallet_not_found_error
        self._kyc_required_error = kyc_required_error

    @staticmethod
    async def _get_operation_debit(
        session: AsyncSession,
        *,
        wallet_id: str,
        operation_key: str,
    ) -> LedgerEntryModel | None:
        return (
            await session.execute(
                select(LedgerEntryModel).where(
                    cast(
                        ColumnElement[bool],
                        LedgerEntryModel.wallet_id == wallet_id,
                    ),
                    cast(
                        ColumnElement[bool],
                        LedgerEntryModel.operation_key == operation_key,
                    ),
                )
            )
        ).scalar_one_or_none()

    @staticmethod
    def _assert_operation_debit_matches(
        entry: LedgerEntryModel,
        *,
        wallet_id: str,
        operation_key: str,
        service_category: ServiceCategory,
        charge_amount: Decimal,
        request_path: str | None,
    ) -> None:
        if (
            entry.wallet_id != wallet_id
            or entry.operation_key != operation_key
            or entry.action != LedgerAction.DEBIT.value
            or entry.amount != -charge_amount
            or entry.service_category != service_category.value
            or entry.request_path != request_path
        ):
            raise LedgerOperationConflictError("ledger_operation_key_reused")

    async def _dry_run_charge(
        self,
        wallet_id: str,
        service_category: ServiceCategory,
        units: Decimal,
        charge_amount: Decimal,
        description: str,
        session_id: str | None,
    ) -> SimulatedChargeResult:
        """
        Simulate a charge without affecting real balance or velocity.

        This is a dry-run operation that:
        - Does NOT record to the real ledger
        - Does NOT trigger velocity monitoring
        - Returns cost estimate with virtual balance impact

        If session_id is provided, uses the shadow ledger for stateful tracking.
        If session_id is None, returns a single-shot estimate based on real balance.
        """
        if session_id:
            shadow_ledger = get_shadow_ledger()
            try:
                session = await shadow_ledger.get_session(session_id)
                if session is None:
                    raise ValueError(f"Session not found: {session_id}")
                result = await shadow_ledger.simulate_charge(
                    session_id=session_id,
                    service_category=service_category,
                    units=float(units),
                    description=description,
                    blocked_reason=(
                        "wallet_expired"
                        if await self._wallet_engine.wallet_is_expired(
                            session.wallet_id
                        )
                        else None
                    ),
                )
                return result
            except ValueError as e:
                if "Session not found" in str(e):
                    raise ValueError(f"Dry-run session not found: {session_id}")
                raise

        wallet = await self._wallet_engine.get_wallet(wallet_id)
        if not wallet:
            raise self._wallet_not_found_error(wallet_id)

        # Bug fix: SimulatedChargeResult declares these fields as Decimal for
        # monetary precision. `wallet` here is the WalletResponse schema,
        # whose `balance` is already a display float; use the paired
        # `balance_exact` decimal string (falling back to the float only if
        # it's somehow absent) instead of round-tripping through float twice.
        wallet_balance = (
            Decimal(wallet.balance_exact)
            if wallet.balance_exact is not None
            else Decimal(str(wallet.balance))
        )
        wallet_expired = await self._wallet_engine.wallet_is_expired(wallet_id)
        would_succeed = not wallet_expired and wallet_balance >= charge_amount

        return SimulatedChargeResult(
            session_id=session_id or "",
            wallet_id=wallet_id,
            service_category=service_category.value,
            units=float(units),
            credits_would_charge=charge_amount,
            simulated_balance_before=wallet_balance,
            simulated_balance_after=wallet_balance - charge_amount,
            would_succeed=would_succeed,
            reason=(
                "wallet_expired"
                if wallet_expired
                else (None if would_succeed else "insufficient_simulated_funds")
            ),
            dry_run=True,
        )

    async def _apply_charge_to_locked_wallet(
        self,
        session: AsyncSession,
        *,
        wallet_id: str,
        service_category: ServiceCategory,
        units: Decimal,
        unit_name: str,
        credits_per_unit: Decimal,
        charge_amount: Decimal,
        compute_cost: Decimal,
        margin: Decimal,
        request_path: str | None,
        description: str,
        operation_key: str | None,
        velocity_hourly_reset_at: datetime | None = None,
        velocity_daily_reset_at: datetime | None = None,
    ) -> LedgerEntry | InsufficientFundsResponse:
        result = await session.execute(
            select(WalletModel)
            .where(cast(ColumnElement[bool], WalletModel.wallet_id == wallet_id))
            .with_for_update()
        )
        wallet = result.scalar_one_or_none()

        if not wallet:
            # The velocity monitor returns early without recording when the
            # wallet is absent, so there is nothing to reverse here.
            raise self._wallet_not_found_error(wallet_id)

        async def reverse_velocity_record() -> None:
            # Declined inside this transaction, which is still alive, so the
            # reversal lands here and the refreshed wallet below reflects it.
            await _reverse_velocity_counters(
                session,
                wallet_id=wallet_id,
                amount=charge_amount,
                hourly_reset_at=velocity_hourly_reset_at,
                daily_reset_at=velocity_daily_reset_at,
            )
            await session.refresh(wallet)

        if wallet.status not in SPENDABLE_WALLET_STATUSES:
            await reverse_velocity_record()
            return InsufficientFundsResponse(
                error=(
                    "wallet_frozen"
                    if wallet.status == WalletStatus.FROZEN.value
                    else "insufficient_funds"
                ),
                wallet_id=wallet_id,
                current_balance=float(wallet.balance),
                current_balance_exact=str(wallet.balance),
                required_amount=float(charge_amount),
                required_amount_exact=str(charge_amount),
                shortfall=0.0,
                shortfall_exact="0",
                top_up_url="",
                message=f"Wallet is {wallet.status} and cannot be charged.",
            )

        try:
            await self._wallet_engine.ensure_wallet_not_expired(session, wallet)
        except WalletExpiredError:
            await reverse_velocity_record()
            return InsufficientFundsResponse(
                error="wallet_expired",
                wallet_id=wallet_id,
                current_balance=float(wallet.balance),
                current_balance_exact=str(wallet.balance),
                required_amount=float(charge_amount),
                required_amount_exact=str(charge_amount),
                shortfall=0.0,
                shortfall_exact="0",
                top_up_url="",
                message="Child wallet TTL expired and cannot be charged.",
            )

        if wallet.wallet_type == WalletType.CHILD.value and wallet.max_spend:
            new_debits = wallet.lifetime_debits + charge_amount
            if new_debits > wallet.max_spend:
                remaining = wallet.max_spend - wallet.lifetime_debits
                shortfall = charge_amount - remaining
                await reverse_velocity_record()
                return InsufficientFundsResponse(
                    wallet_id=wallet_id,
                    current_balance=float(wallet.balance),
                    current_balance_exact=str(wallet.balance),
                    required_amount=float(charge_amount),
                    required_amount_exact=str(charge_amount),
                    shortfall=float(shortfall),
                    shortfall_exact=str(shortfall),
                    top_up_url="/v1/billing/top-up/prepare",
                    message="Child wallet lifetime spend cap exceeded.",
                )

        # `is not None`, not truthiness: daily_limit is Optional where None
        # means "no cap", so Decimal("0") is a real limit -- the one an
        # operator sets to stop a runaway agent dead. Reading it as falsy
        # turned the strictest possible cap into no cap at all, and this is
        # the only hard daily-spend enforcement in the system (the velocity
        # monitor only raises alerts). app/db/converters.py already draws the
        # same distinction the same way.
        if wallet.daily_limit is not None and wallet.daily_spent > wallet.daily_limit:
            prior_daily_spent = wallet.daily_spent - charge_amount
            remaining = wallet.daily_limit - prior_daily_spent
            shortfall = charge_amount - remaining
            await reverse_velocity_record()
            return InsufficientFundsResponse(
                wallet_id=wallet_id,
                current_balance=float(wallet.balance),
                current_balance_exact=str(wallet.balance),
                required_amount=float(charge_amount),
                required_amount_exact=str(charge_amount),
                shortfall=float(shortfall),
                shortfall_exact=str(shortfall),
                top_up_url="/v1/billing/top-up/prepare",
                message="Daily spending limit exceeded.",
            )

        if wallet.balance < charge_amount:
            shortfall = charge_amount - wallet.balance
            await reverse_velocity_record()
            return InsufficientFundsResponse(
                wallet_id=wallet_id,
                current_balance=float(wallet.balance),
                current_balance_exact=str(wallet.balance),
                required_amount=float(charge_amount),
                required_amount_exact=str(charge_amount),
                shortfall=float(shortfall),
                shortfall_exact=str(shortfall),
                top_up_url="/v1/billing/top-up/prepare",
            )

        # Apply the debit as a single guarded UPDATE. The balance check above is
        # a fast path for the common case; it is *not* what makes the debit
        # safe, because reading the balance in one statement and writing it in
        # another is a read-modify-write. ``SELECT ... FOR UPDATE`` above is a
        # silent no-op on SQLite, so two concurrent charges both read the same
        # balance, both clear the check, and both decrement -- the wallet is
        # overdrawn by the second charge and the service is delivered twice.
        # Repeating ``balance >= charge_amount`` inside the statement makes the
        # database, not this process, decide whether the funds were there.
        debited = await session.execute(
            sa_update(WalletModel)
            .where(
                cast(ColumnElement[bool], WalletModel.wallet_id == wallet_id),
                cast(ColumnElement[bool], WalletModel.balance >= charge_amount),
                # The delegated child-wallet lifetime cap has to travel with the
                # debit for the same reason the balance does. Checking
                # ``lifetime_debits + charge_amount <= max_spend`` above and
                # incrementing here would let two concurrent charges both clear
                # a cap only one of them fits inside, pushing a delegated wallet
                # past the spend ceiling its parent granted it. Non-child
                # wallets, and children with no cap set, are unconstrained.
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
                            WalletModel.lifetime_debits + charge_amount
                            <= cast(Any, WalletModel.max_spend),
                        ),
                    ),
                ),
                # Spendability travels with the debit too. The status check
                # above reads a value loaded before this statement, so a freeze
                # landing in between would otherwise still be debited -- the
                # freeze exists precisely to stop that.
                cast(
                    ColumnElement[bool],
                    cast(Any, WalletModel.status).in_(tuple(SPENDABLE_WALLET_STATUSES)),
                ),
            )
            .values(
                balance=WalletModel.balance - charge_amount,
                lifetime_debits=WalletModel.lifetime_debits + charge_amount,
                updated_at=utc_now(),
            )
            .execution_options(synchronize_session=False)
        )
        if (cast(Any, debited).rowcount or 0) != 1:
            # Lost the race: a concurrent charge took the funds, or consumed the
            # child's remaining cap, after our checks passed. Nothing was
            # debited, so each case is reported exactly as failing the
            # corresponding check above would have reported it.
            await session.refresh(wallet)
            await reverse_velocity_record()
            if wallet.status not in SPENDABLE_WALLET_STATUSES:
                # A non-spendable status landed between the read and the write.
                # Report a real freeze specifically; every other control stays
                # a fail-closed refusal rather than looking like empty balance.
                return InsufficientFundsResponse(
                    error=(
                        "wallet_frozen"
                        if wallet.status == WalletStatus.FROZEN.value
                        else "insufficient_funds"
                    ),
                    wallet_id=wallet_id,
                    current_balance=float(wallet.balance),
                    current_balance_exact=str(wallet.balance),
                    required_amount=float(charge_amount),
                    required_amount_exact=str(charge_amount),
                    # A freeze blocks spending regardless of balance, so this
                    # is not a balance deficit -- matching the non-race frozen
                    # path above, which also reports zero.
                    shortfall=0.0,
                    shortfall_exact="0",
                    top_up_url="",
                    message=f"Wallet is {wallet.status} and cannot be charged.",
                )
            max_spend = wallet.max_spend
            if (
                wallet.wallet_type == WalletType.CHILD.value
                and max_spend is not None
                and wallet.lifetime_debits + charge_amount > max_spend
            ):
                remaining = max_spend - wallet.lifetime_debits
                return InsufficientFundsResponse(
                    wallet_id=wallet_id,
                    current_balance=float(wallet.balance),
                    current_balance_exact=str(wallet.balance),
                    required_amount=float(charge_amount),
                    required_amount_exact=str(charge_amount),
                    shortfall=float(charge_amount - remaining),
                    shortfall_exact=str(charge_amount - remaining),
                    top_up_url="/v1/billing/top-up/prepare",
                    message="Child wallet lifetime spend cap exceeded.",
                )
            shortfall = charge_amount - wallet.balance
            return InsufficientFundsResponse(
                wallet_id=wallet_id,
                current_balance=float(wallet.balance),
                current_balance_exact=str(wallet.balance),
                required_amount=float(charge_amount),
                required_amount_exact=str(charge_amount),
                shortfall=float(shortfall),
                shortfall_exact=str(shortfall),
                top_up_url="/v1/billing/top-up/prepare",
            )
        # The guarded UPDATE bypassed the identity map, so the in-memory wallet
        # still holds the pre-debit balance. Re-read it before anything derives
        # a number from it -- the ledger's ``balance_after`` in particular has
        # to be the balance this debit actually produced.
        await session.refresh(wallet)

        entry = LedgerEntryModel(
            entry_id=str(uuid.uuid4()),
            wallet_id=wallet_id,
            action=LedgerAction.DEBIT.value,
            amount=-charge_amount,
            balance_after=wallet.balance,
            service_category=service_category.value,
            description=(
                description or f"{units} × {unit_name} @ {credits_per_unit} credits"
            ),
            request_path=request_path,
            compute_cost=compute_cost,
            margin=margin,
            operation_key=operation_key,
        )
        session.add(entry)

        # Same falsy-zero trap as the daily cap. auto_refill_threshold defaults
        # to 100 and is non-Optional, so `or` could only ever fire on exactly
        # Decimal("0") -- the value a wallet sets to mean "never alert, never
        # refill" -- and silently restored the 100 it opted out of, alerting on
        # every charge below that.
        low_balance_threshold = (
            wallet.auto_refill_threshold
            if wallet.auto_refill_threshold is not None
            else Decimal("100")
        )
        if wallet.balance < low_balance_threshold:
            session.add(
                BillingAlertModel(
                    alert_id=str(uuid.uuid4())[:12],
                    wallet_id=wallet_id,
                    alert_type=AlertType.LOW_BALANCE.value,
                    current_balance=wallet.balance,
                    message=(
                        f"Wallet balance low: {wallet.balance} credits remaining."
                    ),
                    severity=AlertSeverity.WARNING.value,
                )
            )

        return ledger_entry_model_to_schema(entry)

    # --- Micro-Metering & Charging ---

    async def charge(
        self,
        wallet_id: str,
        service_category: ServiceCategory,
        units: Decimal = Decimal("1"),
        request_path: str | None = None,
        description: str = "",
        dry_run: bool = False,
        dry_run_session_id: str | None = None,
        operation_key: str | None = None,
    ) -> LedgerEntry | InsufficientFundsResponse | SimulatedChargeResult:
        """
        Charge an agent wallet for API usage with ACID transaction.

        Uses SELECT ... FOR UPDATE to lock the wallet row during the transaction.
        Also checks spend velocity and auto-freezes on anomalous patterns.

        If dry_run=True, simulates the charge without affecting real balance
        or triggering velocity monitoring. Uses the shadow ledger for tracking.
        """
        # Non-finite units are refused here, not only at the HTTP boundary.
        # This is the choke point every caller passes through -- the governed
        # MCP path and the SDK reach it without going near the billing router
        # -- and ``Decimal("Infinity")`` survives every arithmetic step below
        # until ``charge_amount - compute_cost`` raises InvalidOperation. A
        # non-finite amount that instead reached a write would sit in the
        # ledger permanently: no reconciliation can subtract infinity back
        # out, and every balance derived from it is NaN thereafter.
        if not units.is_finite():
            raise ValueError(f"units must be a finite number, got {units}")
        if units <= 0:
            # Same reasoning, sharper consequence. A negative ``units`` makes
            # ``charge_amount`` negative, and the guarded debit below then
            # reads ``balance >= charge_amount`` as trivially true and applies
            # ``balance - charge_amount`` -- which *raises* the balance. The
            # result is credits minted from nothing, recorded as a DEBIT
            # ledger entry with a positive amount, so the audit trail agrees
            # it was a charge. Zero writes a meaningless zero-value debit.
            # The router refuses both through ``gt=0``; the governed MCP path
            # and the SDK never pass through the router.
            raise ValueError(f"units must be greater than zero, got {units}")

        pricing = self._default_pricing.get(service_category)
        if not pricing:
            raise ValueError(f"No pricing for {service_category.value}")

        unit_name, credits_per_unit, _ = pricing
        charge_amount = units * credits_per_unit

        if dry_run:
            return await self._dry_run_charge(
                wallet_id=wallet_id,
                service_category=service_category,
                units=units,
                charge_amount=charge_amount,
                description=description or f"{units} × {unit_name}",
                session_id=dry_run_session_id,
            )

        compute_cost = units * self._compute_costs.get(service_category, Decimal("0"))
        margin = charge_amount - compute_cost

        if operation_key is not None:
            if (
                not operation_key
                or operation_key != operation_key.strip()
                or len(operation_key) > 128
            ):
                raise ValueError("invalid_ledger_operation_key")

        velocity_monitor = get_velocity_monitor()

        async def _attempt() -> (
            LedgerEntry | InsufficientFundsResponse | SimulatedChargeResult
        ):
            return await self._charge_once(
                wallet_id=wallet_id,
                service_category=service_category,
                units=units,
                request_path=request_path,
                description=description,
                operation_key=operation_key,
                charge_amount=charge_amount,
                compute_cost=compute_cost,
                margin=margin,
                unit_name=unit_name,
                credits_per_unit=credits_per_unit,
                velocity_monitor=velocity_monitor,
            )

        if operation_key is None:
            # Standalone velocity commits in its OWN transaction before this
            # one does, and is compensated only on the way out. Restarting
            # that path would double-count the spend cap and the auto-freeze
            # counter, so a conflict here propagates as it always has.
            return await _attempt()

        # Governed: velocity shares this transaction and rolls back with it, the
        # operation key dedupes the debit, and the unique (wallet, key) index
        # makes a second debit structurally impossible -- so restarting the
        # whole transaction is safe, and it is the only cure SQLite offers for
        # a snapshot that went stale under a concurrent writer.
        try:
            return await run_with_write_conflict_retry(
                _attempt,
                on_exhausted=lambda exc: LedgerWriteContendedError(
                    "ledger_write_contended"
                ),
            )
        except LedgerWriteContendedError:
            # One last read before answering "nothing moved". If the final
            # attempt's COMMIT landed and only its acknowledgement was lost,
            # the durable debit exists and this call did succeed; reporting a
            # contention failure there would be the one way this retry could
            # produce a double charge on the caller's retry. The operation key
            # makes that question answerable, so ask it rather than assume.
            adopted = await self._adopt_operation_debit_if_committed(
                wallet_id=wallet_id,
                operation_key=operation_key,
                service_category=service_category,
                charge_amount=charge_amount,
                request_path=request_path,
            )
            if adopted is not None:
                return adopted
            raise

    async def _adopt_operation_debit_if_committed(
        self,
        *,
        wallet_id: str,
        operation_key: str,
        service_category: ServiceCategory,
        charge_amount: Decimal,
        request_path: str | None,
    ) -> LedgerEntry | None:
        """Return the durable debit for this operation key, if one committed.

        Read-only. ``None`` means no debit exists, so the caller may truthfully
        report that nothing moved. A debit that exists but does not match this
        charge's invariants raises rather than being adopted -- silently
        returning someone else's ledger entry would be worse than the failure
        it was covering for.
        """
        async with self._session_factory()() as session:
            existing = await self._get_operation_debit(
                session,
                wallet_id=wallet_id,
                operation_key=operation_key,
            )
            if existing is None:
                return None
            self._assert_operation_debit_matches(
                existing,
                wallet_id=wallet_id,
                operation_key=operation_key,
                service_category=service_category,
                charge_amount=charge_amount,
                request_path=request_path,
            )
            return ledger_entry_model_to_schema(existing)

    async def _charge_once(
        self,
        *,
        wallet_id: str,
        service_category: ServiceCategory,
        units: Decimal,
        request_path: str | None,
        description: str,
        operation_key: str | None,
        charge_amount: Decimal,
        compute_cost: Decimal,
        margin: Decimal,
        unit_name: str,
        credits_per_unit: Decimal,
        velocity_monitor: Any,
    ) -> LedgerEntry | InsufficientFundsResponse | SimulatedChargeResult:
        """One attempt at the debit transaction.

        Separated from ``charge`` so a lost SQLite snapshot can restart it from
        the top. Every piece of per-attempt state is local to this call: a retry
        must not inherit a velocity marker or a freeze flag that a rolled-back
        attempt set, or the compensation on the way out would fire for an
        increment that no longer exists.
        """
        # Only standalone velocity commits outside the billing transaction.
        # Hoist its period markers so an exception can compensate that one
        # external increment from a fresh transaction. Governed velocity uses
        # the billing session and therefore rolls back with the debit.
        recorded_velocity: tuple[datetime | None, datetime | None] | None = None
        velocity_period: tuple[datetime | None, datetime | None] = (None, None)
        notify_governed_freeze = False

        try:
            async with self._session_factory()() as session:
                async with session.begin():
                    dispatch_attempt = None
                    if operation_key is not None:
                        # The idempotency record is the durable lock for this
                        # governed financial operation. Holding it across the
                        # velocity check and debit prevents a concurrent retry
                        # from recording velocity or balance changes twice.
                        operation_record = (
                            await session.execute(
                                select(IdempotencyRecordModel)
                                .where(
                                    cast(
                                        ColumnElement[bool],
                                        IdempotencyRecordModel.record_id
                                        == operation_key,
                                    ),
                                    cast(
                                        ColumnElement[bool],
                                        IdempotencyRecordModel.wallet_id == wallet_id,
                                    ),
                                )
                                .with_for_update(key_share=True)
                            )
                        ).scalar_one_or_none()
                        if operation_record is None:
                            raise ValueError("ledger_operation_record_not_found")
                        existing_entry = await self._get_operation_debit(
                            session,
                            wallet_id=wallet_id,
                            operation_key=operation_key,
                        )
                        if (
                            existing_entry is None
                            and operation_record.ledger_entry_id is not None
                        ):
                            raise LedgerOperationConflictError(
                                "ledger_operation_checkpoint_conflict"
                            )
                        if existing_entry is not None:
                            self._assert_operation_debit_matches(
                                existing_entry,
                                wallet_id=wallet_id,
                                operation_key=operation_key,
                                service_category=service_category,
                                charge_amount=charge_amount,
                                request_path=request_path,
                            )
                            if operation_record.ledger_entry_id not in {
                                None,
                                existing_entry.entry_id,
                            }:
                                raise LedgerOperationConflictError(
                                    "ledger_operation_checkpoint_conflict"
                                )
                            operation_record.ledger_entry_id = existing_entry.entry_id
                            session.add(operation_record)
                            return ledger_entry_model_to_schema(existing_entry)

                        dispatch_attempt = (
                            await session.execute(
                                select(McpDispatchAttemptModel).where(
                                    cast(
                                        ColumnElement[bool],
                                        McpDispatchAttemptModel.idempotency_record_id
                                        == operation_key,
                                    ),
                                    cast(
                                        ColumnElement[bool],
                                        McpDispatchAttemptModel.wallet_id == wallet_id,
                                    ),
                                )
                            )
                        ).scalar_one_or_none()
                        if dispatch_attempt is not None and (
                            dispatch_attempt.state != "prepared"
                            or dispatch_attempt.dispatch_claim_hash is not None
                            or dispatch_attempt.dispatched_at is not None
                            or dispatch_attempt.ledger_entry_id is not None
                        ):
                            raise LedgerOperationConflictError(
                                "ledger_operation_dispatch_unavailable"
                            )

                    if dispatch_attempt is not None:
                        # Share the attempt write lock with pre-dispatch cleanup
                        # through the debit commit. Governed velocity uses this
                        # same session, so SQLite does not open a second writer
                        # while the fence is held.
                        fenced = await session.execute(
                            sa_update(McpDispatchAttemptModel)
                            .where(
                                cast(
                                    ColumnElement[bool],
                                    McpDispatchAttemptModel.attempt_id
                                    == dispatch_attempt.attempt_id,
                                ),
                                cast(
                                    ColumnElement[bool],
                                    McpDispatchAttemptModel.state == "prepared",
                                ),
                                cast(
                                    Any, McpDispatchAttemptModel.dispatch_claim_hash
                                ).is_(None),
                                cast(Any, McpDispatchAttemptModel.dispatched_at).is_(
                                    None
                                ),
                                cast(Any, McpDispatchAttemptModel.ledger_entry_id).is_(
                                    None
                                ),
                            )
                            .values(state="prepared")
                            .execution_options(synchronize_session=False)
                        )
                        if cast(Any, fenced).rowcount != 1:
                            raise LedgerOperationConflictError(
                                "ledger_operation_dispatch_unavailable"
                            )

                    if operation_key is not None:
                        velocity = await velocity_monitor.check_and_record_charge(
                            wallet_id,
                            charge_amount,
                            session=session,
                        )
                        notify_governed_freeze = velocity.should_freeze
                    else:
                        velocity = await velocity_monitor.check_and_record_charge(
                            wallet_id,
                            charge_amount,
                        )
                    velocity_period = (
                        getattr(velocity, "hourly_reset_at", None),
                        getattr(velocity, "daily_reset_at", None),
                    )
                    if operation_key is None:
                        recorded_velocity = velocity_period

                    charge = await self._apply_charge_to_locked_wallet(
                        session,
                        wallet_id=wallet_id,
                        service_category=service_category,
                        units=units,
                        unit_name=unit_name,
                        credits_per_unit=credits_per_unit,
                        charge_amount=charge_amount,
                        compute_cost=compute_cost,
                        margin=margin,
                        request_path=request_path,
                        description=description,
                        operation_key=operation_key,
                        # Which period the increment landed in, so a reversal
                        # cannot decrement a later one.
                        velocity_hourly_reset_at=velocity_period[0],
                        velocity_daily_reset_at=velocity_period[1],
                    )
                    if operation_key is not None and isinstance(charge, LedgerEntry):
                        assert operation_record is not None
                        operation_record.ledger_entry_id = charge.entry_id
                        session.add(operation_record)
            if notify_governed_freeze:
                await velocity_monitor.notify_committed_freeze(wallet_id)
            return charge
        except IntegrityError:
            # Standalone velocity committed before the transaction that just
            # rolled back, so its increment survived without a debit. Governed
            # velocity shares the rolled-back transaction and records no
            # external compensation marker.
            await self._reverse_recorded_velocity(
                wallet_id=wallet_id,
                amount=charge_amount,
                recorded=recorded_velocity,
            )
            if operation_key is None:
                raise
            # The unique wallet/operation-key constraint is the final guard if
            # a non-service writer raced the locked governed path. The failed
            # transaction rolled back its balance mutation; adopt only an
            # invariant-equivalent durable debit, and restore the idempotency
            # checkpoint in the same recovery transaction.
            async with self._session_factory()() as recovery_session:
                async with recovery_session.begin():
                    operation_record = (
                        await recovery_session.execute(
                            select(IdempotencyRecordModel)
                            .where(
                                cast(
                                    ColumnElement[bool],
                                    IdempotencyRecordModel.record_id == operation_key,
                                ),
                                cast(
                                    ColumnElement[bool],
                                    IdempotencyRecordModel.wallet_id == wallet_id,
                                ),
                            )
                            .with_for_update(key_share=True)
                        )
                    ).scalar_one_or_none()
                    if operation_record is None:
                        raise ValueError("ledger_operation_record_not_found")
                    existing_entry = await self._get_operation_debit(
                        recovery_session,
                        wallet_id=wallet_id,
                        operation_key=operation_key,
                    )
                    if existing_entry is None:
                        raise
                    self._assert_operation_debit_matches(
                        existing_entry,
                        wallet_id=wallet_id,
                        operation_key=operation_key,
                        service_category=service_category,
                        charge_amount=charge_amount,
                        request_path=request_path,
                    )
                    if operation_record.ledger_entry_id not in {
                        None,
                        existing_entry.entry_id,
                    }:
                        raise LedgerOperationConflictError(
                            "ledger_operation_checkpoint_conflict"
                        )
                    operation_record.ledger_entry_id = existing_entry.entry_id
                    recovery_session.add(operation_record)
                return ledger_entry_model_to_schema(existing_entry)
        except Exception:
            # Same debt on every other standalone path after its external
            # increment landed. No-op for governed velocity because it rolled
            # back with this transaction.
            await self._reverse_recorded_velocity(
                wallet_id=wallet_id,
                amount=charge_amount,
                recorded=recorded_velocity,
            )
            raise

    async def _reverse_recorded_velocity(
        self,
        *,
        wallet_id: str,
        amount: Decimal,
        recorded: tuple[datetime | None, datetime | None] | None,
    ) -> None:
        """Reverse a committed velocity increment from a fresh session.

        ``recorded`` is None when the monitor never got as far as recording,
        which is the common case for early validation failures -- there is
        nothing owed then. Otherwise the reversal needs a session of its own:
        the transaction that would have carried it has already rolled back.
        """

        if recorded is None:
            return
        async with self._session_factory()() as session:
            async with session.begin():
                await _reverse_velocity_counters(
                    session,
                    wallet_id=wallet_id,
                    amount=amount,
                    hourly_reset_at=recorded[0],
                    daily_reset_at=recorded[1],
                )

    async def refund_charge(
        self,
        *,
        wallet_id: str,
        charge_entry_id: str,
        description: str = "",
    ) -> LedgerEntry:
        """Reverse a prior debit with a correlated refund ledger entry."""
        async with self._session_factory()() as session:
            async with session.begin():
                refund_entry_id = f"refund-{charge_entry_id}"
                wallet_result = await session.execute(
                    select(WalletModel)
                    .where(
                        cast(ColumnElement[bool], WalletModel.wallet_id == wallet_id)
                    )
                    .with_for_update()
                )
                wallet = wallet_result.scalar_one_or_none()
                if not wallet:
                    raise self._wallet_not_found_error(wallet_id)

                charge_result = await session.execute(
                    select(LedgerEntryModel).where(
                        cast(
                            ColumnElement[bool],
                            LedgerEntryModel.entry_id == charge_entry_id,
                        ),
                        cast(
                            ColumnElement[bool], LedgerEntryModel.wallet_id == wallet_id
                        ),
                        cast(
                            ColumnElement[bool],
                            LedgerEntryModel.action == LedgerAction.DEBIT.value,
                        ),
                    )
                )
                charge_entry = charge_result.scalar_one_or_none()
                if not charge_entry:
                    raise ValueError(f"Debit ledger entry not found: {charge_entry_id}")

                existing_refund_result = await session.execute(
                    select(LedgerEntryModel).where(
                        cast(
                            ColumnElement[bool], LedgerEntryModel.wallet_id == wallet_id
                        ),
                        cast(
                            ColumnElement[bool],
                            LedgerEntryModel.action == LedgerAction.REFUND.value,
                        ),
                        cast(
                            ColumnElement[bool],
                            LedgerEntryModel.correlation_id == charge_entry_id,
                        ),
                    )
                )
                existing_refund = existing_refund_result.scalar_one_or_none()
                if existing_refund:
                    return ledger_entry_model_to_schema(existing_refund)

                existing_refund_result = await session.execute(
                    select(LedgerEntryModel).where(
                        cast(
                            ColumnElement[bool],
                            LedgerEntryModel.entry_id == refund_entry_id,
                        ),
                        cast(
                            ColumnElement[bool], LedgerEntryModel.wallet_id == wallet_id
                        ),
                        cast(
                            ColumnElement[bool],
                            LedgerEntryModel.action == LedgerAction.REFUND.value,
                        ),
                    )
                )
                existing_refund = existing_refund_result.scalar_one_or_none()
                if existing_refund:
                    return ledger_entry_model_to_schema(existing_refund)

                refund_amount = abs(charge_entry.amount)
                # Credit relatively, in one statement, for the same reason the
                # debit does. Two refunds for *different* charges landing
                # together each read the same balance and each write their own
                # total, so one credit is silently lost -- and a lost credit is
                # the customer's money. The duplicate-refund guard above does
                # not help here: these are distinct, legitimate refunds.
                refund_values: dict[str, Any] = {
                    "balance": WalletModel.balance + refund_amount,
                    "lifetime_debits": clamped_decrement(
                        WalletModel.lifetime_debits, refund_amount
                    ),
                    "updated_at": utc_now(),
                }
                if _timestamp_in_current_period(
                    charge_entry.timestamp,
                    wallet.hourly_reset_at,
                ):
                    refund_values["hourly_spent"] = clamped_decrement(
                        WalletModel.hourly_spent, refund_amount
                    )
                if _timestamp_in_current_period(
                    charge_entry.timestamp,
                    wallet.daily_reset_at,
                ):
                    # daily_spent is incremented exactly once per charge (by the
                    # velocity monitor), so reverse exactly one increment.
                    refund_values["daily_spent"] = clamped_decrement(
                        WalletModel.daily_spent, refund_amount
                    )
                await session.execute(
                    sa_update(WalletModel)
                    .where(
                        cast(ColumnElement[bool], WalletModel.wallet_id == wallet_id)
                    )
                    .values(**refund_values)
                    .execution_options(synchronize_session=False)
                )
                # ``balance_after`` below must be the balance this refund
                # produced, not the one read before it.
                await session.refresh(wallet)

                entry = LedgerEntryModel(
                    entry_id=refund_entry_id,
                    wallet_id=wallet_id,
                    action=LedgerAction.REFUND.value,
                    amount=refund_amount,
                    balance_after=wallet.balance,
                    service_category=charge_entry.service_category,
                    description=description or f"Refund for {charge_entry.description}",
                    request_path=charge_entry.request_path,
                    compute_cost=Decimal("0"),
                    margin=Decimal("0"),
                    correlation_id=charge_entry_id,
                )
                session.add(entry)

            await session.commit()
            return ledger_entry_model_to_schema(entry)

    # --- Top-Up (Fiat Ingestion) ---

    async def top_up(
        self,
        wallet_id: str,
        amount_fiat: Decimal,
        payment_method: str = "stripe",
        payment_token: str | None = None,
    ) -> TopUpResponse:
        """Reject direct credit minting that lacks verified provider settlement."""
        raise DirectTopUpDisabledError(
            "direct_top_up_disabled: use the Stripe prepare and verified webhook flow"
        )

    # --- Arbitrage Reporting ---

    async def get_arbitrage_report(self) -> ArbitrageReport:
        """Compute profitability in [now - 24 hours, now), across all wallets."""
        end = utc_now()
        start = end - timedelta(days=1)
        async with self._session_factory()() as session:
            result = await session.execute(
                select(LedgerEntryModel).where(
                    cast(
                        ColumnElement[bool],
                        LedgerEntryModel.action == LedgerAction.DEBIT.value,
                    ),
                    cast(ColumnElement[bool], LedgerEntryModel.timestamp >= start),
                    cast(ColumnElement[bool], LedgerEntryModel.timestamp < end),
                )
            )
            entries = list(result.scalars().all())

            total_revenue = Decimal("0")
            total_cost = Decimal("0")
            by_service: dict[str, dict] = {}

            for entry in entries:
                charge = abs(entry.amount)
                cost = entry.compute_cost or Decimal("0")
                margin = entry.margin or Decimal("0")

                total_revenue += charge
                total_cost += cost

                cat = entry.service_category or "unknown"
                if cat not in by_service:
                    by_service[cat] = {
                        "revenue": Decimal("0"),
                        "cost": Decimal("0"),
                        "margin": Decimal("0"),
                        "transactions": 0,
                    }
                by_service[cat]["revenue"] += charge
                by_service[cat]["cost"] += cost
                by_service[cat]["margin"] += margin
                by_service[cat]["transactions"] += 1

            # Compute percentages
            for cat_data in by_service.values():
                rev = cat_data["revenue"]
                cat_data["margin_pct"] = (
                    (Decimal("100") * cat_data["margin"] / rev)
                    if rev > 0
                    else Decimal("0")
                )

            gross_margin = total_revenue - total_cost
            margin_pct = (
                (Decimal("100") * gross_margin / total_revenue)
                if total_revenue > 0
                else Decimal("0")
            )

            # Top profitable actions
            top_actions = sorted(
                entries,
                key=lambda e: e.margin or Decimal("0"),
                reverse=True,
            )[:5]

            return ArbitrageReport(
                period=f"{start.isoformat()}Z to {end.isoformat()}Z (end exclusive)",
                total_revenue=float(total_revenue),
                total_revenue_exact=str(total_revenue),
                total_compute_cost=float(total_cost),
                total_compute_cost_exact=str(total_cost),
                gross_margin=float(gross_margin),
                gross_margin_exact=str(gross_margin),
                margin_percentage=float(margin_pct),
                margin_percentage_exact=str(margin_pct),
                by_service=by_service,
                top_profitable_actions=[
                    {
                        "entry_id": e.entry_id,
                        "service": e.service_category or "unknown",
                        "charge": abs(e.amount),
                        "cost": e.compute_cost or Decimal("0"),
                        "margin": e.margin or Decimal("0"),
                        "path": e.request_path,
                    }
                    for e in top_actions
                ],
            )

    # --- Pricing ---

    def get_pricing_table(self) -> list[ServicePricing]:
        """Return the pricing table for the services this deployment mounts.

        Categories whose only services are the frozen proof-surface routers are
        omitted when those routers are not mounted. Listing them made the always
        -mounted billing router publish a price list for media, IoT, oracle,
        red-team and friends in the OpenAPI of a deployment that cannot serve
        any of them — the advertising docs/PROOF_SURFACES.md rule 3 forbids.

        This filters presentation only. DEFAULT_PRICING stays authoritative
        server-side for every category, since it supplies the fallback price and
        the unit divisor in pricing.charge_units_for, and a registered tool's
        own credits_per_unit remains the authoritative per-tool price via
        /v1/quotes and /mcp/tools.json.
        """
        show_proof_surfaces = get_settings().ENABLE_PROOF_SURFACES
        return [
            ServicePricing(
                service_category=cat,
                unit=unit,
                credits_per_unit=float(price),
                credits_per_unit_exact=str(price),
                description=desc,
            )
            for cat, (unit, price, desc) in self._default_pricing.items()
            if show_proof_surfaces or cat not in PROOF_SURFACE_CATEGORIES
        ]

    # --- Alerts ---

    async def get_alerts(self, wallet_id: str | None = None) -> list[BillingAlert]:
        """Get billing alerts."""
        async with self._session_factory()() as session:
            if wallet_id:
                result = await session.execute(
                    select(BillingAlertModel)
                    .where(
                        cast(
                            ColumnElement[bool],
                            BillingAlertModel.wallet_id == wallet_id,
                        )
                    )
                    .order_by(
                        cast(ColumnElement[Any], BillingAlertModel.created_at).desc()
                    )
                )
            else:
                result = await session.execute(
                    select(BillingAlertModel).order_by(
                        cast(ColumnElement[Any], BillingAlertModel.created_at).desc()
                    )
                )
            alerts = list(result.scalars().all())
            return [billing_alert_model_to_schema(a) for a in alerts]

    # --- Wallet Queries ---

    async def get_ledger(self, wallet_id: str, limit: int = 50) -> list[LedgerEntry]:
        """Get ledger entries for a wallet."""
        async with self._session_factory()() as session:
            result = await session.execute(
                select(LedgerEntryModel)
                .where(
                    cast(ColumnElement[bool], LedgerEntryModel.wallet_id == wallet_id)
                )
                .order_by(cast(ColumnElement[Any], LedgerEntryModel.timestamp).desc())
                .limit(limit)
            )
            entries = list(result.scalars().all())
            return [ledger_entry_model_to_schema(e) for e in entries]

    # --- Service Registry ---

    async def register_service(
        self,
        owner_wallet_id: str,
        name: str,
        description: str,
        category: ServiceCategory,
        credits_per_unit: Decimal,
        unit_name: str = "request",
        mcp_manifest: dict | None = None,
    ) -> ServiceRegistration:
        """Register a billable service in the marketplace."""
        service_id = f"svc-{uuid.uuid4().hex[:12]}"

        async with self._session_factory()() as session:
            service = ServiceRegistryModel(
                service_id=service_id,
                name=name,
                description=description,
                owner_wallet_id=owner_wallet_id,
                category=category.value,
                credits_per_unit=credits_per_unit,
                unit_name=unit_name,
                mcp_manifest=json.dumps(mcp_manifest) if mcp_manifest else None,
                is_active=True,
            )
            session.add(service)
            await session.commit()

        logger.info(f"Registered service {service_id}: {name}")

        return ServiceRegistration(
            service_id=service_id,
            name=name,
            description=description,
            owner_wallet_id=owner_wallet_id,
            category=category,
            credits_per_unit=float(credits_per_unit),
            credits_per_unit_exact=str(credits_per_unit),
            unit_name=unit_name,
            mcp_manifest=mcp_manifest,
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )

    async def list_services(
        self,
        category: ServiceCategory | None = None,
        active_only: bool = True,
    ) -> list[ServiceRegistration]:
        """List services in the marketplace."""
        async with self._session_factory()() as session:
            query = select(ServiceRegistryModel)
            if category:
                query = query.where(
                    cast(
                        ColumnElement[bool],
                        ServiceRegistryModel.category == category.value,
                    )
                )
            if active_only:
                query = query.where(
                    cast(
                        ColumnElement[bool],
                        # SQLModel exposes is_active as a plain `bool` at the
                        # class level to mypy, but at runtime it's an
                        # InstrumentedAttribute with SQL comparator methods
                        # like `.is_()`.
                        cast(Any, ServiceRegistryModel.is_active).is_(True),
                    )
                )

            result = await session.execute(query)
            services = result.scalars().all()

            return [
                ServiceRegistration(
                    service_id=s.service_id,
                    name=s.name,
                    description=s.description,
                    owner_wallet_id=s.owner_wallet_id,
                    category=ServiceCategory(s.category),
                    credits_per_unit=float(s.credits_per_unit),
                    credits_per_unit_exact=str(s.credits_per_unit),
                    unit_name=s.unit_name,
                    mcp_manifest=json.loads(s.mcp_manifest) if s.mcp_manifest else None,
                    is_active=s.is_active,
                    created_at=s.created_at,
                )
                for s in services
            ]
