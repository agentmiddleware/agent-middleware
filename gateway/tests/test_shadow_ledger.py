"""Focused unit tests for app.services.shadow_ledger.

Covers the main public behavior of ShadowLedger through its in-memory
fallback path (empty Redis URL, so no network or external services), plus
the pure DryRunSession balance math. Fast by design: no database, no app.
"""

from decimal import Decimal
from types import SimpleNamespace

from app.schemas.billing import ServiceCategory
from app.services.pricing import DEFAULT_PRICING
from app.services.shadow_ledger import (
    DryRunSession,
    ShadowLedger,
    get_shadow_ledger,
)


def make_ledger() -> ShadowLedger:
    """Build a ShadowLedger pinned to its in-memory store.

    An empty ``_redis_url`` makes ``_get_redis`` return None immediately,
    so every operation below uses the plain-dict fallback and never touches
    the network.
    """
    ledger = ShadowLedger()
    ledger._redis = None
    ledger._redis_url = ""
    return ledger


async def make_session(ledger=None, wallet_id="w-1", balance=Decimal("100")):
    ledger = ledger or make_ledger()
    session = await ledger.create_session(wallet_id=wallet_id, real_balance=balance)
    return ledger, session


def test_dry_run_session_balance_math():
    session = DryRunSession(session_id="s", wallet_id="w", real_balance=Decimal("100"))
    assert session.total_simulated == Decimal("0")
    assert session.virtual_balance == Decimal("100")


async def test_create_session_returns_unique_ids_with_real_balance():
    ledger = make_ledger()
    first = await ledger.create_session("w-1", Decimal("100"))
    second = await ledger.create_session("w-1", Decimal("100"))
    assert first.session_id != second.session_id
    assert first.real_balance == Decimal("100")
    assert first.simulated_charges == []
    assert first.virtual_balance == Decimal("100")


async def test_get_session_round_trip_and_missing_returns_none():
    ledger, session = await make_session()
    found = await ledger.get_session(session.session_id)
    assert found is not None
    assert found.session_id == session.session_id
    assert found.wallet_id == "w-1"
    assert await ledger.get_session("no-such-session") is None


async def test_simulate_charge_success_reduces_virtual_balance():
    ledger, session = await make_session(balance=Decimal("100"))
    price = DEFAULT_PRICING[ServiceCategory.TELEMETRY_PM][1]
    result = await ledger.simulate_charge(
        session.session_id, ServiceCategory.TELEMETRY_PM, units=3.0
    )
    assert result.dry_run is True
    assert result.would_succeed is True
    assert result.credits_would_charge == Decimal(str(3.0)) * price
    assert result.simulated_balance_before == Decimal("100")
    assert (
        result.simulated_balance_after == Decimal("100") - result.credits_would_charge
    )
    assert isinstance(result.simulated_balance_after, Decimal)
    stored = await ledger.get_session(session.session_id)
    assert len(stored.simulated_charges) == 1
    assert stored.virtual_balance == result.simulated_balance_after


async def test_simulate_charge_records_charges_cumulatively():
    ledger, session = await make_session(balance=Decimal("1000"))
    await ledger.simulate_charge(
        session.session_id, ServiceCategory.TELEMETRY_PM, units=2.0
    )
    result = await ledger.simulate_charge(
        session.session_id, ServiceCategory.TELEMETRY_PM, units=3.0
    )
    stored = await ledger.get_session(session.session_id)
    assert len(stored.simulated_charges) == 2
    assert stored.total_simulated == Decimal("100") - result.simulated_balance_after


async def test_simulate_charge_insufficient_funds_records_nothing():
    ledger, session = await make_session(balance=Decimal("2"))
    result = await ledger.simulate_charge(
        session.session_id, ServiceCategory.RED_TEAM, units=1.0
    )
    assert result.would_succeed is False
    assert result.reason == "insufficient_simulated_funds"
    stored = await ledger.get_session(session.session_id)
    assert stored.simulated_charges == []
    assert stored.virtual_balance == Decimal("2")


async def test_simulate_charge_blocked_reason_is_preserved():
    ledger, session = await make_session()
    result = await ledger.simulate_charge(
        session.session_id,
        ServiceCategory.TELEMETRY_PM,
        units=1.0,
        blocked_reason="velocity_blocked",
    )
    assert result.would_succeed is False
    assert result.reason == "velocity_blocked"
    stored = await ledger.get_session(session.session_id)
    assert stored.simulated_charges == []


