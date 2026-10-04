"""
Tests for Spend Velocity Monitoring.
Validates velocity tracking, anomaly detection, and auto-freeze.
"""

import pytest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

from app.core.time import to_naive_utc
from app.services.velocity_monitor import (
    VelocityMonitor,
    VelocityCheckResult,
)


class TestVelocityCheckResult:
    """Tests for VelocityCheckResult."""

    def test_allowed_result(self):
        result = VelocityCheckResult(
            allowed=True,
            reason="Within velocity limits",
        )
        assert result.allowed is True
        assert result.alert_triggered is False
        assert result.should_freeze is False

    def test_alert_result(self):
        result = VelocityCheckResult(
            allowed=True,
            reason="Hourly spend velocity exceeded",
            alert_triggered=True,
            exceeded_limit="hourly",
            current_spend=1500.0,
            limit=1000.0,
        )
        assert result.allowed is True
        assert result.alert_triggered is True
        assert result.exceeded_limit == "hourly"

    def test_freeze_result(self):
        result = VelocityCheckResult(
            allowed=False,
            reason="Wallet frozen due to anomalous spend velocity",
            alert_triggered=True,
            should_freeze=True,
        )
        assert result.allowed is False
        assert result.should_freeze is True


class TestVelocityMonitorReset:
    """Tests for hourly/daily counter reset logic."""

    def test_reset_hourly_after_1_hour(self):
        monitor = VelocityMonitor()
        wallet = MagicMock()
        wallet.hourly_reset_at = datetime.now(timezone.utc) - timedelta(hours=2)
        wallet.hourly_spent = Decimal("500")
        wallet.daily_reset_at = datetime.now(timezone.utc)
        wallet.daily_spent = Decimal("1000")

        now = datetime.now(timezone.utc)
        monitor._reset_if_needed(wallet, now)

        assert wallet.hourly_spent == Decimal("0")
        assert wallet.hourly_reset_at == to_naive_utc(now)

    def test_reset_daily_after_midnight(self):
        monitor = VelocityMonitor()
        wallet = MagicMock()
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        wallet.hourly_reset_at = yesterday
        wallet.hourly_spent = Decimal("500")
        wallet.daily_reset_at = yesterday.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        wallet.daily_spent = Decimal("1000")

        now = datetime.now(timezone.utc)
        monitor._reset_if_needed(wallet, now)

        assert wallet.daily_spent == Decimal("0")
        assert wallet.daily_reset_at.date() == now.date()


class TestVelocityMonitorLimits:
    """Tests for velocity limit checking."""

    def test_within_limits(self):
        monitor = VelocityMonitor()
        wallet = MagicMock()
        wallet.wallet_id = "test-wallet"
        wallet.hourly_spent = Decimal("500")
        wallet.daily_spent = Decimal("5000")
        wallet.velocity_alerts_triggered = 0

        result = monitor._check_limits(
            wallet=wallet,
            hourly_limit=Decimal("1000"),
            daily_limit=Decimal("10000"),
            charge_amount=Decimal("100"),
        )

        assert result.allowed is True
        assert result.alert_triggered is False

    def test_hourly_limit_exceeded(self):
        monitor = VelocityMonitor()
        wallet = MagicMock()
        wallet.wallet_id = "test-wallet"
        wallet.hourly_spent = Decimal("1100")
        wallet.daily_spent = Decimal("5000")
        wallet.velocity_alerts_triggered = 0

        result = monitor._check_limits(
            wallet=wallet,
            hourly_limit=Decimal("1000"),
            daily_limit=Decimal("10000"),
            charge_amount=Decimal("100"),
        )

        assert result.allowed is True
        assert result.alert_triggered is True
        assert result.exceeded_limit == "hourly"
        assert result.should_freeze is False

    def test_freeze_after_threshold_exceeded(self):
        monitor = VelocityMonitor()
        wallet = MagicMock()
        wallet.wallet_id = "test-wallet"
        wallet.hourly_spent = Decimal("1100")
        wallet.daily_spent = Decimal("5000")
        wallet.velocity_alerts_triggered = 3

        result = monitor._check_limits(
            wallet=wallet,
            hourly_limit=Decimal("1000"),
            daily_limit=Decimal("10000"),
            charge_amount=Decimal("100"),
        )

        assert result.alert_triggered is True
        assert result.should_freeze is True


