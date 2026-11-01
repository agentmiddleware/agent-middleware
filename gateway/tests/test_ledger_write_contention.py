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
from app.services.ownership import CallerIdentity
from app.db.models import McpDispatchAttemptModel
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.billing_engine import BillingEngine
from app.services.idempotency import (
    GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
    get_idempotency_service,
)
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

    def patch(self, monkeypatch: Any) -> _FlushFault:
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
            caller=CallerIdentity.admin(),
        )
    with pytest.raises(ValueError, match="finite"):
        await engine.charge(
            wallet_id="agt-does-not-matter",
            service_category=ServiceCategory.AGENT_COMMS,
            units=Decimal("Infinity"),
            operation_key="never-used",
            caller=CallerIdentity.admin(),
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


# ---------------------------------------------------------------------------
# Pre-charge faults must release the quote, the reservation and the record.
#
# These faults are injected at the exact call that raises (the debit, the wallet
# policy, the daily-spend read), one shot each, so the retry runs against the
# state the first call left behind. Reservation counters are read from storage:
# a status code alone cannot tell a release from a double release.
# ---------------------------------------------------------------------------


def _one_shot(error: BaseException, real: Any) -> tuple[Any, dict[str, int]]:
    """Wrap ``real`` so its first call raises ``error`` and later calls pass."""
    state = {"calls": 0}

    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        state["calls"] += 1
        if state["calls"] == 1:
            raise error
        return await real(*args, **kwargs)

    return wrapper, state


async def _capped_governed_call(
    client: AsyncClient, tool_name: str, *, with_quote: bool, idem_key: str
) -> tuple[dict[str, Any], dict[str, Any], str, str | None]:
    from tests.test_permit_reservation_unwind import _create_capped_permit

    ctx = await provision_agent_wallet(client)
    permit_id = await _create_capped_permit(
        client,
        wallet_id=ctx["agent_wallet_id"],
        key_id=ctx["key_id"],
        tool_name=tool_name,
        max_calls=1,
        idem_key=f"{idem_key}-permit",
    )
    body = _call_body(
        tool_name=tool_name,
        wallet_id=ctx["agent_wallet_id"],
        permit_id=permit_id,
        idempotency_key=idem_key,
    )
    quote_id: str | None = None
    if with_quote:
        from app.services.quotes import get_quote_service

        quote = await get_quote_service().create_quote(
            wallet_id=ctx["agent_wallet_id"],
            tool=tool_name,
            quoted_credits=Decimal(str(TOOL_COST)),
            category=ServiceCategory.AGENT_COMMS.value,
        )
        quote_id = quote.quote_id
        body["params"]["mcpContext"]["quote_id"] = quote_id
    return ctx, body, permit_id, quote_id


async def _quote_status(quote_id: str) -> str:
    from app.services.quotes import get_quote_service

    quote = await get_quote_service().get_quote(quote_id)
    assert quote is not None
    return str(quote.status)


async def _record(ctx: dict[str, Any], key: str) -> Any:
    return await get_idempotency_service().get_record(
        wallet_id=ctx["agent_wallet_id"],
        endpoint=GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
        idempotency_key=key,
    )


@pytest.mark.anyio
async def test_quote_is_released_when_the_debit_is_contended_once(
    client: AsyncClient, clean_database: None, governed_tool, monkeypatch
) -> None:
    """The retry the contention answer advises must keep its locked price."""
    from app.services.agent_money import get_agent_money
    from app.services.billing_engine import LedgerWriteContendedError
    from tests.test_permit_reservation_unwind import _calls_made

    tool_name, runs = governed_tool
    ctx, body, permit_id, quote_id = await _capped_governed_call(
        client, tool_name, with_quote=True, idem_key="prefault-quote-1"
    )
    assert quote_id is not None
    money = get_agent_money()
    wrapper, state = _one_shot(LedgerWriteContendedError(), money.charge)
    monkeypatch.setattr(money, "charge", wrapper)

    first = await client.post("/mcp/messages", json=body, headers=ctx["agent_headers"])
    assert first.json()["error"]["message"] == "ledger_write_contended", first.text
    assert state["calls"] == 1
    # Nothing moved, and everything the call held is back: the quote, the
    # credits, the per-tool use and the key.
    assert await _quote_status(quote_id) == "active"
    assert await _spent_credits(permit_id) == Decimal("0")
    assert await _calls_made(permit_id, tool_name) == 0
    assert await _record(ctx, "prefault-quote-1") is None
    assert runs["count"] == 0

    second = await client.post("/mcp/messages", json=body, headers=ctx["agent_headers"])
    assert second.status_code == 200, second.text
    assert "error" not in second.json(), second.text
    assert second.json()["result"]["receipt"]["outcome"] == "success"
    assert await _quote_status(quote_id) == "consumed"
    assert (
        await _debit_count(
            client, ctx["agent_wallet_id"], ctx["agent_headers"], tool_name
        )
        == 1
    )
    assert await _spent_credits(permit_id) == Decimal(str(TOOL_COST))
    assert await _calls_made(permit_id, tool_name) == 1
    assert runs["count"] == 1


@pytest.mark.anyio
async def test_the_reservation_goes_back_exactly_once_on_a_contended_debit(
    client: AsyncClient, clean_database: None, governed_tool, monkeypatch
) -> None:
    """Adding the quote release must not add a second reservation release."""
    from app.routers import mcp as mcp_router
    from app.services.agent_money import get_agent_money
    from app.services.billing_engine import LedgerWriteContendedError

    tool_name, _ = governed_tool
    ctx, body, _permit_id, _quote_id = await _capped_governed_call(
        client, tool_name, with_quote=True, idem_key="prefault-once-1"
    )
    money = get_agent_money()
    wrapper, _ = _one_shot(LedgerWriteContendedError(), money.charge)
    monkeypatch.setattr(money, "charge", wrapper)
    real_release = mcp_router._release_local_permit_reservation
    releases: list[str] = []

    async def counted(*args: Any, **kwargs: Any) -> None:
        releases.append(str(kwargs.get("reason")))
        await real_release(*args, **kwargs)

    monkeypatch.setattr(mcp_router, "_release_local_permit_reservation", counted)
    first = await client.post("/mcp/messages", json=body, headers=ctx["agent_headers"])
    assert first.json()["error"]["message"] == "ledger_write_contended", first.text
    assert releases == ["ledger_write_contended"]


@pytest.mark.anyio
@pytest.mark.parametrize("failing", ["evaluate_wallet_policy", "get_daily_spend"])
async def test_the_key_is_freed_when_the_wallet_policy_raises_once(
    client: AsyncClient,
    clean_database: None,
    governed_tool,
    monkeypatch,
    failing: str,
) -> None:
    """A generic fault after the record begins and before any charge."""
    from app.routers import mcp as mcp_router
    from app.services.agent_money import get_agent_money
    from tests.test_permit_reservation_unwind import _calls_made

    tool_name, runs = governed_tool
    key = f"prefault-policy-{failing}"
    ctx, body, permit_id, _ = await _capped_governed_call(
        client, tool_name, with_quote=False, idem_key=key
    )
    if failing == "evaluate_wallet_policy":
        wrapper, state = _one_shot(
            RuntimeError("policy backend down"), mcp_router.evaluate_wallet_policy
        )
        monkeypatch.setattr(mcp_router, "evaluate_wallet_policy", wrapper)
    else:
        money = get_agent_money()
        wrapper, state = _one_shot(
            RuntimeError("spend read failed"), money.get_daily_spend
        )
        monkeypatch.setattr(money, "get_daily_spend", wrapper)

    first = await client.post("/mcp/messages", json=body, headers=ctx["agent_headers"])
    assert first.json()["error"]["message"] == "internal_error", first.text
    assert state["calls"] == 1
    assert await _record(ctx, key) is None
    # The reserve comes after the policy check, so nothing was ever held.
    assert await _spent_credits(permit_id) == Decimal("0")
    assert await _calls_made(permit_id, tool_name) == 0

    second = await client.post("/mcp/messages", json=body, headers=ctx["agent_headers"])
    assert second.status_code == 200, second.text
    assert "error" not in second.json(), second.text
    assert second.json()["result"]["receipt"]["outcome"] == "success"
    assert await _spent_credits(permit_id) == Decimal(str(TOOL_COST))
    assert await _calls_made(permit_id, tool_name) == 1
    assert runs["count"] == 1


@pytest.mark.anyio
async def test_a_fault_after_the_charge_keeps_the_record_and_the_quote(
    client: AsyncClient, clean_database: None, governed_tool, monkeypatch
) -> None:
    """Negative: once the debit committed, nothing may be handed back."""
    tool_name, runs = governed_tool
    key = "postcharge-keep-1"
    ctx, body, permit_id, quote_id = await _capped_governed_call(
        client, tool_name, with_quote=True, idem_key=key
    )
    assert quote_id is not None
    idem = get_idempotency_service()
    wrapper, state = _one_shot(RuntimeError("checkpoint lost"), idem.mark_charged)
    monkeypatch.setattr(idem, "mark_charged", wrapper)

    first = await client.post("/mcp/messages", json=body, headers=ctx["agent_headers"])
    assert first.json()["error"]["message"] == "internal_error", first.text
    assert state["calls"] == 1
    monkeypatch.undo()

    assert (
        await _debit_count(
            client, ctx["agent_wallet_id"], ctx["agent_headers"], tool_name
        )
        == 1
    )
    assert await _quote_status(quote_id) == "consumed"
    assert await _record(ctx, key) is not None
    assert await _spent_credits(permit_id) == Decimal(str(TOOL_COST))
    assert runs["count"] == 0


@pytest.mark.anyio
async def test_abandon_never_deletes_a_record_with_a_response_or_a_ledger_entry(
    client: AsyncClient, clean_database: None
) -> None:
    """Negative: the backstop every pre-charge release relies on."""
    idem = get_idempotency_service()
    wallet = (await provision_agent_wallet(client))["agent_wallet_id"]
    endpoint = "/mcp/messages"

    charged = await idem.begin_with_record(
        wallet_id=wallet,
        endpoint=endpoint,
        idempotency_key="guard-charged",
        request_payload={"n": 1},
    )
    await idem.mark_charged(
        wallet_id=wallet,
        endpoint=endpoint,
        idempotency_key="guard-charged",
        ledger_entry_id="led-1",
    )
    answered = await idem.begin_with_record(
        wallet_id=wallet,
        endpoint=endpoint,
        idempotency_key="guard-answered",
        request_payload={"n": 2},
    )
    await idem.complete(
        wallet_id=wallet,
        endpoint=endpoint,
        idempotency_key="guard-answered",
        response_reference=None,
        response_json={"ok": True},
    )
    for key, begun in (("guard-charged", charged), ("guard-answered", answered)):
        assert (
            await idem.abandon(
                wallet_id=wallet,
                endpoint=endpoint,
                idempotency_key=key,
                expected_record_id=begun.record_id,
            )
            is False
        )
        assert (
            await idem.get_record(
                wallet_id=wallet, endpoint=endpoint, idempotency_key=key
            )
            is not None
        )


@pytest.mark.anyio
async def test_a_fault_after_the_reserve_does_not_free_the_key_over_a_held_reservation(
    client: AsyncClient, clean_database: None, governed_tool, monkeypatch
) -> None:
    """Negative: the generic abandon stops at the reserve.

    Past the reserve the call holds a permit reservation that this wrapper cannot
    hand back exactly once, so freeing the key there would let the retry reserve
    a second time on top of the first. The record stays, as it did before.
    """
    from app.services.quotes import get_quote_service
    from tests.test_permit_reservation_unwind import _calls_made

    tool_name, runs = governed_tool
    key = "afterreserve-keep-1"
    ctx, body, permit_id, quote_id = await _capped_governed_call(
        client, tool_name, with_quote=True, idem_key=key
    )
    quotes = get_quote_service()
    wrapper, state = _one_shot(RuntimeError("quote store down"), quotes.consume)
    monkeypatch.setattr(quotes, "consume", wrapper)

    first = await client.post("/mcp/messages", json=body, headers=ctx["agent_headers"])
    assert first.json()["error"]["message"] == "internal_error", first.text
    assert state["calls"] == 1
    monkeypatch.undo()

    assert await _record(ctx, key) is not None
    assert await _spent_credits(permit_id) == Decimal(str(TOOL_COST))
    assert await _calls_made(permit_id, tool_name) == 1
    assert runs["count"] == 0
