"""SEL-351: optional per-upstream status lookup for stale claimed dispatches.

The hook may only move a stale ``dispatch_claimed`` attempt to confirmed
success or confirmed not-delivered. It never re-sends, it decides the outcome
before the single receipt is signed, and any doubt leaves the attempt
``delivery_uncertain``.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.db.database import get_session_factory
from app.db.models import McpDispatchAttemptModel, ReceiptModel
from app.services.agent_money import get_agent_money
from app.services.mcp_dispatch_attempts import (
    DISPATCH_CLAIMED,
    DISPATCH_LEGACY_DISPATCHED,
    get_mcp_dispatch_attempt_service,
)
from app.services.mcp_dispatch_reconciliation import (
    McpDispatchReconciliationService,
    UpstreamDispatchStatus,
    UpstreamDispatchStatusQuery,
)
from app.services.permits import get_permit_service
from app.services.receipts import get_receipt_service
from tests.test_mcp_dispatch_reconciliation import (
    CREDITS,
    SeededAttempt,
    _attempt,
    _ledger_counts,
    _replay,
    _seed_attempt,
    client as client,
)

ORIGIN = "https://partner.example"
UPSTREAM_RESULT = {
    "content": [{"type": "text", "text": "partner confirms done"}],
    "structuredContent": {"order_id": "ord-1"},
    "isError": False,
}


class RecordingLookup:
    def __init__(self, answer: UpstreamDispatchStatus | None | Exception) -> None:
        self.answer = answer
        self.queries: list[UpstreamDispatchStatusQuery] = []

    async def __call__(
        self, query: UpstreamDispatchStatusQuery
    ) -> UpstreamDispatchStatus | None:
        self.queries.append(query)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _reconciler(lookup) -> McpDispatchReconciliationService:
    return McpDispatchReconciliationService(
        max_result_bytes=4096,
        status_lookups={ORIGIN: lookup},
    )


async def _seed_claimed(client: AsyncClient, suffix: str) -> SeededAttempt:
    seed = await _seed_attempt(client, suffix=suffix, state="dispatched")
    assert (await _attempt(seed.attempt_id)).state == DISPATCH_CLAIMED
    return seed


async def _receipt_count(idempotency_record_id: str) -> int:
    factory = get_session_factory()
    async with factory() as session:
        return int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(ReceiptModel)
                    .where(ReceiptModel.idempotency_record_id == idempotency_record_id)
                )
            ).scalar_one()
        )


async def _assert_stays_uncertain(seed: SeededAttempt) -> None:
    attempt = await _attempt(seed.attempt_id)
    assert attempt.state == "delivery_uncertain"
    assert attempt.debit_refunded_at is None
    assert await _ledger_counts(seed.wallet_id) == (1, 0)
    receipt = await get_receipt_service().get_receipt_by_idempotency_record_id(
        attempt.idempotency_record_id
    )
    assert receipt is not None and receipt.outcome == "delivery_uncertain"
    _, status = await _replay(seed)
    assert status == 504


@pytest.mark.anyio
async def test_provider_done_resolves_to_success_once_and_keeps_charge(
    client: AsyncClient,
    clean_database,
) -> None:
    seed = await _seed_claimed(client, "lookup-done")
    lookup = RecordingLookup(
        UpstreamDispatchStatus(delivered=True, result_payload=UPSTREAM_RESULT)
    )
    reconciler = _reconciler(lookup)

    result = await reconciler.reconcile(idle_seconds=300)

    assert result.status_resolved == 1
    assert result.dispatched_uncertain == 0
    assert result.repaired == 1
    assert result.failed_attempt_ids == ()
    assert len(lookup.queries) == 1
    query = lookup.queries[0]
    attempt = await _attempt(seed.attempt_id)
    assert query.attempt_id == seed.attempt_id
    assert query.invocation_id == attempt.idempotency_record_id
    assert query.idempotency_key == seed.idempotency_key
    assert query.upstream_origin == ORIGIN
    assert query.upstream_tool_name == "partner_lookup"
    assert query.request_hash == attempt.request_hash

    assert attempt.state == "succeeded"
    assert attempt.debit_refunded_at is None
    assert await _ledger_counts(seed.wallet_id) == (1, 0)
    permit = await get_permit_service().get_permit(seed.permit_id)
    assert permit is not None and permit.spent_credits == CREDITS
    receipt = await get_receipt_service().get_receipt_by_idempotency_record_id(
        attempt.idempotency_record_id
    )
    assert receipt is not None
    assert receipt.outcome == "success"
    assert receipt.credits_charged == CREDITS
    assert receipt.response_hash == attempt.response_hash
    assert await _receipt_count(attempt.idempotency_record_id) == 1
    replay, status = await _replay(seed)
    assert status == 200
    assert replay["structuredContent"] == UPSTREAM_RESULT["structuredContent"]
    assert replay["receipt"]["receipt_id"] == receipt.receipt_id

    again = await reconciler.reconcile(idle_seconds=0)
    assert again.repaired == 0
    assert len(lookup.queries) == 1
    assert await _receipt_count(attempt.idempotency_record_id) == 1


@pytest.mark.anyio
async def test_provider_not_done_refunds_through_existing_compensation(
    client: AsyncClient,
    clean_database,
) -> None:
    seed = await _seed_claimed(client, "lookup-not-done")
    lookup = RecordingLookup(UpstreamDispatchStatus(delivered=False))

    result = await _reconciler(lookup).reconcile(idle_seconds=300)

    assert result.status_resolved == 1
    assert result.failed_attempt_ids == ()
    attempt = await _attempt(seed.attempt_id)
    assert attempt.state == "returned_error"
    assert attempt.error_code == "reconciled_not_delivered"
    assert attempt.debit_refunded_at is not None
    assert attempt.budget_released_at is not None
    assert await _ledger_counts(seed.wallet_id) == (1, 1)
    wallet = await get_agent_money().get_wallet(seed.wallet_id)
    assert wallet is not None and wallet.balance == Decimal("1000")
    permit = await get_permit_service().get_permit(seed.permit_id)
    assert permit is not None and permit.spent_credits == Decimal("0")
    receipt = await get_receipt_service().get_receipt_by_idempotency_record_id(
        attempt.idempotency_record_id
    )
    assert receipt is not None
    assert receipt.outcome == "failed_refunded"
    assert receipt.credits_charged == Decimal("0")
    assert receipt.reason_code == "failed_refunded"
    assert await _receipt_count(attempt.idempotency_record_id) == 1
    replay, status = await _replay(seed)
    assert status == 502
    assert replay["error"] == "failed_refunded"

    again = await _reconciler(lookup).reconcile(idle_seconds=0)
    assert again.repaired == 0
    assert await _ledger_counts(seed.wallet_id) == (1, 1)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "answer",
    [
        ConnectionError("partner unreachable"),
        RuntimeError("partner 500"),
        None,
        UpstreamDispatchStatus(delivered=True, result_payload=None),
        UpstreamDispatchStatus(delivered=True, result_payload={"blob": "x" * 8192}),
    ],
    ids=["unreachable", "raises", "unknown", "done-without-result", "too-large"],
)
async def test_unproven_lookup_leaves_attempt_delivery_uncertain(
    client: AsyncClient,
    clean_database,
    answer,
) -> None:
    seed = await _seed_claimed(client, "lookup-unproven")
    lookup = RecordingLookup(answer)

    result = await _reconciler(lookup).reconcile(idle_seconds=300)

    assert len(lookup.queries) == 1
    assert result.dispatched_uncertain == 1
    assert result.status_resolved == 0
    assert result.failed_attempt_ids == ()
    await _assert_stays_uncertain(seed)


@pytest.mark.anyio
async def test_lookup_for_another_origin_keeps_default_behaviour(
    client: AsyncClient,
    clean_database,
) -> None:
    seed = await _seed_claimed(client, "lookup-other-origin")
    lookup = RecordingLookup(UpstreamDispatchStatus(delivered=False))
    reconciler = McpDispatchReconciliationService(
        max_result_bytes=4096,
        status_lookups={"https://someone-else.example": lookup},
    )

    result = await reconciler.reconcile(idle_seconds=300)

    assert lookup.queries == []
    assert result.dispatched_uncertain == 1
    await _assert_stays_uncertain(seed)


@pytest.mark.anyio
async def test_already_uncertain_receipt_is_never_rewritten(
    client: AsyncClient,
    clean_database,
) -> None:
    seed = await _seed_claimed(client, "lookup-after-receipt")
    await McpDispatchReconciliationService(max_result_bytes=4096).reconcile(
        idle_seconds=300
    )
    attempt = await _attempt(seed.attempt_id)
    original = await get_receipt_service().get_receipt_by_idempotency_record_id(
        attempt.idempotency_record_id
    )
    assert original is not None and original.outcome == "delivery_uncertain"

    lookup = RecordingLookup(UpstreamDispatchStatus(delivered=False))
    result = await _reconciler(lookup).reconcile(idle_seconds=0)

    assert lookup.queries == []
    assert result.repaired == 0
    after = await get_receipt_service().get_receipt_by_idempotency_record_id(
        attempt.idempotency_record_id
    )
    assert after == original
    assert await _receipt_count(attempt.idempotency_record_id) == 1
    await _assert_stays_uncertain(seed)


@pytest.mark.anyio
async def test_legacy_dispatched_row_is_not_offered_to_the_lookup(
    client: AsyncClient,
    clean_database,
) -> None:
    seed = await _seed_claimed(client, "lookup-legacy")
    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            row = await session.get(McpDispatchAttemptModel, seed.attempt_id)
            assert row is not None
            row.state = DISPATCH_LEGACY_DISPATCHED
            row.dispatch_claim_hash = None
            session.add(row)
    lookup = RecordingLookup(UpstreamDispatchStatus(delivered=False))

    result = await _reconciler(lookup).reconcile(idle_seconds=0)

    assert lookup.queries == []
    assert result.dispatched_uncertain == 1
    await _assert_stays_uncertain(seed)


@pytest.mark.anyio
async def test_live_claim_owner_finishing_during_lookup_wins(
    client: AsyncClient,
    clean_database,
) -> None:
    seed = await _seed_claimed(client, "lookup-race-owner")
    live_result = {"content": [{"type": "text", "text": "live owner"}]}

    async def owner_finishes_first(
        query: UpstreamDispatchStatusQuery,
    ) -> UpstreamDispatchStatus:
        await get_mcp_dispatch_attempt_service().complete(
            attempt_id=query.attempt_id,
            state="succeeded",
            result_payload=live_result,
            error_code=None,
            max_result_bytes=4096,
        )
        return UpstreamDispatchStatus(delivered=False)

    result = await _reconciler(owner_finishes_first).reconcile(idle_seconds=300)

    assert result.terminal_recovered == 1
    assert result.status_resolved == 0
    assert result.failed_attempt_ids == ()
    attempt = await _attempt(seed.attempt_id)
    assert attempt.state == "succeeded"
    assert attempt.debit_refunded_at is None
    assert await _ledger_counts(seed.wallet_id) == (1, 0)
    receipt = await get_receipt_service().get_receipt_by_idempotency_record_id(
        attempt.idempotency_record_id
    )
    assert receipt is not None and receipt.outcome == "success"
    assert await _receipt_count(attempt.idempotency_record_id) == 1


@pytest.mark.anyio
async def test_concurrent_conflicting_resolutions_settle_exactly_once(
    client: AsyncClient,
    clean_database,
) -> None:
    seed = await _seed_claimed(client, "lookup-race-sweeps")
    arrived = 0
    both_in_flight = asyncio.Event()

    def barrier_lookup(answer: UpstreamDispatchStatus):
        async def lookup(query: UpstreamDispatchStatusQuery) -> UpstreamDispatchStatus:
            nonlocal arrived
            arrived += 1
            if arrived == 2:
                both_in_flight.set()
            await asyncio.wait_for(both_in_flight.wait(), timeout=5)
            return answer

        return lookup

    done = _reconciler(
        barrier_lookup(
            UpstreamDispatchStatus(delivered=True, result_payload=UPSTREAM_RESULT)
        )
    )
    not_done = _reconciler(barrier_lookup(UpstreamDispatchStatus(delivered=False)))

    results = await asyncio.gather(
        done.reconcile(idle_seconds=300),
        not_done.reconcile(idle_seconds=300),
    )

    assert arrived == 2
    assert all(r.failed_attempt_ids == () for r in results)
    assert sum(r.status_resolved for r in results) == 1
    assert sum(r.terminal_recovered for r in results) == 1
    attempt = await _attempt(seed.attempt_id)
    assert attempt.state in {"succeeded", "returned_error"}
    assert await _receipt_count(attempt.idempotency_record_id) == 1
    receipt = await get_receipt_service().get_receipt_by_idempotency_record_id(
        attempt.idempotency_record_id
    )
    assert receipt is not None
    if attempt.state == "succeeded":
        assert receipt.outcome == "success"
        assert await _ledger_counts(seed.wallet_id) == (1, 0)
    else:
        assert receipt.outcome == "failed_refunded"
        assert await _ledger_counts(seed.wallet_id) == (1, 1)
