"""A lost local counter comparison must not become a permanent budget denial."""

from __future__ import annotations

import asyncio
import json
from contextvars import ContextVar
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import false, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.resilience import WRITE_CONFLICT_MAX_ATTEMPTS
from app.core.time import utc_now
from app.db.database import get_session_factory
from app.db.models import LedgerEntryModel, PermitModel, ReceiptModel
from app.services.permits import get_permit_service
from app.services.receipts import ReceiptService
from tests.test_permit_write_contention_surface import (
    _call_body,
    client as _client,
    echo_tool as _echo_tool,
)
from tests.test_trust_helpers import BOOTSTRAP_HEADERS, provision_agent_wallet

client = _client
echo_tool = _echo_tool


async def _permit(client, ctx, tool, *, cap=10, budget=100):
    response = await client.post(
        "/v1/permits",
        json={
            "issuer_wallet_id": ctx["agent_wallet_id"],
            "subject_wallet_id": ctx["agent_wallet_id"],
            "subject_key_id": ctx["key_id"],
            "allowed_tools": [tool],
            "scopes": [f"tool:{tool}:invoke", "billing:charge"],
            "max_credits": budget,
            "max_calls_per_tool": {tool: cap},
            "expires_at": (utc_now() + timedelta(hours=1)).isoformat(),
        },
        headers={**BOOTSTRAP_HEADERS, "Idempotency-Key": "counter-permit"},
    )
    assert response.status_code == 201, response.text
    return response.json()["permit_id"]


def _synchronize_reservation_reads(monkeypatch):
    """Hold two real reads before their real SQLite guarded updates."""
    service_type = type(get_permit_service())
    original_reserve = service_type.authorize_and_reserve
    original_validate = service_type._validate_model_for_action
    in_reservation = ContextVar("counter_reservation", default=False)
    both_read = asyncio.Event()
    reads = 0

    async def reserve(self, **kwargs):
        token = in_reservation.set(True)
        try:
            return await original_reserve(self, **kwargs)
        finally:
            in_reservation.reset(token)

    async def validate(self, **kwargs):
        nonlocal reads
        result = await original_validate(self, **kwargs)
        if in_reservation.get():
            if kwargs["session"].bind.dialect.name != "sqlite":
                pytest.skip("PostgreSQL row locks serialize these reads")
            reads += 1
            if reads == 2:
                both_read.set()
            await asyncio.wait_for(both_read.wait(), timeout=5)
        return result

    monkeypatch.setattr(service_type, "authorize_and_reserve", reserve)
    monkeypatch.setattr(service_type, "_validate_model_for_action", validate)


async def _accounting(permit_id):
    async with get_session_factory()() as session:
        receipts = (
            (
                await session.execute(
                    select(ReceiptModel).where(ReceiptModel.permit_id == permit_id)
                )
            )
            .scalars()
            .all()
        )
        debits = (
            (
                await session.execute(
                    select(LedgerEntryModel).where(LedgerEntryModel.action == "debit")
                )
            )
            .scalars()
            .all()
        )
    return receipts, debits


async def _assert_reserved(permit_id, tool, count):
    async with get_session_factory()() as session:
        permit = await session.get(PermitModel, permit_id)
    assert permit is not None
    assert permit.spent_credits == Decimal(2 * count)
    assert json.loads(permit.tool_call_counts_json or "{}").get(tool, 0) == count


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("cap", "budget", "denial"),
    [
        (10, 100, None),
        (1, 100, "permit_max_calls_exceeded"),
        (10, 2, "permit_budget_exceeded"),
    ],
)
async def test_concurrent_reservations_distinguish_contention_from_denial(
    client, clean_database, monkeypatch, cap, budget, denial
):
    ctx = await provision_agent_wallet(client)
    tool = "local.counter"
    permit_id = await _permit(client, ctx, tool, cap=cap, budget=budget)
    _synchronize_reservation_reads(monkeypatch)

    async def reserve():
        return await get_permit_service().authorize_and_reserve(
            permit_id=permit_id,
            wallet_id=ctx["agent_wallet_id"],
            tool_name=tool,
            estimated_credits=Decimal("2"),
            key_id=ctx["key_id"],
        )

    results = await asyncio.gather(reserve(), reserve())
    assert [r.reason for r in results if not r.allowed] == (
        [] if denial is None else [denial]
    )
    await _assert_reserved(permit_id, tool, 2 if denial is None else 1)


