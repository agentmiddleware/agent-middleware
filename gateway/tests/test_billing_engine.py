"""Focused unit tests for app.services.billing_engine.BillingEngine.

Covers the paths that need no database or network: direct top-up refusal,
the pricing table (including proof-surface filtering), the single-shot
dry-run estimate, operation-key match checks, and the timestamp helpers.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.config import get_settings
from app.db.models import LedgerEntryModel
from app.schemas.billing import LedgerAction, ServiceCategory
from app.services.agent_money import COMPUTE_COSTS
from app.services.billing_engine import (
    BillingEngine,
    DirectTopUpDisabledError,
    LedgerOperationConflictError,
    _as_utc,
    _timestamp_in_current_period,
)
from app.services.pricing import DEFAULT_PRICING, PROOF_SURFACE_CATEGORIES


class _WalletNotFoundError(LookupError):
    def __init__(self, wallet_id: str):
        self.wallet_id = wallet_id
        super().__init__(f"Wallet not found: {wallet_id}")


def _make_engine(*, wallets=None, expired=False):
    """Build a BillingEngine with stub collaborators (no DB, no network)."""
    wallets = wallets or {}

    async def get_wallet(wallet_id: str):
        return wallets.get(wallet_id)

    async def wallet_is_expired(wallet_id: str):
        return expired

    wallet_engine = SimpleNamespace(
        get_wallet=get_wallet,
        wallet_is_expired=wallet_is_expired,
    )

    def session_factory():
        raise AssertionError("this test must not touch the database")

    return BillingEngine(
        session_factory=session_factory,
        settings=get_settings(),
        wallet_engine=wallet_engine,
        default_pricing=dict(DEFAULT_PRICING),
        compute_costs=dict(COMPUTE_COSTS),
        wallet_not_found_error=_WalletNotFoundError,
        kyc_required_error=lambda wallet_id, status: RuntimeError(status),
    )


def _wallet(balance: str):
    return SimpleNamespace(balance=float(balance), balance_exact=balance)


# --- Direct top-up refusal ---


async def test_top_up_always_refused():
    engine = _make_engine()
    with pytest.raises(DirectTopUpDisabledError, match="direct_top_up_disabled"):
        await engine.top_up("spn-123", Decimal("10"))


# --- Pricing table ---


def test_pricing_table_exact_strings_match_prices():
    engine = _make_engine()
    table = engine.get_pricing_table()
    assert table, "pricing table must not be empty"
    for entry in table:
        expected = DEFAULT_PRICING[entry.service_category][1]
        assert Decimal(entry.credits_per_unit_exact) == expected
        assert entry.credits_per_unit == float(expected)


def test_pricing_table_hides_proof_surfaces_when_disabled(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ENABLE_PROOF_SURFACES", False)
    engine = _make_engine()
    categories = {entry.service_category for entry in engine.get_pricing_table()}
    assert ServiceCategory.PLATFORM_FEE in categories
    assert not (categories & set(PROOF_SURFACE_CATEGORIES))


def test_pricing_table_shows_everything_when_enabled(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ENABLE_PROOF_SURFACES", True)
    engine = _make_engine()
    categories = {entry.service_category for entry in engine.get_pricing_table()}
    assert categories == set(DEFAULT_PRICING)


# --- Single-shot dry-run estimates (session_id=None) ---


async def test_dry_run_succeeds_when_funds_cover_charge():
    engine = _make_engine(wallets={"w-1": _wallet("100")})
    result = await engine._dry_run_charge(
        "w-1", ServiceCategory.PLATFORM_FEE, Decimal("2"), Decimal("10"), "probe", None
    )
    assert result.would_succeed is True
    assert result.reason is None
    assert result.simulated_balance_before == Decimal("100")
    assert result.simulated_balance_after == Decimal("90")
    assert result.dry_run is True


async def test_dry_run_reports_insufficient_simulated_funds():
    engine = _make_engine(wallets={"w-1": _wallet("5")})
    result = await engine._dry_run_charge(
        "w-1", ServiceCategory.PLATFORM_FEE, Decimal("2"), Decimal("10"), "probe", None
    )
    assert result.would_succeed is False
    assert result.reason == "insufficient_simulated_funds"
    assert result.simulated_balance_after == Decimal("-5")


async def test_dry_run_reports_expired_wallet():
    engine = _make_engine(wallets={"w-1": _wallet("100")}, expired=True)
    result = await engine._dry_run_charge(
        "w-1", ServiceCategory.PLATFORM_FEE, Decimal("1"), Decimal("1"), "probe", None
    )
    assert result.would_succeed is False
    assert result.reason == "wallet_expired"


async def test_dry_run_unknown_wallet_raises():
    engine = _make_engine(wallets={})
    with pytest.raises(_WalletNotFoundError):
        await engine._dry_run_charge(
            "w-missing",
            ServiceCategory.PLATFORM_FEE,
            Decimal("1"),
            Decimal("1"),
            "probe",
            None,
        )


# --- Operation-key match checks ---


def _debit_entry(**overrides):
    fields = {
        "entry_id": "le-1",
        "wallet_id": "w-1",
        "operation_key": "op-1",
        "action": LedgerAction.DEBIT.value,
        "amount": Decimal("-10"),
        "balance_after": Decimal("90"),
        "service_category": ServiceCategory.PLATFORM_FEE.value,
        "request_path": "/v1/billing/charge",
    }
    fields.update(overrides)
    return LedgerEntryModel(**fields)


def test_operation_debit_match_accepts_identical_request():
    entry = _debit_entry()
    BillingEngine._assert_operation_debit_matches(
        entry,
        wallet_id="w-1",
        operation_key="op-1",
        service_category=ServiceCategory.PLATFORM_FEE,
        charge_amount=Decimal("10"),
        request_path="/v1/billing/charge",
    )


def test_operation_debit_match_rejects_different_amount():
    entry = _debit_entry()
    with pytest.raises(
        LedgerOperationConflictError, match="ledger_operation_key_reused"
    ):
        BillingEngine._assert_operation_debit_matches(
            entry,
            wallet_id="w-1",
            operation_key="op-1",
            service_category=ServiceCategory.PLATFORM_FEE,
            charge_amount=Decimal("999"),
            request_path="/v1/billing/charge",
        )


# --- Timestamp helpers ---


def test_as_utc_treats_naive_as_utc():
    assert _as_utc(datetime(2026, 1, 1, 12, 0, 0)) == datetime(
        2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc
    )


def test_as_utc_converts_aware_to_utc():
    eastern = timezone(timedelta(hours=-5))
    value = datetime(2026, 1, 1, 7, 0, 0, tzinfo=eastern)
    assert _as_utc(value) == datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_timestamp_in_current_period_edge_cases():
    now = datetime.now(timezone.utc)
    assert _timestamp_in_current_period(now, None) is True
    assert _timestamp_in_current_period(now, now - timedelta(hours=1)) is True
    assert _timestamp_in_current_period(now - timedelta(days=2), now) is False
