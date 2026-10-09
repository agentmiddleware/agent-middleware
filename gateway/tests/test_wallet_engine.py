"""Focused coverage for app/services/wallet_engine.py.

Exercises the wallet lifecycle engine directly (creation, provisioning,
transfers, child delegation and reclaim, reads, and TTL helpers) including
edge and error cases. Runs against the test database via the existing
``clean_database`` fixture; no network or external services.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.time import utc_now
from app.db.models import WalletModel
from app.schemas.billing import WalletStatus, WalletType
from app.services.agent_money import (
    InsufficientFundsError,
    WalletNotFoundError,
    get_agent_money,
)
from app.services.wallet_engine import (
    MAX_CHILD_WALLET_TTL_SECONDS,
    WalletExpiredError,
    _validate_child_wallet_ttl,
    child_wallet_expires_at,
)


def _engine():
    return get_agent_money()._wallet_engine


async def _sponsor(balance: Decimal = Decimal("100")):
    return await _engine().create_sponsor_wallet(
        "Engine probe", "engine@example.test", balance
    )


async def test_sponsor_creation_and_reads(clean_database):
    engine = _engine()
    sponsor = await engine.create_sponsor_wallet(
        "Ada", "ada@example.test", Decimal("25")
    )
    assert sponsor.balance == Decimal("25")
    assert sponsor.wallet_type == WalletType.SPONSOR

    fetched = await engine.get_wallet(sponsor.wallet_id)
    assert fetched is not None
    assert fetched.wallet_id == sponsor.wallet_id

    assert await engine.get_wallet("spn-missing") is None

    wallets = await engine.list_wallets()
    assert sponsor.wallet_id in {w.wallet_id for w in wallets}
    sponsors = await engine.list_wallets(wallet_type=WalletType.SPONSOR.value)
    assert sponsor.wallet_id in {w.wallet_id for w in sponsors}
    agents = await engine.list_wallets(wallet_type=WalletType.AGENT.value)
    assert all(w.wallet_type == WalletType.AGENT for w in agents)


async def test_agent_provisioning_deducts_sponsor(clean_database):
    engine = _engine()
    sponsor = await _sponsor(Decimal("100"))
    agent = await engine.create_agent_wallet(sponsor.wallet_id, "a1", Decimal("40"))

    assert agent.balance == Decimal("40")
    refreshed = await engine.get_wallet(sponsor.wallet_id)
    assert refreshed is not None
    assert refreshed.balance == Decimal("60")


async def test_agent_provisioning_negative_budget_rejected(clean_database):
    engine = _engine()
    sponsor = await _sponsor()
    with pytest.raises(ValueError, match="cannot be negative"):
        await engine.create_agent_wallet(sponsor.wallet_id, "a1", Decimal("-1"))


async def test_agent_provisioning_insufficient_funds(clean_database):
    engine = _engine()
    sponsor = await _sponsor(Decimal("10"))
    with pytest.raises(InsufficientFundsError):
        await engine.create_agent_wallet(sponsor.wallet_id, "a1", Decimal("50"))


async def test_agent_provisioning_unknown_sponsor(clean_database):
    with pytest.raises(WalletNotFoundError):
        await _engine().create_agent_wallet("spn-missing", "a1", Decimal("5"))


async def test_transfer_moves_credits(clean_database):
    engine = _engine()
    sponsor = await _sponsor(Decimal("100"))
    src = await engine.create_agent_wallet(sponsor.wallet_id, "src", Decimal("50"))
    dst = await engine.create_agent_wallet(sponsor.wallet_id, "dst", Decimal("10"))

    receipt = await engine.transfer(src.wallet_id, dst.wallet_id, Decimal("20"))
    assert receipt["status"] == "completed"
    assert receipt["from_balance_after"] == Decimal("30")
    assert receipt["to_balance_after"] == Decimal("30")


async def test_transfer_rejects_bad_amount_and_same_wallet(clean_database):
    engine = _engine()
    sponsor = await _sponsor(Decimal("100"))
    src = await engine.create_agent_wallet(sponsor.wallet_id, "src", Decimal("50"))
    dst = await engine.create_agent_wallet(sponsor.wallet_id, "dst", Decimal("10"))

    with pytest.raises(ValueError, match="positive"):
        await engine.transfer(src.wallet_id, dst.wallet_id, Decimal("0"))
    with pytest.raises(ValueError, match="same wallet"):
        await engine.transfer(src.wallet_id, src.wallet_id, Decimal("5"))


async def test_transfer_insufficient_funds(clean_database):
    engine = _engine()
    sponsor = await _sponsor(Decimal("100"))
    src = await engine.create_agent_wallet(sponsor.wallet_id, "src", Decimal("5"))
    dst = await engine.create_agent_wallet(sponsor.wallet_id, "dst", Decimal("5"))
    with pytest.raises(InsufficientFundsError):
        await engine.transfer(src.wallet_id, dst.wallet_id, Decimal("50"))


async def test_child_lifecycle_delegate_and_reclaim(clean_database):
    engine = _engine()
    sponsor = await _sponsor(Decimal("100"))
    parent = await engine.create_agent_wallet(sponsor.wallet_id, "p", Decimal("60"))

    child = await engine.create_child_wallet(
        parent.wallet_id, "c1", Decimal("20"), Decimal("20")
    )
    assert child.balance == Decimal("20")

    budget = await engine.get_swarm_budget(parent.wallet_id)
    assert budget["active_children"] == 1
    assert budget["total_delegated"] == Decimal("20")

    result = await engine.reclaim_child_wallet(child.wallet_id)
    assert result["credits_reclaimed"] == Decimal("20")
    assert result["child_status"] == WalletStatus.CLOSED
    assert result["parent_balance_after"] == Decimal("60")

    with pytest.raises(ValueError, match="already closed"):
        await engine.reclaim_child_wallet(child.wallet_id)


async def test_child_ttl_validation_rejected(clean_database):
    engine = _engine()
    sponsor = await _sponsor(Decimal("100"))
    parent = await engine.create_agent_wallet(sponsor.wallet_id, "p", Decimal("60"))

    with pytest.raises(ValueError, match="ttl_seconds"):
        await engine.create_child_wallet(
            parent.wallet_id, "c1", Decimal("5"), Decimal("50"), ttl_seconds=0
        )
    with pytest.raises(ValueError, match="ttl_seconds"):
        await engine.create_child_wallet(
            parent.wallet_id,
            "c1",
            Decimal("5"),
            Decimal("50"),
            ttl_seconds=MAX_CHILD_WALLET_TTL_SECONDS + 1,
        )
    with pytest.raises(ValueError, match="cannot be negative"):
        await engine.create_child_wallet(
            parent.wallet_id, "c1", Decimal("-5"), Decimal("50")
        )


async def test_reclaim_non_child_rejected(clean_database):
    engine = _engine()
    sponsor = await _sponsor(Decimal("100"))
    agent = await engine.create_agent_wallet(sponsor.wallet_id, "a", Decimal("10"))
    with pytest.raises(ValueError, match="Can only reclaim from child wallets"):
        await engine.reclaim_child_wallet(agent.wallet_id)
    with pytest.raises(ValueError, match="no parent"):
        await engine.reclaim_child_wallet(sponsor.wallet_id)


def test_child_wallet_expiry_helper():
    sponsor_row = WalletModel(
        wallet_id="spn-x",
        wallet_type=WalletType.SPONSOR.value,
        balance=Decimal("0"),
    )
    assert child_wallet_expires_at(sponsor_row) is None

    now = utc_now()
    child_row = WalletModel(
        wallet_id="chd-x",
        wallet_type=WalletType.CHILD.value,
        balance=Decimal("0"),
        created_at=now,
        ttl_seconds=3600,
    )
    assert child_wallet_expires_at(child_row) == now + timedelta(seconds=3600)


def test_validate_child_wallet_ttl_bounds():
    _validate_child_wallet_ttl(None)
    _validate_child_wallet_ttl(60)
    with pytest.raises(ValueError, match="ttl_seconds"):
        _validate_child_wallet_ttl(0)
    with pytest.raises(ValueError, match="ttl_seconds"):
        _validate_child_wallet_ttl(-5)
    with pytest.raises(ValueError, match="ttl_seconds"):
        _validate_child_wallet_ttl(MAX_CHILD_WALLET_TTL_SECONDS + 1)


async def test_daily_spend_unknown_wallet_is_zero(clean_database):
    assert await _engine().get_daily_spend("agt-missing") == Decimal("0")


async def test_wallet_is_expired_false_for_fresh_wallet(clean_database):
    engine = _engine()
    sponsor = await _sponsor(Decimal("100"))
    assert await engine.wallet_is_expired(sponsor.wallet_id) is False
    assert await engine.wallet_is_expired("spn-missing") is False


async def test_expired_error_message_carries_wallet_id():
    err = WalletExpiredError("chd-x", utc_now())
    assert "chd-x" in str(err)
