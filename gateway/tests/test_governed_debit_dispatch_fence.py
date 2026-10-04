"""Focused regressions for the governed dispatch/debit transaction fence."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.core import health
from app.core.config import get_settings
from app.db.database import get_session_factory
from app.db.models import (
    IdempotencyRecordModel,
    LedgerEntryModel,
    McpDispatchAttemptModel,
    WalletModel,
)
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.agent_money import get_agent_money
from app.services.billing_engine import LedgerOperationConflictError
from app.services.idempotency import GOVERNED_MCP_IDEMPOTENCY_ENDPOINT
from app.services.mcp_dispatch_attempts import (
    dispatch_reconciliation_idle_seconds,
)
from app.services.velocity_monitor import get_velocity_monitor
from tests.test_mcp_dispatch_reconciliation import (
    ENDPOINT,
    _attempt,
    _seed_attempt,
)
from tests.test_trust_helpers import provision_agent_wallet


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as value:
        yield value


async def _wallet_snapshot(wallet_id: str) -> dict[str, object]:
    async with get_session_factory()() as session:
        wallet = await session.get(WalletModel, wallet_id)
        assert wallet is not None
        return {
            name: getattr(wallet, name)
            for name in (
                "balance",
                "lifetime_debits",
                "hourly_spent",
                "daily_spent",
                "last_charge_at",
                "hourly_reset_at",
                "daily_reset_at",
                "velocity_alerts_triggered",
                "status",
            )
        }


async def _operation_debit_count(operation_key: str) -> int:
    async with get_session_factory()() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(LedgerEntryModel)
            .where(
                LedgerEntryModel.operation_key == operation_key,
                LedgerEntryModel.action == "debit",
            )
        )
    return int(count or 0)


async def _set_freeze_boundary(wallet_id: str, *, status: str = "active") -> None:
    async with get_session_factory()() as session:
        async with session.begin():
            wallet = await session.get(WalletModel, wallet_id)
            assert wallet is not None
            wallet.hourly_limit = Decimal("1")
            wallet.daily_limit = Decimal("1000")
            wallet.velocity_alerts_triggered = get_velocity_monitor()._freeze_threshold
            wallet.status = status
            session.add(wallet)


async def _prepared_seed(client: AsyncClient, suffix: str):
    return await _seed_attempt(
        client,
        suffix=suffix,
        state="prepared",
        create_charge=False,
        attach_charge=False,
        idempotency_endpoint=GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
    )


@pytest.mark.anyio
async def test_idempotency_checkpoint_conflict_precedes_wallet_mutation(
    client: AsyncClient,
    clean_database,
) -> None:
    seed = await _prepared_seed(client, "checkpoint-conflict")
    async with get_session_factory()() as session:
        async with session.begin():
            record = await session.get(
                IdempotencyRecordModel,
                (await _attempt(seed.attempt_id)).idempotency_record_id,
            )
            assert record is not None
            record.ledger_entry_id = "tampered-ledger-entry"
            session.add(record)
    before = await _wallet_snapshot(seed.wallet_id)

    with pytest.raises(
        LedgerOperationConflictError,
        match="ledger_operation_checkpoint_conflict",
    ):
        await get_agent_money().charge(
            wallet_id=seed.wallet_id,
            service_category=ServiceCategory.AGENT_COMMS,
            units=Decimal("1"),
            request_path=ENDPOINT,
            operation_key=(await _attempt(seed.attempt_id)).idempotency_record_id,
        )

    assert await _wallet_snapshot(seed.wallet_id) == before
    assert (
        await _operation_debit_count(
            (await _attempt(seed.attempt_id)).idempotency_record_id
        )
        == 0
    )


@pytest.mark.anyio
async def test_claimed_dispatch_blocks_debit_and_velocity_mutation(
    client: AsyncClient,
    clean_database,
) -> None:
    seed = await _prepared_seed(client, "claimed-before-debit")
    attempt = await _attempt(seed.attempt_id)
    async with get_session_factory()() as session:
        async with session.begin():
            stored = await session.get(McpDispatchAttemptModel, seed.attempt_id)
            assert stored is not None
            stored.dispatch_claim_hash = "a" * 64
            session.add(stored)
    before = await _wallet_snapshot(seed.wallet_id)

    with pytest.raises(
        LedgerOperationConflictError,
        match="ledger_operation_dispatch_unavailable",
    ):
        await get_agent_money().charge(
            wallet_id=seed.wallet_id,
            service_category=ServiceCategory.AGENT_COMMS,
            units=Decimal("1"),
            request_path=ENDPOINT,
            operation_key=attempt.idempotency_record_id,
        )

    assert await _wallet_snapshot(seed.wallet_id) == before
    assert await _operation_debit_count(attempt.idempotency_record_id) == 0


@pytest.mark.anyio
async def test_retry_repairs_checkpoint_without_second_debit_or_velocity_increment(
    client: AsyncClient,
    clean_database,
) -> None:
    seed = await _prepared_seed(client, "retry-checkpoint")
    attempt = await _attempt(seed.attempt_id)
    money = get_agent_money()
    first = await money.charge(
        wallet_id=seed.wallet_id,
        service_category=ServiceCategory.AGENT_COMMS,
        units=Decimal("1"),
        request_path=ENDPOINT,
        operation_key=attempt.idempotency_record_id,
    )
    assert hasattr(first, "entry_id")
    after_first = await _wallet_snapshot(seed.wallet_id)

    async with get_session_factory()() as session:
        async with session.begin():
            record = await session.get(
                IdempotencyRecordModel,
                attempt.idempotency_record_id,
            )
            assert record is not None
            assert record.ledger_entry_id == first.entry_id
            record.ledger_entry_id = None
            session.add(record)

    replay = await money.charge(
        wallet_id=seed.wallet_id,
        service_category=ServiceCategory.AGENT_COMMS,
        units=Decimal("1"),
        request_path=ENDPOINT,
        operation_key=attempt.idempotency_record_id,
    )

    assert replay.entry_id == first.entry_id
    assert await _wallet_snapshot(seed.wallet_id) == after_first
    assert await _operation_debit_count(attempt.idempotency_record_id) == 1
    async with get_session_factory()() as session:
        record = await session.get(
            IdempotencyRecordModel,
            attempt.idempotency_record_id,
        )
    assert record is not None
    assert record.ledger_entry_id == first.entry_id


@pytest.mark.anyio
async def test_governed_failure_rolls_back_velocity_freeze_and_debit(
    client: AsyncClient,
    clean_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = await _prepared_seed(client, "rollback-accounting")
    attempt = await _attempt(seed.attempt_id)
    await _set_freeze_boundary(seed.wallet_id)
    before = await _wallet_snapshot(seed.wallet_id)
    money = get_agent_money()
    notify = AsyncMock()
    monkeypatch.setattr(get_velocity_monitor(), "_notify_freeze", notify)

    async def fail_after_velocity(session, **_kwargs):
        wallet = await session.get(WalletModel, seed.wallet_id)
        assert wallet is not None
        assert wallet.status == "frozen"
        raise RuntimeError("guarded_charge_interrupted")

    monkeypatch.setattr(
        money._billing_engine,
        "_apply_charge_to_locked_wallet",
        fail_after_velocity,
    )
    with pytest.raises(RuntimeError, match="guarded_charge_interrupted"):
        await money.charge(
            wallet_id=seed.wallet_id,
            service_category=ServiceCategory.AGENT_COMMS,
            units=Decimal("1"),
            request_path=ENDPOINT,
            operation_key=attempt.idempotency_record_id,
        )

    assert await _wallet_snapshot(seed.wallet_id) == before
    assert await _operation_debit_count(attempt.idempotency_record_id) == 0
    assert (await _attempt(seed.attempt_id)).state == "prepared"
    notify.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("source_status", ["active", "pending_kyc"])
async def test_freeze_notification_observes_committed_governed_state(
    client: AsyncClient,
    clean_database,
    monkeypatch: pytest.MonkeyPatch,
    source_status: str,
) -> None:
    seed = await _prepared_seed(client, f"committed-freeze-{source_status}")
    attempt = await _attempt(seed.attempt_id)
    await _set_freeze_boundary(seed.wallet_id, status=source_status)

    async def assert_committed(wallet: WalletModel) -> None:
        snapshot = await _wallet_snapshot(wallet.wallet_id)
        assert snapshot["status"] == "frozen"
        assert snapshot["velocity_alerts_triggered"] == (
            get_velocity_monitor()._freeze_threshold + 1
        )
        # The freeze commits, while the refused charge's spend increment is
        # reversed in that same transaction.
        assert snapshot["hourly_spent"] == Decimal("0")

    notify = AsyncMock(side_effect=assert_committed)
    monkeypatch.setattr(get_velocity_monitor(), "_notify_freeze", notify)
    result = await get_agent_money().charge(
        wallet_id=seed.wallet_id,
        service_category=ServiceCategory.AGENT_COMMS,
        units=Decimal("1"),
        request_path=ENDPOINT,
        operation_key=attempt.idempotency_record_id,
    )

    assert result.error == "wallet_frozen"
    assert await _operation_debit_count(attempt.idempotency_record_id) == 0
    notify.assert_awaited_once()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("limit_field", "other_field", "expected_limit_key", "expected_pct_key"),
    [
        ("hourly_limit", "daily_limit", "hourly_limit", "hourly_pct"),
        ("daily_limit", "hourly_limit", "daily_limit", "daily_pct"),
    ],
)
async def test_zero_velocity_limit_is_not_replaced_by_default(
    client: AsyncClient,
    clean_database,
    limit_field: str,
    other_field: str,
    expected_limit_key: str,
    expected_pct_key: str,
) -> None:
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    async with get_session_factory()() as session:
        async with session.begin():
            wallet = await session.get(WalletModel, wallet_id)
            assert wallet is not None
            setattr(wallet, limit_field, Decimal("0"))
            setattr(wallet, other_field, Decimal("1000"))
            session.add(wallet)

    before = await get_velocity_monitor().get_velocity_status(wallet_id)
    result = await get_velocity_monitor().check_and_record_charge(
        wallet_id,
        Decimal("0.5"),
    )
    status = await get_velocity_monitor().get_velocity_status(wallet_id)

    assert result.alert_triggered is True
    assert result.exceeded_limit == limit_field.removesuffix("_limit")
    assert result.limit == 0.0
    assert before[expected_pct_key] == 0.0
    assert status[expected_limit_key] == 0.0
    assert status[expected_pct_key] == 101.0


@pytest.mark.anyio
async def test_upstream_health_uses_global_reconciliation_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "MCP_UPSTREAM_ENABLED", True)
    monkeypatch.setattr(settings, "MCP_UPSTREAM_PUBLIC_TOOL_ID", "partner.tool")
    monkeypatch.setattr(settings, "MCP_UPSTREAM_CONNECT_TIMEOUT_SECONDS", 400.0)
    monkeypatch.setattr(settings, "MCP_UPSTREAM_CALL_TIMEOUT_SECONDS", 100.0)

    registry = SimpleNamespace(
        get_local=lambda _tool_id: {
            "service_id": "partner.tool",
            "execution_backend": "upstream_mcp",
            "upstream_tool_name": "write",
            "upstream_origin": "https://partner.example",
        },
        get_executor=lambda _tool_id: object(),
    )
    dispatch = SimpleNamespace(
        summarize=AsyncMock(
            return_value=SimpleNamespace(
                state_counts={},
                stale_active=0,
                unfinalized_terminal=0,
                terminal_idempotency_incomplete=0,
                reconciliation_backlog=0,
            )
        )
    )
    monkeypatch.setattr(
        "app.services.service_registry.get_service_registry",
        lambda: registry,
    )
    monkeypatch.setattr(
        "app.services.mcp_dispatch_attempts.get_mcp_dispatch_attempt_service",
        lambda: dispatch,
    )
    monkeypatch.setattr(
        "app.services.upstream_mcp.get_upstream_mcp_metrics_snapshot",
        lambda: {},
    )

    result = await health._check_upstream_mcp()

    expected = dispatch_reconciliation_idle_seconds(
        connect_timeout_seconds=400.0,
        call_timeout_seconds=100.0,
    )
    assert result["status"] == "up"
    dispatch.summarize.assert_awaited_once_with(idle_seconds=expected)
    assert expected > 300
