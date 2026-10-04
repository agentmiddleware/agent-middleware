"""
Spend Velocity Monitor
Detects anomalous spending patterns and auto-freezes wallets.

Architecture:
1. On every charge, check hourly/daily spend vs limits
2. If spend exceeds threshold, trigger alert
3. If spend exceeds freeze threshold, auto-freeze wallet
4. Notify sponsor via Slack/email
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Optional, cast

from sqlalchemy import select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from ..core.time import to_naive_utc, utc_now
from ..db.database import get_session_factory
from ..db.models import WalletModel
from ..schemas.billing import WalletStatus
from ..services.notifications import get_notification_service
from ..core.config import get_settings
from .wallet_status import SPENDABLE_WALLET_STATUSES

logger = logging.getLogger(__name__)
settings = get_settings()

_ZERO_LIMIT_BREACH_PERCENT = 101.0


def _usage_percentage(spent: Decimal, limit: Decimal) -> float:
    """Return a finite utilization signal, including for a strict zero cap."""
    if limit > 0:
        return float(spent / limit * 100)
    if limit == 0 and spent > 0:
        # The mathematical ratio is undefined, while 100% would look like the
        # non-breaching equality boundary. Keep the existing numeric schema and
        # report a small, explicit breach value that is safe to serialize.
        return _ZERO_LIMIT_BREACH_PERCENT
    return 0.0


class VelocityCheckResult:
    """Result of a velocity check."""

    def __init__(
        self,
        allowed: bool,
        reason: str,
        alert_triggered: bool = False,
        exceeded_limit: str | None = None,
        current_spend: float | None = None,
        limit: float | None = None,
        should_freeze: bool = False,
        hourly_reset_at: datetime | None = None,
        daily_reset_at: datetime | None = None,
    ):
        self.allowed = allowed
        self.reason = reason
        self.alert_triggered = alert_triggered
        self.exceeded_limit = exceeded_limit
        self.current_spend = current_spend
        self.limit = limit
        self.should_freeze = should_freeze
        # The period this charge's increment was actually recorded into, or
        # None when nothing was recorded. A caller reversing the increment
        # needs it: the counters roll over on their own schedule, and a
        # reversal that lands after a rollover would decrement a period this
        # charge never contributed to. See BillingEngine's
        # ``reverse_velocity_record``.
        self.hourly_reset_at = hourly_reset_at
        self.daily_reset_at = daily_reset_at


class WalletFrozenError(Exception):
    """Raised when a wallet is frozen due to anomalous spend."""

    def __init__(self, wallet_id: str, reason: str):
        self.wallet_id = wallet_id
        self.reason = reason
        super().__init__(f"Wallet {wallet_id} is frozen: {reason}")


class VelocityMonitor:
    """
    Monitors spend velocity and auto-freezes wallets on anomaly.

    Thresholds (can be overridden per wallet):
    - Default hourly limit: 1000 credits/hour (configurable)
    - Default daily limit: 10000 credits/day (configurable)
    - Freeze after 3 velocity alerts
    """

    def __init__(self):
        self._session_factory = get_session_factory
        self._default_hourly_limit = Decimal(str(settings.VELOCITY_HOURLY_LIMIT))
        self._default_daily_limit = Decimal(str(settings.VELOCITY_DAILY_LIMIT))
        self._alert_threshold = settings.VELOCITY_ALERT_THRESHOLD
        self._freeze_threshold = settings.VELOCITY_FREEZE_THRESHOLD

    async def check_and_record_charge(
        self,
        wallet_id: str,
        charge_amount: Decimal,
        *,
        session: AsyncSession | None = None,
    ) -> VelocityCheckResult:
        """
        Check if a charge is within velocity limits and record it.

        Returns:
            VelocityCheckResult with status and any alerts

        Raises:
            WalletFrozenError: If wallet should be frozen
        """
        # Governed billing supplies its transaction so the velocity mutation,
        # dispatch fence, debit, and idempotency checkpoint either all commit or
        # all roll back. The caller also owns any post-commit notification.
        if session is not None:
            return await self._record_charge(session, wallet_id, charge_amount)

        async with self._session_factory()() as owned_session:
            async with owned_session.begin():
                velocity_result = await self._record_charge(
                    owned_session,
                    wallet_id,
                    charge_amount,
                )
        if velocity_result.should_freeze:
            await self.notify_committed_freeze(wallet_id)
        return velocity_result

    async def _record_charge(
        self,
        session: AsyncSession,
        wallet_id: str,
        charge_amount: Decimal,
    ) -> VelocityCheckResult:
        result = await session.execute(
            select(WalletModel)
            .where(cast(ColumnElement[bool], WalletModel.wallet_id == wallet_id))
            .with_for_update()
        )
        wallet = result.scalar_one_or_none()

        if not wallet:
            return VelocityCheckResult(
                allowed=True,
                reason="Wallet not found",
            )

        now = utc_now()

        self._reset_if_needed(wallet, now)
        # Land any period reset before the relative increment below, so the
        # increment applies on top of the reset value rather than racing it
        # inside the same statement.
        await session.flush()

        hourly_limit = (
            wallet.hourly_limit
            if wallet.hourly_limit is not None
            else self._default_hourly_limit
        )
        daily_limit = (
            wallet.daily_limit
            if wallet.daily_limit is not None
            else self._default_daily_limit
        )

        # Accumulate relatively, in one statement. Reading a counter and
        # writing back read+charge is a read-modify-write serialized only by the
        # ``SELECT ... FOR UPDATE`` above, which is a silent no-op on SQLite:
        # concurrent charges each add to the same observed total and all but
        # one increment is lost. This counter is what the spend cap and anomaly
        # auto-freeze are measured against, so under-counting silently disables
        # both controls precisely when spend is most concurrent.
        await session.execute(
            sa_update(WalletModel)
            .where(cast(ColumnElement[bool], WalletModel.wallet_id == wallet_id))
            .values(
                hourly_spent=WalletModel.hourly_spent + charge_amount,
                daily_spent=WalletModel.daily_spent + charge_amount,
                last_charge_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        # The limit check below must see the totals this charge produced, not
        # the ones read before it.
        await session.refresh(wallet)

        # Stamp the period the increment landed in, so a separately committed
        # caller reversing it can refuse to decrement a later one.
        recorded_hourly_reset_at = wallet.hourly_reset_at
        recorded_daily_reset_at = wallet.daily_reset_at

        velocity_result = self._check_limits(
            wallet=wallet,
            hourly_limit=hourly_limit,
            daily_limit=daily_limit,
            charge_amount=charge_amount,
        )
        velocity_result.hourly_reset_at = recorded_hourly_reset_at
        velocity_result.daily_reset_at = recorded_daily_reset_at

        if velocity_result.should_freeze:
            # Velocity owns transitions from the two spendable states into
            # ``frozen``. It does not own an operator suspension, closure, or a
            # future control state. Decide that authority at write time so
            # SQLite's no-op ``FOR UPDATE`` cannot let a stale ORM object
            # overwrite a stronger state committed concurrently.
            frozen = await session.execute(
                sa_update(WalletModel)
                .where(
                    cast(
                        ColumnElement[bool],
                        WalletModel.wallet_id == wallet_id,
                    ),
                    cast(Any, WalletModel.status).in_(tuple(SPENDABLE_WALLET_STATUSES)),
                )
                .values(
                    status=WalletStatus.FROZEN.value,
                    velocity_alerts_triggered=(
                        WalletModel.velocity_alerts_triggered + 1
                    ),
                )
                .execution_options(synchronize_session=False)
            )
            freeze_applied = (cast(Any, frozen).rowcount or 0) == 1
            await session.refresh(wallet)
            if not freeze_applied:
                velocity_result.should_freeze = False
                return velocity_result
            logger.warning(
                f"Auto-freezing wallet {wallet_id}: "
                f"hourly_spent={wallet.hourly_spent}, "
                f"limit={hourly_limit}, "
                f"alerts={wallet.velocity_alerts_triggered}"
            )
            return VelocityCheckResult(
                allowed=False,
                reason="Wallet frozen due to anomalous spend velocity",
                alert_triggered=True,
                should_freeze=True,
                hourly_reset_at=recorded_hourly_reset_at,
                daily_reset_at=recorded_daily_reset_at,
            )

        if velocity_result.alert_triggered:
            wallet.velocity_alerts_triggered += 1
            await session.flush()

        return velocity_result

    async def notify_committed_freeze(self, wallet_id: str) -> None:
        """Notify only after the transaction that froze the wallet committed."""
        async with self._session_factory()() as session:
            wallet = await session.get(WalletModel, wallet_id)
        if wallet is not None and wallet.status == "frozen":
            await self._notify_freeze(wallet)

    def _reset_if_needed(self, wallet: WalletModel, now: datetime) -> None:
        """Reset hourly/daily counters if period has elapsed."""
        now = to_naive_utc(now)
        if wallet.hourly_reset_at is None:
            wallet.hourly_reset_at = now
        else:
            last_reset = to_naive_utc(wallet.hourly_reset_at)
            if now - last_reset >= timedelta(hours=1):
                wallet.hourly_spent = Decimal("0")
                wallet.hourly_reset_at = now

        if wallet.daily_reset_at is None:
            wallet.daily_reset_at = now.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        else:
            last_reset = to_naive_utc(wallet.daily_reset_at)
            if now - last_reset >= timedelta(days=1):
                wallet.daily_spent = Decimal("0")
                wallet.daily_reset_at = now.replace(
                    hour=0, minute=0, second=0, microsecond=0
                )

    def _check_limits(
        self,
        wallet: WalletModel,
        hourly_limit: Decimal,
        daily_limit: Decimal,
        charge_amount: Decimal,
    ) -> VelocityCheckResult:
        """Check if charge exceeds velocity limits."""
        hourly_exceeded = wallet.hourly_spent > hourly_limit
        daily_exceeded = wallet.daily_spent > daily_limit

        if hourly_exceeded or daily_exceeded:
            exceeded_limit = "hourly" if hourly_exceeded else "daily"
            current_spend = (
                wallet.hourly_spent if hourly_exceeded else wallet.daily_spent
            )
            limit = hourly_limit if hourly_exceeded else daily_limit

            should_freeze = wallet.velocity_alerts_triggered >= self._freeze_threshold

            logger.warning(
                f"Velocity alert for wallet {wallet.wallet_id}: "
                f"{exceeded_limit} spend {current_spend} exceeds limit {limit}. "
                f"Alerts: {wallet.velocity_alerts_triggered}, Freeze: {should_freeze}"
            )

            return VelocityCheckResult(
                allowed=True,
                reason=f"{exceeded_limit.capitalize()} spend velocity exceeded",
                alert_triggered=True,
                exceeded_limit=exceeded_limit,
                current_spend=float(current_spend),
                limit=float(limit),
                should_freeze=should_freeze,
            )

        return VelocityCheckResult(
            allowed=True,
            reason="Within velocity limits",
        )

    async def _notify_freeze(self, wallet: WalletModel) -> None:
        """Send freeze notification to sponsor."""
        notifications = get_notification_service()
        await notifications.send_wallet_frozen_alert(
            wallet_id=wallet.wallet_id,
            reason="anomalous_spend",
            sponsor_email=wallet.email,
            wallet_owner=wallet.owner_name,
        )

    async def get_velocity_status(self, wallet_id: str) -> dict:
        """Get current velocity status for a wallet."""
        async with self._session_factory()() as session:
            result = await session.execute(
                select(WalletModel).where(
                    cast(ColumnElement[bool], WalletModel.wallet_id == wallet_id)
                )
            )
            wallet = result.scalar_one_or_none()

            if not wallet:
                return {"error": "Wallet not found"}

            now = utc_now()
            self._reset_if_needed(wallet, now)

            hourly_limit = (
                wallet.hourly_limit
                if wallet.hourly_limit is not None
                else self._default_hourly_limit
            )
            daily_limit = (
                wallet.daily_limit
                if wallet.daily_limit is not None
                else self._default_daily_limit
            )

            return {
                "wallet_id": wallet_id,
                "hourly_spent": float(wallet.hourly_spent),
                "hourly_limit": float(hourly_limit),
                "hourly_pct": _usage_percentage(
                    wallet.hourly_spent,
                    hourly_limit,
                ),
                "daily_spent": float(wallet.daily_spent),
                "daily_limit": float(daily_limit),
                "daily_pct": _usage_percentage(
                    wallet.daily_spent,
                    daily_limit,
                ),
                "velocity_alerts": wallet.velocity_alerts_triggered,
                "status": wallet.status,
                "last_charge_at": (
                    wallet.last_charge_at.isoformat() if wallet.last_charge_at else None
                ),
            }


_velocity_monitor: Optional[VelocityMonitor] = None


def get_velocity_monitor() -> VelocityMonitor:
    """Get or create the VelocityMonitor singleton."""
    global _velocity_monitor
    if _velocity_monitor is None:
        _velocity_monitor = VelocityMonitor()
    return _velocity_monitor
