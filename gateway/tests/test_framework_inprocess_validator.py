"""End-to-end proof of the in-process permit validator + governed middleware.

Covers WP4 against the real ASGI app: GovernedEdgeSession verifying a real
signed permit against the real ``/.well-known/trust-keys.json``, the
framework ``governed_tool`` wrappers driving the governed loop to a
verifiable receipt (with idempotent replay), a locally-denied call proven to
never reach the server, the BatchingReceiptEmitter's ordering/poison/flush
behavior, and the <5ms in-process overhead acceptance test.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.service_registry import get_service_registry
from app.trust.receipts import BatchingReceiptEmitter, ReceiptError, get_receipt_service
from b2a_sdk.client import AgentMiddlewareClient
from b2a_sdk.edge_client import GovernedEdgeSession, LocalPermitValidator
from b2a_sdk.errors import PermitDeniedError
from framework_integrations.langgraph_middleware import (
    governed_tool as langgraph_governed_tool,
)
from framework_integrations.pydantic_ai_middleware import (
    governed_tool as pydantic_ai_governed_tool,
)
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class _CountingASGITransport(ASGITransport):
    """ASGI transport that counts every request the SDK client sends."""

    def __init__(self, target_app) -> None:
        super().__init__(app=target_app)
        self.requests = 0

    async def handle_async_request(self, request):
        self.requests += 1
        return await super().handle_async_request(request)


@pytest.mark.anyio
async def test_governed_session_verifies_permit_against_published_trust_keys(
    client,
    clean_database,
) -> None:
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    permit = await create_tool_permit(
        client,
        wallet_id=wallet_id,
        key_id=provisioned["key_id"],
        tool_name="inproc-open-tool",
        idem_key="inproc-permit-open",
    )

    sdk = AgentMiddlewareClient(
        api_key=provisioned["agent_headers"]["X-API-Key"],
        base_url="http://test",
        transport=ASGITransport(app=app),
    )
    try:
        session = await GovernedEdgeSession.open(
            sdk, permit_id=permit["permit_id"], wallet_id=wallet_id
        )
        # The real permit signature verifies locally against the real
        # published trust keys.
        assert session.validator.verify_permit() is True

        # Local tampering with a signed field is detected offline.
        tampered = session.validator.permit
        tampered["max_credits"] = "999999"
        assert session.validator.verify_permit(tampered) is False

        # spent_credits is deliberately NOT signed (mutable server state).
        respent = session.validator.permit
        respent["spent_credits"] = "49"
        assert session.validator.verify_permit(respent) is True
    finally:
        await sdk.close()


@pytest.mark.anyio
async def test_governed_tool_end_to_end_receipt_and_replay(
    client,
    clean_database,
) -> None:
    registry = get_service_registry()
    calls = 0

    def inproc_echo(message: str = "ok") -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"message": message}

    registry.register_local(
        service_id="inproc-echo",
        name="Inproc Echo",
        description="Governed in-process validator e2e tool",
        category=ServiceCategory.AGENT_COMMS,
        func=inproc_echo,
        credits_per_unit=2.0,
        unit_name="call",
    )
    try:
        provisioned = await provision_agent_wallet(client)
        wallet_id = provisioned["agent_wallet_id"]
        permit = await create_tool_permit(
            client,
            wallet_id=wallet_id,
            key_id=provisioned["key_id"],
            tool_name="inproc-echo",
            idem_key="inproc-permit-e2e",
        )
        sdk = AgentMiddlewareClient(
            api_key=provisioned["agent_headers"]["X-API-Key"],
            base_url="http://test",
            transport=ASGITransport(app=app),
        )
        try:
            session = await GovernedEdgeSession.open(
                sdk, permit_id=permit["permit_id"], wallet_id=wallet_id
            )

            @pydantic_ai_governed_tool(
                session, tool_name="inproc-echo", credits_hint=Decimal("2")
            )
            async def echo(message: str = "ok") -> dict[str, Any]:
                """Client-side stub; the registered tool executes server-side."""
                raise AssertionError("stub body must never run locally")

            result = await echo(message="hello", idempotency_key="inproc-invoke-1")
            # Local tools return MCP text content; the tool ran server-side.
            assert calls == 1
            assert isinstance(result, list)
            assert json.loads(result[0]["text"])["message"] == "hello"

            receipt = echo.last_receipt
            assert receipt is not None
            assert receipt.outcome == "success"
            assert receipt.permit_id == permit["permit_id"]
            assert receipt.credits_charged == Decimal("2")

            verify_resp = await client.post(
                "/v1/receipts/verify",
                json={"receipt_id": receipt.receipt_id},
                headers=provisioned["agent_headers"],
            )
            assert verify_resp.status_code == 200
            assert verify_resp.json()["valid"] is True

            # Replay with the same caller-owned idempotency key: same
            # receipt, no second dispatch, no double charge.
            await echo(message="hello", idempotency_key="inproc-invoke-1")
            assert echo.last_receipt.receipt_id == receipt.receipt_id
            assert calls == 1

            # Local usage bookkeeping advanced (conservatively: the replay
            # also reserved locally, which under-states remaining budget).
            assert session.validator.reserved_credits == Decimal("4")
            assert session.validator.call_counts == {"inproc-echo": 2}
        finally:
            await sdk.close()
    finally:
        registry.unregister_local("inproc-echo")


@pytest.mark.anyio
async def test_local_denial_raises_without_hitting_the_server(
    client,
    clean_database,
) -> None:
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    permit = await create_tool_permit(
        client,
        wallet_id=wallet_id,
        key_id=provisioned["key_id"],
        tool_name="inproc-allowed-tool",
        idem_key="inproc-permit-denial",
    )

    transport = _CountingASGITransport(app)
    sdk = AgentMiddlewareClient(
        api_key=provisioned["agent_headers"]["X-API-Key"],
        base_url="http://test",
        transport=transport,
    )
    try:
        session = await GovernedEdgeSession.open(
            sdk, permit_id=permit["permit_id"], wallet_id=wallet_id
        )
        requests_after_open = transport.requests
        assert requests_after_open == 2  # permit fetch + trust keys, once

        @langgraph_governed_tool(session, tool_name="out-of-scope-tool")
        async def out_of_scope() -> dict[str, Any]:
            """Stub for a tool the permit does not allow."""
            raise AssertionError("stub body must never run locally")

        with pytest.raises(PermitDeniedError) as exc_info:
            await out_of_scope()
        assert exc_info.value.reason == "permit_tool_not_allowed"
        # The RPC hop was eliminated: no request left the client.
        assert transport.requests == requests_after_open
    finally:
        await sdk.close()


@pytest.mark.anyio
async def test_governed_tool_refuses_sync_functions_at_call_time(
    client,
    clean_database,
) -> None:
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    permit = await create_tool_permit(
        client,
        wallet_id=wallet_id,
        key_id=provisioned["key_id"],
        tool_name="inproc-sync-tool",
        idem_key="inproc-permit-sync",
    )
    sdk = AgentMiddlewareClient(
        api_key=provisioned["agent_headers"]["X-API-Key"],
        base_url="http://test",
        transport=ASGITransport(app=app),
    )
    try:
        session = await GovernedEdgeSession.open(
            sdk, permit_id=permit["permit_id"], wallet_id=wallet_id
        )
        for decorate in (pydantic_ai_governed_tool, langgraph_governed_tool):

            def sync_stub() -> dict[str, Any]:
                return {}

            wrapped = decorate(session, tool_name="inproc-sync-tool")(sync_stub)
            # Matches b2a_sdk.decorators.billable: the refusal happens at
            # call time, not decoration time.
            with pytest.raises(RuntimeError, match="requires an async function"):
                wrapped()
    finally:
        await sdk.close()


@pytest.mark.anyio
async def test_governed_tool_sends_declared_defaults_and_owns_key_handling(
    client,
    clean_database,
) -> None:
    """The stub's contract governs the wire call.

    Declared-default parameters must travel to the server (the stub's
    default, not the server implementation's); a supplied blank idempotency
    key is rejected rather than silently replaced; an omitted (or None) key
    derives a fresh one per call, making each call a new invocation.
    """
    registry = get_service_registry()
    calls = 0
    received: list[dict[str, Any]] = []

    def inproc_defaults(
        message: str = "server-default", label: str = "server-label"
    ) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        received.append({"message": message, "label": label})
        return {"message": message, "label": label}

    registry.register_local(
        service_id="inproc-default-tool",
        name="Inproc Defaults",
        description="Governed default-parameter contract tool",
        category=ServiceCategory.AGENT_COMMS,
        func=inproc_defaults,
        credits_per_unit=1.0,
        unit_name="call",
    )
    try:
        provisioned = await provision_agent_wallet(client)
        wallet_id = provisioned["agent_wallet_id"]
        permit = await create_tool_permit(
            client,
            wallet_id=wallet_id,
            key_id=provisioned["key_id"],
            tool_name="inproc-default-tool",
            idem_key="inproc-permit-defaults",
        )
        sdk = AgentMiddlewareClient(
            api_key=provisioned["agent_headers"]["X-API-Key"],
            base_url="http://test",
            transport=ASGITransport(app=app),
        )
        try:
            session = await GovernedEdgeSession.open(
                sdk, permit_id=permit["permit_id"], wallet_id=wallet_id
            )

            @langgraph_governed_tool(session, tool_name="inproc-default-tool")
            async def defaults_stub(
                message: str, label: str = "stub-label"
            ) -> dict[str, Any]:
                """Client-side stub with its own declared default."""
                raise AssertionError("stub body must never run locally")

            # The stub's declared default travels to the server; the server
            # implementation's differing default must never be substituted.
            await defaults_stub(message="explicit", idempotency_key="inproc-defaults-1")
            assert calls == 1
            assert received[-1] == {"message": "explicit", "label": "stub-label"}

            # A supplied blank/whitespace key is a caller bug: reject it
            # instead of silently degrading replay to a fresh invocation.
            for blank in ("", "   "):
                with pytest.raises(ValueError, match="idempotency_key"):
                    await defaults_stub(message="explicit", idempotency_key=blank)
            assert calls == 1  # the rejected calls never reached the server

            # Omitted key: a fresh uuid per call, so each call is a new
            # invocation with its own receipt. Explicit None behaves the same.
            await defaults_stub(message="explicit")
            first_receipt = defaults_stub.last_receipt
            await defaults_stub(message="explicit")
            second_receipt = defaults_stub.last_receipt
            await defaults_stub(message="explicit", idempotency_key=None)
            third_receipt = defaults_stub.last_receipt
            assert calls == 4
            receipt_ids = {
                first_receipt.receipt_id,
                second_receipt.receipt_id,
                third_receipt.receipt_id,
            }
            assert len(receipt_ids) == 3
        finally:
            await sdk.close()
    finally:
        registry.unregister_local("inproc-default-tool")


@pytest.mark.anyio
async def test_governed_tool_last_receipt_is_per_task(
    client,
    clean_database,
) -> None:
    """Concurrent tasks each observe their OWN receipt via last_receipt.

    The read is backed by a contextvar: task A completes its call first,
    task B then completes its own call, and only afterwards does task A read
    ``last_receipt`` — a shared mutable attribute would hand task A the
    receipt of task B's call.
    """
    registry = get_service_registry()

    def inproc_conc(message: str = "ok") -> dict[str, Any]:
        return {"message": message}

    registry.register_local(
        service_id="inproc-conc-tool",
        name="Inproc Concurrency",
        description="Governed per-task receipt tool",
        category=ServiceCategory.AGENT_COMMS,
        func=inproc_conc,
        credits_per_unit=1.0,
        unit_name="call",
    )
    try:
        provisioned = await provision_agent_wallet(client)
        wallet_id = provisioned["agent_wallet_id"]
        permit = await create_tool_permit(
            client,
            wallet_id=wallet_id,
            key_id=provisioned["key_id"],
            tool_name="inproc-conc-tool",
            idem_key="inproc-permit-conc",
        )
        sdk = AgentMiddlewareClient(
            api_key=provisioned["agent_headers"]["X-API-Key"],
            base_url="http://test",
            transport=ASGITransport(app=app),
        )
        try:
            session = await GovernedEdgeSession.open(
                sdk, permit_id=permit["permit_id"], wallet_id=wallet_id
            )

            @pydantic_ai_governed_tool(session, tool_name="inproc-conc-tool")
            async def conc_stub(message: str = "ok") -> dict[str, Any]:
                """Client-side stub; the registered tool executes server-side."""
                raise AssertionError("stub body must never run locally")

            a_called = asyncio.Event()
            b_done = asyncio.Event()

            async def first_task() -> Any:
                await conc_stub(message="alpha", idempotency_key="inproc-conc-a")
                a_called.set()
                # Read only after the other task has completed ITS call —
                # the interleaving a shared attribute cannot survive.
                await b_done.wait()
                return conc_stub.last_receipt

            async def second_task() -> Any:
                await a_called.wait()
                await conc_stub(message="beta", idempotency_key="inproc-conc-b")
                b_done.set()
                return conc_stub.last_receipt

            receipt_a, receipt_b = await asyncio.gather(first_task(), second_task())
            assert receipt_a is not None and receipt_b is not None
            assert receipt_a.receipt_id != receipt_b.receipt_id

            # Anchor each observed receipt to its own idempotency key via
            # governed replay: the same key returns the same receipt.
            await conc_stub(message="alpha", idempotency_key="inproc-conc-a")
            assert conc_stub.last_receipt.receipt_id == receipt_a.receipt_id
            await conc_stub(message="beta", idempotency_key="inproc-conc-b")
            assert conc_stub.last_receipt.receipt_id == receipt_b.receipt_id
        finally:
            await sdk.close()
    finally:
        registry.unregister_local("inproc-conc-tool")


class _RecordingReceiptService:
    """Delegates to the real ReceiptService, recording arrival order."""

    def __init__(self) -> None:
        self.sequence: list[int] = []

    async def create_receipt(self, **kwargs: Any):
        self.sequence.append(kwargs["request_payload"]["seq"])
        return await get_receipt_service().create_receipt(**kwargs)


@pytest.mark.anyio
async def test_batching_receipt_emitter_flushes_ordered_and_isolates_poison(
    client,
    clean_database,
) -> None:
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    permit = await create_tool_permit(
        client,
        wallet_id=wallet_id,
        key_id=provisioned["key_id"],
        tool_name="inproc-batch-tool",
        idem_key="inproc-permit-batch",
    )

    def receipt_kwargs(seq: int, **overrides: Any) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "permit_id": permit["permit_id"],
            "wallet_id": wallet_id,
            "key_id": provisioned["key_id"],
            "tool": "inproc-batch-tool",
            "request_payload": {"seq": seq},
            "response_payload": {"ok": True},
            "ledger_entry_id": None,
            "credits_authorized": Decimal("1"),
            "credits_charged": Decimal("1"),
            "outcome": "success",
            "audit_event_id": None,
        }
        kwargs.update(overrides)
        return kwargs

    recording = _RecordingReceiptService()
    emitter = BatchingReceiptEmitter(recording, max_batch=4, flush_interval=0.01)
    await emitter.start()

    futures = []
    for seq in range(5):
        if seq == 2:
            # Poisoned: reason_code on a success outcome is rejected by
            # ReceiptService — only this item's future may fail.
            futures.append(
                await emitter.enqueue(
                    **receipt_kwargs(seq, reason_code="poisoned_reason")
                )
            )
        else:
            futures.append(await emitter.enqueue(**receipt_kwargs(seq)))

    good = [futures[0], futures[1], futures[3], futures[4]]
    receipts = await asyncio.gather(*good)
    with pytest.raises(ReceiptError):
        await futures[2]

    # Arrival order was preserved through the batches (poison included).
    assert recording.sequence == [0, 1, 2, 3, 4]

    # Every non-poisoned receipt landed and verifies server-side.
    for receipt in receipts:
        verify_resp = await client.post(
            "/v1/receipts/verify",
            json={"receipt_id": receipt.receipt_id},
            headers=provisioned["agent_headers"],
        )
        assert verify_resp.status_code == 200
        assert verify_resp.json()["valid"] is True

    # aclose flushes what is still queued before stopping.
    tail = await emitter.enqueue(**receipt_kwargs(5))
    await emitter.aclose()
    assert tail.done()
    tail_receipt = tail.result()
    verify_resp = await client.post(
        "/v1/receipts/verify",
        json={"receipt_id": tail_receipt.receipt_id},
        headers=provisioned["agent_headers"],
    )
    assert verify_resp.json()["valid"] is True
    assert recording.sequence == [0, 1, 2, 3, 4, 5]

    # Closed means closed.
    with pytest.raises(RuntimeError, match="receipt_emitter_closed"):
        await emitter.enqueue(**receipt_kwargs(6))


class _NullReceiptService:
    """No-op sink so the perf test measures bookkeeping, not the database."""

    async def create_receipt(self, **kwargs: Any) -> dict[str, Any]:
        return kwargs


class _StallingReceiptService:
    """Stalls every create_receipt until released, counting arrivals."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.calls = 0

    async def create_receipt(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        await self.release.wait()
        return kwargs


@pytest.mark.anyio
async def test_batching_receipt_emitter_saturation_rejects_and_closes_cleanly() -> None:
    """A stalled ReceiptService must not grow an unbounded backlog.

    With ``max_pending`` items already waiting, ``enqueue`` raises
    ``ReceiptError("receipt_emitter_saturated")`` instead of queueing, and a
    rejected call leaves the queue accounting intact: once the service
    unsticks, every ACCEPTED item still flushes and ``aclose()`` returns.
    """
    service = _StallingReceiptService()
    emitter = BatchingReceiptEmitter(
        service, max_batch=1, flush_interval=0.0, max_pending=2
    )
    await emitter.start()

    first = await emitter.enqueue(seq=0)
    # Let the drain task pull the first item and stall inside create_receipt,
    # so the queue slots below are purely pending backlog.
    while service.calls == 0:
        await asyncio.sleep(0)

    second = await emitter.enqueue(seq=1)
    third = await emitter.enqueue(seq=2)
    with pytest.raises(ReceiptError, match="receipt_emitter_saturated"):
        await emitter.enqueue(seq=3)

    # Backpressure, not breakage: releasing the service drains the accepted
    # items and aclose() completes (join stays balanced despite the reject).
    service.release.set()
    await emitter.aclose()
    assert [future.result()["seq"] for future in (first, second, third)] == [0, 1, 2]
    with pytest.raises(RuntimeError, match="receipt_emitter_closed"):
        await emitter.enqueue(seq=4)


def test_batching_receipt_emitter_rejects_invalid_construction() -> None:
    """flush_interval must be finite and non-negative; max_pending positive.

    ``inf`` would make ``_drain`` wait forever on a partial batch (and
    ``aclose`` hang in ``queue.join()``); ``NaN`` slips past a plain ``< 0``
    comparison entirely.
    """
    with pytest.raises(ValueError, match="flush_interval must be finite"):
        BatchingReceiptEmitter(_NullReceiptService(), flush_interval=float("inf"))
    with pytest.raises(ValueError, match="flush_interval must be finite"):
        BatchingReceiptEmitter(_NullReceiptService(), flush_interval=float("nan"))
    with pytest.raises(ValueError, match="flush_interval must be non-negative"):
        BatchingReceiptEmitter(_NullReceiptService(), flush_interval=-0.5)
    with pytest.raises(ValueError, match="max_pending must be at least 1"):
        BatchingReceiptEmitter(_NullReceiptService(), max_pending=0)
    with pytest.raises(ValueError, match="max_batch must be at least 1"):
        BatchingReceiptEmitter(_NullReceiptService(), max_batch=0)


@pytest.mark.anyio
async def test_inprocess_validation_overhead_under_5ms() -> None:
    """Mean added overhead of local check + record + batching enqueue < 5ms.

    Compares N bare async calls against N calls that additionally run the
    validator's check+record path and a BatchingReceiptEmitter enqueue (with
    a null sink, so the number measures the in-process bookkeeping the WP
    adds — not the database write, which happens off the caller's path).

    Robust to a loaded runner: each loop is measured over three rounds and
    the BEST (minimum) elapsed time per side is compared — a scheduler
    hiccup inflates individual rounds, but the minimum approximates the
    machine's actual cost of each path.
    """
    iterations = 200
    rounds = 3
    now = datetime.now(timezone.utc)
    permit = {
        "permit_id": "permit-perf-1",
        "issuer_wallet_id": "wallet-perf",
        "subject_wallet_id": "wallet-perf",
        "subject_key_id": "key-perf",
        "scopes": ["tool:perf-tool:invoke", "billing:charge"],
        "allowed_tools": ["perf-tool"],
        "max_credits": "1000000",
        "spent_credits": "0",
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "nonce": "nonce-perf",
        "status": "active",
        "signature": "unused",
        "key_id": "unused",
        "issued_at": now.isoformat(),
    }
    validator = LocalPermitValidator(permit, {})
    emitter = BatchingReceiptEmitter(
        _NullReceiptService(),
        max_batch=64,
        # Headroom for every round's enqueues even if the drain task never
        # gets scheduled mid-round (the measured loops do not yield).
        max_pending=iterations * rounds + 16,
    )
    await emitter.start()

    async def bare(message: str) -> dict[str, str]:
        return {"message": message}

    try:
        # Warm up both paths outside the measurement.
        await bare("warmup")
        assert validator.check("perf-tool", Decimal("1")).allowed is True
        warm = await emitter.enqueue(seq=-1)

        baseline_rounds: list[float] = []
        for _ in range(rounds):
            start = time.perf_counter()
            for _ in range(iterations):
                await bare("payload")
            baseline_rounds.append(time.perf_counter() - start)

        futures = []
        governed_rounds: list[float] = []
        seq = 0
        for _ in range(rounds):
            start = time.perf_counter()
            for _ in range(iterations):
                decision = validator.check("perf-tool", Decimal("1"))
                assert decision.allowed is True
                validator.record_use("perf-tool", Decimal("1"))
                await bare("payload")
                futures.append(
                    await emitter.enqueue(seq=seq, idempotency_key=uuid.uuid4().hex)
                )
                seq += 1
            governed_rounds.append(time.perf_counter() - start)
            # Let the drain catch up between rounds so backlog from one
            # round cannot bleed into the next round's measurement.
            await asyncio.gather(*futures[-iterations:])

        mean_overhead = (min(governed_rounds) - min(baseline_rounds)) / iterations
        assert mean_overhead < 0.005, (
            f"mean in-process overhead {mean_overhead * 1000:.3f}ms "
            f"exceeds the 5ms acceptance bound "
            f"(baseline rounds {baseline_rounds}, governed rounds {governed_rounds})"
        )
        await asyncio.gather(warm, *futures)
    finally:
        await emitter.aclose()
