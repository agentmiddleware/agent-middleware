"""max_calls_per_tool must hold when reservations race.

``PermitService.authorize_and_reserve`` enforces ``max_calls_per_tool`` with an
optimistic compare-and-swap: the guarded UPDATE only matches while
``tool_call_counts_json`` still holds the value the caller read. On SQLite the
``with_for_update()`` row lock is a silent no-op, so that predicate is the only
thing stopping two callers that both read "0 calls made" from both reserving
the last slot.

``test_max_calls_cap_holds_when_both_reservations_validate_first`` forces that
schedule with a barrier; ``test_max_calls_concurrent_race`` is an end-to-end
smoke check through ``/mcp/messages`` that does not control interleaving.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import anyio
import pytest
from httpx import ASGITransport, AsyncClient

from app.db.database import get_session_factory
from app.db.models import PermitModel, ReceiptModel
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.permits import PermitService, get_permit_service
from app.services.service_registry import get_service_registry
from sqlalchemy import select, func
from tests.conftest import requires_sqlite_row_lock_noop
from tests.test_trust_helpers import (
    BOOTSTRAP_HEADERS,
    provision_agent_wallet,
)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _register_test_tool(tool_name: str, credits: float = 1.0):
    """Register a simple echo tool for testing."""
    registry = get_service_registry()

    def test_echo(input: str = "ok") -> dict:
        return {"message": input}

    registry.register_local(
        service_id=tool_name,
        name="Test Echo",
        description="Test tool for max_calls race",
        category=ServiceCategory.AGENT_COMMS,
        func=test_echo,
        credits_per_unit=credits,
        unit_name="call",
    )
    return registry


async def _create_capped_permit(
    client: AsyncClient,
    *,
    wallet_id: str,
    key_id: str,
    tool_name: str,
    max_calls: int,
    idem_key: str,
) -> str:
    """Create a permit whose only binding limit is max_calls_per_tool."""
    permit_resp = await client.post(
        "/v1/permits",
        json={
            "issuer_wallet_id": wallet_id,
            "subject_wallet_id": wallet_id,
            "subject_key_id": key_id,
            "allowed_tools": [tool_name],
            "scopes": [f"tool:{tool_name}:invoke", "billing:charge"],
            "max_credits": 50,
            "max_calls_per_tool": {tool_name: max_calls},
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=30)
            ).isoformat(),
        },
        headers={**BOOTSTRAP_HEADERS, "Idempotency-Key": idem_key},
    )
    assert permit_resp.status_code == 201, permit_resp.text
    return permit_resp.json()["permit_id"]


async def _invoke_governed(
    client: AsyncClient,
    *,
    wallet_id: str,
    permit_id: str,
    tool_name: str,
    arguments: dict | None = None,
    idem_key: str = "invoke-1",
    headers: dict | None = None,
):
    """Invoke a tool under a governed permit via JSON-RPC."""
    resp = await client.post(
        "/mcp/messages",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments or {},
                "mcpContext": {
                    "wallet_id": wallet_id,
                    "permit_id": permit_id,
                    "idempotency_key": idem_key,
                },
            },
        },
        headers=headers or BOOTSTRAP_HEADERS,
    )
    return resp


@pytest.mark.anyio
async def test_max_calls_concurrent_race(client, clean_database):
    """Smoke check: three concurrent invokes never settle more than two calls.

    This does not control interleaving, so it can pass on a lucky schedule even
    against an implementation without the compare-and-swap. The barriered test
    below is the regression guard for the race itself.
    """
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    key_id = provisioned["key_id"]
    agent_headers = provisioned["agent_headers"]
    tool_name = "race-test-echo"

    _register_test_tool(tool_name, credits=1.0)
    try:
        permit_id = await _create_capped_permit(
            client,
            wallet_id=wallet_id,
            key_id=key_id,
            tool_name=tool_name,
            max_calls=2,
            idem_key="race-permit-1",
        )

        # Launch 3 concurrent calls
        tasks = [
            _invoke_governed(
                client,
                wallet_id=wallet_id,
                permit_id=permit_id,
                tool_name=tool_name,
                arguments={"input": f"call{i}"},
                idem_key=f"race-{i}",
                headers=agent_headers,
            )
            for i in range(1, 4)
        ]

        responses = await asyncio.gather(*tasks)

        success_count = sum(
            1 for r in responses if r.status_code == 200 and "result" in r.json()
        )

        # Verify receipt count in database
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(func.count())
                .select_from(ReceiptModel)
                .where(
                    ReceiptModel.permit_id == permit_id,
                    ReceiptModel.tool == tool_name,
                    ReceiptModel.outcome == "success",
                )
            )
            actual_receipt_count = int(result.scalar() or 0)

        assert actual_receipt_count == success_count
        assert actual_receipt_count <= 2, (
            "max_calls_per_tool=2 was violated: "
            f"{actual_receipt_count} successful receipts exist"
        )

    finally:
        get_service_registry().unregister_local(tool_name)


@requires_sqlite_row_lock_noop
@pytest.mark.anyio
async def test_max_calls_cap_holds_when_both_reservations_validate_first(
    client, clean_database, monkeypatch
):
    """Two reservations that both read "0 calls made" cannot both take the slot.

    Both callers are held at a barrier immediately after validation -- after
    each has loaded the permit row and approved the call against
    ``max_calls_per_tool={tool: 1}``, and before either issues its guarded
    UPDATE -- and are released together. Each therefore computes the same next
    counter value from the same stale read, and the credit budget has room for
    both, so the compare-and-swap on ``tool_call_counts_json`` is the only
    thing that can deny the second caller. Without the barrier the race only
    happens on a lucky schedule.

    Skipped on PostgreSQL: there the row lock is real, so the second caller
    blocks before validation while the first waits at the barrier.
    """
    provisioned = await provision_agent_wallet(client)
    tool_name = "race-test-cas"
    permit_id = await _create_capped_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name=tool_name,
        max_calls=1,
        idem_key="race-cas-permit",
    )

    concurrency = 2
    original_validate = PermitService._validate_model_for_action
    arrived = 0
    both_validated = anyio.Event()

    async def _barriered_validate(self, **kwargs):
        # Validate for real first, so each caller reads and approves the
        # permit exactly as production does...
        result = await original_validate(self, **kwargs)
        nonlocal arrived
        arrived += 1
        if arrived >= concurrency:
            both_validated.set()
        # ...then hold every caller until all of them have. fail_after keeps
        # a lost caller from hanging the suite.
        with anyio.fail_after(30):
            await both_validated.wait()
        return result

    monkeypatch.setattr(
        PermitService, "_validate_model_for_action", _barriered_validate
    )

    results: dict[int, object] = {}

    async def _reserve(index: int) -> None:
        try:
            results[index] = await get_permit_service().authorize_and_reserve(
                permit_id=permit_id,
                wallet_id=provisioned["agent_wallet_id"],
                tool_name=tool_name,
                estimated_credits=Decimal("1"),
                key_id=provisioned["key_id"],
            )
        except Exception as exc:  # surfaced below as a failure, not swallowed
            results[index] = exc

    with anyio.fail_after(90):
        async with anyio.create_task_group() as tg:
            for index in range(concurrency):
                tg.start_soon(_reserve, index)

    # A loser that hit a transient SQLite write conflict restarts its whole
    # transaction and passes the (already open) barrier a second time.
    assert arrived >= concurrency, "both callers must reach the barrier"
    outcomes = list(results.values())
    assert len(outcomes) == concurrency
    for outcome in outcomes:
        assert not isinstance(outcome, Exception), outcome

    allowed = [v for v in outcomes if v.allowed]
    denied = [v for v in outcomes if not v.allowed]
    assert len(allowed) == 1, "exactly one reservation may take the only call"
    assert len(denied) == 1
    assert denied[0].reason == "permit_max_calls_exceeded"
    assert denied[0].details == {"tool": tool_name, "limit": 1, "calls_made": 1}

    # The stored counter and budget reflect exactly one reservation.
    factory = get_session_factory()
    async with factory() as session:
        stored = await session.get(PermitModel, permit_id)
        assert stored is not None
        assert json.loads(stored.tool_call_counts_json) == {tool_name: 1}
        assert stored.spent_credits == Decimal("1")
