from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.core.time import utc_now
from app.db.database import get_session_factory
from app.db.models import (
    ControlPlaneAuditEventModel,
    HumanApprovalModel,
    IdempotencyRecordModel,
    LedgerEntryModel,
    McpDispatchAttemptModel,
    ReceiptModel,
)
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.agent_money import get_agent_money
from app.services.audit_chain import verify_audit_chain
from app.services.audit_log import record_audit_event
from app.services.idempotency import (
    GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
    get_idempotency_service,
)
from app.services.mcp_dispatch_attempts import (
    DISPATCH_CLAIMED,
    DispatchClaimUnavailableError,
    DispatchAttemptContext,
    McpDispatchAttemptService,
    get_mcp_dispatch_attempt_service,
)
from app.services.mcp_dispatch_reconciliation import (
    McpDispatchReconciliationService,
    get_mcp_dispatch_reconciliation_service,
)
from app.services.permits import PermitError, get_permit_service
from app.services.receipts import get_receipt_service
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet

CREDITS = Decimal("1.5")
ENDPOINT = "/mcp/messages"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as value:
        yield value


@dataclass(frozen=True)
class SeededAttempt:
    wallet_id: str
    key_id: str
    permit_id: str
    tool_name: str
    idempotency_key: str
    request_payload: dict[str, Any]
    idempotency_endpoint: str
    attempt_id: str
    ledger_entry_id: str | None
    approval_id: str | None


async def _seed_attempt(
    client: AsyncClient,
    *,
    suffix: str,
    state: str,
    attach_charge: bool = True,
    result_payload: dict[str, Any] | None = None,
    error_code: str | None = None,
    idempotency_endpoint: str = ENDPOINT,
    requires_human_approval: bool = False,
    create_charge: bool = True,
    claim_dispatch: bool = True,
) -> SeededAttempt:
    provisioned = await provision_agent_wallet(client)
    tool_name = f"reconcile-{suffix}"
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name=tool_name,
        idem_key=f"reconcile-permit-{suffix}",
        requires_human_approval=requires_human_approval,
    )
    if not requires_human_approval:
        validation = await get_permit_service().authorize_and_reserve(
            permit_id=permit["permit_id"],
            wallet_id=provisioned["agent_wallet_id"],
            tool_name=tool_name,
            estimated_credits=CREDITS,
            key_id=provisioned["key_id"],
        )
        assert validation.allowed is True

    request_payload = {
        "tool": tool_name,
        "arguments": {"value": suffix},
        "wallet_id": provisioned["agent_wallet_id"],
        "permit_id": permit["permit_id"],
    }
    idempotency_key = f"reconcile-invoke-{suffix}"
    begun = await get_idempotency_service().begin_with_record(
        wallet_id=provisioned["agent_wallet_id"],
        endpoint=idempotency_endpoint,
        idempotency_key=idempotency_key,
        request_payload=request_payload,
    )
    approval_id = None
    if requires_human_approval:
        approval_id = f"appr-reconcile-{suffix}"
        factory = get_session_factory()
        async with factory() as session:
            session.add(
                HumanApprovalModel(
                    approval_id=approval_id,
                    wallet_id=provisioned["agent_wallet_id"],
                    permit_id=permit["permit_id"],
                    tool=tool_name,
                    idempotency_key=idempotency_key,
                    request_hash="a" * 64,
                    status="consumed",
                    simulated=True,
                    expires_at=utc_now() + timedelta(minutes=30),
                )
            )
            await session.commit()
    dispatch = get_mcp_dispatch_attempt_service()
    prepare_kwargs: dict[str, Any] = {
        "idempotency_record_id": begun.record_id,
        "wallet_id": provisioned["agent_wallet_id"],
        "permit_id": permit["permit_id"],
        "approval_id": approval_id,
        "key_id": provisioned["key_id"],
        "public_tool_id": tool_name,
        "upstream_tool_name": "partner_lookup",
        "upstream_origin": "https://partner.example",
        "request_hash": begun.request_hash,
        "credits_authorized": CREDITS,
    }
    if requires_human_approval:
        validation, attempt = await dispatch.authorize_reserve_and_prepare(
            **prepare_kwargs
        )
        assert validation.allowed is True
        assert attempt is not None
    else:
        attempt = await dispatch.prepare(**prepare_kwargs)
    ledger_entry_id = None
    if create_charge:
        charge = await get_agent_money().charge(
            wallet_id=provisioned["agent_wallet_id"],
            service_category=ServiceCategory.AGENT_COMMS,
            units=Decimal("1"),
            request_path=ENDPOINT,
            operation_key=begun.record_id,
        )
        assert hasattr(charge, "entry_id")
        ledger_entry_id = charge.entry_id
        if attach_charge:
            attempt = await dispatch.attach_charge(
                attempt_id=attempt.attempt_id,
                ledger_entry_id=ledger_entry_id,
                credits_charged=CREDITS,
            )
    if state != "prepared" and claim_dispatch:
        attempt = await dispatch.claim_dispatch(attempt.attempt_id)
    if state not in {"prepared", "dispatched"}:
        if attempt.state == "prepared":
            attempt = await dispatch.complete_pre_dispatch_failure(
                attempt_id=attempt.attempt_id,
                expected_updated_at=attempt.updated_at,
                ledger_entry_id=ledger_entry_id,
                credits_charged=(CREDITS if ledger_entry_id is not None else None),
                result_payload=result_payload,
                error_code=error_code,
                max_result_bytes=4096,
            )
        else:
            attempt = await dispatch.complete(
                attempt_id=attempt.attempt_id,
                state=state,
                result_payload=result_payload,
                error_code=error_code,
                max_result_bytes=4096,
            )
    await _make_stale(attempt.attempt_id)
    return SeededAttempt(
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        permit_id=permit["permit_id"],
        tool_name=tool_name,
        idempotency_key=idempotency_key,
        request_payload=request_payload,
        idempotency_endpoint=idempotency_endpoint,
        attempt_id=attempt.attempt_id,
        ledger_entry_id=ledger_entry_id,
        approval_id=approval_id,
    )


async def _make_stale(attempt_id: str) -> None:
    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            attempt = await session.get(McpDispatchAttemptModel, attempt_id)
            assert attempt is not None
            attempt.updated_at = utc_now() - timedelta(minutes=10)
            session.add(attempt)


async def _attempt(attempt_id: str) -> McpDispatchAttemptModel:
    context = await get_mcp_dispatch_attempt_service().get_context(attempt_id)
    assert context is not None
    return context.attempt


