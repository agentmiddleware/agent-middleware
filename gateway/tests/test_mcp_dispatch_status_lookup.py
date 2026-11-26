"""SEL-351: optional per-upstream status lookup for stale claimed dispatches.

The hook may only move a stale ``dispatch_claimed`` attempt to a
provider-confirmed delivered result. That result goes through the live path's
result gate, so an ``isError`` result is refunded just like on the live path.
The hook never re-sends, it decides the outcome before the single receipt is
signed, and any doubt leaves the attempt ``delivery_uncertain``, including a
provider claim of non-delivery: a paused worker may still hold the claim.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import inspect
import time
from decimal import Decimal

import pytest
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import func, select

from app.db.database import get_session_factory
from app.db.models import McpDispatchAttemptModel, ReceiptModel
from app.services import mcp_dispatch_reconciliation as reconciliation_module
from app.services.agent_money import get_agent_money
from app.services.idempotency import ACTION_MCP_IDEMPOTENCY_ENDPOINT
from app.services.mcp_dispatch_attempts import (
    DISPATCH_CLAIMED,
    DISPATCH_LEGACY_DISPATCHED,
    DispatchAttemptConflictError,
    get_mcp_dispatch_attempt_service,
)
from app.services.mcp_dispatch_reconciliation import (
    McpDispatchReconciliationService,
    UpstreamDispatchStatus,
    UpstreamDispatchStatusQuery,
    get_mcp_dispatch_reconciliation_service,
    register_upstream_status_lookup,
)
from app.services.permits import get_permit_service
from app.services.receipts import get_receipt_service
from app.services.service_registry import get_service_registry
from app.services.upstream_mcp import (
    DiscoveredUpstreamTool,
    UpstreamMcpAdapter,
    UpstreamMcpConfiguration,
)
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
UPSTREAM_TOKEN = "partner-bearer-token-sel-351"
UPSTREAM_RESULT = {
    "content": [{"type": "text", "text": "partner confirms done"}],
    "structuredContent": {"order_id": "ord-1"},
    "isError": False,
}
ERROR_RESULT = {
    "content": [{"type": "text", "text": "partner rejected the order"}],
    "isError": True,
}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"order_id": {"type": "string"}},
    "required": ["order_id"],
}


class RecordingLookup:
    def __init__(self, answer: object) -> None:
        self.answer = answer
        self.queries: list[UpstreamDispatchStatusQuery] = []

    async def __call__(self, query: UpstreamDispatchStatusQuery) -> object:
        self.queries.append(query)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture(autouse=True)
def restore_executor_registry():
    executors = get_service_registry()._executor_registry
    saved = dict(executors)
    yield
    executors.clear()
    executors.update(saved)


def _reconciler(lookup, **kwargs) -> McpDispatchReconciliationService:
    return McpDispatchReconciliationService(
        max_result_bytes=4096,
        status_lookups={ORIGIN: lookup},
        **kwargs,
    )


def _register_executor(tool_name: str, *, discovered: bool = True) -> None:
    # The live result gate needs the binding's adapter (bearer token, size
    # bound, discovered output schema); no network is used.
    adapter = UpstreamMcpAdapter(
        UpstreamMcpConfiguration(
            url=f"{ORIGIN}/mcp",
            origin=ORIGIN,
            tool_name="partner_lookup",
            public_tool_id=tool_name,
            bearer_token=SecretStr(UPSTREAM_TOKEN),
            credits_per_call=CREDITS,
            connect_timeout_seconds=5.0,
            call_timeout_seconds=5.0,
            max_response_bytes=4096,
            environment="test",
        )
    )
    if discovered:
        adapter._discovered_tool = DiscoveredUpstreamTool(
            name="partner_lookup",
            description="partner lookup",
            input_schema={"type": "object"},
            output_schema=OUTPUT_SCHEMA,
        )
    get_service_registry()._executor_registry[tool_name] = adapter


async def _seed_claimed(client: AsyncClient, suffix: str) -> SeededAttempt:
    seed = await _seed_attempt(client, suffix=suffix, state="dispatched")
    assert (await _attempt(seed.attempt_id)).state == DISPATCH_CLAIMED
    _register_executor(seed.tool_name)
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
    assert attempt.budget_released_at is None
    assert await _ledger_counts(seed.wallet_id) == (1, 0)
    permit = await get_permit_service().get_permit(seed.permit_id)
    assert permit is not None and permit.spent_credits == CREDITS
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
    assert query.forwarded_idempotency_key == seed.idempotency_key
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
async def test_provider_done_with_is_error_is_refunded_like_the_live_path(
    client: AsyncClient,
    clean_database,
) -> None:
    seed = await _seed_claimed(client, "lookup-is-error")
    lookup = RecordingLookup(
        UpstreamDispatchStatus(delivered=True, result_payload=ERROR_RESULT)
    )

    result = await _reconciler(lookup).reconcile(idle_seconds=300)

    assert result.status_resolved == 1
    assert result.failed_attempt_ids == ()
    attempt = await _attempt(seed.attempt_id)
    assert attempt.state == "returned_error"
    assert attempt.error_code == "upstream_returned_error"
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
    assert receipt.reason_code == "upstream_returned_error"
    assert await _receipt_count(attempt.idempotency_record_id) == 1
    replay, status = await _replay(seed)
    assert status == 502
    assert replay["error"] == "upstream_returned_error"
    assert replay["upstream_result"]["isError"] is True

    again = await _reconciler(lookup).reconcile(idle_seconds=0)
    assert again.repaired == 0
    assert await _ledger_counts(seed.wallet_id) == (1, 1)


@pytest.mark.anyio
async def test_provider_not_delivered_stays_uncertain_without_refund(
    client: AsyncClient,
    clean_database,
) -> None:
    seed = await _seed_claimed(client, "lookup-not-done")
    lookup = RecordingLookup(UpstreamDispatchStatus(delivered=False))

    result = await _reconciler(lookup).reconcile(idle_seconds=300)

    assert len(lookup.queries) == 1
    assert result.status_resolved == 0
    assert result.dispatched_uncertain == 1
    assert result.failed_attempt_ids == ()
    await _assert_stays_uncertain(seed)


@pytest.mark.anyio
async def test_paused_live_worker_cannot_be_refunded_by_not_delivered(
    client: AsyncClient,
    clean_database,
) -> None:
    seed = await _seed_claimed(client, "lookup-paused-worker")
    resume = asyncio.Event()
    sends = 0

    async def paused_worker() -> None:
        # Holds the durable claim (seeded) and is paused just before
        # call_tool(). Nothing about the row stops it from sending later.
        nonlocal sends
        await resume.wait()
        sends += 1
        with pytest.raises(
            DispatchAttemptConflictError, match="dispatch_terminal_conflict"
        ):
            await get_mcp_dispatch_attempt_service().complete(
                attempt_id=seed.attempt_id,
                state="succeeded",
                result_payload=UPSTREAM_RESULT,
                error_code=None,
                max_result_bytes=4096,
            )

    worker = asyncio.create_task(paused_worker())
    lookup = RecordingLookup(UpstreamDispatchStatus(delivered=False))

    result = await _reconciler(lookup).reconcile(idle_seconds=300)

    assert len(lookup.queries) == 1
    assert result.status_resolved == 0
    assert result.dispatched_uncertain == 1
    # Nothing was paid back while the claim holder could still send.
    attempt = await _attempt(seed.attempt_id)
    assert attempt.debit_refunded_at is None
    assert attempt.budget_released_at is None
    assert await _ledger_counts(seed.wallet_id) == (1, 0)

    resume.set()
    await asyncio.wait_for(worker, timeout=5)

    assert sends == 1
    # The call really ran and the charge still stands: no refund to undo.
    await _assert_stays_uncertain(seed)


@pytest.mark.anyio
async def test_hanging_lookup_times_out_to_delivery_uncertain(
    client: AsyncClient,
    clean_database,
) -> None:
    seed = await _seed_claimed(client, "lookup-hangs")
    started = asyncio.Event()

    async def hangs(query: UpstreamDispatchStatusQuery) -> UpstreamDispatchStatus:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    reconciler = _reconciler(hangs, status_lookup_timeout_seconds=0.05)

    result = await asyncio.wait_for(reconciler.reconcile(idle_seconds=300), 10)

    assert started.is_set()
    assert result.dispatched_uncertain == 1
    assert result.status_resolved == 0
    assert result.failed_attempt_ids == ()
    await _assert_stays_uncertain(seed)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "answer",
    [
        ConnectionError("partner unreachable"),
        RuntimeError("partner 500"),
        None,
        UpstreamDispatchStatus(delivered=True, result_payload=None),
        UpstreamDispatchStatus(
            delivered=True,
            result_payload={
                "content": [{"type": "text", "text": "x" * 8192}],
                # Schema-valid, so the size bound is what rejects it.
                "structuredContent": {"order_id": "ord-1"},
                "isError": False,
            },
        ),
        UpstreamDispatchStatus(delivered="yes", result_payload=UPSTREAM_RESULT),  # type: ignore[arg-type]
        UpstreamDispatchStatus(delivered=1, result_payload=UPSTREAM_RESULT),  # type: ignore[arg-type]
        {"delivered": True, "result_payload": UPSTREAM_RESULT},
        UpstreamDispatchStatus(
            delivered=True, result_payload={"content": "nope", "isError": False}
        ),
        UpstreamDispatchStatus(
            delivered=True,
            result_payload={
                "content": [{"type": "text", "text": f"echo {UPSTREAM_TOKEN}"}],
                # Schema-valid, so the token check is what rejects it.
                "structuredContent": {"order_id": "ord-1"},
                "isError": False,
            },
        ),
        # The live SDK validator rejects these inside call_tool(), which the
        # live path turns into delivery_uncertain, never a signed success.
        UpstreamDispatchStatus(
            delivered=True,
            result_payload={
                "content": [{"type": "text", "text": "done"}],
                "structuredContent": {"order_id": 7},
                "isError": False,
            },
        ),
        UpstreamDispatchStatus(
            delivered=True,
            result_payload={
                "content": [{"type": "text", "text": "done"}],
                "isError": False,
            },
        ),
    ],
    ids=[
        "unreachable",
        "raises",
        "unknown",
        "done-without-result",
        "too-large",
        "delivered-not-bool-str",
        "delivered-not-bool-int",
        "not-a-status-object",
        "malformed-mcp-result",
        "reflects-bearer-token",
        "structured-content-violates-output-schema",
        "structured-content-missing-under-output-schema",
    ],
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
@pytest.mark.parametrize("executor", ["missing", "undiscovered"])
async def test_done_without_a_live_executor_to_validate_stays_uncertain(
    client: AsyncClient,
    clean_database,
    executor: str,
) -> None:
    seed = await _seed_claimed(client, "lookup-no-executor")
    if executor == "missing":
        get_service_registry()._executor_registry.pop(seed.tool_name)
    else:
        # No discovered tool means no output schema to validate against.
        _register_executor(seed.tool_name, discovered=False)
    lookup = RecordingLookup(
        UpstreamDispatchStatus(delivered=True, result_payload=UPSTREAM_RESULT)
    )

    result = await _reconciler(lookup).reconcile(idle_seconds=300)

    assert len(lookup.queries) == 1
    assert result.status_resolved == 0
    assert result.dispatched_uncertain == 1
    await _assert_stays_uncertain(seed)


@pytest.mark.anyio
async def test_action_call_does_not_claim_a_forwarded_key(
    client: AsyncClient,
    clean_database,
) -> None:
    seed = await _seed_claimed(client, "lookup-forwarded-key")
    context = await get_mcp_dispatch_attempt_service().get_context(seed.attempt_id)
    assert context is not None
    lookup = RecordingLookup(None)
    reconciler = _reconciler(lookup)

    assert await reconciler._lookup_status(context) is None
    assert lookup.queries[-1].forwarded_idempotency_key == seed.idempotency_key

    action_context = dataclasses.replace(
        context, endpoint=ACTION_MCP_IDEMPOTENCY_ENDPOINT
    )
    assert await reconciler._lookup_status(action_context) is None
    query = lookup.queries[-1]
    assert query.forwarded_idempotency_key is None
    assert query.invocation_id == context.attempt.idempotency_record_id


@pytest.mark.anyio
async def test_registered_lookup_is_used_by_production_singleton(
    client: AsyncClient,
    clean_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reconciliation_module, "_REGISTERED_STATUS_LOOKUPS", {})
    monkeypatch.setattr(reconciliation_module, "_service", None)
    seed = await _seed_claimed(client, "lookup-registered")
    lookup = RecordingLookup(
        UpstreamDispatchStatus(delivered=True, result_payload=UPSTREAM_RESULT)
    )
    # Built before registration, exactly like the lifespan cleanup singleton.
    singleton = get_mcp_dispatch_reconciliation_service()
    with pytest.raises(TypeError, match="async callable"):
        register_upstream_status_lookup(ORIGIN, None)  # type: ignore[arg-type]
    register_upstream_status_lookup(ORIGIN, lookup)

    result = await singleton.reconcile(idle_seconds=300)

    assert len(lookup.queries) == 1
    assert result.status_resolved == 1
    assert (await _attempt(seed.attempt_id)).state == "succeeded"


@pytest.mark.anyio
async def test_nothing_registered_keeps_singleton_conservative(
    client: AsyncClient,
    clean_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reconciliation_module, "_REGISTERED_STATUS_LOOKUPS", {})
    monkeypatch.setattr(reconciliation_module, "_service", None)
    seed = await _seed_claimed(client, "lookup-unregistered")

    result = await get_mcp_dispatch_reconciliation_service().reconcile(idle_seconds=300)

    assert result.status_resolved == 0
    assert result.dispatched_uncertain == 1
    await _assert_stays_uncertain(seed)


@pytest.mark.anyio
async def test_lookup_for_another_origin_keeps_default_behaviour(
    client: AsyncClient,
    clean_database,
) -> None:
    seed = await _seed_claimed(client, "lookup-other-origin")
    lookup = RecordingLookup(
        UpstreamDispatchStatus(delivered=True, result_payload=UPSTREAM_RESULT)
    )
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
    plain = McpDispatchReconciliationService(max_result_bytes=4096, status_lookups={})
    await plain.reconcile(idle_seconds=300)
    attempt = await _attempt(seed.attempt_id)
    original = await get_receipt_service().get_receipt_by_idempotency_record_id(
        attempt.idempotency_record_id
    )
    assert original is not None and original.outcome == "delivery_uncertain"

    lookup = RecordingLookup(
        UpstreamDispatchStatus(delivered=True, result_payload=UPSTREAM_RESULT)
    )
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
    lookup = RecordingLookup(
        UpstreamDispatchStatus(delivered=True, result_payload=UPSTREAM_RESULT)
    )

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
        return UpstreamDispatchStatus(delivered=True, result_payload=ERROR_RESULT)

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
@pytest.mark.parametrize(
    ("winner_payload", "loser_payload", "state", "outcome", "ledger"),
    [
        (UPSTREAM_RESULT, ERROR_RESULT, "succeeded", "success", (1, 0)),
        (ERROR_RESULT, UPSTREAM_RESULT, "returned_error", "failed_refunded", (1, 1)),
    ],
)
async def test_concurrent_conflicting_resolutions_settle_exactly_once(
    client: AsyncClient,
    clean_database,
    winner_payload: dict,
    loser_payload: dict,
    state: str,
    outcome: str,
    ledger: tuple[int, int],
) -> None:
    seed = await _seed_claimed(client, "lookup-race-sweeps")
    arrived = 0
    both_in_flight = asyncio.Event()
    winner_finished = asyncio.Event()

    async def arrive() -> None:
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both_in_flight.set()
        await asyncio.wait_for(both_in_flight.wait(), timeout=5)

    async def winner_lookup(
        query: UpstreamDispatchStatusQuery,
    ) -> UpstreamDispatchStatus:
        await arrive()
        return UpstreamDispatchStatus(delivered=True, result_payload=winner_payload)

    async def loser_lookup(
        query: UpstreamDispatchStatusQuery,
    ) -> UpstreamDispatchStatus:
        await arrive()
        # Both sweeps hold a stale claim and a conflicting proof. Releasing
        # the loser only after the winner settled fixes who wins; letting the
        # event loop pick also raced two finalizers through refund_charge,
        # which fails the loser sweep on SQLite write contention.
        await asyncio.wait_for(winner_finished.wait(), timeout=5)
        return UpstreamDispatchStatus(delivered=True, result_payload=loser_payload)

    async def run_winner():
        try:
            return await _reconciler(winner_lookup).reconcile(idle_seconds=300)
        finally:
            winner_finished.set()

    won, lost = await asyncio.gather(
        run_winner(),
        _reconciler(loser_lookup).reconcile(idle_seconds=300),
    )

    assert arrived == 2
    assert won.failed_attempt_ids == ()
    assert lost.failed_attempt_ids == ()
    assert (won.status_resolved, won.terminal_recovered) == (1, 0)
    assert (lost.status_resolved, lost.terminal_recovered) == (0, 1)
    attempt = await _attempt(seed.attempt_id)
    assert attempt.state == state
    assert await _receipt_count(attempt.idempotency_record_id) == 1
    receipt = await get_receipt_service().get_receipt_by_idempotency_record_id(
        attempt.idempotency_record_id
    )
    assert receipt is not None
    assert receipt.outcome == outcome
    assert await _ledger_counts(seed.wallet_id) == ledger


@pytest.mark.anyio
async def test_timed_out_sweep_settles_uncertain_before_late_confirmation(
    client: AsyncClient,
    clean_database,
) -> None:
    seed = await _seed_claimed(client, "lookup-race-timeout")
    arrived = 0
    both_in_flight = asyncio.Event()
    timed_out_finished = asyncio.Event()

    async def arrive() -> None:
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both_in_flight.set()
        await asyncio.wait_for(both_in_flight.wait(), timeout=5)

    async def times_out(
        query: UpstreamDispatchStatusQuery,
    ) -> UpstreamDispatchStatus:
        await arrive()
        # The exact exception asyncio.wait_for raises on expiry. Raising it
        # once both sweeps hold the stale snapshot fixes the order without a
        # wall-clock race; the real wait_for expiry is covered by
        # test_hanging_lookup_times_out_to_delivery_uncertain.
        raise TimeoutError

    async def confirms_late(
        query: UpstreamDispatchStatusQuery,
    ) -> UpstreamDispatchStatus:
        await arrive()
        await asyncio.wait_for(timed_out_finished.wait(), timeout=5)
        return UpstreamDispatchStatus(delivered=True, result_payload=UPSTREAM_RESULT)

    async def run_timed_out():
        try:
            return await _reconciler(times_out).reconcile(idle_seconds=300)
        finally:
            timed_out_finished.set()

    # A generous bound so the late lookup really answers instead of timing
    # out itself while the first sweep finalizes.
    late_reconciler = _reconciler(confirms_late, status_lookup_timeout_seconds=30)
    timed_out, late = await asyncio.gather(
        run_timed_out(),
        late_reconciler.reconcile(idle_seconds=300),
    )

    assert arrived == 2
    assert timed_out.failed_attempt_ids == ()
    assert late.failed_attempt_ids == ()
    assert (timed_out.dispatched_uncertain, timed_out.status_resolved) == (1, 0)
    # The late confirmation hits the guarded complete(), loses, and adopts
    # the already-signed uncertain outcome instead of signing a success.
    assert (late.status_resolved, late.terminal_recovered) == (0, 1)
    attempt = await _attempt(seed.attempt_id)
    assert await _receipt_count(attempt.idempotency_record_id) == 1
    await _assert_stays_uncertain(seed)


def _sync_lookup(query: UpstreamDispatchStatusQuery) -> None:
    return None


async def _async_lookup(query: UpstreamDispatchStatusQuery, extra: int = 0) -> None:
    return None


class _SyncCallLookup:
    def __call__(self, query: UpstreamDispatchStatusQuery) -> None:
        return None


@pytest.mark.parametrize(
    "lookup",
    [
        _sync_lookup,
        lambda query: None,
        _SyncCallLookup(),
        functools.partial(_sync_lookup),
        # A sync function that returns a coroutine still runs its own body
        # before wait_for can bound anything.
        lambda query: _async_lookup(query),
        "not-callable",
        None,
    ],
    ids=[
        "sync-def",
        "sync-lambda",
        "sync-call-object",
        "partial-of-sync",
        "sync-returning-coroutine",
        "string",
        "none",
    ],
)
def test_sync_or_non_callable_lookups_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    lookup: object,
) -> None:
    monkeypatch.setattr(reconciliation_module, "_REGISTERED_STATUS_LOOKUPS", {})
    with pytest.raises(TypeError, match="async callable"):
        register_upstream_status_lookup(ORIGIN, lookup)  # type: ignore[arg-type]
    assert reconciliation_module._REGISTERED_STATUS_LOOKUPS == {}
    with pytest.raises(TypeError, match="async callable"):
        McpDispatchReconciliationService(
            max_result_bytes=4096,
            status_lookups={ORIGIN: lookup},  # type: ignore[dict-item]
        )


@pytest.mark.anyio
@pytest.mark.skipif(
    not hasattr(inspect, "markcoroutinefunction"),
    reason="inspect.markcoroutinefunction needs Python 3.12+",
)
@pytest.mark.parametrize(
    "wrap", [False, True], ids=["marked-sync", "partial-of-marked-sync"]
)
async def test_marked_sync_lookup_returning_non_awaitable_stays_uncertain(
    client: AsyncClient,
    clean_database,
    wrap: bool,
) -> None:
    # Registration rejects marked sync functions; this injects one past that
    # check to prove the runtime awaitable check still holds: its bare return
    # value, even a well-formed delivered answer, is not proof.
    seed = await _seed_claimed(client, "lookup-marked-sync")
    calls: list[UpstreamDispatchStatusQuery] = []

    def marked(query: UpstreamDispatchStatusQuery) -> UpstreamDispatchStatus:
        calls.append(query)
        return UpstreamDispatchStatus(delivered=True, result_payload=UPSTREAM_RESULT)

    inspect.markcoroutinefunction(marked)
    lookup = functools.partial(marked) if wrap else marked
    service = _reconciler(_async_lookup)
    service._status_lookups = {ORIGIN: lookup}

    result = await service.reconcile(idle_seconds=300)

    assert len(calls) == 1
    assert result.dispatched_uncertain == 1
    assert result.status_resolved == 0
    assert result.failed_attempt_ids == ()
    await _assert_stays_uncertain(seed)


@pytest.mark.skipif(
    not hasattr(inspect, "markcoroutinefunction"),
    reason="inspect.markcoroutinefunction needs Python 3.12+",
)
@pytest.mark.parametrize(
    "shape",
    ["function", "partial", "nested-partial", "bound-method", "call-object"],
)
def test_marked_blocking_sync_lookup_is_rejected_without_being_called(
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    # The marker fools inspect.iscoroutinefunction; the code object does not.
    invoked: list[bool] = []

    def blocking(*args: object) -> None:
        invoked.append(True)
        time.sleep(5)

    class Holder:
        def method(self, query: UpstreamDispatchStatusQuery) -> None:
            blocking(query)

        inspect.markcoroutinefunction(method)

    class CallObject:
        def __call__(self, query: UpstreamDispatchStatusQuery) -> None:
            blocking(query)

        inspect.markcoroutinefunction(__call__)

    inspect.markcoroutinefunction(blocking)
    lookup = {
        "function": blocking,
        "partial": functools.partial(blocking),
        "nested-partial": functools.partial(functools.partial(blocking)),
        "bound-method": Holder().method,
        "call-object": CallObject(),
    }[shape]
    assert inspect.iscoroutinefunction(
        lookup if shape != "call-object" else type(lookup).__call__
    )

    monkeypatch.setattr(reconciliation_module, "_REGISTERED_STATUS_LOOKUPS", {})
    with pytest.raises(TypeError, match="status lookup must be an async callable"):
        register_upstream_status_lookup(ORIGIN, lookup)  # type: ignore[arg-type]
    assert reconciliation_module._REGISTERED_STATUS_LOOKUPS == {}
    with pytest.raises(TypeError, match="status lookup must be an async callable"):
        McpDispatchReconciliationService(
            max_result_bytes=4096,
            status_lookups={ORIGIN: lookup},  # type: ignore[dict-item]
        )
    assert invoked == []


class _AsyncMethodHolder:
    async def lookup(self, query: UpstreamDispatchStatusQuery) -> None:
        return None


@pytest.mark.parametrize(
    "lookup",
    [
        _async_lookup,
        RecordingLookup(None),
        functools.partial(_async_lookup, extra=1),
        functools.partial(RecordingLookup(None)),
        functools.partial(functools.partial(_async_lookup), extra=1),
        _AsyncMethodHolder().lookup,
        functools.partial(_AsyncMethodHolder().lookup),
    ],
    ids=[
        "async-def",
        "async-call-object",
        "partial-of-async",
        "partial-of-async-call-object",
        "nested-partial",
        "bound-async-method",
        "partial-of-bound-async-method",
    ],
)
def test_async_lookups_are_accepted(
    monkeypatch: pytest.MonkeyPatch,
    lookup: object,
) -> None:
    monkeypatch.setattr(reconciliation_module, "_REGISTERED_STATUS_LOOKUPS", {})
    register_upstream_status_lookup(ORIGIN, lookup)  # type: ignore[arg-type]
    assert reconciliation_module._REGISTERED_STATUS_LOOKUPS == {ORIGIN: lookup}
    McpDispatchReconciliationService(
        max_result_bytes=4096,
        status_lookups={ORIGIN: lookup},  # type: ignore[dict-item]
    )
