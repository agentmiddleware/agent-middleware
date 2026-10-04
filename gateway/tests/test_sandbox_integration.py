"""
Tests for Dry-Run Sandbox Integration
====================================

Tests for:
- Shadow ledger session creation and management
- Simulated charge tracking
- State accumulation (cumulative balance deduction)
- Session expiry and cleanup
- SDK simulate_session context manager
"""

import asyncio
import pytest
from datetime import datetime, timezone
from decimal import Decimal

from app.services.shadow_ledger import (
    ShadowLedger,
    DryRunSession,
    SimulatedCharge,
    get_shadow_ledger,
)
from app.schemas.billing import ServiceCategory


class TestShadowLedger:
    """Test shadow ledger operations."""

    @pytest.fixture
    def ledger(self):
        return ShadowLedger()

    def test_create_session(self, ledger):
        """Creating a session returns a DryRunSession with unique ID."""
        session = asyncio.run(
            ledger.create_session(
                wallet_id="test-wallet",
                real_balance=Decimal("1000"),
            )
        )

        assert session is not None
        assert session.wallet_id == "test-wallet"
        assert session.real_balance == Decimal("1000")
        assert session.session_id is not None
        assert len(session.simulated_charges) == 0
        assert session.virtual_balance == Decimal("1000")

    def test_session_tracks_cumulative_charges(self, ledger):
        """Simulated charges accumulate in the session."""

        async def scenario():
            session = await ledger.create_session(
                wallet_id="test-wallet",
                real_balance=Decimal("1000"),
            )

            result1 = await ledger.simulate_charge(
                session_id=session.session_id,
                service_category=ServiceCategory.CONTENT_FACTORY,
                units=10.0,
                description="First charge",
            )

            assert result1.would_succeed is True
            assert result1.simulated_balance_after == 500.0

            result2 = await ledger.simulate_charge(
                session_id=session.session_id,
                service_category=ServiceCategory.IOT_BRIDGE,
                units=5.0,
                description="Second charge",
            )

            assert result2.would_succeed is True
            assert result2.simulated_balance_after == 490.0

            final_session = await ledger.get_session(session.session_id)

            assert len(final_session.simulated_charges) == 2
            assert final_session.virtual_balance == Decimal("490")
            assert final_session.total_simulated == Decimal("510")

        asyncio.run(scenario())

    def test_session_returns_insufficient_funds_when_exceeding_balance(self, ledger):
        """Simulated charges return would_succeed=False when exceeding virtual balance."""

        async def scenario():
            session = await ledger.create_session(
                wallet_id="test-wallet",
                real_balance=Decimal("100"),
            )

            result = await ledger.simulate_charge(
                session_id=session.session_id,
                service_category=ServiceCategory.CONTENT_FACTORY,
                units=50.0,
                description="Large charge",
            )

            assert result.would_succeed is False
            assert result.simulated_balance_after < 0
            assert result.reason == "insufficient_simulated_funds"

        asyncio.run(scenario())

    def test_session_end_returns_summary(self, ledger):
        """Ending a session returns a complete summary."""

        async def scenario():
            session = await ledger.create_session(
                wallet_id="test-wallet",
                real_balance=Decimal("500"),
            )

            await ledger.simulate_charge(
                session_id=session.session_id,
                service_category=ServiceCategory.TELEMETRY_PM,
                units=100.0,
                description="Test charge",
            )

            summary = await ledger.end_session(session.session_id)

            assert summary is not None
            assert summary.wallet_id == "test-wallet"
            assert summary.total_simulated_credits == Decimal("100")
            assert summary.charge_count == 1
            assert summary.virtual_balance_after == Decimal("400")

        asyncio.run(scenario())

    def test_nonexistent_session_returns_none(self, ledger):
        """Getting a nonexistent session returns None."""
        result = asyncio.run(ledger.get_session("nonexistent-id"))
        assert result is None

    def test_nonexistent_session_end_returns_none(self, ledger):
        """Ending a nonexistent session returns None."""
        result = asyncio.run(ledger.end_session("nonexistent-id"))
        assert result is None

    def test_virtual_balance_calculation(self, ledger):
        """Virtual balance correctly reflects real balance minus charges."""

        async def scenario():
            session = await ledger.create_session(
                wallet_id="test-wallet",
                real_balance=Decimal("1000"),
            )

            for i in range(5):
                await ledger.simulate_charge(
                    session_id=session.session_id,
                    service_category=ServiceCategory.IOT_BRIDGE,
                    units=1.0,
                    description=f"Charge {i}",
                )

            final_session = await ledger.get_session(session.session_id)

            assert final_session.virtual_balance == Decimal("990")
            assert final_session.total_simulated == Decimal("10")

        asyncio.run(scenario())