@pytest.mark.anyio
async def test_concurrent_http_calls_and_replays_charge_once_per_key(
    client, clean_database, monkeypatch, echo_tool
):
    ctx = await provision_agent_wallet(client)
    tool, runs = echo_tool
    permit_id = await _permit(client, ctx, tool)
    _synchronize_reservation_reads(monkeypatch)

    async def invoke(key):
        response = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool,
                wallet_id=ctx["agent_wallet_id"],
                permit_id=permit_id,
                idempotency_key=key,
            ),
            headers=ctx["agent_headers"],
        )
        assert response.status_code == 200, response.text
        assert "error" not in response.json(), response.text
        return response.json()

    keys = ["counter-a", "counter-b"]
    results = await asyncio.gather(*(invoke(key) for key in keys))
    for key, expected in zip(keys, results, strict=True):
        assert await invoke(key) == expected
    assert runs["count"] == 2
    await _assert_reserved(permit_id, tool, 2)
    receipts, debits = await _accounting(permit_id)
    assert len(receipts) == len(debits) == 2
    assert {r.ledger_entry_id for r in receipts} == {d.entry_id for d in debits}
    assert all(d.amount == Decimal("-2") for d in debits)
    assert len({r.idempotency_record_id for r in receipts}) == 2
    for receipt in receipts:
        valid, reason, _ = await ReceiptService().verify_receipt(receipt.receipt_id)
        assert valid, reason


@pytest.mark.anyio
async def test_persistent_counter_collision_exhausts_without_effect_and_can_retry(
    client, clean_database, echo_tool
):
    ctx = await provision_agent_wallet(client)
    tool, runs = echo_tool
    permit_id = await _permit(client, ctx, tool)
    body = _call_body(
        tool_name=tool,
        wallet_id=ctx["agent_wallet_id"],
        permit_id=permit_id,
        idempotency_key="counter-exhausted",
    )
    original_execute = AsyncSession.execute
    misses = 0

    async def lose_comparison(session, statement, *args, **kwargs):
        nonlocal misses
        if getattr(statement, "is_update", False) and statement.table.name == "permits":
            misses += 1
            # A real zero-row update, with no mutation, on every retry.
            statement = statement.where(false())
        return await original_execute(session, statement, *args, **kwargs)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(AsyncSession, "execute", lose_comparison)
        response = await client.post(
            "/mcp/messages", json=body, headers=ctx["agent_headers"]
        )
    assert response.status_code == 200, response.text
    assert response.json()["error"]["code"] == -32005
    assert response.json()["error"]["message"] == "permit_write_contended"
    assert misses == WRITE_CONFLICT_MAX_ATTEMPTS
    assert runs["count"] == 0
    await _assert_reserved(permit_id, tool, 0)
    assert await _accounting(permit_id) == ([], [])

    retry = await client.post("/mcp/messages", json=body, headers=ctx["agent_headers"])
    assert retry.status_code == 200 and "error" not in retry.json(), retry.text
    replay = await client.post("/mcp/messages", json=body, headers=ctx["agent_headers"])
    assert replay.json() == retry.json()
    assert runs["count"] == 1
    await _assert_reserved(permit_id, tool, 1)
    receipts, debits = await _accounting(permit_id)
    assert len(receipts) == len(debits) == 1
    assert receipts[0].ledger_entry_id == debits[0].entry_id
    assert debits[0].amount == Decimal("-2")