async def _ledger_counts(wallet_id: str) -> tuple[int, int]:
    factory = get_session_factory()
    async with factory() as session:
        rows = (
            await session.execute(
                select(LedgerEntryModel.action, func.count())
                .where(LedgerEntryModel.wallet_id == wallet_id)
                .group_by(LedgerEntryModel.action)
            )
        ).all()
    counts = {str(action): int(count) for action, count in rows}
    return counts.get("debit", 0), counts.get("refund", 0)


async def _replay(seed: SeededAttempt) -> tuple[dict[str, Any], int]:
    record = await get_idempotency_service().get_record(
        wallet_id=seed.wallet_id,
        endpoint=seed.idempotency_endpoint,
        idempotency_key=seed.idempotency_key,
    )
    assert record is not None
    assert record.response_json is not None
    return json.loads(record.response_json), record.status_code


@pytest.mark.anyio
async def test_effect_free_stale_mcp_identity_is_released_for_safe_retry(
    client: AsyncClient,
    clean_database,
) -> None:
    provisioned = await provision_agent_wallet(client)
    payload = {
        "tool_name": "partner-unstarted",
        "arguments": {"value": "safe"},
        "wallet_id": provisioned["agent_wallet_id"],
        "permit_id": "permit-never-reserved",
    }
    idempotency_key = "unstarted-before-atomic-prepare"
    begun = await get_idempotency_service().begin_with_record(
        wallet_id=provisioned["agent_wallet_id"],
        endpoint=GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
        idempotency_key=idempotency_key,
        request_payload=payload,
        operation_kind="upstream_mcp",
    )
    factory = get_session_factory()
    async with factory() as session:
        record = await session.get(IdempotencyRecordModel, begun.record_id)
        assert record is not None
        record.created_at = utc_now() - timedelta(minutes=10)
        session.add(record)
        await session.commit()

    repaired, needs_review = await get_idempotency_service().reconcile_stuck_records(
        idle_seconds=300
    )

    assert (repaired, needs_review) == (1, 0)
    assert (
        await get_idempotency_service().get_record(
            wallet_id=provisioned["agent_wallet_id"],
            endpoint=GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
            idempotency_key=idempotency_key,
        )
        is None
    )


@pytest.mark.anyio
async def test_stale_local_identity_is_not_deleted_without_compensation_proof(
    client: AsyncClient,
    clean_database,
) -> None:
    provisioned = await provision_agent_wallet(client)
    idempotency_key = "local-reservation-order-is-different"
    begun = await get_idempotency_service().begin_with_record(
        wallet_id=provisioned["agent_wallet_id"],
        endpoint=GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
        idempotency_key=idempotency_key,
        request_payload={"tool_name": "local-tool", "arguments": {}},
        operation_kind="local",
    )
    factory = get_session_factory()
    async with factory() as session:
        record = await session.get(IdempotencyRecordModel, begun.record_id)
        assert record is not None
        record.created_at = utc_now() - timedelta(minutes=10)
        session.add(record)
        await session.commit()

    repaired, needs_review = await get_idempotency_service().reconcile_stuck_records(
        idle_seconds=300
    )

    assert (repaired, needs_review) == (0, 0)
    assert (
        await get_idempotency_service().get_record(
            wallet_id=provisioned["agent_wallet_id"],
            endpoint=GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
            idempotency_key=idempotency_key,
        )
        is not None
    )


@pytest.mark.anyio
async def test_stale_prepared_adopts_debit_refunds_and_finalizes_once(
    client: AsyncClient,
    clean_database,
) -> None:
    seed = await _seed_attempt(
        client,
        suffix="prepared-after-debit",
        state="prepared",
        attach_charge=False,
    )
    before = await get_agent_money().get_wallet(seed.wallet_id)
    assert before is not None
    assert before.balance == Decimal("998.5")

    result = await get_mcp_dispatch_reconciliation_service().reconcile(idle_seconds=300)

    assert result.prepared_finalized == 1
    assert result.failed_attempt_ids == ()
    attempt = await _attempt(seed.attempt_id)
    assert attempt.state == "returned_error"
    assert attempt.ledger_entry_id == seed.ledger_entry_id
    assert attempt.debit_refunded_at is not None
    assert attempt.budget_released_at is not None
    receipt = await get_receipt_service().get_receipt_by_idempotency_record_id(
        attempt.idempotency_record_id
    )
    assert receipt is not None
    assert receipt.outcome == "failed_refunded"
    assert receipt.credits_charged == Decimal("0")
    assert receipt.ledger_entry_id == seed.ledger_entry_id
    valid, reason, _ = await get_receipt_service().verify_receipt(receipt.receipt_id)
    assert (valid, reason) == (True, None)
    after = await get_agent_money().get_wallet(seed.wallet_id)
    permit = await get_permit_service().get_permit(seed.permit_id)
    assert after is not None and after.balance == Decimal("1000")
    assert permit is not None and permit.spent_credits == Decimal("0")
    assert await _ledger_counts(seed.wallet_id) == (1, 1)
    replay, status = await _replay(seed)
    assert status == 502
    assert replay["error"] == "failed_refunded"
    assert replay["receipt"]["receipt_id"] == receipt.receipt_id

    again = await get_mcp_dispatch_reconciliation_service().reconcile(idle_seconds=0)
    assert again.repaired == 0
    assert await _ledger_counts(seed.wallet_id) == (1, 1)
    permit = await get_permit_service().get_permit(seed.permit_id)
    assert permit is not None and permit.spent_credits == Decimal("0")