class TestDryRunSession:
    """Test DryRunSession dataclass."""

    def test_session_initialization(self):
        """Session initializes with correct defaults."""
        session = DryRunSession(
            session_id="test-123",
            wallet_id="wallet-456",
            real_balance=Decimal("1000"),
        )

        assert session.session_id == "test-123"
        assert session.wallet_id == "wallet-456"
        assert session.real_balance == Decimal("1000")
        assert session.simulated_charges == []
        assert session.total_simulated == Decimal("0")
        assert session.virtual_balance == Decimal("1000")

    def test_add_charge(self):
        """Adding a charge updates total_simulated and virtual_balance."""
        session = DryRunSession(
            session_id="test-123",
            wallet_id="wallet-456",
            real_balance=Decimal("1000"),
        )

        charge = SimulatedCharge(
            charge_id="ch-1",
            service_category="content_factory",
            units=10.0,
            credits=Decimal("500"),
            description="Test charge",
            timestamp=datetime.now(timezone.utc),
        )

        session.add_charge(charge)

        assert len(session.simulated_charges) == 1
        assert session.total_simulated == Decimal("500")
        assert session.virtual_balance == Decimal("500")

    def test_multiple_charges_accumulate(self):
        """Multiple charges accumulate correctly."""
        session = DryRunSession(
            session_id="test-123",
            wallet_id="wallet-456",
            real_balance=Decimal("1000"),
        )

        for i in range(3):
            session.add_charge(
                SimulatedCharge(
                    charge_id=f"ch-{i}",
                    service_category="content_factory",
                    units=10.0,
                    credits=Decimal("100"),
                    description=f"Charge {i}",
                    timestamp=datetime.now(timezone.utc),
                )
            )

        assert session.total_simulated == Decimal("300")
        assert session.virtual_balance == Decimal("700")


class TestGlobalSingleton:
    """Test global singleton getter."""

    def test_get_shadow_ledger(self):
        """get_shadow_ledger returns a ShadowLedger instance."""
        ledger = get_shadow_ledger()
        assert ledger is not None
        assert isinstance(ledger, ShadowLedger)