class TestVelocityMonitorIntegration:
    """Integration tests for velocity monitoring."""

    @pytest.mark.asyncio
    async def test_velocity_status_endpoint(self):
        """Test that velocity status endpoint returns correct data."""
        from httpx import AsyncClient, ASGITransport
        from app.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"X-API-Key": "test-key"}

            resp = await client.post(
                "/v1/billing/wallets/sponsor",
                json={
                    "sponsor_name": "Test",
                    "email": "t@t.com",
                    "initial_credits": 10000,
                },
                headers=headers,
            )
            wallet_id = resp.json()["wallet_id"]

            velocity_resp = await client.get(
                f"/v1/billing/wallets/{wallet_id}/velocity",
                headers=headers,
            )

            assert velocity_resp.status_code == 200
            data = velocity_resp.json()
            assert data["wallet_id"] == wallet_id
            assert "hourly_spent" in data
            assert "hourly_limit" in data
            assert "daily_spent" in data
            assert "daily_limit" in data
            assert "velocity_alerts" in data


class TestVelocityFreezeStatusGuard:
    """Auto-freeze must not clobber a stronger wallet control state."""

    @staticmethod
    async def _seed_wallet(status: str) -> str:
        import uuid

        from app.db.database import get_session_factory
        from app.db.models import WalletModel

        wallet_id = f"agt-vel-{uuid.uuid4().hex[:12]}"
        factory = get_session_factory()
        async with factory() as session:
            async with session.begin():
                session.add(
                    WalletModel(
                        wallet_id=wallet_id,
                        wallet_type="agent",
                        owner_name="velocity freeze guard probe",
                        email=f"{wallet_id}@example.com",
                        balance=Decimal("100"),
                        status=status,
                        # A tiny cap plus pre-loaded spend and a saturated alert
                        # counter make the next charge trip the freeze branch.
                        hourly_limit=Decimal("1"),
                        daily_limit=Decimal("1000000"),
                        hourly_spent=Decimal("5"),
                        velocity_alerts_triggered=10_000,
                    )
                )
        return wallet_id

    @staticmethod
    async def _status(wallet_id: str) -> str:
        from app.db.database import get_session_factory
        from app.db.models import WalletModel

        factory = get_session_factory()
        async with factory() as session:
            wallet = await session.get(WalletModel, wallet_id)
            assert wallet is not None
            return wallet.status

    @pytest.mark.anyio
    @pytest.mark.parametrize("protected_status", ["closed", "suspended"])
    async def test_freeze_does_not_clobber_stronger_status(
        self, clean_database, protected_status
    ):
        """A velocity trip must not downgrade a closed/suspended wallet.

        Overwriting a stronger control state with ``frozen`` would let an
        operator later lift the freeze and restore spendability the stronger
        control had permanently removed.
        """
        wallet_id = await self._seed_wallet(protected_status)

        result = await VelocityMonitor().check_and_record_charge(
            wallet_id, Decimal("1")
        )

        # The anomaly is still detected, but no freeze transition or downstream
        # freeze notification is owed when a stronger status already owns the
        # wallet.
        assert result.alert_triggered is True
        assert result.should_freeze is False
        assert await self._status(wallet_id) == protected_status

    @pytest.mark.anyio
    @pytest.mark.parametrize("spendable_status", ["active", "pending_kyc"])
    async def test_freeze_applies_to_every_spendable_status(
        self,
        clean_database,
        spendable_status,
    ):
        """The anomaly control freezes every status billing permits to spend."""
        wallet_id = await self._seed_wallet(spendable_status)

        result = await VelocityMonitor().check_and_record_charge(
            wallet_id, Decimal("1")
        )

        assert result.should_freeze is True
        assert await self._status(wallet_id) == "frozen"
