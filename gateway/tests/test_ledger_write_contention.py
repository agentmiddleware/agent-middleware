"""A lost SQLite snapshot on the governed debit must never wedge the caller.

SQLite in WAL mode cannot make a transaction that has already read upgrade to a
write once another connection has committed: it answers SQLITE_BUSY_SNAPSHOT
and, deliberately, does not consult the busy handler, because waiting could
only deadlock. The governed charge reads the idempotency record, the dispatch
attempt and the wallet before its first write, so it is exactly that shape, and
the only cure the engine offers is to restart the whole transaction.

Before the fix that lock escaped as an unclassified ``internal_error``. That was
the visible symptom; the injury was quieter. Nothing had been charged, but the
permit reservation and the ``in_progress`` idempotency record were both
committed, and neither self-heals -- the reaper deliberately leaves a local
record with no debit exactly as it found it. A caller that followed the
documented advice and retried its key got ``idempotency_in_progress`` forever,
against a reservation it could never spend.

Every conflict here is injected, never raced: a test that waits for real
contention is a test that passes on a fast machine for the wrong reason. The
injection shape is borrowed from
``tests/test_idempotency.py::test_begin_translates_sqlite_lock_error_to_in_progress``,
including its final case -- a substantive OperationalError must still propagate,
or the retry would be a way to hide real faults.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_session_factory
from app.db.models import McpDispatchAttemptModel
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.billing_engine import BillingEngine
from app.services.idempotency import get_idempotency_service
from app.services.mcp_dispatch_reconciliation import (
    get_mcp_dispatch_reconciliation_service,
)
from app.services.permits import get_permit_service
from app.services.service_registry import get_service_registry
from tests.test_mcp_upstream_governed import FakeUpstreamExecutor
from tests.test_trust_helpers import (
    BOOTSTRAP_HEADERS,
    create_tool_permit,
    provision_agent_wallet,
)

TOOL_COST = 2.0


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as value:
        yield value


def _register_local_tool(tool_name: str, func: Any) -> None:
    get_service_registry().register_local(
        service_id=tool_name,
        name=f"Contention {tool_name}",
        description="Ledger write-contention test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=func,
        credits_per_unit=TOOL_COST,
        unit_name="call",
    )


def _call_body(
    *,
    tool_name: str,
    wallet_id: str,
    permit_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": f"call-{idempotency_key}",
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": {"message": "hello"},
            "mcpContext": {
                "wallet_id": wallet_id,
                "permit_id": permit_id,
                "idempotency_key": idempotency_key,
            },
        },
    }


def _lock_error() -> OperationalError:
    return OperationalError("UPDATE wallets ...", {}, Exception("database is locked"))


class _FlushFault:
    """Fail the first ``n`` flushes with a write conflict, then let them land.

    ``n=None`` fails every flush, which is how the exhaustion path is driven.
    Patching ``flush`` rather than ``commit`` puts the fault where the real one
    occurs: the velocity write is the governed transaction's first write, and
    it is the statement that has to upgrade the read snapshot.

    ``patch`` hands back a plain function rather than this instance because
    only a function is a descriptor: assigned to the class, it binds and
    receives the session, which an instance with ``__call__`` would not.
    """

    def __init__(self, n: int | None, error: OperationalError | None = None) -> None:
        self.remaining = n
        self.calls = 0
        self._error = error or _lock_error()
        self._real = AsyncSession.flush

    def patch(self, monkeypatch: Any) -> "_FlushFault":
        fault = self

        async def flush(session: AsyncSession, *args: Any, **kwargs: Any) -> Any:
            fault.calls += 1
            if fault.remaining is None or fault.calls <= fault.remaining:
                raise fault._error
            return await fault._real(session, *args, **kwargs)

        monkeypatch.setattr(AsyncSession, "flush", flush)
        return self


async def _spent_credits(permit_id: str) -> Decimal:
    permit = await get_permit_service().get_permit(permit_id)
    assert permit is not None
    return Decimal(str(permit.spent_credits))


async def _debit_count(
    client: AsyncClient, wallet_id: str, headers: dict[str, str], tool_name: str
) -> int:
    resp = await client.get(f"/v1/billing/ledger/{wallet_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    return sum(
        1
        for entry in resp.json()["entries"]
        if entry["service_category"] == "agent_comms"
        and tool_name in entry.get("description", "")
        and entry.get("action") == "debit"
    )


@pytest.fixture
def governed_tool():
    tool_name = "contention-echo"
    runs = {"count": 0}

    def echo(message: str = "ok") -> dict[str, Any]:
        runs["count"] += 1
        return {"message": message}

    _register_local_tool(tool_name, echo)
    try:
        yield tool_name, runs
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_transient_conflict_is_retried_and_charges_exactly_once(
    client: AsyncClient, clean_database: None, governed_tool, monkeypatch
) -> None:
    """A conflict that clears must cost the caller nothing but latency."""
    tool_name, runs = governed_tool
    ctx = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=ctx["agent_wallet_id"],
        key_id=ctx["key_id"],
        tool_name=tool_name,
        max_credits=10,
        idem_key="contention-permit-1",
    )

    fault = _FlushFault(2).patch(monkeypatch)

    resp = await client.post(
        "/mcp/messages",
        json=_call_body(
            tool_name=tool_name,
            wallet_id=ctx["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key="contention-ok-1",
        ),
        headers=ctx["agent_headers"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "error" not in body, body
    assert body["result"]["receipt"]["outcome"] == "success"

    monkeypatch.undo()
    # Retried, not skipped: the fault has to have actually fired.
    assert fault.calls > 2
    # And the retry may not multiply anything the caller pays for.
    assert (
        await _debit_count(
            client, ctx["agent_wallet_id"], ctx["agent_headers"], tool_name
        )
        == 1
    )
    assert await _spent_credits(permit["permit_id"]) == Decimal(str(TOOL_COST))
    assert runs["count"] == 1


@pytest.mark.anyio
async def test_persistent_conflict_answers_typed_and_never_internal_error(
    client: AsyncClient, clean_database: None, governed_tool, monkeypatch
) -> None:
    """The whole point: contention is a reason, not an unclassified failure."""
    tool_name, _ = governed_tool
    ctx = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=ctx["agent_wallet_id"],
        key_id=ctx["key_id"],
        tool_name=tool_name,
        max_credits=10,
        idem_key="contention-permit-2",
    )

    _FlushFault(None).patch(monkeypatch)
    resp = await client.post(
        "/mcp/messages",
        json=_call_body(
            tool_name=tool_name,
            wallet_id=ctx["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key="contention-wedge-1",
        ),
        headers=ctx["agent_headers"],
    )
    monkeypatch.undo()

    assert resp.status_code == 200, resp.text
    error = resp.json()["error"]
    assert error["message"] == "ledger_write_contended"
    assert error["code"] == -32005
    # The regression this file exists for: an unclassified answer tells the
    # caller nothing about whether its money moved.
    assert error["message"] != "internal_error"
    assert "correlation_id" not in (error.get("data") or {})


@pytest.mark.anyio
async def test_persistent_conflict_leaves_no_charge_and_frees_the_key(
    client: AsyncClient, clean_database: None, governed_tool, monkeypatch
) -> None:
    """Nothing moved, so nothing may be left holding the caller's budget."""
    tool_name, runs = governed_tool
    ctx = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=ctx["agent_wallet_id"],
        key_id=ctx["key_id"],
        tool_name=tool_name,
        max_credits=10,
        idem_key="contention-permit-3",
    )

    _FlushFault(None).patch(monkeypatch)
    await client.post(
        "/mcp/messages",
        json=_call_body(
            tool_name=tool_name,
            wallet_id=ctx["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key="contention-wedge-2",
        ),
        headers=ctx["agent_headers"],
    )
    monkeypatch.undo()

    assert (
        await _debit_count(
            client, ctx["agent_wallet_id"], ctx["agent_headers"], tool_name
        )
        == 0
    )
    # The reservation is handed back rather than left to expire with the permit.
    assert await _spent_credits(permit["permit_id"]) == Decimal("0")
    # The tool never ran: the charge precedes execution on the local path.
    assert runs["count"] == 0
    # And the key is free, not stuck in progress forever.
    record = await get_idempotency_service().get_record(
        wallet_id=ctx["agent_wallet_id"],
        endpoint="/mcp/messages",
        idempotency_key="contention-wedge-2",
    )
    assert record is None


@pytest.mark.anyio
async def test_the_same_key_succeeds_once_contention_clears(
    client: AsyncClient, clean_database: None, governed_tool, monkeypatch
) -> None:
    """The wedge, as a regression test. This fails before the fix.

    A caller that does exactly what docs/failure-semantics.md tells it to do --
    retry the same idempotency key -- must get its call, not a permanent
    ``idempotency_in_progress`` against a reservation it cannot spend.
    """
    tool_name, runs = governed_tool
    ctx = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=ctx["agent_wallet_id"],
        key_id=ctx["key_id"],
        tool_name=tool_name,
        max_credits=10,
        idem_key="contention-permit-4",
    )
    body = _call_body(
        tool_name=tool_name,
        wallet_id=ctx["agent_wallet_id"],
        permit_id=permit["permit_id"],
        idempotency_key="contention-retry-me",
    )

    _FlushFault(None).patch(monkeypatch)
    first = await client.post("/mcp/messages", json=body, headers=ctx["agent_headers"])
    monkeypatch.undo()
    assert first.json()["error"]["message"] == "ledger_write_contended"

    second = await client.post("/mcp/messages", json=body, headers=ctx["agent_headers"])
    assert second.status_code == 200, second.text
    assert "error" not in second.json(), second.text
    assert second.json()["result"]["receipt"]["outcome"] == "success"
    assert (
        await _debit_count(
            client, ctx["agent_wallet_id"], ctx["agent_headers"], tool_name
        )
        == 1
    )
    assert runs["count"] == 1