@pytest.mark.anyio
async def test_terminal_race_repair_is_counted_exactly_once(
    client: AsyncClient,
    clean_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = await _seed_attempt(
        client,
        suffix="terminal-race-count",
        state="dispatched",
    )
    dispatch = get_mcp_dispatch_attempt_service()
    reconciler = McpDispatchReconciliationService(dispatch_service=dispatch)
    real_list_stale = dispatch.list_stale_contexts
    raced = False

    async def list_then_complete_terminal(
        *,
        idle_seconds: int,
        limit: int,
    ) -> list[DispatchAttemptContext]:
        nonlocal raced
        contexts = await real_list_stale(
            idle_seconds=idle_seconds,
            limit=limit,
        )
        if not raced:
            raced = True
            await dispatch.complete(
                attempt_id=seed.attempt_id,
                state="succeeded",
                result_payload={"content": [{"type": "text", "text": "raced"}]},
                error_code=None,
                max_result_bytes=4096,
            )
            receipt = await get_receipt_service().get_receipt_by_idempotency_record_id(
                (await _attempt(seed.attempt_id)).idempotency_record_id
            )
            assert receipt is None
        return contexts

    monkeypatch.setattr(dispatch, "list_stale_contexts", list_then_complete_terminal)

    result = await reconciler.reconcile(idle_seconds=300)

    assert result.prepared_finalized == 0
    assert result.dispatched_uncertain == 0
    assert result.terminal_recovered == 1
    assert result.idempotency_recovered == 0
    assert result.repaired == 1
    assert result.failed_attempt_ids == ()
    attempt = await _attempt(seed.attempt_id)
    assert attempt.state == "succeeded"
    assert (
        await get_receipt_service().get_receipt_by_idempotency_record_id(
            attempt.idempotency_record_id
        )
        is not None
    )

    again = await reconciler.reconcile(idle_seconds=0)
    assert again.repaired == 0
    assert again.failed_attempt_ids == ()


@pytest.mark.anyio
async def test_terminal_repair_uses_shorter_idle_window_than_live_claim(
    client: AsyncClient,
    clean_database,
) -> None:
    claimed_seed = await _seed_attempt(
        client,
        suffix="split-idle-live-claim",
        state="dispatched",
    )
    terminal_seed = await _seed_attempt(
        client,
        suffix="split-idle-terminal",
        state="succeeded",
        result_payload={"content": [], "isError": False},
    )

    result = await get_mcp_dispatch_reconciliation_service().reconcile(
        idle_seconds=11_430,
        terminal_idle_seconds=300,
    )

    assert result.terminal_recovered == 1
    assert result.dispatched_uncertain == 0
    claimed = await _attempt(claimed_seed.attempt_id)
    terminal = await _attempt(terminal_seed.attempt_id)
    assert claimed.state == DISPATCH_CLAIMED
    assert terminal.state == "succeeded"
    assert (
        await get_receipt_service().get_receipt_by_idempotency_record_id(
            claimed.idempotency_record_id
        )
        is None
    )
    assert (
        await get_receipt_service().get_receipt_by_idempotency_record_id(
            terminal.idempotency_record_id
        )
        is not None
    )
    replay, status = await _replay(terminal_seed)
    assert status == 200
    assert replay["receipt"]["outcome"] == "success"


@pytest.mark.anyio
async def test_duplicate_retry_immediately_finalizes_fresh_terminal_attempt(
    client: AsyncClient,
    clean_database,
) -> None:
    seed = await _seed_attempt(
        client,
        suffix="fresh-terminal-retry",
        state="succeeded",
        result_payload={"content": [{"type": "text", "text": "done"}]},
    )
    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            attempt = await session.get(McpDispatchAttemptModel, seed.attempt_id)
            assert attempt is not None
            attempt.updated_at = utc_now()
            session.add(attempt)
    before_ledger = await _ledger_counts(seed.wallet_id)

    replayed = await get_idempotency_service().begin_with_record(
        wallet_id=seed.wallet_id,
        endpoint=seed.idempotency_endpoint,
        idempotency_key=seed.idempotency_key,
        request_payload=seed.request_payload,
    )

    assert replayed.replay is not None
    assert replayed.replay.status_code == 200
    assert replayed.replay.response_json is not None
    assert replayed.replay.response_json["content"] == [
        {"type": "text", "text": "done"}
    ]
    assert replayed.replay.response_json["receipt"]["outcome"] == "success"
    assert await _ledger_counts(seed.wallet_id) == before_ledger


@pytest.mark.anyio
@pytest.mark.parametrize("entrypoint", ["periodic", "targeted"])
async def test_fresh_dispatch_claim_wins_over_pre_dispatch_reconciliation(
    client: AsyncClient,
    clean_database,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    seed = await _seed_attempt(
        client,
        suffix=f"claim-vs-{entrypoint}-reconcile",
        state="prepared",
    )
    dispatch = get_mcp_dispatch_attempt_service()
    reconciler = McpDispatchReconciliationService(dispatch_service=dispatch)
    real_complete = dispatch.complete_pre_dispatch_failure
    reconciler_ready = asyncio.Event()
    resume_reconciler = asyncio.Event()

    async def gated_complete(**kwargs: Any) -> McpDispatchAttemptModel:
        reconciler_ready.set()
        await resume_reconciler.wait()
        return await real_complete(**kwargs)

    monkeypatch.setattr(
        dispatch,
        "complete_pre_dispatch_failure",
        gated_complete,
    )
    if entrypoint == "periodic":
        reconciliation = asyncio.create_task(reconciler.reconcile(idle_seconds=300))
    else:
        reconciliation = asyncio.create_task(
            reconciler.reconcile_attempt(seed.attempt_id)
        )
    await asyncio.wait_for(reconciler_ready.wait(), timeout=5)
    try:
        claimed = await McpDispatchAttemptService().claim_dispatch(seed.attempt_id)
    finally:
        resume_reconciler.set()
    first_result = await asyncio.wait_for(reconciliation, timeout=5)

    if entrypoint == "periodic":
        assert first_result.prepared_finalized == 0
        assert first_result.dispatched_uncertain == 0
        assert first_result.failed_attempt_ids == ()
    else:
        assert first_result is None
    after_claim = await _attempt(seed.attempt_id)
    assert after_claim.state == "dispatch_claimed"
    assert after_claim.dispatch_claim_hash == claimed.dispatch_claim_hash
    assert after_claim.dispatched_at == claimed.dispatched_at
    assert after_claim.result_json is None
    assert after_claim.error_code is None
    assert after_claim.completed_at is None
    assert after_claim.debit_refunded_at is None
    assert after_claim.budget_released_at is None
    assert await _ledger_counts(seed.wallet_id) == (1, 0)
    wallet = await get_agent_money().get_wallet(seed.wallet_id)
    permit = await get_permit_service().get_permit(seed.permit_id)
    assert wallet is not None and wallet.balance == Decimal("998.5")
    assert permit is not None and permit.spent_credits == CREDITS
    receipt = await get_receipt_service().get_receipt_by_idempotency_record_id(
        after_claim.idempotency_record_id
    )
    assert receipt is None
    record = await get_idempotency_service().get_record(
        wallet_id=seed.wallet_id,
        endpoint=seed.idempotency_endpoint,
        idempotency_key=seed.idempotency_key,
    )
    assert record is not None
    assert record.response_json is None
    assert record.response_reference is None
    with pytest.raises(
        DispatchClaimUnavailableError,
        match="dispatch_claim_unavailable",
    ):
        await McpDispatchAttemptService().claim_dispatch(seed.attempt_id)

    # Once the owner is independently stale, ambiguity is the conservative
    # terminal disposition. It remains charged and can never be redispatched.
    await _make_stale(seed.attempt_id)
    second_result = await McpDispatchReconciliationService().reconcile(idle_seconds=300)
    assert second_result.dispatched_uncertain == 1
    assert second_result.failed_attempt_ids == ()
    terminal = await _attempt(seed.attempt_id)
    assert terminal.state == "delivery_uncertain"
    assert terminal.dispatch_claim_hash == claimed.dispatch_claim_hash
    assert terminal.debit_refunded_at is None
    assert terminal.budget_released_at is None
    assert await _ledger_counts(seed.wallet_id) == (1, 0)
    receipt = await get_receipt_service().get_receipt_by_idempotency_record_id(
        terminal.idempotency_record_id
    )
    assert receipt is not None
    assert receipt.outcome == "delivery_uncertain"
    assert receipt.credits_charged == CREDITS
    assert receipt.ledger_entry_id == seed.ledger_entry_id
    valid, reason, _ = await get_receipt_service().verify_receipt(receipt.receipt_id)
    assert (valid, reason) == (True, None)
    replay, status = await _replay(seed)
    assert status == 504
    assert replay["error"] == "delivery_uncertain"


@pytest.mark.anyio
async def test_fresh_charge_attachment_wins_over_stale_prepared_reconciliation(
    client: AsyncClient,
    clean_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = await _seed_attempt(
        client,
        suffix="attach-vs-reconcile",
        state="prepared",
        attach_charge=False,
    )
    assert seed.ledger_entry_id is not None
    dispatch = get_mcp_dispatch_attempt_service()
    reconciler = McpDispatchReconciliationService(dispatch_service=dispatch)
    real_complete = dispatch.complete_pre_dispatch_failure
    reconciler_ready = asyncio.Event()
    resume_reconciler = asyncio.Event()

    async def gated_complete(**kwargs: Any) -> McpDispatchAttemptModel:
        reconciler_ready.set()
        await resume_reconciler.wait()
        return await real_complete(**kwargs)

    monkeypatch.setattr(
        dispatch,
        "complete_pre_dispatch_failure",
        gated_complete,
    )
    reconciliation = asyncio.create_task(reconciler.reconcile(idle_seconds=300))
    await asyncio.wait_for(reconciler_ready.wait(), timeout=5)
    try:
        attached = await McpDispatchAttemptService().attach_charge(
            attempt_id=seed.attempt_id,
            ledger_entry_id=seed.ledger_entry_id,
            credits_charged=CREDITS,
        )
    finally:
        resume_reconciler.set()
    result = await asyncio.wait_for(reconciliation, timeout=5)

    assert result.prepared_finalized == 0
    assert result.failed_attempt_ids == ()
    current = await _attempt(seed.attempt_id)
    assert current.state == "prepared"
    assert current.updated_at == attached.updated_at
    assert current.ledger_entry_id == seed.ledger_entry_id
    assert current.completed_at is None
    assert current.debit_refunded_at is None
    assert current.budget_released_at is None
    assert await _ledger_counts(seed.wallet_id) == (1, 0)
    assert (
        await get_receipt_service().get_receipt_by_idempotency_record_id(
            current.idempotency_record_id
        )
        is None
    )

    claimed = await McpDispatchAttemptService().claim_dispatch(seed.attempt_id)
    assert claimed.state == "dispatch_claimed"


@pytest.mark.anyio
async def test_stale_dispatched_becomes_charged_delivery_uncertain_without_retry(
    client: AsyncClient,
    clean_database,
) -> None:
    seed = await _seed_attempt(
        client,
        suffix="dispatched-timeout",
        state="dispatched",
    )

    result = await get_mcp_dispatch_reconciliation_service().reconcile(idle_seconds=300)

    assert result.dispatched_uncertain == 1
    assert result.failed_attempt_ids == ()
    attempt = await _attempt(seed.attempt_id)
    assert attempt.state == "delivery_uncertain"
    assert attempt.debit_refunded_at is None
    assert attempt.budget_released_at is None
    assert await _ledger_counts(seed.wallet_id) == (1, 0)
    wallet = await get_agent_money().get_wallet(seed.wallet_id)
    permit = await get_permit_service().get_permit(seed.permit_id)
    assert wallet is not None and wallet.balance == Decimal("998.5")
    assert permit is not None and permit.spent_credits == CREDITS
    receipt = await get_receipt_service().get_receipt_by_idempotency_record_id(
        attempt.idempotency_record_id
    )
    assert receipt is not None
    assert receipt.outcome == "delivery_uncertain"
    assert receipt.credits_charged == CREDITS
    replay, status = await _replay(seed)
    assert status == 504
    assert replay["error"] == "delivery_uncertain"
    assert replay["dispatch"] == {
        "attempt_id": seed.attempt_id,
        "state": "delivery_uncertain",
    }

    again = await get_mcp_dispatch_reconciliation_service().reconcile(idle_seconds=0)
    assert again.repaired == 0
    assert await _ledger_counts(seed.wallet_id) == (1, 0)


@pytest.mark.anyio
async def test_terminal_success_is_reconstructed_from_bounded_result(
    client: AsyncClient,
    clean_database,
) -> None:
    upstream_result = {
        "content": [{"type": "text", "text": "confirmed"}],
        "structuredContent": {"answer": 42},
        "isError": False,
    }
    seed = await _seed_attempt(
        client,
        suffix="success-result-persisted",
        state="succeeded",
        result_payload=upstream_result,
    )

    result = await get_mcp_dispatch_reconciliation_service().reconcile(idle_seconds=300)

    assert result.terminal_recovered == 1
    attempt = await _attempt(seed.attempt_id)
    receipt = await get_receipt_service().get_receipt_by_idempotency_record_id(
        attempt.idempotency_record_id
    )
    assert receipt is not None
    assert receipt.outcome == "success"
    assert receipt.response_hash == attempt.response_hash
    replay, status = await _replay(seed)
    assert status == 200
    assert replay["content"] == upstream_result["content"]
    assert replay["structuredContent"] == upstream_result["structuredContent"]
    assert replay["receipt"]["receipt_id"] == receipt.receipt_id
    assert await _ledger_counts(seed.wallet_id) == (1, 0)


@pytest.mark.anyio
async def test_approval_required_terminal_crash_reconciliation_preserves_approval(
    client: AsyncClient,
    clean_database,
) -> None:
    upstream_result = {
        "content": [{"type": "text", "text": "approved response"}],
        "structuredContent": {"approved": True},
        "isError": False,
    }
    seed = await _seed_attempt(
        client,
        suffix="approved-success-crash",
        state="succeeded",
        result_payload=upstream_result,
        idempotency_endpoint=GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
        requires_human_approval=True,
    )
    assert seed.approval_id is not None
    assert (await _attempt(seed.attempt_id)).approval_id == seed.approval_id

    result = await get_mcp_dispatch_reconciliation_service().reconcile(idle_seconds=300)

    assert result.terminal_recovered == 1
    attempt = await _attempt(seed.attempt_id)
    receipt = await get_receipt_service().get_receipt_by_idempotency_record_id(
        attempt.idempotency_record_id
    )
    assert receipt is not None
    assert receipt.approval_id == seed.approval_id
    valid, reason, _ = await get_receipt_service().verify_receipt(receipt.receipt_id)
    assert (valid, reason) == (True, None)
    replay, status = await _replay(seed)
    assert status == 200
    assert replay["receipt"]["receipt_id"] == receipt.receipt_id
    assert replay["receipt"]["approval_id"] == seed.approval_id

    factory = get_session_factory()
    async with factory() as session:
        audit = (
            await session.execute(
                select(ControlPlaneAuditEventModel).where(
                    ControlPlaneAuditEventModel.request_id == seed.attempt_id
                )
            )
        ).scalar_one()
    assert json.loads(audit.metadata_json or "{}")["approval_id"] == seed.approval_id
    assert (await verify_audit_chain(wallet_id=seed.wallet_id)).valid is True


@pytest.mark.anyio
async def test_reconciliation_rejects_tampered_human_approval_linkage(
    client: AsyncClient,
    clean_database,
) -> None:
    seed = await _seed_attempt(
        client,
        suffix="approval-linkage-tampered",
        state="succeeded",
        result_payload={"content": [], "isError": False},
        idempotency_endpoint=GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
        requires_human_approval=True,
    )
    wrong_approval_id = "appr-reconcile-wrong-binding"
    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            session.add(
                HumanApprovalModel(
                    approval_id=wrong_approval_id,
                    wallet_id=seed.wallet_id,
                    permit_id=seed.permit_id,
                    tool="different-tool",
                    idempotency_key="different-idempotency-key",
                    request_hash="b" * 64,
                    status="consumed",
                    simulated=True,
                    expires_at=utc_now() + timedelta(minutes=30),
                )
            )
            attempt = await session.get(McpDispatchAttemptModel, seed.attempt_id)
            assert attempt is not None
            attempt.approval_id = wrong_approval_id
            session.add(attempt)

    result = await get_mcp_dispatch_reconciliation_service().reconcile(idle_seconds=300)

    assert result.terminal_recovered == 0
    assert result.failed_attempt_ids == (seed.attempt_id,)
    async with factory() as session:
        attempt = await session.get(McpDispatchAttemptModel, seed.attempt_id)
    assert attempt is not None
    receipt = await get_receipt_service().get_receipt_by_idempotency_record_id(
        attempt.idempotency_record_id
    )
    assert receipt is None
    async with factory() as session:
        audit_count = await session.scalar(
            select(func.count())
            .select_from(ControlPlaneAuditEventModel)
            .where(ControlPlaneAuditEventModel.request_id == seed.attempt_id)
        )
        record = await session.get(
            IdempotencyRecordModel,
            attempt.idempotency_record_id,
        )
    assert int(audit_count or 0) == 0
    assert record is not None
    assert record.response_json is None


@pytest.mark.anyio
async def test_existing_receipt_missing_idempotency_completion_replays_full_result(
    client: AsyncClient,
    clean_database,
) -> None:
    upstream_result = {
        "content": [{"type": "text", "text": "original response"}],
        "structuredContent": {"source": "partner"},
        "isError": False,
    }
    seed = await _seed_attempt(
        client,
        suffix="receipt-before-idem-complete",
        state="succeeded",
        result_payload=upstream_result,
        idempotency_endpoint=GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
    )
    attempt = await _attempt(seed.attempt_id)
    audit = await record_audit_event(
        event="mcp.invoke",
        wallet_id=seed.wallet_id,
        tool=seed.tool_name,
        endpoint=ENDPOINT,
        auth_source="db",
        key_id=seed.key_id,
        ok=True,
        metadata={
            "transport": "jsonrpc",
            "permit_id": seed.permit_id,
            "request_hash": attempt.request_hash,
            "ledger_entry_id": seed.ledger_entry_id,
            "dispatch_attempt_id": seed.attempt_id,
            "dispatch_state": "succeeded",
            "dispatch_response_hash": attempt.response_hash,
            "upstream_tool_name": attempt.upstream_tool_name,
            "upstream_origin": attempt.upstream_origin,
        },
    )
    receipt = await get_receipt_service().create_receipt(
        idempotency_record_id=attempt.idempotency_record_id,
        dispatch_attempt_id=seed.attempt_id,
        permit_id=seed.permit_id,
        wallet_id=seed.wallet_id,
        key_id=seed.key_id,
        tool=seed.tool_name,
        request_payload=None,
        request_hash=attempt.request_hash,
        response_payload=upstream_result,
        ledger_entry_id=seed.ledger_entry_id,
        credits_authorized=CREDITS,
        credits_charged=CREDITS,
        outcome="success",
        audit_event_id=audit.event_id,
    )
    await get_idempotency_service().mark_charged(
        wallet_id=seed.wallet_id,
        endpoint=seed.idempotency_endpoint,
        idempotency_key=seed.idempotency_key,
        ledger_entry_id=seed.ledger_entry_id or "",
    )
    await _make_stale(seed.attempt_id)

    # Sweep order cannot let the generic cleanup replace the stored upstream
    # result with its receipt-only fallback response.
    (
        generic_repaired,
        generic_manual,
    ) = await get_idempotency_service().reconcile_stuck_records(idle_seconds=0)
    assert (generic_repaired, generic_manual) == (0, 0)
    record_before = await get_idempotency_service().get_record(
        wallet_id=seed.wallet_id,
        endpoint=seed.idempotency_endpoint,
        idempotency_key=seed.idempotency_key,
    )
    assert record_before is not None and record_before.response_json is None

    result = await get_mcp_dispatch_reconciliation_service().reconcile(idle_seconds=300)

    assert result.idempotency_recovered == 1
    replay, status = await _replay(seed)
    assert status == 200
    assert replay["content"] == upstream_result["content"]
    assert replay["structuredContent"] == upstream_result["structuredContent"]
    assert replay["receipt"]["receipt_id"] == receipt.receipt_id
    factory = get_session_factory()
    async with factory() as session:
        receipt_count = await session.scalar(
            select(func.count()).select_from(ReceiptModel)
        )
        audit_count = await session.scalar(
            select(func.count())
            .select_from(ControlPlaneAuditEventModel)
            .where(
                ControlPlaneAuditEventModel.tool == seed.tool_name,
                ControlPlaneAuditEventModel.event == "mcp.invoke",
            )
        )
    assert int(receipt_count or 0) == 1
    assert int(audit_count or 0) == 1


@pytest.mark.anyio
async def test_terminal_returned_error_completes_crash_compensation(
    client: AsyncClient,
    clean_database,
) -> None:
    upstream_result = {
        "content": [{"type": "text", "text": "partner rejected"}],
        "isError": True,
    }
    seed = await _seed_attempt(
        client,
        suffix="returned-before-refund",
        state="returned_error",
        result_payload=upstream_result,
        error_code="upstream_returned_error",
    )

    result = await get_mcp_dispatch_reconciliation_service().reconcile(idle_seconds=300)

    assert result.terminal_recovered == 1
    attempt = await _attempt(seed.attempt_id)
    assert attempt.debit_refunded_at is not None
    assert attempt.budget_released_at is not None
    assert await _ledger_counts(seed.wallet_id) == (1, 1)
    permit = await get_permit_service().get_permit(seed.permit_id)
    assert permit is not None and permit.spent_credits == Decimal("0")
    receipt = await get_receipt_service().get_receipt_by_idempotency_record_id(
        attempt.idempotency_record_id
    )
    assert receipt is not None and receipt.outcome == "failed_refunded"
    assert receipt.response_hash == attempt.response_hash
    replay, status = await _replay(seed)
    assert status == 502
    assert replay["error"] == "upstream_returned_error"
    assert replay["upstream_result"] == upstream_result


@pytest.mark.anyio
async def test_wallet_expired_terminal_reconciliation_is_refunded_and_replayable(
    client: AsyncClient,
    clean_database,
) -> None:
    seed = await _seed_attempt(
        client,
        suffix="wallet-expired-before-dispatch",
        state="returned_error",
        result_payload={"error": "wallet_expired"},
        error_code="wallet_expired",
        idempotency_endpoint=GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
        create_charge=False,
        claim_dispatch=False,
    )

    result = await get_mcp_dispatch_reconciliation_service().reconcile(idle_seconds=300)

    assert result.terminal_recovered == 1
    assert result.failed_attempt_ids == ()
    attempt = await _attempt(seed.attempt_id)
    assert attempt.state == "returned_error"
    assert attempt.ledger_entry_id is None
    assert attempt.debit_refunded_at is None
    assert attempt.budget_released_at is not None
    assert await _ledger_counts(seed.wallet_id) == (0, 0)
    permit = await get_permit_service().get_permit(seed.permit_id)
    assert permit is not None and permit.spent_credits == Decimal("0")
    receipt = await get_receipt_service().get_receipt_by_idempotency_record_id(
        attempt.idempotency_record_id
    )
    assert receipt is not None
    assert receipt.outcome == "failed_refunded"
    assert receipt.credits_charged == Decimal("0")
    assert receipt.ledger_entry_id is None
    valid, reason, _ = await get_receipt_service().verify_receipt(receipt.receipt_id)
    assert (valid, reason) == (True, None)
    replay, replay_status = await _replay(seed)
    assert replay_status == 403
    assert replay["error"] == "wallet_expired"
    assert replay["receipt"]["receipt_id"] == receipt.receipt_id

    # A crash after signing but before idempotency completion must adopt the
    # same failed-refunded receipt and restore the same public replay contract.
    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            record = await session.get(
                IdempotencyRecordModel,
                attempt.idempotency_record_id,
            )
            assert record is not None
            record.response_json = None
            record.response_reference = None
            session.add(record)
    await _make_stale(seed.attempt_id)

    repaired = await get_mcp_dispatch_reconciliation_service().reconcile(
        idle_seconds=300
    )

    assert repaired.idempotency_recovered == 1
    restored, restored_status = await _replay(seed)
    assert restored_status == 403
    assert restored == replay


@pytest.mark.anyio
async def test_crash_after_budget_release_does_not_release_unrelated_reservation(
    client: AsyncClient,
    clean_database,
) -> None:
    seed = await _seed_attempt(
        client,
        suffix="after-budget-release",
        state="returned_error",
        result_payload={"error": "confirmed"},
        error_code="upstream_returned_error",
    )
    dispatch = get_mcp_dispatch_attempt_service()
    await get_agent_money().refund_charge(
        wallet_id=seed.wallet_id,
        charge_entry_id=seed.ledger_entry_id or "",
    )
    await dispatch.mark_debit_refunded(
        attempt_id=seed.attempt_id,
        ledger_entry_id=seed.ledger_entry_id or "",
    )
    released = await get_permit_service().release_dispatch_budget_once(seed.attempt_id)
    assert released is True
    # A later reservation on the same permit must survive reconciliation.
    await get_permit_service().reserve_budget(seed.permit_id, Decimal("2"))
    await _make_stale(seed.attempt_id)

    result = await get_mcp_dispatch_reconciliation_service().reconcile(idle_seconds=300)
    assert result.terminal_recovered == 1
    permit = await get_permit_service().get_permit(seed.permit_id)
    assert permit is not None and permit.spent_credits == Decimal("2")
    assert await _ledger_counts(seed.wallet_id) == (1, 1)

    await get_mcp_dispatch_reconciliation_service().reconcile(idle_seconds=0)
    permit = await get_permit_service().get_permit(seed.permit_id)
    assert permit is not None and permit.spent_credits == Decimal("2")


@pytest.mark.anyio
async def test_terminal_response_rejected_retains_charge_and_is_replayable(
    client: AsyncClient,
    clean_database,
) -> None:
    seed = await _seed_attempt(
        client,
        suffix="response-rejected",
        state="response_rejected",
        result_payload={"error": "response_rejected"},
        error_code="upstream_response_too_large",
    )

    result = await get_mcp_dispatch_reconciliation_service().reconcile(idle_seconds=300)

    assert result.terminal_recovered == 1
    attempt = await _attempt(seed.attempt_id)
    receipt = await get_receipt_service().get_receipt_by_idempotency_record_id(
        attempt.idempotency_record_id
    )
    assert receipt is not None
    assert receipt.outcome == "response_rejected"
    assert receipt.credits_charged == CREDITS
    assert await _ledger_counts(seed.wallet_id) == (1, 0)
    replay, status = await _replay(seed)
    assert status == 502
    assert replay["error"] == "response_rejected"


@pytest.mark.anyio
async def test_dispatch_summary_exposes_only_counts_and_backlog(
    client: AsyncClient,
    clean_database,
) -> None:
    await _seed_attempt(
        client,
        suffix="metrics-prepared",
        state="prepared",
    )
    await _seed_attempt(
        client,
        suffix="metrics-terminal",
        state="succeeded",
        result_payload={"content": [], "isError": False},
    )

    metrics = await get_mcp_dispatch_attempt_service().summarize(idle_seconds=300)

    assert metrics.state_counts == {"prepared": 1, "succeeded": 1}
    assert metrics.stale_active == 1
    assert metrics.unfinalized_terminal == 1
    assert metrics.terminal_idempotency_incomplete == 0
    assert metrics.reconciliation_backlog == 2
    assert "payload" not in repr(metrics)


# --- Crash-window adversarial tests ---


@pytest.mark.anyio
async def test_crash_between_debit_and_dispatch_reconciles_refund(
    client: AsyncClient,
    clean_database,
) -> None:
    """Prepared with attached debit (worker died before claiming dispatch)."""
    seed = await _seed_attempt(
        client,
        suffix="prepared-with-debit",
        state="prepared",
        attach_charge=True,  # debit IS attached
    )
    # Verify initial state: prepared with ledger_entry_id
    attempt_before = await _attempt(seed.attempt_id)
    assert attempt_before.state == "prepared"
    assert attempt_before.ledger_entry_id is not None

    result = await get_mcp_dispatch_reconciliation_service().reconcile(idle_seconds=300)

    assert result.prepared_finalized == 1
    assert result.failed_attempt_ids == ()
    attempt = await _attempt(seed.attempt_id)
    assert attempt.state == "returned_error"
    assert attempt.debit_refunded_at is not None
    assert attempt.budget_released_at is not None
    assert await _ledger_counts(seed.wallet_id) == (1, 1)
    permit = await get_permit_service().get_permit(seed.permit_id)
    assert permit is not None and permit.spent_credits == Decimal("0")
    receipt = await get_receipt_service().get_receipt_by_idempotency_record_id(
        attempt.idempotency_record_id
    )
    assert receipt is not None
    assert receipt.outcome == "failed_refunded"
    assert receipt.credits_charged == Decimal("0")
    replay, status = await _replay(seed)
    assert status == 502
    assert replay["error"] == "failed_refunded"


@pytest.mark.anyio
async def test_kill_between_dispatch_and_response_becomes_delivery_uncertain(
    client: AsyncClient,
    clean_database,
) -> None:
    """Dispatched but no terminal outcome (worker died during upstream call)."""
    seed = await _seed_attempt(
        client,
        suffix="dispatched-no-response",
        state="dispatched",
    )
    # Verify initial state
    attempt_before = await _attempt(seed.attempt_id)
    assert attempt_before.state == "dispatch_claimed"
    assert attempt_before.dispatch_claim_hash is not None
    assert attempt_before.ledger_entry_id is not None

    result = await get_mcp_dispatch_reconciliation_service().reconcile(idle_seconds=300)

    assert result.dispatched_uncertain == 1
    assert result.failed_attempt_ids == ()
    attempt = await _attempt(seed.attempt_id)
    assert attempt.state == "delivery_uncertain"
    assert attempt.debit_refunded_at is None
    assert attempt.budget_released_at is None
    assert await _ledger_counts(seed.wallet_id) == (1, 0)
    wallet = await get_agent_money().get_wallet(seed.wallet_id)
    permit = await get_permit_service().get_permit(seed.permit_id)
    assert wallet is not None and wallet.balance == Decimal("998.5")
    assert permit is not None and permit.spent_credits == CREDITS
    receipt = await get_receipt_service().get_receipt_by_idempotency_record_id(
        attempt.idempotency_record_id
    )
    assert receipt is not None
    assert receipt.outcome == "delivery_uncertain"
    assert receipt.credits_charged == CREDITS
    replay, status = await _replay(seed)
    assert status == 504
    assert replay["error"] == "delivery_uncertain"


@pytest.mark.anyio
async def test_lost_commit_ack_recovery_no_double_charge(
    client: AsyncClient,
    clean_database,
) -> None:
    """Lost COMMIT ack: retry adopts existing prepared row, budget not doubled."""
    provisioned = await provision_agent_wallet(client)
    tool_name = "reconcile-lost-ack"
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name=tool_name,
        idem_key="reconcile-permit-lost-ack",
    )

    # Create idempotency record (as the client would)
    idem_key = "lost-ack-invoke-1"
    request_payload = {
        "tool": tool_name,
        "arguments": {"value": "test"},
        "wallet_id": provisioned["agent_wallet_id"],
        "permit_id": permit["permit_id"],
    }
    begun = await get_idempotency_service().begin_with_record(
        wallet_id=provisioned["agent_wallet_id"],
        endpoint=ENDPOINT,
        idempotency_key=idem_key,
        request_payload=request_payload,
    )

    # First call: reserve + prepare (simulates successful DB commit but lost ack)
    (
        validation1,
        attempt1,
    ) = await get_mcp_dispatch_attempt_service().authorize_reserve_and_prepare(
        idempotency_record_id=begun.record_id,
        wallet_id=provisioned["agent_wallet_id"],
        permit_id=permit["permit_id"],
        key_id=provisioned["key_id"],
        public_tool_id=tool_name,
        upstream_tool_name="partner_lookup",
        upstream_origin="https://partner.example",
        request_hash=begun.request_hash,
        credits_authorized=CREDITS,
    )
    assert validation1.allowed is True
    assert attempt1 is not None
    assert attempt1.state == "prepared"

    # Record spent credits after first prepare
    permit_after = await get_permit_service().get_permit(permit["permit_id"])
    assert permit_after is not None
    spent_after_first = permit_after.spent_credits

    # Second call: retry (client never got ack, sends again)
    (
        validation2,
        attempt2,
    ) = await get_mcp_dispatch_attempt_service().authorize_reserve_and_prepare(
        idempotency_record_id=begun.record_id,
        wallet_id=provisioned["agent_wallet_id"],
        permit_id=permit["permit_id"],
        key_id=provisioned["key_id"],
        public_tool_id=tool_name,
        upstream_tool_name="partner_lookup",
        upstream_origin="https://partner.example",
        request_hash=begun.request_hash,
        credits_authorized=CREDITS,
    )
    assert validation2.allowed is True
    assert attempt2 is not None
    # Must be the SAME attempt (adopted, not recreated)
    assert attempt2.attempt_id == attempt1.attempt_id

    # Budget must NOT be doubled
    permit_after_retry = await get_permit_service().get_permit(permit["permit_id"])
    assert permit_after_retry is not None
    assert permit_after_retry.spent_credits == spent_after_first


@pytest.mark.anyio
async def test_concurrent_budget_release_decrements_exactly_once(
    client: AsyncClient,
    clean_database,
) -> None:
    """The release must be once-only without relying on a row lock.

    ``release_dispatch_budget_once`` read ``budget_released_at`` under
    ``with_for_update=True`` and then decremented the permit. SQLAlchemy
    silently drops FOR UPDATE on engines that do not support it, so on those
    two concurrent callers could both observe NULL and both decrement -- the
    permit would be credited back twice for one dispatch. The claim is now a
    guarded UPDATE, which is once-only on every engine.
    """
    seed = await _seed_attempt(
        client,
        suffix="concurrent-release",
        state="returned_error",
        result_payload={"error": "confirmed"},
        error_code="upstream_returned_error",
    )
    permits = get_permit_service()
    before = await permits.get_permit(seed.permit_id)
    assert before is not None
    spent_before = before.spent_credits

    results = await asyncio.gather(
        permits.release_dispatch_budget_once(seed.attempt_id),
        permits.release_dispatch_budget_once(seed.attempt_id),
        return_exceptions=True,
    )
    ok = [r for r in results if r is True]
    errors = [r for r in results if isinstance(r, BaseException)]
    assert not errors, f"release raised: {errors}"
    assert len(ok) == 1, f"expected exactly one release to win, got {results}"

    after = await permits.get_permit(seed.permit_id)
    assert after is not None
    # Exactly one decrement, of exactly the authorized amount.
    assert after.spent_credits == spent_before - CREDITS

    # A third call is still a no-op.
    assert await permits.release_dispatch_budget_once(seed.attempt_id) is False
    final = await permits.get_permit(seed.permit_id)
    assert final is not None and final.spent_credits == after.spent_credits


@pytest.mark.anyio
async def test_dispatch_budget_release_is_once_only_under_a_stale_read(
    client: AsyncClient,
    clean_database,
    monkeypatch,
) -> None:
    """Two callers that both observe an unreleased attempt must release once.

    ``release_dispatch_budget_once`` decided whether it had already run by
    reading ``budget_released_at`` and then writing it — a read-modify-write
    serialized only by ``SELECT ... FOR UPDATE``. That lock is a silent no-op
    on SQLite, so both callers saw NULL, both passed the check, and the
    reservation was released twice: the permit ends up *under*-spent and can
    then exceed the very cap it is meant to enforce.

    The interleave is forced rather than raced. The first caller's read is
    allowed to happen, a second caller then runs to completion, and only then
    does the first caller reach its write — exactly the ordering the row lock
    was supposed to prevent and does not on SQLite.
    """
    seed = await _seed_attempt(
        client,
        suffix="release-once-only",
        state="returned_error",
        result_payload={"error": "confirmed"},
        error_code="upstream_returned_error",
    )
    permits = get_permit_service()
    seeded_permit = await permits.get_permit(seed.permit_id)
    assert seeded_permit is not None
    spent_before = seeded_permit.spent_credits
    assert spent_before == CREDITS

    import app.services.permits as permits_module

    real_factory = get_session_factory()
    state = {"fired": False, "second_result": None}

    class _StaleReadSession:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        async def execute(self, *args, **kwargs):
            # Fires after this caller's `session.get` of the attempt row and
            # before its first write — the read-modify-write window.
            if not state["fired"]:
                state["fired"] = True
                monkeypatch.setattr(
                    permits_module, "get_session_factory", lambda: real_factory
                )
                state["second_result"] = await permits.release_dispatch_budget_once(
                    seed.attempt_id
                )
            return await self._inner.execute(*args, **kwargs)

    class _StaleReadFactory:
        def __init__(self, cm):
            self._cm = cm

        async def __aenter__(self):
            return _StaleReadSession(await self._cm.__aenter__())

        async def __aexit__(self, *exc):
            return await self._cm.__aexit__(*exc)

    monkeypatch.setattr(
        permits_module,
        "get_session_factory",
        lambda: lambda: _StaleReadFactory(real_factory()),
    )

    first_result = await permits.release_dispatch_budget_once(seed.attempt_id)

    assert state["fired"], "the interleave never ran — the test proved nothing"
    # Exactly one caller may claim the release.
    assert [first_result, state["second_result"]].count(True) == 1
    # And the budget moved exactly once, not twice.
    permit = await permits.get_permit(seed.permit_id)
    assert permit is not None
    assert permit.spent_credits == spent_before - CREDITS


@pytest.mark.anyio
async def test_release_dispatch_budget_rejects_a_missing_attempt(
    client: AsyncClient,
    clean_database,
) -> None:
    """An unknown attempt id must not move budget."""
    with pytest.raises(PermitError) as excinfo:
        await get_permit_service().release_dispatch_budget_once("att-does-not-exist")
    assert excinfo.value.reason == "dispatch_attempt_not_found"


@pytest.mark.anyio
async def test_release_dispatch_budget_rejects_a_non_terminal_attempt(
    client: AsyncClient,
    clean_database,
) -> None:
    """Only a terminal ``returned_error`` attempt has a reservation to release.

    A ``dispatched`` attempt may still complete and consume its credits. Giving
    its reservation back early would leave the permit enforcing a cap against a
    reservation that no longer exists, so the same permit could fund the
    in-flight call twice over.
    """
    seed = await _seed_attempt(
        client,
        suffix="release-non-terminal",
        state="dispatched",
    )
    permits = get_permit_service()
    before = await permits.get_permit(seed.permit_id)
    assert before is not None

    with pytest.raises(PermitError) as excinfo:
        await permits.release_dispatch_budget_once(seed.attempt_id)
    assert excinfo.value.reason == "dispatch_budget_release_state_invalid"

    # And no budget moved on the way to the refusal.
    after = await permits.get_permit(seed.permit_id)
    assert after is not None
    assert after.spent_credits == before.spent_credits


@pytest.mark.anyio
async def test_a_failed_refund_does_not_hand_back_the_reservation(
    client: AsyncClient,
    clean_database,
    monkeypatch,
) -> None:
    """The budget-release sweep must not outrun compensation.

    The sweep that repairs a stranded reservation selects on
    ``state='returned_error'`` and ``budget_released_at IS NULL``, and neither
    of those says anything about whether the debit came back. A terminal
    attempt whose refund just failed earlier in the same sweep matches both.

    Releasing it there would cut ``spent_credits`` below what the wallet
    actually paid, and the permit would then admit a further call past
    ``max_credits`` -- an over-spend, which is the opposite failure from the
    stranded reservation the sweep exists to repair, and the worse of the two.
    So the reservation stays held while the money is still out, and the
    compensation path keeps ownership of it.
    """
    seed = await _seed_attempt(
        client,
        suffix="refund-fails-before-release",
        state="returned_error",
        result_payload={
            "content": [{"type": "text", "text": "partner rejected"}],
            "isError": True,
        },
        error_code="upstream_returned_error",
    )

    async def _refund_unavailable(*args: Any, **kwargs: Any):
        raise RuntimeError("refund store unavailable")

    monkeypatch.setattr(type(get_agent_money()), "refund_charge", _refund_unavailable)

    result = await get_mcp_dispatch_reconciliation_service().reconcile(idle_seconds=300)

    # Compensation failed, so nothing here was repaired.
    assert seed.attempt_id in result.failed_attempt_ids
    assert result.budget_released == 0

    attempt = await _attempt(seed.attempt_id)
    assert attempt.debit_refunded_at is None
    assert attempt.budget_released_at is None, (
        "reservation was handed back while the debit still stands"
    )
    # The wallet is still out the money, so the permit must still count it.
    assert await _ledger_counts(seed.wallet_id) == (1, 0)
    permit = await get_permit_service().get_permit(seed.permit_id)
    assert permit is not None and permit.spent_credits == CREDITS