async def test_simulate_charge_unknown_session_raises():
    ledger = make_ledger()
    try:
        await ledger.simulate_charge("missing", ServiceCategory.TELEMETRY_PM, units=1.0)
    except ValueError as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("expected ValueError for unknown session")


async def test_end_session_returns_summary_and_clears_state():
    ledger, session = await make_session(balance=Decimal("100"))
    await ledger.simulate_charge(
        session.session_id, ServiceCategory.TELEMETRY_PM, units=4.0
    )
    summary = await ledger.end_session(session.session_id)
    assert summary is not None
    assert summary.wallet_id == "w-1"
    assert summary.charge_count == 1
    assert summary.total_simulated_credits == Decimal("4.0")
    assert summary.real_balance == Decimal("100")
    assert summary.virtual_balance_after == Decimal("96.0")
    assert await ledger.get_session(session.session_id) is None
    assert await ledger.end_session(session.session_id) is None


class _FakeMoney:
    """Minimal stand-in for AgentMoney: records charges, reports a balance."""

    def __init__(self, balance=Decimal("90")):
        self.charges = []
        self.balance = balance
        self.fail_with = None

    async def charge(self, wallet_id, service_category, units, **kwargs):
        self.charges.append(
            {"wallet_id": wallet_id, "category": service_category, "units": units}
        )
        if self.fail_with is not None:
            raise self.fail_with
        return SimpleNamespace(ok=True)

    async def get_wallet(self, wallet_id):
        return SimpleNamespace(balance=self.balance)


async def test_commit_session_applies_charges_and_claims_session():
    ledger, session = await make_session(balance=Decimal("100"))
    await ledger.simulate_charge(
        session.session_id, ServiceCategory.TELEMETRY_PM, units=2.0
    )
    money = _FakeMoney(balance=Decimal("98"))
    result = await ledger.commit_session(session.session_id, money)
    assert result.success is True
    assert result.committed_charges == 1
    assert result.total_credits_deducted == Decimal("2.0")
    assert result.real_balance_before == Decimal("100")
    assert result.real_balance_after == Decimal("98")
    assert len(money.charges) == 1
    assert result.ledger_entries[0]["committed"] is True
    retry = await ledger.commit_session(session.session_id, money)
    assert retry.success is False
    assert retry.message == "Session not found"
    assert len(money.charges) == 1


async def test_commit_session_with_no_charges_is_a_noop_success():
    ledger, session = await make_session()
    result = await ledger.commit_session(session.session_id, _FakeMoney())
    assert result.success is True
    assert result.message == "No charges to commit"
    assert result.committed_charges == 0


async def test_commit_session_unknown_session_reports_not_found():
    result = await make_ledger().commit_session("missing", _FakeMoney())
    assert result.success is False
    assert result.message == "Session not found"


async def test_commit_session_charge_error_is_recorded_not_raised():
    ledger, session = await make_session()
    await ledger.simulate_charge(
        session.session_id, ServiceCategory.TELEMETRY_PM, units=1.0
    )
    money = _FakeMoney()
    money.fail_with = RuntimeError("wallet store down")
    result = await ledger.commit_session(session.session_id, money)
    assert result.success is False
    assert result.committed_charges == 0
    assert result.ledger_entries[0]["committed"] is False
    assert "wallet store down" in result.ledger_entries[0]["error"]


async def test_revert_session_discards_charges_without_side_effects():
    ledger, session = await make_session(balance=Decimal("100"))
    await ledger.simulate_charge(
        session.session_id, ServiceCategory.TELEMETRY_PM, units=5.0
    )
    money = _FakeMoney()
    result = await ledger.revert_session(session.session_id)
    assert result.reverted is True
    assert result.wallet_id == "w-1"
    assert "No changes made to real wallet" in result.message
    assert money.charges == []
    assert await ledger.get_session(session.session_id) is None


async def test_revert_session_unknown_session_reports_not_found():
    result = await make_ledger().revert_session("missing")
    assert result.reverted is False
    assert result.message == "Session not found"


async def test_list_sessions_filters_by_wallet():
    ledger = make_ledger()
    first = await ledger.create_session("w-1", Decimal("10"))
    await ledger.create_session("w-2", Decimal("20"))
    sessions = await ledger.list_sessions("w-1")
    assert [s.session_id for s in sessions] == [first.session_id]


async def test_close_is_safe_without_redis():
    ledger = make_ledger()
    await ledger.close()


def test_get_shadow_ledger_returns_shared_instance():
    assert get_shadow_ledger() is get_shadow_ledger()
