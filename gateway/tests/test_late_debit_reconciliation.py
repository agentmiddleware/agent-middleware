"""Business-accounting regressions for a delayed governed upstream debit.

The second test deterministically exercises the service-call ordering in which
cleanup observes no debit, the debit commits, and cleanup attempts its terminal
transition using the earlier observation. It is not a process-concurrency test.
"""

# Imported pytest fixtures intentionally share test parameter names.
# ruff: noqa: F811

import asyncio
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Update

from app.core.time import utc_now
from app.db.database import get_session_factory
from app.db.models import BillingAlertModel, WalletModel
from app.schemas.billing import ServiceCategory
from app.services.agent_money import get_agent_money
from app.services.billing_engine import LedgerOperationConflictError
from app.services.idempotency import GOVERNED_MCP_IDEMPOTENCY_ENDPOINT
from app.services.mcp_dispatch_attempts import (
    DispatchAttemptError,
    DispatchClaimUnavailableError,
    get_mcp_dispatch_attempt_service,
)
from app.services.mcp_dispatch_reconciliation import (
    get_mcp_dispatch_reconciliation_service,
)
from app.services.permits import get_permit_service
from app.services.receipts import get_receipt_service
from app.services.velocity_monitor import get_velocity_monitor
from tests.test_mcp_dispatch_reconciliation import (
    CREDITS,
    ENDPOINT,
    _attempt,
    _ledger_counts,
    _replay,
    _seed_attempt,
    client as client,
)


