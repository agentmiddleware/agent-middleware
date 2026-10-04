"""What a contended permit write is allowed to tell the caller.

The permit reserve is the *first* guarded write on the governed money path --
it runs before the debit, the audit append and the receipt insert -- and it was
the last one whose exhaustion had no name on any MCP surface.
``PermitService._run_with_write_retry`` has minted
``PermitError("permit_write_contended")`` since the restart was introduced, and
every other governed surface classifies it: the x402 router answers 503, the
AWI path abandons its key and answers 503, the ACP bridge frees the intent. The
three MCP surfaces did not, so it fell to their catch-alls and left as an
unclassified ``internal_error``.

That is the same defect ``tests/test_receipt_write_contention_surface.py``
exists to prevent, one write earlier, and the reason it matters is the one
written on ``LedgerWriteContendedError``: a client that cannot tell an
unclassified failure from a named retryable one has to assume its money may
have moved.

Two things make this type different from the receipt's, and both are pinned
here:

- ``PermitError`` is one class carrying a dozen unrelated reasons
  (``permit_not_found``, ``dispatch_attempt_not_found``, the budget denials).
  The contention is matched as ``PermitWriteContendedError``, a subclass, so
  the ladders can match contention by type without reclassifying every permit
  denial as retryable. ``test_a_plain_permit_error_is_not_reclassified``
  is the guard on that.
- The governed idempotency record is begun *before* the reserve is attempted,
  so unlike the receipt case the retryable answer is only true if the unwind
  in ``_execute_registered_tool`` releases the record. Every surface test here
  takes the retry it was promised rather than trusting the status code.

Contention is injected at the permit service's own session factory rather than
raced, for the reason the receipt suite gives: a test that waits for a real
lock passes on a fast machine for the wrong reason.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.core.config import get_settings
from app.core.resilience import WRITE_CONFLICT_MAX_ATTEMPTS
from app.db.database import get_session_factory
from app.db.models import McpDispatchAttemptModel, ReceiptModel, WalletModel
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services import permits as permits_module
from app.services.mcp_dispatch_reconciliation import (
    get_mcp_dispatch_reconciliation_service,
)
from app.services.permits import (
    PermitError,
    PermitWriteContendedError,
    get_permit_service,
)
from app.services.service_registry import get_service_registry
from app.services.upstream_mcp import UpstreamMcpResult, UpstreamMcpReturnedError
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet

TOOL_COST = 2.0


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as value:
        yield value


@pytest.fixture
def standard_mcp_enabled(monkeypatch):
    monkeypatch.setenv("ENABLE_STANDARD_MCP_ENDPOINT", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.setenv("ENABLE_STANDARD_MCP_ENDPOINT", "false")
    get_settings.cache_clear()


@pytest.fixture
def echo_tool():
    tool_name = "permit-contention-echo"
    runs = {"count": 0}

    def echo(message: str = "ok") -> dict[str, Any]:
        runs["count"] += 1
        return {"message": message}

    get_service_registry().register_local(
        service_id=tool_name,
        name="Permit contention echo",
        description="Permit write-contention surface test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=echo,
        credits_per_unit=TOOL_COST,
        unit_name="call",
    )
    try:
        yield tool_name, runs
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.fixture
def failing_tool():
    """A tool that runs, has its effect, and then fails.

    The post-effects case needs a call that actually executed: the refund then
    succeeds and the budget release is the only write left to lose.
    """
    tool_name = "permit-contention-boom"
    runs = {"count": 0}

    def boom(message: str = "ok") -> dict[str, Any]:
        runs["count"] += 1
        raise RuntimeError("tool blew up")

    get_service_registry().register_local(
        service_id=tool_name,
        name="Permit contention boom",
        description="Permit write-contention post-effects test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=boom,
        credits_per_unit=TOOL_COST,
        unit_name="call",
    )
    try:
        yield tool_name, runs
    finally:
        get_service_registry().unregister_local(tool_name)


def _lose_permit_writes(
    monkeypatch, *, allow_first: int = 0, failures: int | None = None
) -> dict[str, int]:
    """Make the permit service's own transactions lose write conflicts.

    Patched at the permits module's ``get_session_factory`` so only permit
    writes are affected: the debit, the audit append and the receipt insert
    keep the real factory, which keeps each test's failure attributable to the
    write under test.

    The guarded permit writes commit by leaving an ``async with
    session.begin()`` block rather than by calling ``session.commit()``, so the
    hook is the block's exit. Patching ``commit`` -- which is what the receipt
    suite does, because receipts commit that way -- would silently intercept
    nothing here and every test would pass for the wrong reason.

    ``allow_first`` transactions land untouched; after that, ``failures`` of
    them lose, or all of them when it is ``None``. ``allow_first=0`` contends
    the reserve on the way in; ``allow_first=1`` lets the reserve land and
    contends the release that runs after the tool has already executed.
    """
    real_get_session_factory = permits_module.get_session_factory
    state = {"committed": 0, "lost": 0}

    def patched_get_session_factory():
        maker = real_get_session_factory()

        def make_session(*args: Any, **kwargs: Any):
            session = maker(*args, **kwargs)
            real_begin = session.begin

            def flaky_begin(*b_args: Any, **b_kwargs: Any):
                real_cm = real_begin(*b_args, **b_kwargs)

                class _FlakyTransaction:
                    async def __aenter__(self):
                        return await real_cm.__aenter__()

                    async def __aexit__(self, exc_type, exc, tb):
                        if exc_type is not None:
                            return await real_cm.__aexit__(exc_type, exc, tb)
                        seen = state["committed"] + state["lost"]
                        losing = seen >= allow_first and (
                            failures is None or state["lost"] < failures
                        )
                        if not losing:
                            state["committed"] += 1
                            return await real_cm.__aexit__(None, None, None)
                        state["lost"] += 1
                        # The shape SQLAlchemy raises for SQLITE_BUSY; the
                        # shared classifier matches on the driver text, so the
                        # message is load-bearing, not decoration.
                        lock = OperationalError(
                            "UPDATE permits SET spent_credits=? WHERE permit_id=?",
                            {},
                            Exception("database is locked"),
                        )
                        # Unwind the transaction exactly as a losing COMMIT
                        # would, so the retry starts from a clean session.
                        await real_cm.__aexit__(type(lock), lock, None)
                        raise lock

                return _FlakyTransaction()

            session.begin = flaky_begin  # type: ignore[method-assign]
            return session

        return make_session

    monkeypatch.setattr(
        permits_module, "get_session_factory", patched_get_session_factory
    )
    return state


def _call_body(
    *, tool_name: str, wallet_id: str, permit_id: str, idempotency_key: str
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


def _rest_body(
    *, tool_name: str, wallet_id: str, permit_id: str, idempotency_key: str
) -> dict[str, Any]:
    return {
        "name": tool_name,
        "arguments": {"message": "hello"},
        "mcp_context": {
            "wallet_id": wallet_id,
            "permit_id": permit_id,
            "idempotency_key": idempotency_key,
        },
    }


async def _count_receipts() -> int:
    async with get_session_factory()() as session:
        return int(
            await session.scalar(select(func.count()).select_from(ReceiptModel)) or 0
        )


@pytest.mark.anyio
async def test_a_contended_permit_reserve_is_restarted_until_it_lands(
    client: AsyncClient, clean_database: None, echo_tool
) -> None:
    """The reserve survives lock losses and reserves exactly once.

    The restart replays a whole transaction, and this one is a read-modify-
    write on ``spent_credits``: the thing worth pinning is not that it
    eventually succeeds but that replaying it does not charge the permit's
    budget twice. Each attempt re-reads the row it is about to update, so the
    retries describe one reservation, not several.
    """
    tool_name, runs = echo_tool
    ctx = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=ctx["agent_wallet_id"],
        key_id=ctx["key_id"],
        tool_name=tool_name,
        max_credits=50,
        idem_key="permit-contention-permit-0",
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        state = _lose_permit_writes(monkeypatch, failures=5)
        resp = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=ctx["agent_wallet_id"],
                permit_id=permit["permit_id"],
                idempotency_key="permit-contention-recovers",
            ),
            headers=ctx["agent_headers"],
        )

    assert resp.status_code == 200, resp.text
    assert "error" not in resp.json(), resp.text
    assert state["lost"] == 5, state
    assert runs["count"] == 1

    # One reservation landed, not six: the permit spent the call's cost once.
    detail = await client.get(
        f"/v1/permits/{permit['permit_id']}", headers=ctx["agent_headers"]
    )
    assert detail.status_code == 200, detail.text
    assert float(detail.json()["spent_credits"]) == TOOL_COST, detail.text


@pytest.mark.anyio
async def test_the_messages_surface_names_a_contended_reserve_retryable(
    client: AsyncClient, clean_database: None, echo_tool
) -> None:
    """``/mcp/messages`` classifies in its own ladder, so the name goes there.

    Past the restart budget the loss is still classified, not unclassified.
    Restarting narrows the window; it cannot close it, so the exhaustion branch
    is reachable and is what a caller meets on a badly contended box. Nothing
    ran and nothing was charged, so this earns the same ``-32005`` envelope as
    a lost debit or a busy audit chain, carrying its own reason so a client can
    tell them apart.

    The second half is the obligation the -32005 takes on. The governed record
    is begun before the reserve is attempted, so unless the unwind releases it
    every retry of that key meets ``idempotency_in_progress`` forever and the
    -32005 names a retry the caller can never make. Asserting the code alone
    passes while that is exactly the case.
    """
    tool_name, runs = echo_tool
    ctx = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=ctx["agent_wallet_id"],
        key_id=ctx["key_id"],
        tool_name=tool_name,
        idem_key="permit-contention-permit-1",
    )
    body = _call_body(
        tool_name=tool_name,
        wallet_id=ctx["agent_wallet_id"],
        permit_id=permit["permit_id"],
        idempotency_key="permit-contention-messages-1",
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        state = _lose_permit_writes(monkeypatch, failures=None)
        resp = await client.post(
            "/mcp/messages", json=body, headers=ctx["agent_headers"]
        )

    assert resp.status_code == 200, resp.text
    error = resp.json()["error"]
    assert error["message"] != "internal_error", error
    assert error["message"] == PermitWriteContendedError.reason, error
    assert error["code"] == -32005, error
    # It gave up only after spending the whole documented budget.
    assert state["lost"] == WRITE_CONFLICT_MAX_ATTEMPTS, state
    # Nothing ran and nothing is durable: the state the -32005 claims.
    assert runs["count"] == 0
    assert await _count_receipts() == 0

    # Now take the retry the -32005 promised, on the same key and with the
    # contention gone. It has to reach the real answer.
    again = await client.post("/mcp/messages", json=body, headers=ctx["agent_headers"])
    assert again.status_code == 200, again.text
    payload = again.json()
    assert "error" not in payload, payload
    assert runs["count"] == 1


@pytest.mark.anyio
async def test_the_legacy_rest_surface_names_a_contended_reserve_retryable(
    client: AsyncClient, clean_database: None, echo_tool
) -> None:
    """``/mcp/tools/{id}/invoke`` has its own ladder, so it needs the name too.

    Its ladder ends in ``except Exception`` returning
    ``_internal_error_tool_result`` -- a 200 carrying ``isError`` and an opaque
    ``internal_error`` -- so the omission never failed loudly. Asserting 409 is
    what excludes that fallthrough: the internal_error answer is a 200, so the
    status alone separates them.
    """
    tool_name, runs = echo_tool
    ctx = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=ctx["agent_wallet_id"],
        key_id=ctx["key_id"],
        tool_name=tool_name,
        idem_key="permit-contention-permit-2",
    )
    body = _rest_body(
        tool_name=tool_name,
        wallet_id=ctx["agent_wallet_id"],
        permit_id=permit["permit_id"],
        idempotency_key="permit-contention-rest-1",
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        state = _lose_permit_writes(monkeypatch, failures=None)
        resp = await client.post(
            f"/mcp/tools/{tool_name}/invoke", json=body, headers=ctx["agent_headers"]
        )

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == PermitWriteContendedError.reason
    assert state["lost"] == WRITE_CONFLICT_MAX_ATTEMPTS, state
    assert runs["count"] == 0
    assert await _count_receipts() == 0

    # The retry the 409 promised has to arrive at the real answer.
    again = await client.post(
        f"/mcp/tools/{tool_name}/invoke", json=body, headers=ctx["agent_headers"]
    )
    assert again.status_code == 200, again.text
    assert again.json()["isError"] is False, again.text
    assert runs["count"] == 1


@pytest.mark.anyio
async def test_the_standard_surface_names_a_contended_reserve_retryable(
    client: AsyncClient, clean_database: None, standard_mcp_enabled, echo_tool
) -> None:
    """``/mcp`` maps types into ``McpError`` in a third, separate handler.

    The two JSON-RPC transports do not share an exception ladder:
    ``/mcp/messages`` matches types itself and ``/mcp`` maps them. A name on
    one is not a name on the other, and ``/mcp`` is the surface new
    integrations are pointed at.
    """
    tool_name, runs = echo_tool
    ctx = await provision_agent_wallet(client)

    with pytest.MonkeyPatch.context() as monkeypatch:
        state = _lose_permit_writes(monkeypatch, failures=None)
        resp = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 11,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": {"message": "hello"}},
            },
            headers={
                **ctx["agent_headers"],
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Idempotency-Key": "permit-contention-standard-1",
            },
        )

    assert resp.status_code == 200, resp.text
    error = resp.json()["error"]
    assert error["message"] != "internal_error", error
    assert error["message"] == PermitWriteContendedError.reason, error
    assert error["code"] == -32005, error
    assert state["lost"] == WRITE_CONFLICT_MAX_ATTEMPTS, state
    assert runs["count"] == 0
    assert await _count_receipts() == 0


@pytest.mark.anyio
async def test_a_call_that_already_ran_is_never_told_to_retry(
    client: AsyncClient, clean_database: None, failing_tool
) -> None:
    """The guard on every 409 and -32005 above.

    Naming the reserve retryable is only safe while the *release* -- the same
    ``permit_write_contended``, raised by the same helper, one write later --
    can never reach those handlers. It runs after the tool has executed and
    been charged, so "retry the same key" there would invite a second
    execution of something the caller has already paid for.

    The release absorbs its own contention instead of raising it, for the
    reason ``_release_local_permit_reservation`` already absorbs its half: the
    caller's real answer is the tool's failure, and the receipt that reports it
    is written *after* the release. Letting the loss propagate would not report
    a pre-existing "no receipt" state the way the receipt and audit sites do --
    it would manufacture one, destroying the governance artifact for a call
    that ran in order to report a bookkeeping loss on a wallet the refund had
    already made whole.

    So the assertions are the pair: the answer is the tool's own failure with
    its receipt, and it is emphatically not a retryable code.
    """
    tool_name, runs = failing_tool
    ctx = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=ctx["agent_wallet_id"],
        key_id=ctx["key_id"],
        tool_name=tool_name,
        idem_key="permit-contention-permit-3",
    )
    body = _rest_body(
        tool_name=tool_name,
        wallet_id=ctx["agent_wallet_id"],
        permit_id=permit["permit_id"],
        idempotency_key="permit-contention-posteffects-1",
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        # The reserve lands; every permit write after it loses, which is the
        # budget release that runs once the tool has already blown up.
        state = _lose_permit_writes(monkeypatch, allow_first=1)
        resp = await client.post(
            f"/mcp/tools/{tool_name}/invoke", json=body, headers=ctx["agent_headers"]
        )

    # The tool ran, so the contended write really is the post-effects one.
    assert runs["count"] == 1
    # The release ladder exhausts (WRITE_CONFLICT_MAX_ATTEMPTS), then the
    # drift alert that records what the absorb stranded makes one further,
    # unretried permit write. This harness contends every permit write, so
    # that one loses too and is itself absorbed -- which is the property
    # worth pinning: the write that exists to make the drift visible can
    # never be the write that fails the request.
    assert state["lost"] == WRITE_CONFLICT_MAX_ATTEMPTS + 1, state

    # Not retryable, by status and by body: 409 is the reserve's answer and a
    # charged call must never receive it.
    assert resp.status_code != 409, resp.text
    assert resp.status_code == 500, resp.text
    detail = resp.json()["detail"]
    assert PermitWriteContendedError.reason not in str(detail), detail

    # The caller's real answer survived the contention, and so did the receipt
    # -- the whole reason the release absorbs rather than escalates.
    assert "tool blew up" in detail["error"], detail
    assert detail["receipt"]["outcome"] == "failed_refunded", detail
    assert await _count_receipts() == 1


@pytest.mark.anyio
async def test_a_plain_permit_error_is_not_reclassified_as_retryable(
    client: AsyncClient, clean_database: None, echo_tool, monkeypatch
) -> None:
    """The guard on matching the subclass instead of ``PermitError``.

    ``PermitError`` is one class carrying a dozen unrelated reasons, so the
    tempting one-line fix -- adding ``PermitError`` itself to the retryable
    tuple -- would answer 409 "retry this key" for ``permit_not_found``,
    ``dispatch_attempt_not_found`` and the budget denials alike. Those are not
    transient and no retry fixes them.

    Driven through the same route as the 409 test above, with the same helper
    raising a *non*-contention reason, which must keep its existing route to
    the unclassified channel.
    """
    tool_name, runs = echo_tool
    ctx = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=ctx["agent_wallet_id"],
        key_id=ctx["key_id"],
        tool_name=tool_name,
        idem_key="permit-contention-permit-4",
    )

    async def _not_found(*args: Any, **kwargs: Any):
        raise PermitError("permit_not_found")

    monkeypatch.setattr(
        permits_module.PermitService, "authorize_and_reserve", _not_found
    )

    resp = await client.post(
        f"/mcp/tools/{tool_name}/invoke",
        json=_rest_body(
            tool_name=tool_name,
            wallet_id=ctx["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key="permit-contention-notfound-1",
        ),
        headers=ctx["agent_headers"],
    )

    assert resp.status_code != 409, resp.text
    assert PermitWriteContendedError.reason not in resp.text, resp.text
    assert runs["count"] == 0


@pytest.fixture
def failing_upstream_tool():
    """An upstream tool that dispatches, then returns an MCP error result.

    This is the only way to reach the *other* post-effects release,
    ``release_dispatch_budget_once`` in ``_raise_refunded_upstream_failure``.
    It is a separate site from the local one with a separate guard, and the
    local test cannot reach it: the upstream path reserves through
    ``McpDispatchAttemptService.authorize_reserve_and_prepare`` and gives the
    budget back attempt-keyed, never touching ``release_budget``.
    """
    tool_name = "permit-contention-upstream"
    dispatches = {"count": 0}

    class _ErroringUpstream:
        async def call_tool(
            self,
            arguments: dict[str, Any],
            *,
            invocation_id: str,
            idempotency_key: str,
            before_dispatch: Any,
        ) -> UpstreamMcpResult:
            await before_dispatch()
            dispatches["count"] += 1
            payload = {
                "content": [{"type": "text", "text": "partner refused"}],
                "isError": True,
            }
            canonical = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            raise UpstreamMcpReturnedError(
                UpstreamMcpResult(
                    payload=payload,
                    canonical_json=canonical,
                    response_hash=hashlib.sha256(canonical.encode()).hexdigest(),
                    size_bytes=len(canonical.encode()),
                    is_error=True,
                )
            )

    get_service_registry().register_upstream(
        service_id=tool_name,
        name="Permit contention upstream",
        description="Permit write-contention upstream post-effects test tool",
        category=ServiceCategory.AGENT_COMMS,
        executor=_ErroringUpstream(),
        input_schema={
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        credits_per_unit=TOOL_COST,
        upstream_tool_name="partner.echo",
        upstream_origin="https://partner.example",
    )
    try:
        yield tool_name, dispatches
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_a_dispatched_upstream_call_is_never_told_to_retry(
    client: AsyncClient, clean_database: None, failing_upstream_tool
) -> None:
    """The second post-effects release, on the upstream path.

    ``_raise_refunded_upstream_failure`` gives the dispatch reservation back
    after the call was dispatched, returned an error and was refunded. It is a
    physically separate site from the local ``release_budget`` with its own
    guard, and nothing about the local test covers it: the reserve here goes
    through ``McpDispatchAttemptService``, a different module with its own
    session factory, which is why contending only the permit module's writes
    lands squarely on the release.

    Safer to absorb than the local one, and for an extra reason: this
    reservation is attempt-keyed and ``release_dispatch_budget_once`` is
    idempotent on that key, so ``mcp_dispatch_reconciliation`` re-runs it from
    the attempt row without the caller. Escalating would still skip the audit
    event and the receipt for a call that really was dispatched.
    """
    tool_name, dispatches = failing_upstream_tool
    ctx = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=ctx["agent_wallet_id"],
        key_id=ctx["key_id"],
        tool_name=tool_name,
        idem_key="permit-contention-permit-5",
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        state = _lose_permit_writes(monkeypatch)
        resp = await client.post(
            f"/mcp/tools/{tool_name}/invoke",
            json=_rest_body(
                tool_name=tool_name,
                wallet_id=ctx["agent_wallet_id"],
                permit_id=permit["permit_id"],
                idempotency_key="permit-contention-upstream-1",
            ),
            headers=ctx["agent_headers"],
        )

    # It really was dispatched, and the release really did exhaust: without
    # both, this test would pass without touching the guard.
    assert dispatches["count"] == 1
    assert state["lost"] == WRITE_CONFLICT_MAX_ATTEMPTS, state

    assert resp.status_code != 409, resp.text
    assert resp.status_code == 502, resp.text
    detail = resp.json()["detail"]
    assert PermitWriteContendedError.reason not in str(detail), detail
    assert detail["error"] == "upstream_returned_error", detail
    assert detail["receipt"]["outcome"] == "failed_refunded", detail
    assert await _count_receipts() == 1


@pytest.mark.anyio
async def test_the_messages_surface_never_tells_a_charged_call_to_retry(
    client: AsyncClient, clean_database: None, failing_tool
) -> None:
    """The post-effects guard, pinned on ``/mcp/messages``'s own ladder.

    The surfaces share no exception ladder, so the local test's 409 assertion
    says nothing about this transport: here the wrong answer would be
    ``-32005``, a different code reached through different code. Both halves
    of the split have to be pinned on every surface for the same reason the
    retryable half did.
    """
    tool_name, runs = failing_tool
    ctx = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=ctx["agent_wallet_id"],
        key_id=ctx["key_id"],
        tool_name=tool_name,
        idem_key="permit-contention-permit-6",
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        state = _lose_permit_writes(monkeypatch, allow_first=1)
        resp = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=ctx["agent_wallet_id"],
                permit_id=permit["permit_id"],
                idempotency_key="permit-contention-messages-postfx",
            ),
            headers=ctx["agent_headers"],
        )

    assert runs["count"] == 1
    # The release ladder exhausts (WRITE_CONFLICT_MAX_ATTEMPTS), then the
    # drift alert that records what the absorb stranded makes one further,
    # unretried permit write. This harness contends every permit write, so
    # that one loses too and is itself absorbed -- which is the property
    # worth pinning: the write that exists to make the drift visible can
    # never be the write that fails the request.
    assert state["lost"] == WRITE_CONFLICT_MAX_ATTEMPTS + 1, state

    assert resp.status_code == 200, resp.text
    error = resp.json()["error"]
    assert error["code"] != -32005, error
    assert error["message"] != PermitWriteContendedError.reason, error
    # The caller's real answer and its receipt both survived.
    assert "tool blew up" in error["message"], error
    assert await _count_receipts() == 1


@pytest.mark.anyio
async def test_the_standard_surface_never_tells_a_charged_call_to_retry(
    client: AsyncClient, clean_database: None, standard_mcp_enabled, failing_tool
) -> None:
    """The same guard on ``/mcp``, the third ladder and the third mapping.

    ``/mcp`` maps types into ``McpError`` rather than matching them inline, so
    it is the surface where a retryable code would be produced by yet another
    branch. The auto-minted permit makes this the shortest path of the three,
    which is exactly why it needs its own pin rather than inheriting one.
    """
    tool_name, runs = failing_tool
    ctx = await provision_agent_wallet(client)

    with pytest.MonkeyPatch.context() as monkeypatch:
        state = _lose_permit_writes(monkeypatch, allow_first=1)
        resp = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 21,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": {"message": "hello"}},
            },
            headers={
                **ctx["agent_headers"],
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Idempotency-Key": "permit-contention-standard-postfx",
            },
        )

    assert runs["count"] == 1
    # The release ladder exhausts (WRITE_CONFLICT_MAX_ATTEMPTS), then the
    # drift alert that records what the absorb stranded makes one further,
    # unretried permit write. This harness contends every permit write, so
    # that one loses too and is itself absorbed -- which is the property
    # worth pinning: the write that exists to make the drift visible can
    # never be the write that fails the request.
    assert state["lost"] == WRITE_CONFLICT_MAX_ATTEMPTS + 1, state

    assert resp.status_code == 200, resp.text
    error = resp.json()["error"]
    assert error["code"] != -32005, error
    assert error["message"] != PermitWriteContendedError.reason, error
    assert await _count_receipts() == 1


@pytest.mark.anyio
async def test_a_contended_predispatch_release_still_leaves_the_retry_open(
    client: AsyncClient, clean_database: None, failing_upstream_tool
) -> None:
    """The retry promised by the *pre-dispatch* release has to be real too.

    The third reachable pre-effect site is the budget release in the remote
    insufficient-funds path: the wallet cannot cover the call, the attempt is
    driven terminal and never sent, and only then is the dispatch reservation
    handed back. A contention loss there is classified retryable like any
    other pre-effect loss.

    But this site differs from the reserve in a way that matters. By the time
    it runs, a durable ``mcp_dispatch_attempts`` row exists, and its
    ``idempotency_record_id`` is a NOT NULL foreign key onto the very record
    the unwind tries to delete. If the delete is refused the unwind only logs
    it, and the caller is still told the loss is retryable -- so the -32005
    would name a retry that meets ``idempotency_in_progress`` until background
    reconciliation runs.

    Asserting the code alone cannot see that. This takes the retry.
    """
    tool_name, dispatches = failing_upstream_tool
    ctx = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=ctx["agent_wallet_id"],
        key_id=ctx["key_id"],
        tool_name=tool_name,
        idem_key="permit-contention-permit-7",
    )

    # The tool costs 2 credits; a balance of 1 cannot cover it, so the call is
    # refused before dispatch and the attempt goes terminal unsent.
    async with get_session_factory()() as session:
        wallet = await session.get(WalletModel, ctx["agent_wallet_id"])
        assert wallet is not None
        wallet.balance = Decimal("1")
        session.add(wallet)
        await session.commit()

    body = _call_body(
        tool_name=tool_name,
        wallet_id=ctx["agent_wallet_id"],
        permit_id=permit["permit_id"],
        idempotency_key="permit-contention-predispatch-1",
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        state = _lose_permit_writes(monkeypatch)
        resp = await client.post(
            "/mcp/messages", json=body, headers=ctx["agent_headers"]
        )

    assert resp.status_code == 200, resp.text
    error = resp.json()["error"]
    assert error["code"] == -32005, error
    assert error["message"] == PermitWriteContendedError.reason, error
    # Nothing was dispatched and the release really did exhaust.
    assert dispatches["count"] == 0
    assert state["lost"] == WRITE_CONFLICT_MAX_ATTEMPTS, state

    # The obligation. With the contention gone, the same key has to reach the
    # real answer rather than a record nobody released.
    again = await client.post("/mcp/messages", json=body, headers=ctx["agent_headers"])
    retry_error = again.json().get("error")
    assert retry_error is not None, again.text
    assert retry_error["message"] != "idempotency_in_progress", retry_error
    assert "insufficient_funds" in retry_error["message"], retry_error


@pytest.mark.anyio
async def test_reconciliation_still_releases_a_budget_the_live_path_absorbed(
    client: AsyncClient, clean_database: None, failing_upstream_tool
) -> None:
    """Absorbing the upstream release is only safe if reconciliation finishes it.

    This is the invariant the absorb broke, found by review rather than by the
    suite. ``_compensate_returned_error`` used to skip the release whenever a
    signed ``failed_refunded`` receipt existed, on the documented reasoning
    that the write order is refund -> release -> audit -> receipt, so the
    receipt *proved* the release had already happened.

    Absorbing a contended release and then going on to write the receipt makes
    that proof false: it produces a ``failed_refunded`` receipt with
    ``budget_released_at`` still unset -- the exact state the early return
    assumed impossible. Reconciliation then skipped the very reservation it
    exists to give back, and the permit stayed inflated until expiry, which is
    precisely the "reconciliation re-runs it" guarantee the absorb was
    justified by.

    So this drives the real live path to strand the reservation, then runs the
    real reconciler over it. Reading ``budget_released_at`` instead of
    inferring it from the receipt is what makes the second half pass.
    """
    tool_name, dispatches = failing_upstream_tool
    ctx = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=ctx["agent_wallet_id"],
        key_id=ctx["key_id"],
        tool_name=tool_name,
        idem_key="permit-contention-permit-8",
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        _lose_permit_writes(monkeypatch)
        resp = await client.post(
            f"/mcp/tools/{tool_name}/invoke",
            json=_rest_body(
                tool_name=tool_name,
                wallet_id=ctx["agent_wallet_id"],
                permit_id=permit["permit_id"],
                idempotency_key="permit-contention-reconcile-1",
            ),
            headers=ctx["agent_headers"],
        )

    # The live path did what it is supposed to: dispatched, refunded, absorbed
    # the contended release, and still published the receipt.
    assert resp.status_code == 502, resp.text
    assert dispatches["count"] == 1
    assert await _count_receipts() == 1

    # The stranded state the absorb leaves behind.
    async with get_session_factory()() as session:
        attempt = (
            (await session.execute(select(McpDispatchAttemptModel))).scalars().one()
        )
    assert attempt.state == "returned_error"
    assert attempt.budget_released_at is None, "precondition: release was absorbed"
    stranded = await get_permit_service().get_permit(permit["permit_id"])
    assert stranded is not None
    assert stranded.spent_credits > Decimal("0"), "precondition: reservation is held"

    # Reconciliation owns it from here, which is the whole basis for absorbing.
    await get_mcp_dispatch_reconciliation_service().reconcile(idle_seconds=0)

    async with get_session_factory()() as session:
        settled = (
            (await session.execute(select(McpDispatchAttemptModel))).scalars().one()
        )
    assert settled.budget_released_at is not None, "reconciliation skipped the release"
    repaired = await get_permit_service().get_permit(permit["permit_id"])
    assert repaired is not None
    assert repaired.spent_credits == Decimal("0"), repaired.spent_credits
    # It did not pay the refund or the receipt twice on the way through.
    assert await _count_receipts() == 1
