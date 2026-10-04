"""What a contended receipt insert is allowed to tell the caller.

The receipt is the last write on the governed money path, and it was the only
one without a write-conflict restart: the wallet debit, the permit reserve, the
idempotency record and the dispatch attempt all wrap theirs in
``run_with_write_conflict_retry``. So once those stopped losing SQLite locks,
the surviving contention landed on ``INSERT INTO receipts`` and escaped as an
unclassified JSON-RPC ``internal_error``.

That is the specific thing this file exists to prevent, and the reason it
matters is the one written on ``LedgerWriteContendedError``: a client that
cannot tell an unclassified failure from a named retryable one has to assume
its money may have moved. The case actually observed in CI was a *denial* --
``outcome='denied'``, ``reason_code='permit_budget_exceeded'``,
``credits_charged=0`` -- where nothing had been charged and the true answer was
sitting in the row that failed to insert.

The classification splits the same way the audit chain's does, and for the same
reason (see ``tests/test_audit_chain_contention_surface.py``):

- a refusal that ran nothing may say "retry", because a second attempt costs
  nothing and no terminal record was left behind;
- a call that already ran and was charged may not, because "retry" would invite
  a second execution of something the caller has paid for.

Contention is injected at the receipt service's own session factory rather than
raced. A test that waits for a real lock passes on a fast machine for the wrong
reason -- which is exactly how the original defect survived roughly three weeks
of being called flaky.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.core.config import get_settings
from app.core.resilience import WRITE_CONFLICT_MAX_ATTEMPTS
from app.db.database import get_session_factory
from app.db.models import (
    IdempotencyRecordModel,
    McpDispatchAttemptModel,
    ReceiptModel,
    WalletModel,
)
from app.main import app
from app.routers.mcp import RECEIPT_WRITE_CONTENDED_AFTER_EFFECTS
from app.schemas.billing import ServiceCategory
from app.services import human_approval as human_approval_module
from app.services import receipts as receipts_module
from app.services.human_approval import HumanApprovalService
from app.services.receipts import ReceiptWriteContendedError, get_receipt_service
from app.services.service_registry import get_service_registry
from app.services.upstream_mcp import UpstreamMcpResult
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


@pytest.fixture
def standard_mcp_enabled(monkeypatch):
    monkeypatch.setenv("ENABLE_STANDARD_MCP_ENDPOINT", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.setenv("ENABLE_STANDARD_MCP_ENDPOINT", "false")
    get_settings.cache_clear()


@pytest.fixture
def echo_tool():
    tool_name = "receipt-contention-echo"
    runs = {"count": 0}

    def echo(message: str = "ok") -> dict[str, Any]:
        runs["count"] += 1
        return {"message": message}

    get_service_registry().register_local(
        service_id=tool_name,
        name="Receipt contention echo",
        description="Receipt write-contention surface test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=echo,
        credits_per_unit=TOOL_COST,
        unit_name="call",
    )
    try:
        yield tool_name, runs
    finally:
        get_service_registry().unregister_local(tool_name)


def _lose_receipt_commits(monkeypatch, *, failures: int | None) -> dict[str, int]:
    """Make the receipt insert's own COMMIT lose write conflicts.

    Patched at the receipts module's ``get_session_factory`` so only the
    receipt write is affected: the permit reserve, the debit and the audit
    append keep the real factory, which keeps each test's failure attributable
    to the write under test.

    ``failures`` is how many COMMITs lose before one is allowed through.
    ``None`` loses every time, which is the only way to reach the exhaustion
    branch without waiting on a real race.
    """
    real_get_session_factory = receipts_module.get_session_factory
    state = {"commits": 0}

    def patched_get_session_factory():
        maker = real_get_session_factory()

        def make_session(*args: Any, **kwargs: Any):
            session = maker(*args, **kwargs)
            real_commit = session.commit

            async def flaky_commit() -> None:
                state["commits"] += 1
                if failures is None or state["commits"] <= failures:
                    # The shape SQLAlchemy raises for SQLITE_BUSY; the shared
                    # classifier matches on the driver text, so the message is
                    # load-bearing, not decoration.
                    raise OperationalError(
                        "INSERT INTO receipts (...) VALUES (...)",
                        {},
                        Exception("database is locked"),
                    )
                await real_commit()

            session.commit = flaky_commit  # type: ignore[method-assign]
            return session

        return make_session

    monkeypatch.setattr(
        receipts_module, "get_session_factory", patched_get_session_factory
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


async def _count_receipts() -> int:
    async with get_session_factory()() as session:
        return int(
            await session.scalar(select(func.count()).select_from(ReceiptModel)) or 0
        )


async def _drain_wallet(wallet_id: str) -> None:
    """Drop the balance below the tool cost so the charge refuses.

    The same move ``test_mcp_trust`` uses. It has to be done after the permit
    exists, because permit creation refuses a ``max_credits`` above the
    wallet's balance -- so an unfunded call cannot be set up by pricing the
    tool above the wallet in the first place.
    """
    async with get_session_factory()() as session:
        wallet = (
            await session.execute(
                select(WalletModel).where(WalletModel.wallet_id == wallet_id)
            )
        ).scalar_one()
        wallet.balance = Decimal("1")
        await session.commit()


class _ScriptedSentinel:
    """Sentinel stand-in whose decision is set by the test.

    Creating an approval always answers pending; polling answers ``status``.
    That is the shape ``test_human_approval_gate`` drives, and it is what
    makes a rejected approval reachable over HTTP: the first call parks the
    key on ``human_approval_pending`` (which frees it), and the next call on
    the same key reads the rejection.
    """

    def __init__(self) -> None:
        self.status = "pending"

    async def create_approval(self, **kwargs: Any) -> dict[str, Any]:
        return {"action_id": f"act_{uuid.uuid4().hex[:16]}", "status": "pending"}

    async def get_approval(self, action_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = {"action_id": action_id, "status": self.status}
        if self.status in {"approved", "rejected"}:
            payload["decided_by"] = "reviewer@example.com"
            payload["reason"] = f"scripted {self.status}"
        return payload

    async def wait_approval(self, action_id: str, timeout: float) -> dict[str, Any]:
        return await self.get_approval(action_id)


class _NeverDispatchedExecutor:
    """Upstream stand-in that records whether it was ever reached.

    The branch under test refuses at the charge, after the dispatch attempt is
    prepared and driven terminal but before anything is sent, so ``dispatches``
    staying at zero is part of the assertion rather than incidental.
    """

    def __init__(self) -> None:
        self.dispatches = 0

    async def call_tool(
        self,
        arguments: dict[str, Any],
        *,
        invocation_id: str,
        idempotency_key: str,
        before_dispatch: Callable[[], Awaitable[None]],
    ) -> UpstreamMcpResult:
        await before_dispatch()
        self.dispatches += 1
        raise AssertionError("upstream must not be reached on an unfunded call")


@pytest.mark.anyio
async def test_a_contended_receipt_insert_is_restarted_until_it_lands(
    client: AsyncClient, clean_database: None
) -> None:
    """The write survives lock losses, and leaves exactly one row behind.

    The restart replays a whole transaction, so the thing worth pinning is not
    that it eventually succeeds but that replaying it does not write the
    receipt twice. Every attempt rebuilds its ORM instance from the identity
    fixed before the loop -- same ``receipt_id``, same ``created_at``, same
    signed payload -- so the retries describe one row, not several.
    """
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="receipt-contention-direct",
        idem_key="receipt-contention-permit-0",
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        state = _lose_receipt_commits(monkeypatch, failures=5)
        receipt = await get_receipt_service().create_receipt(
            permit_id=permit["permit_id"],
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool="receipt-contention-direct",
            request_payload={"message": "before"},
            response_payload={"message": "after"},
            ledger_entry_id=None,
            credits_authorized=Decimal("2"),
            credits_charged=Decimal("0"),
            outcome="denied",
            audit_event_id=None,
            reason_code="permit_budget_exceeded",
        )

    assert state["commits"] == 6, state
    assert receipt.outcome == "denied"
    # The denial's real answer survived the contention, which is the whole
    # point: a receipt that reports permit_budget_exceeded is actionable and an
    # internal_error is not.
    assert receipt.reason_code == "permit_budget_exceeded"
    assert receipt.credits_charged == Decimal("0")
    assert await _count_receipts() == 1

    # The row that landed is the one that was signed before the first attempt.
    ok, reason, stored = await get_receipt_service().verify_receipt(receipt.receipt_id)
    assert ok, reason
    assert stored is not None
    assert stored.receipt_id == receipt.receipt_id


@pytest.mark.anyio
async def test_a_denial_reports_its_real_reason_through_receipt_contention(
    client: AsyncClient, clean_database: None, echo_tool
) -> None:
    """End to end, the CI failure this fixes: a budget denial over HTTP.

    The permit's cap is below the tool's cost, so the call is refused before
    anything runs and the denial receipt is the only write left to lose. Before
    the restart the caller got ``internal_error`` here; the correct answer,
    ``permit_budget_exceeded``, was sitting in the row that failed to insert.
    """
    tool_name, runs = echo_tool
    ctx = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=ctx["agent_wallet_id"],
        key_id=ctx["key_id"],
        tool_name=tool_name,
        max_credits=1,  # below TOOL_COST, so the reserve refuses
        idem_key="receipt-contention-permit-1",
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        _lose_receipt_commits(monkeypatch, failures=5)
        resp = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=ctx["agent_wallet_id"],
                permit_id=permit["permit_id"],
                idempotency_key="receipt-contention-denied-1",
            ),
            headers=ctx["agent_headers"],
        )

    assert resp.status_code == 200, resp.text
    error = resp.json()["error"]
    assert error["message"] != "internal_error", error
    assert error["message"] == "permit_budget_exceeded", error
    assert error["code"] == -32003, error
    # A denial that charged nothing still hands back its signed evidence.
    assert error["data"]["receipt"]["outcome"] == "denied", error
    assert runs["count"] == 0


@pytest.mark.anyio
async def test_an_exhausted_denial_receipt_is_named_retryable_not_internal(
    client: AsyncClient, clean_database: None, echo_tool
) -> None:
    """Past the restart budget the loss is still classified, not unclassified.

    Restarting narrows the window; it cannot close it, so the exhaustion branch
    is reachable and is what a caller actually meets on a badly contended box.
    Nothing ran and nothing was charged, and the receipt service proved no row
    is durable before raising, so this earns the same ``-32005`` envelope as a
    lost debit or a busy audit chain -- carrying its own reason, so a client can
    tell the three apart.

    The second half of the test is the obligation that comes with saying
    "retry": the governed idempotency record opened before this denial has to
    be released, or every retry of that key meets ``idempotency_in_progress``
    forever and the -32005 names a retry the caller can never actually make.
    Asserting the code alone passed while that was exactly the case.
    """
    tool_name, runs = echo_tool
    ctx = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=ctx["agent_wallet_id"],
        key_id=ctx["key_id"],
        tool_name=tool_name,
        max_credits=1,
        idem_key="receipt-contention-permit-2",
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        state = _lose_receipt_commits(monkeypatch, failures=None)
        resp = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=ctx["agent_wallet_id"],
                permit_id=permit["permit_id"],
                idempotency_key="receipt-contention-denied-2",
            ),
            headers=ctx["agent_headers"],
        )

    assert resp.status_code == 200, resp.text
    error = resp.json()["error"]
    assert error["message"] != "internal_error", error
    assert error["message"] == ReceiptWriteContendedError.reason, error
    assert error["code"] == -32005, error
    # It gave up only after spending the whole documented budget.
    assert state["commits"] == WRITE_CONFLICT_MAX_ATTEMPTS, state
    assert await _count_receipts() == 0
    assert runs["count"] == 0

    # Now take the retry the -32005 promised, on the same key and with the
    # contention gone. It has to reach the real answer.
    again = await client.post(
        "/mcp/messages",
        json=_call_body(
            tool_name=tool_name,
            wallet_id=ctx["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key="receipt-contention-denied-2",
        ),
        headers=ctx["agent_headers"],
    )
    assert again.status_code == 200, again.text
    retry_error = again.json()["error"]
    assert retry_error["message"] != "idempotency_in_progress", retry_error
    assert retry_error["message"] == "permit_budget_exceeded", retry_error
    assert await _count_receipts() == 1
    assert runs["count"] == 0


@pytest.mark.anyio
async def test_a_charged_call_is_never_told_to_retry_its_receipt(
    client: AsyncClient, clean_database: None, echo_tool
) -> None:
    """The guard on the classification above.

    Here the permit allows the call, so the tool runs and the wallet is charged
    before the receipt is written. Answering ``-32005`` would invite a second
    execution of something already paid for, and it cannot be softened into a
    success either -- without a receipt there is no terminal outcome to publish
    and reconciliation owns the record from here. So it answers ``-32007``,
    the non-retryable name a post-charge audit-chain loss also carries.
    """
    tool_name, runs = echo_tool
    ctx = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=ctx["agent_wallet_id"],
        key_id=ctx["key_id"],
        tool_name=tool_name,
        max_credits=10,
        idem_key="receipt-contention-permit-3",
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        _lose_receipt_commits(monkeypatch, failures=None)
        resp = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=ctx["agent_wallet_id"],
                permit_id=permit["permit_id"],
                idempotency_key="receipt-contention-charged-1",
            ),
            headers=ctx["agent_headers"],
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "error" in body, body
    assert body["error"]["code"] != -32005, body
    assert body["error"]["message"] != ReceiptWriteContendedError.reason, body
    assert body["error"]["code"] == -32007, body
    assert body["error"]["message"] == RECEIPT_WRITE_CONTENDED_AFTER_EFFECTS, body
    # The hazard the assertion above guards is real: the tool did run.
    assert runs["count"] == 1


@pytest.mark.anyio
async def test_a_refunded_call_that_already_ran_is_never_told_to_retry(
    client: AsyncClient, clean_database: None
) -> None:
    """The second post-effect branch, which the success path does not cover.

    A governed tool that performs its side effect and then raises is refunded
    and its budget released, and the failure is receipted through
    ``_finalize_governed_denial`` rather than the success path -- so that helper
    carries ``effects_committed=True`` at this site and converts its own
    contention. The distinction is easy to lose because the receipt records
    ``credits_charged=0``: the money came back, but the call still ran, and
    ``-32005`` here would invite the side effect a second time.

    It also guards the unwind. The retryable path releases the idempotency
    record to make its own advice true; doing that for this call would free a
    key whose side effect already happened.
    """
    tool_name = "receipt-contention-sideeffect"
    runs = {"count": 0}

    def explode_after_running(message: str = "ok") -> dict[str, Any]:
        runs["count"] += 1
        raise RuntimeError("tool_failed_after_side_effect")

    get_service_registry().register_local(
        service_id=tool_name,
        name="Receipt contention side effect",
        description="Receipt write-contention surface test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=explode_after_running,
        credits_per_unit=TOOL_COST,
        unit_name="call",
    )
    try:
        ctx = await provision_agent_wallet(client)
        permit = await create_tool_permit(
            client,
            wallet_id=ctx["agent_wallet_id"],
            key_id=ctx["key_id"],
            tool_name=tool_name,
            max_credits=10,
            idem_key="receipt-contention-permit-4",
        )
        body = _call_body(
            tool_name=tool_name,
            wallet_id=ctx["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key="receipt-contention-refunded-1",
        )

        with pytest.MonkeyPatch.context() as monkeypatch:
            _lose_receipt_commits(monkeypatch, failures=None)
            resp = await client.post(
                "/mcp/messages", json=body, headers=ctx["agent_headers"]
            )

        assert resp.status_code == 200, resp.text
        error = resp.json()["error"]
        assert error["code"] != -32005, error
        assert error["message"] != ReceiptWriteContendedError.reason, error
        assert error["code"] == -32007, error
        assert error["message"] == RECEIPT_WRITE_CONTENDED_AFTER_EFFECTS, error
        assert runs["count"] == 1

        # The key was not freed, so the retry this call was NOT invited to make
        # cannot run the side effect again.
        again = await client.post(
            "/mcp/messages", json=body, headers=ctx["agent_headers"]
        )
        assert again.status_code == 200, again.text
        assert runs["count"] == 1, again.json()
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_the_standard_surface_answers_the_same_named_outcome(
    client: AsyncClient, clean_database: None, standard_mcp_enabled, echo_tool
) -> None:
    """``/mcp`` classifies in its own handler, so the name has to be added there too.

    The two transports do not share an exception ladder: ``/mcp/messages``
    matches types itself and ``/mcp`` maps them into ``McpError``. A name on one
    is not a name on the other, and ``/mcp`` is the surface new integrations are
    pointed at.

    It also pins the body. Refusing a retry without saying what to do instead
    leaves the caller to guess, and the wrong guess -- a fresh idempotency key --
    is a second charged execution of a call that already ran. So the answer
    carries its reason code and the one correct action.
    """
    tool_name, runs = echo_tool
    ctx = await provision_agent_wallet(client)

    with pytest.MonkeyPatch.context() as monkeypatch:
        _lose_receipt_commits(monkeypatch, failures=None)
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
                "Idempotency-Key": "receipt-contention-standard-1",
            },
        )

    assert resp.status_code == 200, resp.text
    error = resp.json()["error"]
    assert error["code"] == -32007, error
    assert error["message"] == RECEIPT_WRITE_CONTENDED_AFTER_EFFECTS, error
    data = error["data"]
    assert data["reason_code"] == RECEIPT_WRITE_CONTENDED_AFTER_EFFECTS, data
    assert data["error"] == "manual_review_required", data
    assert data["remediation"]["type"] == "reconcile_out_of_band", data
    # The tool ran and no receipt landed: exactly the state the name describes.
    assert runs["count"] == 1
    assert await _count_receipts() == 0


@pytest.mark.anyio
async def test_the_legacy_rest_surface_names_an_exhausted_denial_retryable(
    client: AsyncClient, clean_database: None, echo_tool
) -> None:
    """``/mcp/tools/{id}/invoke`` has its own ladder, so it needs the name too.

    The legacy REST route matches exception types in its own ``except`` chain,
    exactly as ``/mcp/messages`` does, and it was the one surface the type was
    never added to. Its ladder ends in ``except Exception`` returning
    ``_internal_error_tool_result`` -- a 200 carrying ``isError`` and an opaque
    ``internal_error`` -- so the omission did not fail loudly anywhere. It
    quietly demoted a classified, retryable loss into the unclassified channel
    for every client still on this transport, which is the precise state
    ``LedgerWriteContendedError`` exists to warn about: a caller that cannot
    tell a contention loss from an unknown fault has to assume its money may
    have moved.

    Asserting 409 is what excludes that fallthrough: the internal_error answer
    is a 200, so no body check is needed to tell the two apart.

    The second half is the obligation the 409 takes on. Naming a loss retryable
    is a promise the retry exists, and the governed idempotency record opened
    before this denial has to have been released for that to be true --
    otherwise every retry of the key meets ``idempotency_in_progress`` and the
    409 points at a door that is locked. So the retry has to arrive at the real
    denial, ``permit_budget_exceeded``, which on this surface is a 403.
    """
    tool_name, runs = echo_tool
    ctx = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=ctx["agent_wallet_id"],
        key_id=ctx["key_id"],
        tool_name=tool_name,
        max_credits=1,
        idem_key="receipt-contention-permit-rest",
    )
    body = {
        "name": tool_name,
        "arguments": {"message": "hello"},
        "mcp_context": {
            "wallet_id": ctx["agent_wallet_id"],
            "permit_id": permit["permit_id"],
            "idempotency_key": "receipt-contention-denied-rest",
        },
    }

    with pytest.MonkeyPatch.context() as monkeypatch:
        state = _lose_receipt_commits(monkeypatch, failures=None)
        resp = await client.post(
            f"/mcp/tools/{tool_name}/invoke",
            json=body,
            headers=ctx["agent_headers"],
        )

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == ReceiptWriteContendedError.reason
    # It gave up only after spending the whole documented budget.
    assert state["commits"] == WRITE_CONFLICT_MAX_ATTEMPTS, state
    # Nothing ran and no receipt is durable: the state the 409 claims.
    assert await _count_receipts() == 0
    assert runs["count"] == 0

    # Now take the retry the 409 promised, on the same key and with the
    # contention gone. It has to reach the real answer.
    again = await client.post(
        f"/mcp/tools/{tool_name}/invoke",
        json=body,
        headers=ctx["agent_headers"],
    )
    assert again.status_code == 403, again.text
    retry_detail = again.json()["detail"]
    assert retry_detail["error"] == "permit_budget_exceeded", retry_detail
    assert retry_detail["receipt"]["credits_charged"] == "0", retry_detail
    assert await _count_receipts() == 1
    assert runs["count"] == 0


@pytest.mark.anyio
async def test_the_legacy_rest_surface_never_tells_a_charged_call_to_retry(
    client: AsyncClient, clean_database: None, echo_tool
) -> None:
    """The guard on the 409 above, and on the clause order that keeps it honest.

    Adding ``ReceiptWriteContendedError`` to the retryable tuple only stays safe
    while the post-effects loss keeps arriving as ``TerminalRecordContendedError``
    and that branch keeps sitting *after* the tuple. Nothing in the types
    enforces either half: all six contention classes derive straight from
    ``RuntimeError``, so no subclass relation makes the order self-correcting,
    and a future edit that moved the terminal type up into the tuple -- or a
    post-effects site that stopped re-typing -- would convert this 500 into a
    409 telling a caller to re-run a call it already paid for.

    The sibling surfaces already pin their half (``-32007`` on ``/mcp`` and
    ``/mcp/messages``); this route did not, which left the ordering the 409
    depends on as the one part of the change with no test under it.

    Here the permit affords the call, so the tool runs and the wallet is charged
    before the receipt insert starts losing. The answer has to be the
    non-retryable one, carrying the reason and the out-of-band remediation
    rather than an opaque internal_error.
    """
    tool_name, runs = echo_tool
    ctx = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=ctx["agent_wallet_id"],
        key_id=ctx["key_id"],
        tool_name=tool_name,
        max_credits=10,
        idem_key="receipt-contention-permit-rest-charged",
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        _lose_receipt_commits(monkeypatch, failures=None)
        resp = await client.post(
            f"/mcp/tools/{tool_name}/invoke",
            json={
                "name": tool_name,
                "arguments": {"message": "hello"},
                "mcp_context": {
                    "wallet_id": ctx["agent_wallet_id"],
                    "permit_id": permit["permit_id"],
                    "idempotency_key": "receipt-contention-charged-rest",
                },
            },
            headers=ctx["agent_headers"],
        )

    # Not 409: that is the retry this call must never be offered. Not 200
    # isError either, which is the internal_error fallthrough.
    assert resp.status_code == 500, resp.text
    detail = resp.json()["detail"]
    assert detail["reason_code"] == RECEIPT_WRITE_CONTENDED_AFTER_EFFECTS, detail
    assert detail["error"] == "manual_review_required", detail
    assert detail["remediation"]["type"] == "reconcile_out_of_band", detail
    # The hazard the status assertion guards is real: the tool did run.
    assert runs["count"] == 1
    assert await _count_receipts() == 0


@pytest.mark.anyio
async def test_a_terminal_dispatch_chain_is_not_advertised_as_a_plain_retry(
    client: AsyncClient, clean_database: None
) -> None:
    """Remote insufficient funds: durable, so it names its owner instead.

    On the upstream path ``complete_pre_dispatch_failure`` drives the dispatch
    attempt terminal *before* this denial is receipted, and
    ``mcp_dispatch_attempts.idempotency_record_id`` is NOT NULL. So the unwind
    that makes the effect-free retry honest cannot run here: its DELETE hits
    the foreign key, raises, and is only logged. Answering
    ``receipt_write_contended`` would then advertise a retry that meets
    ``idempotency_in_progress`` until the dispatch reconciler writes the
    receipt.

    ``idempotency_in_progress`` is the truthful answer, and the one this path
    already gives whenever a durable owner may classify an attempt. The record
    and the attempt must both survive for that owner to find.
    """
    tool_name = "receipt-contention-upstream"
    executor = _NeverDispatchedExecutor()
    get_service_registry().register_upstream(
        service_id=tool_name,
        name="Receipt contention upstream",
        description="Receipt write-contention surface test tool",
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
    try:
        ctx = await provision_agent_wallet(client)
        permit = await create_tool_permit(
            client,
            wallet_id=ctx["agent_wallet_id"],
            key_id=ctx["key_id"],
            tool_name=tool_name,
            max_credits=10,  # the permit allows; the drained wallet is what refuses
            idem_key="receipt-contention-permit-6",
        )
        await _drain_wallet(ctx["agent_wallet_id"])
        body = _call_body(
            tool_name=tool_name,
            wallet_id=ctx["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key="receipt-contention-upstream-1",
        )

        with pytest.MonkeyPatch.context() as monkeypatch:
            _lose_receipt_commits(monkeypatch, failures=None)
            resp = await client.post(
                "/mcp/messages", json=body, headers=ctx["agent_headers"]
            )

        assert resp.status_code == 200, resp.text
        error = resp.json()["error"]
        assert error["message"] != "internal_error", error
        assert error["message"] == "idempotency_in_progress", error
        assert error["code"] == -32005, error
        # Nothing was sent upstream, and the durable chain the reconciler needs
        # is intact: the attempt survived and so did the exact record its
        # foreign key points at. Counting all records would not show this --
        # creating the permit writes one of its own -- and the record this
        # call owns is the one the unwind would have deleted.
        assert executor.dispatches == 0
        async with get_session_factory()() as session:
            attempt = (
                await session.execute(select(McpDispatchAttemptModel))
            ).scalar_one()
            linked = await session.get(
                IdempotencyRecordModel, attempt.idempotency_record_id
            )
        assert linked is not None, "the reconciler's record was deleted"
        assert linked.response_json is None, "no terminal outcome was published"
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_a_consumed_approval_is_not_advertised_as_a_plain_retry(
    client: AsyncClient, clean_database: None, monkeypatch
) -> None:
    """A single-use approval is spent before the denial, so retry is a lie.

    ``HumanApprovalService._finalize`` consumes the approval so it authorizes
    exactly one invoke, and that happens well before a pre-dispatch failure
    like insufficient funds is receipted. The quote consumed on this same path
    is handed back (``QuoteService.release``); the approval is not.

    So freeing the key and advertising ``receipt_write_contended`` would send
    the caller into a *different* denial -- the same key re-reads the same
    approval and is refused for having consumed it. The spent approval is a
    committed effect, so this is the after-effects outcome, with the same
    named reason and remediation a lost post-charge receipt gets.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "SIMULATION_MODE_HUMAN_APPROVAL", True)
    monkeypatch.setattr(settings, "SENTINEL_API_URL", "")
    monkeypatch.setattr(settings, "SENTINEL_API_KEY", "")
    monkeypatch.setattr(settings, "SENTINEL_WAIT_SECONDS", 0.0)

    tool_name = "receipt-contention-approval"
    runs = {"count": 0}

    def echo(message: str = "ok") -> dict[str, Any]:
        runs["count"] += 1
        return {"message": message}

    get_service_registry().register_local(
        service_id=tool_name,
        name="Receipt contention approval",
        description="Receipt write-contention surface test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=echo,
        credits_per_unit=TOOL_COST,
        unit_name="call",
    )
    try:
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
                "requires_human_approval": True,
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(minutes=30)
                ).isoformat(),
            },
            headers={
                **BOOTSTRAP_HEADERS,
                "Idempotency-Key": "receipt-contention-permit-7",
            },
        )
        assert permit_resp.status_code == 201, permit_resp.text
        permit = permit_resp.json()
        assert permit["requires_human_approval"] is True
        await _drain_wallet(ctx["agent_wallet_id"])

        with pytest.MonkeyPatch.context() as inner:
            _lose_receipt_commits(inner, failures=None)
            resp = await client.post(
                "/mcp/messages",
                json=_call_body(
                    tool_name=tool_name,
                    wallet_id=ctx["agent_wallet_id"],
                    permit_id=permit["permit_id"],
                    idempotency_key="receipt-contention-approval-1",
                ),
                headers=ctx["agent_headers"],
            )

        assert resp.status_code == 200, resp.text
        error = resp.json()["error"]
        # Not the retryable envelope: the approval this call spent cannot be
        # spent again, so "retry the same key" is not an answer. It is the
        # named after-effects outcome, on its own code.
        assert error["code"] == -32007, error
        assert error["message"] == RECEIPT_WRITE_CONTENDED_AFTER_EFFECTS, error
        assert runs["count"] == 0
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_a_rejected_approval_denial_is_still_a_plain_retry(
    client: AsyncClient, clean_database: None, monkeypatch
) -> None:
    """A rejected approval spent nothing, so its contended denial may retry.

    The guard on the test above is keyed on *consumption*, and this is why it
    cannot be keyed on ``approval_id``: the rejected and expired terminal path
    carries the approval on its denial receipt as evidence too, but
    ``HumanApprovalService._finalize`` consumes only an approved decision. A
    rejected one left nothing spent, nothing ran and nothing was charged, so
    this is precisely the effect-free loss the retryable answer exists for.

    Getting it wrong is not cosmetic. Answering the after-effects outcome here
    would keep the idempotency record in progress, and reconciliation does not
    delete uncharged local records -- so the same key would never deliver the
    real ``human_approval_rejected`` denial. The retry at the end is the
    proof that the key was freed.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "SIMULATION_MODE_HUMAN_APPROVAL", False)
    monkeypatch.setattr(settings, "SENTINEL_API_URL", "https://sentinel.test")
    monkeypatch.setattr(settings, "SENTINEL_API_KEY", "sk_test_" + "0" * 64)
    monkeypatch.setattr(settings, "SENTINEL_WAIT_SECONDS", 0.0)
    service = HumanApprovalService()
    monkeypatch.setattr(human_approval_module, "_service", service)
    sentinel = _ScriptedSentinel()
    monkeypatch.setattr(service, "_sentinel", lambda: sentinel)

    tool_name = "receipt-contention-rejected-approval"
    runs = {"count": 0}

    def echo(message: str = "ok") -> dict[str, Any]:
        runs["count"] += 1
        return {"message": message}

    get_service_registry().register_local(
        service_id=tool_name,
        name="Receipt contention rejected approval",
        description="Receipt write-contention surface test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=echo,
        credits_per_unit=TOOL_COST,
        unit_name="call",
    )
    try:
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
                "requires_human_approval": True,
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(minutes=30)
                ).isoformat(),
            },
            headers={
                **BOOTSTRAP_HEADERS,
                "Idempotency-Key": "receipt-contention-permit-8",
            },
        )
        assert permit_resp.status_code == 201, permit_resp.text
        permit = permit_resp.json()
        body = _call_body(
            tool_name=tool_name,
            wallet_id=ctx["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key="receipt-contention-rejected-1",
        )

        # Park the key on a pending approval, then have the reviewer reject it.
        pending = await client.post(
            "/mcp/messages", json=body, headers=ctx["agent_headers"]
        )
        assert pending.status_code == 200, pending.text
        pending_error = pending.json()["error"]
        assert pending_error["message"] == "human_approval_pending", pending_error
        approval_id = pending_error["data"]["approval_id"]
        sentinel.status = "rejected"

        with pytest.MonkeyPatch.context() as inner:
            _lose_receipt_commits(inner, failures=None)
            resp = await client.post(
                "/mcp/messages", json=body, headers=ctx["agent_headers"]
            )

        assert resp.status_code == 200, resp.text
        error = resp.json()["error"]
        # The effect-free answer, not the after-effects one: nothing was spent.
        assert error["code"] == -32005, error
        assert error["message"] == ReceiptWriteContendedError.reason, error
        assert await _count_receipts() == 0
        assert runs["count"] == 0

        # Take the retry it advertised. It has to reach the real denial, with
        # its evidence, which is only possible if the key was actually freed.
        again = await client.post(
            "/mcp/messages", json=body, headers=ctx["agent_headers"]
        )
        assert again.status_code == 200, again.text
        denial = again.json()["error"]
        assert denial["message"] != "idempotency_in_progress", denial
        assert denial["code"] == -32003, denial
        assert denial["message"] == "human_approval_rejected", denial
        receipt = denial["data"]["receipt"]
        assert receipt["outcome"] == "denied"
        assert receipt["approval_id"] == approval_id
        assert receipt["credits_charged"] == "0"
        assert await _count_receipts() == 1
        assert runs["count"] == 0
    finally:
        get_service_registry().unregister_local(tool_name)