class TestBillingRouterDryRun:
    """Test dry-run endpoints in billing router."""

    @pytest.fixture
    async def client(self):
        from httpx import AsyncClient, ASGITransport
        from app.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c

    @pytest.mark.anyio
    async def test_create_dry_run_session(self, client):
        """POST /v1/billing/dry-run/session creates a session."""
        wallet_resp = await client.post(
            "/v1/billing/wallets/sponsor",
            json={"sponsor_name": "Test Sponsor", "email": "test@example.com"},
            headers={"X-API-Key": "test-key"},
        )
        wallet_id = wallet_resp.json()["wallet_id"]

        response = await client.post(
            "/v1/billing/dry-run/session",
            json={"wallet_id": wallet_id},
            headers={"X-API-Key": "test-key"},
        )

        assert response.status_code == 201
        data = response.json()
        assert "session_id" in data
        assert data["wallet_id"] == wallet_id
        assert data["real_balance"] == 0.0
        assert data["virtual_balance"] == 0.0

    @pytest.mark.anyio
    async def test_simulate_charge(self, client):
        """POST /v1/billing/dry-run/charge simulates a charge."""
        wallet_resp = await client.post(
            "/v1/billing/wallets/sponsor",
            json={
                "sponsor_name": "Test Sponsor",
                "email": "test@example.com",
                "initial_credits": 10000,
            },
            headers={"X-API-Key": "test-key"},
        )
        wallet_id = wallet_resp.json()["wallet_id"]

        response = await client.post(
            "/v1/billing/dry-run/charge",
            json={"wallet_id": wallet_id, "service": "content_factory", "units": 10.0},
            headers={"X-API-Key": "test-key"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["dry_run"] is True
        assert data["wallet_id"] == wallet_id
        assert data["service_category"] == "content_factory"
        assert data["units"] == 10.0
        assert data["credits_would_charge"] == 500.0

    @pytest.mark.anyio
    async def test_simulate_charge_exact_field_preserves_decimal_precision(
        self, client
    ):
        """credits_would_charge_exact must come from the real Decimal math,
        not from round-tripping through a float first (which loses precision
        for values beyond float64's ~15-17 significant digits). This exercises
        the session-based dry-run path (ShadowLedger.simulate_charge)."""
        wallet_resp = await client.post(
            "/v1/billing/wallets/sponsor",
            json={
                "sponsor_name": "Precision Sponsor",
                "email": "precision@example.com",
                "initial_credits": 10000000,
            },
            headers={"X-API-Key": "test-key"},
        )
        wallet_id = wallet_resp.json()["wallet_id"]

        session_resp = await client.post(
            "/v1/billing/dry-run/session",
            json={"wallet_id": wallet_id},
            headers={"X-API-Key": "test-key"},
        )
        session_id = session_resp.json()["session_id"]

        # red_team is priced at 100.0 credits/unit; this specific units value
        # is chosen so units * 100.0 has more significant digits than a
        # float64 round-trip preserves (verified against the actual formula
        # shadow_ledger.py uses: Decimal(str(units)) * credits_per_unit).
        units = 32383.276483316236
        expected_exact = Decimal(str(units)) * Decimal("100.0")
        # Sanity check this really is an adversarial value in this test env,
        # so the test can't silently pass for the wrong reason.
        assert Decimal(str(float(expected_exact))) != expected_exact

        response = await client.post(
            "/v1/billing/dry-run/charge",
            json={
                "wallet_id": wallet_id,
                "service": "red_team",
                "units": units,
                "dry_run_session_id": session_id,
            },
            headers={"X-API-Key": "test-key"},
        )

        assert response.status_code == 200
        data = response.json()
        assert Decimal(data["credits_would_charge_exact"]) == expected_exact

    @pytest.mark.anyio
    async def test_end_dry_run_session(self, client):
        """DELETE /v1/billing/dry-run/session/{id} ends session."""
        wallet_resp = await client.post(
            "/v1/billing/wallets/sponsor",
            json={
                "sponsor_name": "Test Sponsor",
                "email": "test@example.com",
                "initial_credits": 10000,
            },
            headers={"X-API-Key": "test-key"},
        )
        wallet_id = wallet_resp.json()["wallet_id"]

        create_resp = await client.post(
            "/v1/billing/dry-run/session",
            json={"wallet_id": wallet_id},
            headers={"X-API-Key": "test-key"},
        )
        session_id = create_resp.json()["session_id"]

        await client.post(
            "/v1/billing/dry-run/charge",
            json={
                "wallet_id": wallet_id,
                "service": "iot_bridge",
                "units": 1.0,
                "dry_run_session_id": session_id,
            },
            headers={"X-API-Key": "test-key"},
        )

        end_resp = await client.delete(
            f"/v1/billing/dry-run/session/{session_id}",
            headers={"X-API-Key": "test-key"},
        )

        assert end_resp.status_code == 200
        data = end_resp.json()
        assert data["session_id"] == session_id
        assert data["charge_count"] == 1
        assert data["total_simulated_credits"] == 2.0

    @pytest.mark.anyio
    async def test_commit_dry_run_session(self, client):
        """POST /v1/billing/dry-run/session/{id}/commit commits charges to billing."""
        wallet_resp = await client.post(
            "/v1/billing/wallets/sponsor",
            json={
                "sponsor_name": "Commit Test",
                "email": "commit@test.com",
                "initial_credits": 10000,
            },
            headers={"X-API-Key": "test-key"},
        )
        wallet_id = wallet_resp.json()["wallet_id"]

        create_resp = await client.post(
            "/v1/billing/dry-run/session",
            json={"wallet_id": wallet_id},
            headers={"X-API-Key": "test-key"},
        )
        session_id = create_resp.json()["session_id"]

        charge_resp = await client.post(
            "/v1/billing/dry-run/charge",
            json={
                "wallet_id": wallet_id,
                "service": "iot_bridge",
                "units": 5.0,
                "dry_run_session_id": session_id,
            },
            headers={"X-API-Key": "test-key"},
        )
        assert charge_resp.status_code == 200

        commit_resp = await client.post(
            f"/v1/billing/dry-run/session/{session_id}/commit",
            headers={"X-API-Key": "test-key"},
        )
        assert commit_resp.status_code == 200
        data = commit_resp.json()
        assert data["session_id"] == session_id
        assert data["wallet_id"] == wallet_id
        assert data["committed_charges"] == 1
        assert data["total_credits_deducted"] == 10.0
        assert data["success"] is True

    @pytest.mark.anyio
    async def test_commit_reports_failure_when_real_balance_drained_before_commit(
        self, client
    ):
        """If the real wallet balance drops below a charge's cost between
        simulate and commit (e.g. another real charge lands first), the
        commit must report that charge as not committed instead of silently
        counting it as a successful debit."""
        wallet_resp = await client.post(
            "/v1/billing/wallets/sponsor",
            json={
                "sponsor_name": "Drained Commit Test",
                "email": "drained-commit@test.com",
                "initial_credits": 10,
            },
            headers={"X-API-Key": "test-key"},
        )
        wallet_id = wallet_resp.json()["wallet_id"]

        create_resp = await client.post(
            "/v1/billing/dry-run/session",
            json={"wallet_id": wallet_id},
            headers={"X-API-Key": "test-key"},
        )
        session_id = create_resp.json()["session_id"]

        # Simulated against a virtual balance of 10 credits: 1 unit of
        # iot_bridge costs 2 credits, so this is simulated as succeeding.
        charge_resp = await client.post(
            "/v1/billing/dry-run/charge",
            json={
                "wallet_id": wallet_id,
                "service": "iot_bridge",
                "units": 1.0,
                "dry_run_session_id": session_id,
            },
            headers={"X-API-Key": "test-key"},
        )
        assert charge_resp.status_code == 200
        assert charge_resp.json()["would_succeed"] is True

        # Drain the real wallet down to 1 credit with an unrelated real
        # charge before the dry-run session is committed.
        drain_resp = await client.post(
            f"/v1/billing/charge?wallet_id={wallet_id}&service=iot_bridge&units=4.5",
            headers={"X-API-Key": "test-key"},
        )
        assert drain_resp.status_code == 200
        assert drain_resp.json()["balance_after"] == 1.0

        commit_resp = await client.post(
            f"/v1/billing/dry-run/session/{session_id}/commit",
            headers={"X-API-Key": "test-key"},
        )
        assert commit_resp.status_code == 200
        data = commit_resp.json()
        assert data["committed_charges"] == 0
        assert data["total_credits_deducted"] == 0.0
        assert data["success"] is False
        assert data["ledger_entries"][0]["committed"] is False
        assert data["ledger_entries"][0]["error"] == "insufficient_funds"

        wallet_resp = await client.get(
            f"/v1/billing/wallets/{wallet_id}", headers={"X-API-Key": "test-key"}
        )
        assert wallet_resp.json()["balance"] == 1.0

    @pytest.mark.anyio
    async def test_revert_dry_run_session(self, client):
        """POST /v1/billing/dry-run/session/{id}/revert discards charges."""
        wallet_resp = await client.post(
            "/v1/billing/wallets/sponsor",
            json={
                "sponsor_name": "Revert Test",
                "email": "revert@test.com",
                "initial_credits": 10000,
            },
            headers={"X-API-Key": "test-key"},
        )
        wallet_id = wallet_resp.json()["wallet_id"]

        create_resp = await client.post(
            "/v1/billing/dry-run/session",
            json={"wallet_id": wallet_id},
            headers={"X-API-Key": "test-key"},
        )
        session_id = create_resp.json()["session_id"]

        charge_resp = await client.post(
            "/v1/billing/dry-run/charge",
            json={
                "wallet_id": wallet_id,
                "service": "iot_bridge",
                "units": 5.0,
                "dry_run_session_id": session_id,
            },
            headers={"X-API-Key": "test-key"},
        )
        assert charge_resp.status_code == 200

        revert_resp = await client.post(
            f"/v1/billing/dry-run/session/{session_id}/revert",
            headers={"X-API-Key": "test-key"},
        )
        assert revert_resp.status_code == 200
        data = revert_resp.json()
        assert data["session_id"] == session_id
        assert data["wallet_id"] == wallet_id
        assert data["reverted"] is True

    @pytest.mark.anyio
    async def test_commit_nonexistent_session_returns_404(self, client):
        """Committing non-existent session returns 404."""
        resp = await client.post(
            "/v1/billing/dry-run/session/nonexistent-session/commit",
            headers={"X-API-Key": "test-key"},
        )
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_revert_nonexistent_session_returns_404(self, client):
        """Reverting non-existent session returns 404."""
        resp = await client.post(
            "/v1/billing/dry-run/session/nonexistent-session/revert",
            headers={"X-API-Key": "test-key"},
        )
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_commit_empty_session(self, client):
        """Committing session with no charges succeeds with message."""
        wallet_resp = await client.post(
            "/v1/billing/wallets/sponsor",
            json={
                "sponsor_name": "Empty Commit",
                "email": "empty@test.com",
                "initial_credits": 10000,
            },
            headers={"X-API-Key": "test-key"},
        )
        wallet_id = wallet_resp.json()["wallet_id"]

        create_resp = await client.post(
            "/v1/billing/dry-run/session",
            json={"wallet_id": wallet_id},
            headers={"X-API-Key": "test-key"},
        )
        session_id = create_resp.json()["session_id"]

        commit_resp = await client.post(
            f"/v1/billing/dry-run/session/{session_id}/commit",
            headers={"X-API-Key": "test-key"},
        )
        assert commit_resp.status_code == 200
        data = commit_resp.json()
        assert data["committed_charges"] == 0
        assert data["success"] is True
        assert "No charges to commit" in data["message"]
