"""Focused unit tests for app/db/models.py.

Covers the main public behavior of the SQLModel table definitions:
defaults, required fields, table registration, and a SQLite in-memory
persistence round trip. No network or external services.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

import app.db.models as models
from app.db.models import (
    APIKeyModel,
    BillingAlertModel,
    DailyBalanceSnapshot,
    LedgerEntryModel,
    TelemetryEventModel,
    WalletModel,
)


@pytest.fixture
def memory_session():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def test_wallet_defaults():
    wallet = WalletModel(wallet_id="w1", wallet_type="sponsor")
    assert wallet.balance == Decimal("0")
    assert wallet.lifetime_credits == Decimal("0")
    assert wallet.lifetime_debits == Decimal("0")
    assert wallet.status == "active"
    assert wallet.kyc_status == "not_required"
    assert wallet.auto_refill is False
    assert wallet.owner_name is None
    assert wallet.parent_wallet_id is None
    assert wallet.velocity_alerts_triggered == 0


def test_wallet_timestamps_are_naive_utc():
    wallet = WalletModel(wallet_id="w1", wallet_type="sponsor")
    for value in (wallet.created_at, wallet.updated_at):
        assert isinstance(value, datetime)
        assert value.tzinfo is None


def test_ledger_entry_defaults():
    entry = LedgerEntryModel(
        entry_id="e1",
        wallet_id="w1",
        action="credit",
        amount=Decimal("10.5"),
        balance_after=Decimal("10.5"),
    )
    assert entry.description == ""
    assert entry.operation_key is None
    assert entry.payment_intent_id is None
    assert entry.stripe_event_id is None
    assert entry.timestamp.tzinfo is None


def test_billing_alert_defaults():
    alert = BillingAlertModel(
        alert_id="a1", wallet_id="w1", alert_type="low_balance", message="low"
    )
    assert alert.severity == "info"
    assert alert.acknowledged is False
    assert alert.threshold_amount is None
    assert alert.current_balance is None


def test_api_key_bounded_use_defaults():
    key = APIKeyModel(key_id="k1", wallet_id="w1", key_hash="h" * 64, key_prefix="abc")
    assert key.status == "active"
    assert key.max_uses is None
    assert key.use_count == 0
    assert key.rotation_count == 0


def test_telemetry_event_defaults():
    event = TelemetryEventModel(
        event_id="ev1",
        batch_id="b1",
        event_type="log",
        severity="info",
        source="test",
    )
    assert event.message == ""
    assert event.payload_json is None
    assert event.event_timestamp is None
    assert event.ingested_at.tzinfo is None


def test_daily_balance_snapshot_defaults():
    snapshot = DailyBalanceSnapshot(
        snapshot_id="s1",
        wallet_id="w1",
        date=datetime(2026, 1, 1),
        opening_balance=Decimal("5"),
        closing_balance=Decimal("7"),
    )
    assert snapshot.total_credits == Decimal("0")
    assert snapshot.total_debits == Decimal("0")
    assert snapshot.transaction_count == 0


def test_wallet_missing_required_fields_rejected_on_commit(memory_session):
    # SQLModel table models accept missing fields as None at construction;
    # the NOT NULL constraint is enforced by the database on flush.
    wallet = WalletModel()
    assert wallet.wallet_id is None
    assert wallet.wallet_type is None
    memory_session.add(wallet)
    with pytest.raises(IntegrityError):
        memory_session.commit()


def test_ledger_entry_missing_amount_rejected_on_commit(memory_session):
    entry = LedgerEntryModel(
        entry_id="e1",
        wallet_id="w1",
        action="credit",
        balance_after=Decimal("1"),
    )
    assert entry.amount is None
    memory_session.add(entry)
    with pytest.raises(IntegrityError):
        memory_session.commit()


def test_main_tables_registered():
    tables = SQLModel.metadata.tables
    for name in (
        "wallets",
        "ledger_entries",
        "billing_alerts",
        "daily_balance_snapshots",
        "api_keys",
    ):
        assert name in tables


def test_ledger_unique_constraint_registered():
    table = SQLModel.metadata.tables["ledger_entries"]
    names = {c.name for c in table.constraints if c.name is not None}
    assert "uq_ledger_wallet_operation_key" in names


def test_persistence_round_trip(memory_session):
    wallet = WalletModel(
        wallet_id="w1", wallet_type="sponsor", balance=Decimal("100.25")
    )
    memory_session.add(wallet)
    memory_session.commit()

    entry = LedgerEntryModel(
        entry_id="e1",
        wallet_id="w1",
        action="debit",
        amount=Decimal("-10.5"),
        balance_after=Decimal("89.75"),
    )
    memory_session.add(entry)
    memory_session.commit()

    loaded = memory_session.get(WalletModel, "w1")
    assert loaded is not None
    assert loaded.balance == Decimal("100.25")
    rows = memory_session.exec(
        select(LedgerEntryModel).where(LedgerEntryModel.wallet_id == "w1")
    ).all()
    assert len(rows) == 1
    assert rows[0].amount == Decimal("-10.5")


def test_negative_wallet_balance_allowed_for_unfunded_liability(
    memory_session,
):
    wallet = WalletModel(
        wallet_id="w-neg", wallet_type="sponsor", balance=Decimal("-25.5")
    )
    memory_session.add(wallet)
    memory_session.commit()
    loaded = memory_session.get(WalletModel, "w-neg")
    assert loaded is not None
    assert loaded.balance == Decimal("-25.5")


def test_duplicate_wallet_id_rejected(memory_session):
    memory_session.add(WalletModel(wallet_id="w-dup", wallet_type="sponsor"))
    memory_session.commit()
    memory_session.add(WalletModel(wallet_id="w-dup", wallet_type="sponsor"))
    with pytest.raises(IntegrityError):
        memory_session.commit()


def test_all_model_classes_exposed():
    for name in (
        "WalletModel",
        "LedgerEntryModel",
        "BillingAlertModel",
        "DailyBalanceSnapshot",
        "ServiceRegistryModel",
        "KYCVerificationModel",
        "APIKeyModel",
        "TelemetryEventModel",
    ):
        assert hasattr(models, name)