@pytest.mark.anyio
async def test_a_substantive_operational_error_is_not_retried(
    client: AsyncClient, clean_database: None, governed_tool, monkeypatch
) -> None:
    """The retry must not become a way to hide real database faults.

    A disk I/O error is not contention. It must propagate on the first attempt
    and still land as ``internal_error``, which is the honest answer for a
    fault the gateway cannot classify -- and which the disclosure contract in
    ``tests/test_mcp_internal_error_disclosure.py`` depends on continuing to
    exist.
    """
    tool_name, _ = governed_tool
    ctx = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=ctx["agent_wallet_id"],
        key_id=ctx["key_id"],
        tool_name=tool_name,
        max_credits=10,
        idem_key="contention-permit-5",
    )

    fault = _FlushFault(
        None,
        error=OperationalError("UPDATE wallets ...", {}, Exception("disk I/O error")),
    ).patch(monkeypatch)
    resp = await client.post(
        "/mcp/messages",
        json=_call_body(
            tool_name=tool_name,
            wallet_id=ctx["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key="contention-disk-1",
        ),
        headers=ctx["agent_headers"],
    )
    monkeypatch.undo()

    assert resp.json()["error"]["message"] == "internal_error"
    # One attempt, not forty: a substantive fault is not worth retrying, and
    # retrying it would delay the answer by the whole backoff budget.
    assert fault.calls == 1


@pytest.mark.anyio
async def test_invalid_units_are_refused_before_any_retry(
    clean_database: None, monkeypatch
) -> None:
    """Validation stays outside the retried closure.

    ``units <= 0`` mints credits if it reaches a write, so it is refused at the
    choke point every caller passes through. The retry must not put a loop
    between the caller and that guard.
    """
    from app.services.agent_money import get_agent_money

    fault = _FlushFault(None).patch(monkeypatch)
    engine = get_agent_money()._billing_engine
    with pytest.raises(ValueError, match="greater than zero"):
        await engine.charge(
            wallet_id="agt-does-not-matter",
            service_category=ServiceCategory.AGENT_COMMS,
            units=Decimal("-1"),
            operation_key="never-used",
        )
    with pytest.raises(ValueError, match="finite"):
        await engine.charge(
            wallet_id="agt-does-not-matter",
            service_category=ServiceCategory.AGENT_COMMS,
            units=Decimal("Infinity"),
            operation_key="never-used",
        )
    monkeypatch.undo()
    # Refused before a transaction was ever opened.
    assert fault.calls == 0


@pytest.mark.anyio
async def test_another_wallet_cannot_reuse_the_freed_key(
    client: AsyncClient, clean_database: None, governed_tool, monkeypatch
) -> None:
    """Freeing the key must not widen who may use it.

    Idempotency records are scoped by (wallet, endpoint, key). Abandoning one
    wallet's record on contention must leave a different wallet exactly where
    it was -- it may neither read the first wallet's operation nor spend
    against its permit.
    """
    tool_name, _ = governed_tool
    victim = await provision_agent_wallet(client)
    other = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=victim["agent_wallet_id"],
        key_id=victim["key_id"],
        tool_name=tool_name,
        max_credits=10,
        idem_key="contention-permit-6",
    )
    shared_key = "contention-cross-wallet"

    _FlushFault(None).patch(monkeypatch)
    await client.post(
        "/mcp/messages",
        json=_call_body(
            tool_name=tool_name,
            wallet_id=victim["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=shared_key,
        ),
        headers=victim["agent_headers"],
    )
    monkeypatch.undo()

    # The other wallet presenting the victim's permit is refused, and the
    # refusal is a denial rather than an unclassified error.
    resp = await client.post(
        "/mcp/messages",
        json=_call_body(
            tool_name=tool_name,
            wallet_id=other["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=shared_key,
        ),
        headers=other["agent_headers"],
    )
    payload = resp.json()
    assert "error" in payload, payload
    assert payload["error"]["message"] != "internal_error"
    assert (
        await _debit_count(
            client, other["agent_wallet_id"], other["agent_headers"], tool_name
        )
        == 0
    )


def _register_upstream_tool(tool_name: str, executor: Any) -> None:
    get_service_registry().register_upstream(
        service_id=tool_name,
        name="Contention upstream tool",
        description="Ledger write-contention test tool, remote backend",
        category=ServiceCategory.AGENT_COMMS,
        executor=executor,
        input_schema={
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        credits_per_unit=TOOL_COST,
        upstream_tool_name="partner.echo",
        upstream_origin="https://partner.example",
    )


async def _all_attempt_ids() -> list[str]:
    async with get_session_factory()() as session:
        rows = await session.execute(select(McpDispatchAttemptModel.attempt_id))
        return [row[0] for row in rows.all()]


@pytest.mark.anyio
async def test_a_contended_remote_charge_releases_its_reservation_once(
    client: AsyncClient, clean_database: None, monkeypatch
) -> None:
    """A remote reservation is attempt-keyed, so only reconciliation frees it.

    On the local path nothing durable owns the reservation, so the contention
    handler hands it back itself. Remote is the opposite: the prepared dispatch
    attempt owns it, and the single legal release is
    ``release_dispatch_budget_once``, once that attempt is terminal. Releasing
    it by hand as well would decrement the same reservation twice -- once in
    the handler, once when reconciliation or the stale sweep finalizes the
    attempt -- giving the permit back credits nobody reserved and letting it
    spend past its cap.

    The charge left standing by the first call is what makes a second decrement
    visible at all: ``release_budget`` clamps at zero, so against an otherwise
    idle permit a double release looks exactly like a correct one.
    """
    tool_name = "contention-upstream-echo"
    executor = FakeUpstreamExecutor(mode="success")
    _register_upstream_tool(tool_name, executor)
    try:
        ctx = await provision_agent_wallet(client)
        permit = await create_tool_permit(
            client,
            wallet_id=ctx["agent_wallet_id"],
            key_id=ctx["key_id"],
            tool_name=tool_name,
            max_credits=10,
            idem_key="contention-permit-remote",
        )
        permit_id = permit["permit_id"]

        first = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=ctx["agent_wallet_id"],
                permit_id=permit_id,
                idempotency_key="contention-remote-1",
            ),
            headers=ctx["agent_headers"],
        )
        assert first.status_code == 200, first.text
        assert "error" not in first.json(), first.text
        charged = await _spent_credits(permit_id)
        assert charged > Decimal("0"), (
            "the permit must be holding one real reservation before the "
            "contended call, or a double release cannot be told apart from "
            "the correct single one"
        )

        async def _always_contended(self: Any, **_kwargs: Any) -> Any:
            raise _lock_error()

        monkeypatch.setattr(BillingEngine, "_charge_once", _always_contended)
        contended = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=ctx["agent_wallet_id"],
                permit_id=permit_id,
                idempotency_key="contention-remote-2",
            ),
            headers=ctx["agent_headers"],
        )
        monkeypatch.undo()

        # The charge precedes dispatch, so the contended call never sent.
        assert executor.dispatch_count == 1
        # A lost write conflict is transient on either backend, so the remote
        # answer matches the local one instead of publishing a terminal outcome
        # that would make this key permanently unusable.
        assert contended.status_code == 200, contended.text
        error = contended.json()["error"]
        assert error["message"] == "ledger_write_contended", error
        # Exactly one decrement: the contended call's own reservation, never
        # the one the first call is still holding.
        assert await _spent_credits(permit_id) == charged

        # The prepared row is gone, not left terminal or awaiting a sweep, so
        # there is no second reservation release pending anywhere.
        assert len(await _all_attempt_ids()) == 1
        reconciler = get_mcp_dispatch_reconciliation_service()
        for attempt_id in await _all_attempt_ids():
            await reconciler.reconcile_attempt(attempt_id)
        assert await _spent_credits(permit_id) == charged

        # The point of answering retryable: the same key runs once contention
        # clears, rather than replaying a verdict about a call that never made
        # it out of the gateway.
        retry = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=ctx["agent_wallet_id"],
                permit_id=permit_id,
                idempotency_key="contention-remote-2",
            ),
            headers=ctx["agent_headers"],
        )
        assert retry.status_code == 200, retry.text
        assert "error" not in retry.json(), retry.text
        assert executor.dispatch_count == 2
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_a_capped_permit_can_still_spend_its_call_after_contention(
    client: AsyncClient, clean_database: None, governed_tool, monkeypatch
) -> None:
    """Giving back the credits is only half the reservation.

    ``authorize_and_reserve`` increments the ``max_calls_per_tool`` counter
    atomically with the budget, so a compensation that returns only the credits
    leaves the use consumed. This path's whole answer is "retry this key", and
    on a permit capped at one call that retry would be denied
    ``permit_max_calls_exceeded`` for a call that never ran, never charged and
    never dispatched -- one lost write conflict would end the permit.
    """
    tool_name, runs = governed_tool
    ctx = await provision_agent_wallet(client)
    permit_resp = await client.post(
        "/v1/permits",
        json={
            "issuer_wallet_id": ctx["agent_wallet_id"],
            "subject_wallet_id": ctx["agent_wallet_id"],
            "subject_key_id": ctx["key_id"],
            "allowed_tools": [tool_name],
            "scopes": [f"tool:{tool_name}:invoke", "billing:charge"],
            "max_credits": 10,
            "max_calls_per_tool": {tool_name: 1},
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=30)
            ).isoformat(),
        },
        headers={**BOOTSTRAP_HEADERS, "Idempotency-Key": "contention-permit-capped"},
    )
    assert permit_resp.status_code == 201, permit_resp.text
    permit_id = permit_resp.json()["permit_id"]

    _FlushFault(None).patch(monkeypatch)
    await client.post(
        "/mcp/messages",
        json=_call_body(
            tool_name=tool_name,
            wallet_id=ctx["agent_wallet_id"],
            permit_id=permit_id,
            idempotency_key="contention-capped-1",
        ),
        headers=ctx["agent_headers"],
    )
    monkeypatch.undo()

    assert runs["count"] == 0

    retry = await client.post(
        "/mcp/messages",
        json=_call_body(
            tool_name=tool_name,
            wallet_id=ctx["agent_wallet_id"],
            permit_id=permit_id,
            idempotency_key="contention-capped-1",
        ),
        headers=ctx["agent_headers"],
    )
    assert retry.status_code == 200, retry.text
    body = retry.json()
    assert "error" not in body, body
    assert runs["count"] == 1
