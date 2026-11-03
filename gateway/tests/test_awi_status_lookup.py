"""Data-free status lookup tests with in-memory contexts and service doubles."""

import asyncio
import functools
import inspect
from dataclasses import FrozenInstanceError
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.services import awi_http_governance as awi
from app.services import mcp_dispatch_reconciliation as mcp
from app.services.permits import register_permit_upstream_status_lookup


@pytest.mark.anyio
@pytest.mark.parametrize("error_path", [False, True])
@pytest.mark.parametrize(
    "answer_kind",
    [
        "absent",
        "none",
        "unknown",
        "not_dispatched",
        "delivered",
        "raises",
        "timeout",
        "malformed",
    ],
)
async def test_awi_status_outcomes(monkeypatch, error_path, answer_kind):
    ctx = awi.AwiHttpGovernedContext(
        auth=SimpleNamespace(),
        wallet_id="test-wallet",
        permit_id="test-permit",
        idempotency_key="test-key",
        tool_name="awi_execute",
        credits=Decimal("3"),
        permit=SimpleNamespace(),
        endpoint="POST /v1/awi/execute",
        record_id="test-record",
        request_payload={"commands": []},
        ledger_entry_id="test-charge",
        credits_charged=Decimal("3"),
        dispatch_started=True,
    )
    queries = []

    async def lookup(query):
        queries.append(query)
        if answer_kind == "raises":
            raise RuntimeError("provider unavailable")
        if answer_kind == "timeout":
            await asyncio.Event().wait()
        if answer_kind == "none":
            return None
        if answer_kind == "malformed":
            return {"effect_status": "not_dispatched"}
        return awi.AwiStatusAnswer(effect_status=answer_kind)

    monkeypatch.setattr(awi, "_REGISTERED_AWI_STATUS_LOOKUP", None)
    monkeypatch.setattr(awi, "STATUS_LOOKUP_TIMEOUT_SECONDS", 0.001)
    if answer_kind != "absent":
        awi.register_awi_status_lookup(lookup)
    money = SimpleNamespace(refund_charge=AsyncMock())
    permits = SimpleNamespace(release_budget=AsyncMock())
    monkeypatch.setattr(awi, "get_agent_money", lambda: money)
    monkeypatch.setattr(awi, "get_permit_service", lambda: permits)
    finalize = AsyncMock(return_value={"error": "action_failed"})
    monkeypatch.setattr(awi, "_finalize_awi_http", finalize)
    response = {"status": "failed", "error": "action_failed"}
    if error_path:
        with pytest.raises(HTTPException) as exc:
            await awi.raise_awi_http_error(ctx, status_code=502, detail=response)
        assert exc.value.status_code == 502
    else:
        await awi.complete_awi_http_governed(
            ctx, request_payload=ctx.request_payload, response_payload=response
        )
    refunded = answer_kind == "not_dispatched"
    assert finalize.call_args.kwargs["outcome"] == (
        "failed_refunded" if refunded else "delivery_uncertain"
    )
    assert finalize.call_args.kwargs["response_payload"] == response
    assert money.refund_charge.await_count == int(refunded)
    assert permits.release_budget.await_count == int(refunded)
    assert ctx.credits_charged == (Decimal("0") if refunded else Decimal("3"))
    assert len(queries) == int(answer_kind != "absent")
    if queries:
        query = queries[0]
        assert query.wallet_id == ctx.wallet_id
        assert query.permit_id == ctx.permit_id
        assert query.idempotency_key == ctx.idempotency_key
        assert query.record_id == ctx.record_id
        assert query.tool_name == ctx.tool_name
        assert query.endpoint == ctx.endpoint
        assert len(query.request_hash) == 64
        with pytest.raises(FrozenInstanceError):
            query.wallet_id = "different"


@pytest.mark.parametrize(
    "register",
    [
        awi.register_awi_status_lookup,
        functools.partial(
            register_permit_upstream_status_lookup, "https://test.invalid"
        ),
    ],
)
def test_registration_rejects_sync(register):
    with pytest.raises(TypeError, match="async callable"):
        register(lambda query: None)


def test_permit_registration_delegates(monkeypatch):
    monkeypatch.setattr(mcp, "_REGISTERED_STATUS_LOOKUPS", {})

    async def lookup(query):
        return None

    register_permit_upstream_status_lookup("https://test.invalid", lookup)
    assert mcp._REGISTERED_STATUS_LOOKUPS == {"https://test.invalid": lookup}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "response",
    [{"status": "success"}, {"status": "failed", "effect_status": "not_dispatched"}],
)
async def test_awi_lookup_only_for_uncertain(monkeypatch, response):
    lookup = AsyncMock()
    monkeypatch.setattr(awi, "_REGISTERED_AWI_STATUS_LOOKUP", lookup)
    monkeypatch.setattr(awi, "_refund_undispatched", AsyncMock())
    monkeypatch.setattr(awi, "_finalize_awi_http", AsyncMock())
    ctx = SimpleNamespace(replay_response=None)
    await awi.complete_awi_http_governed(
        ctx, request_payload={}, response_payload=response
    )
    lookup.assert_not_called()


@pytest.mark.parametrize("wrapper", ["function", "partial", "method", "object"])
def test_awi_registration_accepts_async_forms(monkeypatch, wrapper):
    monkeypatch.setattr(awi, "_REGISTERED_AWI_STATUS_LOOKUP", None)

    class Lookup:
        async def __call__(self, query):
            return None

    async def lookup(query):
        return None

    candidate = {
        "function": lookup,
        "partial": functools.partial(lookup),
        "method": Lookup().__call__,
        "object": Lookup(),
    }[wrapper]
    awi.register_awi_status_lookup(candidate)
    assert awi._REGISTERED_AWI_STATUS_LOOKUP is candidate


def test_awi_registration_rejects_marked_sync():
    if not hasattr(inspect, "markcoroutinefunction"):
        pytest.skip("inspect.markcoroutinefunction requires Python 3.12")
    marked = inspect.markcoroutinefunction(lambda query: None)
    with pytest.raises(TypeError, match="async callable"):
        awi.register_awi_status_lookup(marked)