async def _wallet_accounting_snapshot(wallet_id: str) -> dict:
    async with get_session_factory()() as session:
        wallet = await session.get(WalletModel, wallet_id)
        assert wallet is not None
        fields = (
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
        snapshot = {name: getattr(wallet, name) for name in fields}
        snapshot["billing_alerts"] = await session.scalar(
            select(func.count())
            .select_from(BillingAlertModel)
            .where(BillingAlertModel.wallet_id == wallet_id)
        )
        return snapshot


async def _set_velocity_boundary(wallet_id: str, *, freeze: bool) -> None:
    async with get_session_factory()() as session:
        async with session.begin():
            wallet = await session.get(WalletModel, wallet_id)
            assert wallet is not None
            wallet.hourly_limit = Decimal("1") if freeze else Decimal("1000")
            wallet.auto_refill_threshold = Decimal("2000")
            wallet.velocity_alerts_triggered = get_velocity_monitor()._freeze_threshold
            wallet.last_charge_at = utc_now() - timedelta(days=2)
            wallet.hourly_reset_at = wallet.last_charge_at
            wallet.daily_reset_at = wallet.last_charge_at
            session.add(wallet)


@pytest.mark.anyio
async def test_delayed_debit_cannot_contradict_completed_no_dispatch_cleanup(
    client: AsyncClient,
    clean_database,
) -> None:
    seed = await _seed_attempt(
        client,
        suffix="late-debit-after-cleanup",
        state="prepared",
        create_charge=False,
        attach_charge=False,
        idempotency_endpoint=GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
    )
    prepared = await _attempt(seed.attempt_id)
    money = get_agent_money()
    wallet_before = await money.get_wallet(seed.wallet_id)
    assert wallet_before is not None
    assert wallet_before.balance > CREDITS

    reconciler = get_mcp_dispatch_reconciliation_service()
    result = await reconciler.reconcile(idle_seconds=300)
    assert result.prepared_finalized == 1
    assert result.failed_attempt_ids == ()

    terminal = await _attempt(seed.attempt_id)
    receipt = await get_receipt_service().get_receipt_by_idempotency_record_id(
        prepared.idempotency_record_id
    )
    assert terminal.state == "returned_error"
    assert terminal.dispatched_at is None
    assert receipt is not None
    assert receipt.outcome == "failed_refunded"
    assert receipt.credits_charged == Decimal("0")
    assert receipt.ledger_entry_id is None
    assert await _ledger_counts(seed.wallet_id) == (0, 0)

    # Resume the original worker's first debit after cleanup has committed.
    # Rejecting the completed financial operation is an acceptable outcome.
    try:
        await money.charge(
            wallet_id=seed.wallet_id,
            service_category=ServiceCategory.AGENT_COMMS,
            units=Decimal("1"),
            request_path=ENDPOINT,
            operation_key=prepared.idempotency_record_id,
        )
    except (LedgerOperationConflictError, ValueError):
        pass

    wallet_after = await money.get_wallet(seed.wallet_id)
    assert wallet_after is not None
    assert wallet_after.balance == wallet_before.balance, (
        "A completed, undispatched failed_refunded invocation must not acquire "
        "a later net debit that contradicts its immutable zero-charge receipt"
    )
    assert await _ledger_counts(seed.wallet_id) == (0, 0), (
        "The completed no-debit operation must reject a fresh debit"
    )
    permit = await get_permit_service().get_permit(seed.permit_id)
    assert permit is not None
    assert permit.spent_credits == Decimal("0")
    replay, status = await _replay(seed)
    assert status == 502
    assert replay["error"] == "failed_refunded"
    assert replay["receipt"]["receipt_id"] == receipt.receipt_id
    valid, reason, _ = await get_receipt_service().verify_receipt(receipt.receipt_id)
    assert (valid, reason) == (True, None)


@pytest.mark.anyio
async def test_cleanup_winning_before_debit_fence_leaves_no_velocity_residue(
    client: AsyncClient,
    clean_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = await _seed_attempt(
        client,
        suffix="cleanup-before-fence",
        state="prepared",
        create_charge=False,
        attach_charge=False,
        idempotency_endpoint=GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
    )
    prepared = await _attempt(seed.attempt_id)
    await _set_velocity_boundary(seed.wallet_id, freeze=True)
    before = await _wallet_accounting_snapshot(seed.wallet_id)
    notify = AsyncMock()
    monkeypatch.setattr(get_velocity_monitor(), "_notify_freeze", notify)
    fence_ready = asyncio.Event()
    release_fence = asyncio.Event()
    real_execute = AsyncSession.execute
    charge_task = None

    async def gated_execute(session, statement, *args, **kwargs):
        if (
            asyncio.current_task() is charge_task
            and isinstance(statement, Update)
            and statement.table.name == "mcp_dispatch_attempts"
            and not fence_ready.is_set()
        ):
            fence_ready.set()
            await release_fence.wait()
        return await real_execute(session, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", gated_execute)
    charge_task = asyncio.create_task(
        get_agent_money().charge(
            wallet_id=seed.wallet_id,
            service_category=ServiceCategory.AGENT_COMMS,
            units=Decimal("1"),
            request_path=ENDPOINT,
            operation_key=prepared.idempotency_record_id,
        )
    )
    try:
        await asyncio.wait_for(fence_ready.wait(), timeout=5)
        # Complete only the attempt while billing owns the identity lock. Full
        # replay finalization follows after the rejected worker releases it.
        await asyncio.wait_for(
            get_mcp_dispatch_attempt_service().complete_pre_dispatch_failure(
                attempt_id=seed.attempt_id,
                expected_updated_at=prepared.updated_at,
                result_payload={"error": "failed_refunded"},
                error_code="reconciled_stale_prepared",
                max_result_bytes=4096,
            ),
            timeout=5,
        )
    finally:
        release_fence.set()
        charge_results = await asyncio.wait_for(
            asyncio.gather(charge_task, return_exceptions=True), timeout=5
        )
    assert isinstance(charge_results[0], LedgerOperationConflictError)
    assert "dispatch_unavailable" in str(charge_results[0])
    await get_mcp_dispatch_reconciliation_service().reconcile_attempt(seed.attempt_id)

    assert await _wallet_accounting_snapshot(seed.wallet_id) == before
    assert await _ledger_counts(seed.wallet_id) == (0, 0)
    notify.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("failure_point", ["after_freeze", "after_debit"])
async def test_governed_charge_exception_rolls_back_all_accounting_state(
    client: AsyncClient,
    clean_database,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    seed = await _seed_attempt(
        client,
        suffix=f"charge-rollback-{failure_point}",
        state="prepared",
        create_charge=False,
        attach_charge=False,
        idempotency_endpoint=GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
    )
    prepared = await _attempt(seed.attempt_id)
    await _set_velocity_boundary(seed.wallet_id, freeze=failure_point == "after_freeze")
    before = await _wallet_accounting_snapshot(seed.wallet_id)
    money = get_agent_money()
    real_apply = money._billing_engine._apply_charge_to_locked_wallet
    notify = AsyncMock()
    monkeypatch.setattr(get_velocity_monitor(), "_notify_freeze", notify)

    async def fail_apply(session, **kwargs):
        if failure_point == "after_debit":
            debit = await real_apply(session, **kwargs)
            assert hasattr(debit, "entry_id")
            await session.flush()
        else:
            wallet = await session.get(WalletModel, seed.wallet_id)
            assert wallet is not None and wallet.status == "frozen"
            assert (
                wallet.velocity_alerts_triggered > before["velocity_alerts_triggered"]
            )
        notify.assert_not_awaited()
        raise RuntimeError("guarded_charge_interrupted")

    monkeypatch.setattr(
        money._billing_engine, "_apply_charge_to_locked_wallet", fail_apply
    )
    with pytest.raises(RuntimeError, match="guarded_charge_interrupted"):
        await money.charge(
            wallet_id=seed.wallet_id,
            service_category=ServiceCategory.AGENT_COMMS,
            units=Decimal("1"),
            request_path=ENDPOINT,
            operation_key=prepared.idempotency_record_id,
        )
    assert await _wallet_accounting_snapshot(seed.wallet_id) == before
    assert await _ledger_counts(seed.wallet_id) == (0, 0)
    unchanged = await _attempt(seed.attempt_id)
    assert unchanged.state == "prepared"
    assert unchanged.ledger_entry_id is None
    notify.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("governed", [False, True])
async def test_freeze_notification_observes_only_committed_state(
    client: AsyncClient,
    clean_database,
    monkeypatch: pytest.MonkeyPatch,
    governed: bool,
) -> None:
    seed = await _seed_attempt(
        client,
        suffix=f"freeze-notify-{governed}",
        state="prepared",
        create_charge=False,
        attach_charge=False,
        idempotency_endpoint=GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
    )
    prepared = await _attempt(seed.attempt_id)
    await _set_velocity_boundary(seed.wallet_id, freeze=True)
    before = await _wallet_accounting_snapshot(seed.wallet_id)

    async def verify_committed(wallet):
        stored = await _wallet_accounting_snapshot(wallet.wallet_id)
        assert stored["status"] == "frozen"
        assert (
            stored["velocity_alerts_triggered"]
            == before["velocity_alerts_triggered"] + 1
        )
        assert stored["last_charge_at"] != before["last_charge_at"]

    notify = AsyncMock(side_effect=verify_committed)
    monitor = get_velocity_monitor()
    monkeypatch.setattr(monitor, "_notify_freeze", notify)
    if governed:
        result = await get_agent_money().charge(
            wallet_id=seed.wallet_id,
            service_category=ServiceCategory.AGENT_COMMS,
            units=Decimal("1"),
            request_path=ENDPOINT,
            operation_key=prepared.idempotency_record_id,
        )
        assert result.error == "wallet_frozen"
    else:
        result = await monitor.check_and_record_charge(seed.wallet_id, CREDITS)
        assert result.should_freeze is True
    notify.assert_awaited_once()
    assert await _ledger_counts(seed.wallet_id) == (0, 0)


@pytest.mark.anyio
async def test_debit_after_cleanup_observation_is_captured_or_yields_safely(
    client: AsyncClient,
    clean_database,
) -> None:
    seed = await _seed_attempt(
        client,
        suffix="late-debit-before-terminal-cas",
        state="prepared",
        create_charge=False,
        attach_charge=False,
        idempotency_endpoint=GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
    )
    observed_attempt = await _attempt(seed.attempt_id)
    money = get_agent_money()
    wallet_before = await money.get_wallet(seed.wallet_id)
    assert wallet_before is not None

    dispatch = get_mcp_dispatch_attempt_service()
    reconciler = get_mcp_dispatch_reconciliation_service()
    observed_debit = await reconciler._find_operation_debit(observed_attempt)
    assert observed_debit is None

    # The debit wins after the cleanup read, but before cleanup's terminal CAS.
    debit = await money.charge(
        wallet_id=seed.wallet_id,
        service_category=ServiceCategory.AGENT_COMMS,
        units=Decimal("1"),
        request_path=ENDPOINT,
        operation_key=observed_attempt.idempotency_record_id,
    )
    assert hasattr(debit, "entry_id")
    assert await _ledger_counts(seed.wallet_id) == (1, 0)

    try:
        await dispatch.complete_pre_dispatch_failure(
            attempt_id=seed.attempt_id,
            expected_updated_at=observed_attempt.updated_at,
            ledger_entry_id=None,
            credits_charged=None,
            result_payload={
                "error": "failed_refunded",
                "error_code": "reconciled_stale_prepared",
            },
            error_code="reconciled_stale_prepared",
            max_result_bytes=4096,
        )
    except DispatchClaimUnavailableError:
        # A fix may atomically attach/update the attempt with the debit, making
        # the old cleanup snapshot yield. Fresh reconciliation must still work.
        pass

    repair_error = None
    try:
        await reconciler.reconcile_attempt(seed.attempt_id)
    except DispatchAttemptError as exc:
        repair_error = str(exc)
    assert repair_error is None, (
        "A debit committed before no-dispatch terminalization must remain "
        f"recoverable by its logical operation identity; repair failed: {repair_error}"
    )

    wallet_after = await money.get_wallet(seed.wallet_id)
    permit = await get_permit_service().get_permit(seed.permit_id)
    terminal = await _attempt(seed.attempt_id)
    assert wallet_after is not None
    assert wallet_after.balance == wallet_before.balance
    assert permit is not None
    assert permit.spent_credits == Decimal("0")
    assert terminal.state == "returned_error"
    assert terminal.dispatched_at is None
    assert terminal.ledger_entry_id == debit.entry_id
    assert terminal.debit_refunded_at is not None
    assert await _ledger_counts(seed.wallet_id) == (1, 1)

    receipt = await get_receipt_service().get_receipt_by_idempotency_record_id(
        observed_attempt.idempotency_record_id
    )
    assert receipt is not None
    assert receipt.outcome == "failed_refunded"
    assert receipt.credits_charged == Decimal("0")
    assert receipt.ledger_entry_id == debit.entry_id
    replay, status = await _replay(seed)
    assert status == 502
    assert replay["receipt"]["receipt_id"] == receipt.receipt_id
    valid, reason, _ = await get_receipt_service().verify_receipt(receipt.receipt_id)
    assert (valid, reason) == (True, None)

    await reconciler.reconcile_attempt(seed.attempt_id)
    assert await _ledger_counts(seed.wallet_id) == (1, 1)


@pytest.mark.anyio
async def test_existing_debit_is_adopted_after_its_cleanup_refund(
    client: AsyncClient,
    clean_database,
) -> None:
    seed = await _seed_attempt(
        client,
        suffix="existing-debit-after-cleanup",
        state="prepared",
        attach_charge=False,
        idempotency_endpoint=GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
    )
    prepared = await _attempt(seed.attempt_id)
    reconciler = get_mcp_dispatch_reconciliation_service()
    await reconciler.reconcile_attempt(seed.attempt_id)
    before = await get_agent_money().get_wallet(seed.wallet_id)
    assert before is not None
    assert await _ledger_counts(seed.wallet_id) == (1, 1)

    replayed_debit = await get_agent_money().charge(
        wallet_id=seed.wallet_id,
        service_category=ServiceCategory.AGENT_COMMS,
        units=Decimal("1"),
        request_path=ENDPOINT,
        operation_key=prepared.idempotency_record_id,
    )
    assert replayed_debit.entry_id == seed.ledger_entry_id
    after = await get_agent_money().get_wallet(seed.wallet_id)
    assert after is not None
    assert after.balance == before.balance
    assert await _ledger_counts(seed.wallet_id) == (1, 1)


@pytest.mark.anyio
async def test_cleanup_after_inflight_debit_refunds_once(
    client: AsyncClient,
    clean_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = await _seed_attempt(
        client,
        suffix="inflight-debit-cleanup",
        state="prepared",
        create_charge=False,
        attach_charge=False,
        idempotency_endpoint=GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
    )
    prepared = await _attempt(seed.attempt_id)
    money = get_agent_money()
    before = await money.get_wallet(seed.wallet_id)
    assert before is not None
    debit_ready = asyncio.Event()
    release_debit = asyncio.Event()
    cleanup_ready = asyncio.Event()
    real_apply = money._billing_engine._apply_charge_to_locked_wallet
    dispatch = get_mcp_dispatch_attempt_service()
    real_complete = dispatch.complete_pre_dispatch_failure

    async def gated_apply(*args, **kwargs):
        debit_ready.set()
        await release_debit.wait()
        return await real_apply(*args, **kwargs)

    async def observed_complete(**kwargs):
        cleanup_ready.set()
        return await real_complete(**kwargs)

    monkeypatch.setattr(
        money._billing_engine, "_apply_charge_to_locked_wallet", gated_apply
    )
    monkeypatch.setattr(dispatch, "complete_pre_dispatch_failure", observed_complete)
    debit_task = asyncio.create_task(
        money.charge(
            wallet_id=seed.wallet_id,
            service_category=ServiceCategory.AGENT_COMMS,
            units=Decimal("1"),
            request_path=ENDPOINT,
            operation_key=prepared.idempotency_record_id,
        )
    )
    cleanup_task = None
    try:
        await asyncio.wait_for(debit_ready.wait(), timeout=5)
        cleanup_task = asyncio.create_task(
            get_mcp_dispatch_reconciliation_service().reconcile_attempt(seed.attempt_id)
        )
        await asyncio.wait_for(cleanup_ready.wait(), timeout=5)
    finally:
        release_debit.set()
        tasks = [debit_task]
        if cleanup_task is not None:
            tasks.append(cleanup_task)
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)

    after = await money.get_wallet(seed.wallet_id)
    terminal = await _attempt(seed.attempt_id)
    assert after is not None
    assert after.balance == before.balance
    assert terminal.state == "returned_error"
    assert terminal.dispatched_at is None
    assert terminal.ledger_entry_id is not None
    assert terminal.debit_refunded_at is not None
    assert await _ledger_counts(seed.wallet_id) == (1, 1)
    receipt = await get_receipt_service().get_receipt_by_idempotency_record_id(
        prepared.idempotency_record_id
    )
    assert receipt is not None
    assert receipt.outcome == "failed_refunded"
    assert receipt.ledger_entry_id == terminal.ledger_entry_id
    assert receipt.credits_charged == Decimal("0")
