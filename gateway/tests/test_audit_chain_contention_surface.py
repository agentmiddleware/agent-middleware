"""Where an audit-chain loss is retryable, and the one place it must not be.

``append_chained_audit_event`` retries a contended per-wallet chain head 64
times and then raises ``AuditChainContendedError``. What that should mean to
the caller depends entirely on whether the call had already run.

A refusal that ran nothing -- a permit denial, a lost quote -- can say "retry":
no tool executed and no wallet moved, so a second attempt costs nothing and the
first left no terminal record behind. That is the retryable ``-32005`` answer.

Every audit write past the charge is the opposite, and is the reason this file
exists. It audits *after* the tool ran and the wallet was charged, so "retry"
would invite a second execution of a call the caller already paid for. Nor can
it be softened into a success: ``create_receipt`` takes
``audit_event_id=audit_event.event_id``, so no audit event means no signed
receipt either, and ``reconcile_stuck_records`` repairs a stuck record only when
a receipt exists -- without one it counts the record for manual review instead.
The honest answer there is its own non-retryable code, ``-32007``: the state is
one the pipeline classified deliberately, so spending the unclassified
``-32603`` on it hid a known outcome among genuine bugs and left a client free
to read "internal error" as "try again".

Finalization is only the most obvious of those sites. The local
refund-succeeded path and the upstream post-charge helpers audit after
execution too, and guarding finalize alone left both free to hand a caller who
had already run and paid a "retry" -- so each is pinned separately below rather
than trusted to a shared reading of the control flow.

The retryable half carries its own obligation, tested here too: the -32005 has
to name a retry the caller can actually make. A governed call has an
idempotency record open by the time these refusals happen, and reconciliation
deliberately does not delete uncharged local records, so a record left in
progress would meet every retry of that key with ``idempotency_in_progress``
forever.

The last two tests pin that obligation from both sides.

A remote refusal has already driven its dispatch attempt terminal, and
``mcp_dispatch_attempts`` pins the record by a NOT NULL foreign key, so the
release cannot happen at all -- the unwind's DELETE raises and is only logged.
There the retryable answer has to name the reconciler that owns the record
instead of a retry of the key.

A local refusal behind a human-approval gate is the opposite, and is why the
retryable answer is kept there rather than withheld. The single-use approval is
spent, so the retry is refused -- but refused with a signed, receipted
human_approval_consumed denial, which is a terminal answer. Withholding the
release to spare the caller that refusal would strand the key instead, in
exactly the way the paragraph above describes, and no ``-32007`` would be owed
either: nothing was charged and nothing ran, so there is no committed effect for
that code to name. The second request is asserted, not just the first, because
that is where the difference shows.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.config import get_settings
from app.db.database import get_session_factory
from app.db.models import (
    IdempotencyRecordModel,
    McpDispatchAttemptModel,
    WalletModel,
)
from app.main import app
from app.routers.mcp import AUDIT_CHAIN_CONTENDED_AFTER_EFFECTS
from app.schemas.billing import ServiceCategory
from app.services import audit_chain
from app.services.idempotency import (
    GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
    get_idempotency_service,
)
from app.services.service_registry import get_service_registry
from app.services.upstream_mcp import UpstreamMcpResult
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
def audited_tool():
    tool_name = "audit-contention-echo"
    runs = {"count": 0}

    def echo(message: str = "ok") -> dict[str, Any]:
        runs["count"] += 1
        return {"message": message}

    get_service_registry().register_local(
        service_id=tool_name,
        name="Audit contention echo",
        description="Audit-chain contention surface test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=echo,
        credits_per_unit=TOOL_COST,
        unit_name="call",
    )
    try:
        yield tool_name, runs
    finally:
        get_service_registry().unregister_local(tool_name)


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


def _always_contended(monkeypatch) -> None:
    """Make every chain-head append lose, deterministically.

    Patched at ``_sign_with_previous`` rather than raced: a test that waits for
    real contention passes on a fast machine for the wrong reason.
    """

    def _lost(*args: Any, **kwargs: Any) -> None:
        raise audit_chain._HeadConflict()

    monkeypatch.setattr(audit_chain, "_sign_with_previous", _lost)


@pytest.mark.anyio
async def test_a_refusal_that_ran_nothing_answers_audit_contention_as_retryable(
    client: AsyncClient, clean_database: None, audited_tool, monkeypatch
) -> None:
    """A denial whose audit write loses the chain head is safe to retry.

    The permit is not valid for this tool, so the call is refused before
    anything runs. Writing that denial to the audit chain is what fails here,
    and because nothing executed the caller can simply try again -- which is
    what -32005 says. Before this it landed as an unclassified internal_error.
    """
    tool_name, runs = audited_tool
    ctx = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=ctx["agent_wallet_id"],
        key_id=ctx["key_id"],
        tool_name="some-other-tool",
        idem_key="audit-contention-permit-1",
    )

    _always_contended(monkeypatch)
    resp = await client.post(
        "/mcp/messages",
        json=_call_body(
            tool_name=tool_name,
            wallet_id=ctx["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key="audit-contention-denied-1",
        ),
        headers=ctx["agent_headers"],
    )
    monkeypatch.undo()

    assert resp.status_code == 200, resp.text
    error = resp.json()["error"]
    assert error["message"] != "internal_error", error
    assert error["code"] == -32005, error
    # The tool never ran, which is what makes the retryable answer honest.
    assert runs["count"] == 0

    # Now take the retry that -32005 promised, on the same key. Asserting the
    # code alone proved the classification and nothing about the promise: the
    # governed record opened before this denial was left in progress, so the
    # retry came back idempotency_in_progress and no retry of that key could
    # ever have made progress again.
    again = await client.post(
        "/mcp/messages",
        json=_call_body(
            tool_name=tool_name,
            wallet_id=ctx["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key="audit-contention-denied-1",
        ),
        headers=ctx["agent_headers"],
    )
    assert again.status_code == 200, again.text
    retry_error = again.json()["error"]
    assert retry_error["message"] != "idempotency_in_progress", retry_error
    # It reaches the real answer instead: the permit does not cover this tool.
    assert retry_error["code"] != -32005, retry_error
    assert runs["count"] == 0


@pytest.mark.anyio
async def test_finalization_never_tells_a_charged_caller_to_retry(
    client: AsyncClient, clean_database: None, audited_tool, monkeypatch
) -> None:
    """The call ran and was charged, so its audit loss stays non-retryable.

    This is the guard on the classification above. Finalization writes its
    audit event after execution, so answering -32005 there would invite a
    second run of a call the caller already paid for, and reporting success
    would hand back a charged call with no receipt -- the receipt is built from
    the audit event's id -- and a record reconciliation can only flag for
    manual review. It fails non-retryably under its own name, ``-32007``:
    named because the code classified this state on purpose, and separate from
    ``-32603`` so an operator no longer has to grep correlation ids to tell it
    from a genuine bug.
    """
    tool_name, runs = audited_tool
    ctx = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=ctx["agent_wallet_id"],
        key_id=ctx["key_id"],
        tool_name=tool_name,
        max_credits=10,
        idem_key="audit-contention-permit-2",
    )

    real_sign = audit_chain._sign_with_previous
    state = {"executed": False}

    def _lose_only_after_the_tool_ran(*args: Any, **kwargs: Any) -> None:
        # Pre-charge audits still succeed; only the post-execution finalize
        # write loses, which is the case under test.
        if state["executed"]:
            raise audit_chain._HeadConflict()
        return real_sign(*args, **kwargs)

    def echo_then_arm(message: str = "ok") -> dict[str, Any]:
        runs["count"] += 1
        state["executed"] = True
        return {"message": message}

    get_service_registry().register_local(
        service_id=tool_name,
        name="Audit contention echo",
        description="Audit-chain contention surface test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=echo_then_arm,
        credits_per_unit=TOOL_COST,
        unit_name="call",
    )
    monkeypatch.setattr(
        audit_chain, "_sign_with_previous", _lose_only_after_the_tool_ran
    )

    resp = await client.post(
        "/mcp/messages",
        json=_call_body(
            tool_name=tool_name,
            wallet_id=ctx["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key="audit-contention-charged-1",
        ),
        headers=ctx["agent_headers"],
    )
    monkeypatch.undo()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "error" in body, body
    # Never the retryable code: the tool ran once and the wallet was charged.
    assert body["error"]["code"] != -32005, body
    assert body["error"]["code"] == -32007, body
    assert body["error"]["message"] == AUDIT_CHAIN_CONTENDED_AFTER_EFFECTS, body
    assert runs["count"] == 1


@pytest.mark.anyio
async def test_a_local_call_that_ran_and_refunded_is_never_told_to_retry(
    client: AsyncClient, clean_database: None, monkeypatch
) -> None:
    """The refund-succeeded path audits after execution, so it is not retryable.

    A local tool that performs its side effect and then raises is refunded, its
    permit budget released, and the failure audited -- and that audit write sits
    outside the finalize loop. Guarding finalize alone therefore left this path
    answering -32005 for a call that had already run. With legacy unpermitted
    MCP there is no durable idempotency record behind that answer, so the retry
    it invites simply runs the side effect a second time.
    """
    tool_name = "audit-contention-sideeffect"
    runs = {"count": 0}

    def explode_after_running(message: str = "ok") -> dict[str, Any]:
        runs["count"] += 1
        raise RuntimeError("tool_failed_after_side_effect")

    get_service_registry().register_local(
        service_id=tool_name,
        name="Audit contention side effect",
        description="Audit-chain contention surface test tool",
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
            idem_key="audit-contention-permit-3",
        )

        real_sign = audit_chain._sign_with_previous

        def _lose_only_after_the_tool_ran(*args: Any, **kwargs: Any) -> None:
            if runs["count"]:
                raise audit_chain._HeadConflict()
            return real_sign(*args, **kwargs)

        monkeypatch.setattr(
            audit_chain, "_sign_with_previous", _lose_only_after_the_tool_ran
        )
        resp = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=ctx["agent_wallet_id"],
                permit_id=permit["permit_id"],
                idempotency_key="audit-contention-sideeffect-1",
            ),
            headers=ctx["agent_headers"],
        )
        monkeypatch.undo()
    finally:
        get_service_registry().unregister_local(tool_name)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "error" in body, body
    assert body["error"]["code"] != -32005, body
    assert body["error"]["code"] == -32007, body
    assert body["error"]["message"] == AUDIT_CHAIN_CONTENDED_AFTER_EFFECTS, body
    assert runs["count"] == 1


@pytest.mark.anyio
async def test_a_charged_upstream_call_is_never_told_to_retry(
    client: AsyncClient, clean_database: None, monkeypatch
) -> None:
    """The upstream success audit runs after dispatch and charge, so not retryable.

    ``_execute_upstream_after_charge`` and its two failure helpers write their
    audit events once the partner has been called and the wallet debited. They
    are a separate code path from finalize, and guarding finalize alone left
    them answering -32005 for a call the partner had already executed -- the
    one outcome the gateway's at-most-one-dispatch promise cannot survive.
    """
    tool_name = "audit-contention-upstream"
    dispatches = {"count": 0}

    class _CountingUpstream:
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
            payload = {"content": [{"type": "text", "text": "partner ok"}]}
            canonical = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            return UpstreamMcpResult(
                payload=payload,
                canonical_json=canonical,
                response_hash=hashlib.sha256(canonical.encode()).hexdigest(),
                size_bytes=len(canonical.encode()),
                is_error=False,
            )

    get_service_registry().register_upstream(
        service_id=tool_name,
        name="Audit contention upstream",
        description="Audit-chain contention surface test tool",
        category=ServiceCategory.AGENT_COMMS,
        executor=_CountingUpstream(),
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
        ctx = await provision_agent_wallet(client)
        permit = await create_tool_permit(
            client,
            wallet_id=ctx["agent_wallet_id"],
            key_id=ctx["key_id"],
            tool_name=tool_name,
            max_credits=10,
            idem_key="audit-contention-permit-4",
        )

        real_sign = audit_chain._sign_with_previous

        def _lose_only_after_dispatch(*args: Any, **kwargs: Any) -> None:
            if dispatches["count"]:
                raise audit_chain._HeadConflict()
            return real_sign(*args, **kwargs)

        monkeypatch.setattr(
            audit_chain, "_sign_with_previous", _lose_only_after_dispatch
        )
        resp = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=ctx["agent_wallet_id"],
                permit_id=permit["permit_id"],
                idempotency_key="audit-contention-upstream-1",
            ),
            headers=ctx["agent_headers"],
        )
        monkeypatch.undo()
    finally:
        get_service_registry().unregister_local(tool_name)

    assert resp.status_code == 200, resp.text
    # The partner ran exactly once; nothing may invite a second send.
    assert dispatches["count"] == 1
    body = resp.json()
    if "error" in body:
        assert body["error"]["code"] != -32005, body
        assert body["error"]["code"] == -32007, body
        assert body["error"]["message"] == AUDIT_CHAIN_CONTENDED_AFTER_EFFECTS, body


@pytest.mark.anyio
async def test_a_contended_refusal_does_not_free_another_calls_live_key(
    client: AsyncClient, clean_database: None, audited_tool, monkeypatch
) -> None:
    """The unwind releases this invocation's record, never the winner's.

    Two governed calls share an idempotency key. The first is granted the
    record and runs on. The second is refused ``idempotency_in_progress`` --
    and audits that refusal like any other refusal, so its audit write can lose
    the chain head too.

    Releasing by (wallet, endpoint, key) at that point deletes the *winner's*
    live, uncharged row: those coordinates name whichever row holds them now,
    and the loser never held one. The winner would then keep running with its
    at-most-once protection gone, and the freed key would be available to
    execute and debit the same call a second time -- a worse outcome than the
    stranded key the unwind exists to prevent.

    So the release is pinned to the record id the begin returned to *this*
    invocation, and this test is the guard on that pin.
    """
    tool_name, runs = audited_tool
    ctx = await provision_agent_wallet(client)
    wallet_id = ctx["agent_wallet_id"]
    permit = await create_tool_permit(
        client,
        wallet_id=wallet_id,
        key_id=ctx["key_id"],
        tool_name=tool_name,
        max_credits=10,
        idem_key="audit-contention-permit-5",
    )
    shared_key = "audit-contention-concurrent-1"

    # The winner: hold the record open exactly as an in-flight call would.
    idem = get_idempotency_service()
    winner = await idem.begin_with_record(
        wallet_id=wallet_id,
        endpoint=GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
        idempotency_key=shared_key,
        request_payload={
            "tool_name": tool_name,
            "arguments": {"message": "hello"},
            "wallet_id": wallet_id,
            "permit_id": permit["permit_id"],
        },
        operation_kind="local",
    )
    assert winner.replay is None

    _always_contended(monkeypatch)
    loser = await client.post(
        "/mcp/messages",
        json=_call_body(
            tool_name=tool_name,
            wallet_id=wallet_id,
            permit_id=permit["permit_id"],
            idempotency_key=shared_key,
        ),
        headers=ctx["agent_headers"],
    )
    monkeypatch.undo()

    assert loser.status_code == 200, loser.text
    assert "error" in loser.json(), loser.text

    # The winner still holds its record, and still the same one.
    held = await idem.get_record(
        wallet_id=wallet_id,
        endpoint=GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
        idempotency_key=shared_key,
    )
    assert held is not None, "the loser's unwind deleted the winner's record"
    assert held.record_id == winner.record_id
    # Nothing executed on either call.
    assert runs["count"] == 0


@pytest.mark.anyio
async def test_a_genuine_bug_in_the_same_window_stays_unclassified(
    client: AsyncClient, clean_database: None, audited_tool, monkeypatch
) -> None:
    """The discriminator the name is for: -32007 is not the new internal_error.

    Same tool, same charge, same post-execution window as the test above. The
    only difference is the cause -- the chain signer raises an ordinary
    exception instead of losing the head -- and that is a fault the pipeline
    genuinely did not classify. It must still land on ``-32603`` with a
    correlation id.

    Without this, ``-32007`` would prove nothing. A named code that every
    post-charge failure returns is the same undifferentiated bucket as
    ``internal_error``, just spelled differently, and an operator would be back
    to grepping correlation ids to find the real bug.
    """
    tool_name, runs = audited_tool
    ctx = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=ctx["agent_wallet_id"],
        key_id=ctx["key_id"],
        tool_name=tool_name,
        max_credits=10,
        idem_key="audit-contention-permit-6",
    )

    real_sign = audit_chain._sign_with_previous
    state = {"executed": False}

    def _explode_only_after_the_tool_ran(*args: Any, **kwargs: Any) -> None:
        if state["executed"]:
            # Not a _HeadConflict: nothing about this is contention, so the
            # chain's retry budget never sees it and no site re-types it.
            raise RuntimeError("chain_signer_exploded")
        return real_sign(*args, **kwargs)

    def echo_then_arm(message: str = "ok") -> dict[str, Any]:
        runs["count"] += 1
        state["executed"] = True
        return {"message": message}

    get_service_registry().register_local(
        service_id=tool_name,
        name="Audit contention echo",
        description="Audit-chain contention surface test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=echo_then_arm,
        credits_per_unit=TOOL_COST,
        unit_name="call",
    )
    monkeypatch.setattr(
        audit_chain, "_sign_with_previous", _explode_only_after_the_tool_ran
    )

    resp = await client.post(
        "/mcp/messages",
        json=_call_body(
            tool_name=tool_name,
            wallet_id=ctx["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key="audit-contention-bug-1",
        ),
        headers=ctx["agent_headers"],
    )
    monkeypatch.undo()

    assert resp.status_code == 200, resp.text
    error = resp.json()["error"]
    assert error["code"] == -32603, error
    assert error["message"] == "internal_error", error
    # The unclassified channel keeps its correlation id, and the exception text
    # stays server-side.
    assert error["data"]["correlation_id"], error
    assert "chain_signer_exploded" not in resp.text
    assert runs["count"] == 1


@pytest.mark.anyio
async def test_the_standard_surface_answers_the_same_named_outcome(
    client: AsyncClient, clean_database: None, standard_mcp_enabled, monkeypatch
) -> None:
    """``/mcp`` and ``/mcp/messages`` agree, and the answer is actionable.

    The two transports classify in separate handlers, so a name added to one is
    not a name on the other -- and ``/mcp`` is the surface new integrations are
    told to use. It also pins the body: a caller that is refused a retry has to
    be told what to do instead, which here is "do not mint a new key, reconcile
    out of band". A bare code would leave the correct action to guesswork, and
    the wrong guess is a second charged execution.
    """
    tool_name = "audit-contention-standard"
    runs = {"count": 0}
    state = {"executed": False}

    def echo_then_arm(message: str = "ok") -> dict[str, Any]:
        runs["count"] += 1
        state["executed"] = True
        return {"message": message}

    get_service_registry().register_local(
        service_id=tool_name,
        name="Audit contention standard",
        description="Audit-chain contention surface test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=echo_then_arm,
        credits_per_unit=TOOL_COST,
        unit_name="call",
    )
    try:
        ctx = await provision_agent_wallet(client)
        real_sign = audit_chain._sign_with_previous

        def _lose_only_after_the_tool_ran(*args: Any, **kwargs: Any) -> None:
            # The permit is auto-minted on this surface, and that mint audits
            # too -- so losing from the start would refuse the call before it
            # ever reached the window under test.
            if state["executed"]:
                raise audit_chain._HeadConflict()
            return real_sign(*args, **kwargs)

        monkeypatch.setattr(
            audit_chain, "_sign_with_previous", _lose_only_after_the_tool_ran
        )
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
                "Idempotency-Key": "audit-contention-standard-1",
            },
        )
        monkeypatch.undo()
    finally:
        get_service_registry().unregister_local(tool_name)

    assert resp.status_code == 200, resp.text
    error = resp.json()["error"]
    assert error["code"] == -32007, error
    assert error["message"] == AUDIT_CHAIN_CONTENDED_AFTER_EFFECTS, error
    data = error["data"]
    assert data["reason_code"] == AUDIT_CHAIN_CONTENDED_AFTER_EFFECTS, data
    assert data["error"] == "manual_review_required", data
    assert data["remediation"]["type"] == "reconcile_out_of_band", data
    assert runs["count"] == 1


class _NeverDispatchedExecutor:
    """Upstream stand-in that records whether it was ever reached.

    The branch below refuses at the charge, after the dispatch attempt has been
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


async def _drain_wallet(wallet_id: str) -> None:
    """Drop the balance below the tool cost so the charge refuses.

    It has to be done after the permit exists, because permit creation refuses
    a ``max_credits`` above the wallet's balance -- so an unfunded call cannot
    be set up by pricing the tool above the wallet in the first place.
    """
    async with get_session_factory()() as session:
        wallet = (
            await session.execute(
                select(WalletModel).where(WalletModel.wallet_id == wallet_id)
            )
        ).scalar_one()
        wallet.balance = Decimal("1")
        await session.commit()


@pytest.mark.anyio
async def test_a_terminal_dispatch_chain_is_not_advertised_as_a_plain_retry(
    client: AsyncClient, clean_database: None, monkeypatch
) -> None:
    """Remote insufficient funds: durable, so it names its owner instead.

    Nothing ran here, so by the rule at the top of this file the loss is
    retryable -- but the release that makes a retryable answer honest cannot
    happen. ``complete_pre_dispatch_failure`` drives the dispatch attempt
    terminal *before* this denial is audited, and
    ``mcp_dispatch_attempts.idempotency_record_id`` is NOT NULL under
    ``PRAGMA foreign_keys=ON``, so the unwind's DELETE raises on the foreign key
    and is only logged. Answering ``audit_chain_head_contention`` would then
    advertise a retry that meets ``idempotency_in_progress`` until the dispatch
    reconciler writes the receipt.

    ``idempotency_in_progress`` is the truthful answer, and the one this path
    already gives whenever a durable owner may classify an attempt. The record
    and the attempt must both survive for that owner to find.
    """
    tool_name = "audit-contention-unfunded-upstream"
    executor = _NeverDispatchedExecutor()
    get_service_registry().register_upstream(
        service_id=tool_name,
        name="Audit contention unfunded upstream",
        description="Audit-chain contention surface test tool",
        category=ServiceCategory.AGENT_COMMS,
        executor=executor,
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
        ctx = await provision_agent_wallet(client)
        permit = await create_tool_permit(
            client,
            wallet_id=ctx["agent_wallet_id"],
            key_id=ctx["key_id"],
            tool_name=tool_name,
            max_credits=10,  # the permit allows; the drained wallet is what refuses
            idem_key="audit-contention-permit-6",
        )
        await _drain_wallet(ctx["agent_wallet_id"])

        _always_contended(monkeypatch)
        resp = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=ctx["agent_wallet_id"],
                permit_id=permit["permit_id"],
                idempotency_key="audit-contention-unfunded-upstream-1",
            ),
            headers=ctx["agent_headers"],
        )
        monkeypatch.undo()

        assert resp.status_code == 200, resp.text
        error = resp.json()["error"]
        assert error["message"] != "internal_error", error
        assert error["message"] != audit_chain.AuditChainContendedError.reason, error
        assert error["message"] == "idempotency_in_progress", error
        assert error["code"] == -32005, error
        # Nothing was sent upstream, and the durable chain the reconciler needs
        # is intact: the attempt survived and so did the exact record its
        # foreign key points at. Counting all records would not show this --
        # creating the permit writes one of its own -- and the record this call
        # owns is the one the unwind would have deleted.
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
async def test_a_consumed_approval_still_reaches_a_terminal_answer(
    client: AsyncClient, clean_database: None, monkeypatch
) -> None:
    """A spent approval does not make the retryable answer a lie.

    ``HumanApprovalService._finalize`` consumes the approval so it authorizes
    exactly one invoke, well before a pre-dispatch failure like insufficient
    funds is audited, and unlike the quote released on that same path
    (``QuoteService.release``) it is not compensated. It is tempting to read
    that as "no retry can reproduce this call, so do not advertise one" and
    withhold the release.

    That reading is wrong, and this test is the guard on not taking it. The
    retry is refused -- but refused *terminally*, with a signed
    human_approval_consumed receipt, so the key resolves. Withholding the
    release would strand it instead: the record is local and uncharged (the
    charge refusing is why this path was reached at all), and
    ``reconcile_stuck_records`` adopts a local row only on proof of a committed
    debit, so no pass would ever touch it. Every later use of that key would
    answer ``idempotency_in_progress`` forever, with no receipt, no audit event
    and no manual-review count -- the same false retry, made permanent.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "SIMULATION_MODE_HUMAN_APPROVAL", True)
    monkeypatch.setattr(settings, "SENTINEL_API_URL", "")
    monkeypatch.setattr(settings, "SENTINEL_API_KEY", "")
    monkeypatch.setattr(settings, "SENTINEL_WAIT_SECONDS", 0.0)

    tool_name = "audit-contention-approval"
    runs = {"count": 0}

    def echo(message: str = "ok") -> dict[str, Any]:
        runs["count"] += 1
        return {"message": message}

    get_service_registry().register_local(
        service_id=tool_name,
        name="Audit contention approval",
        description="Audit-chain contention surface test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=echo,
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
            max_credits=10,  # the permit allows; the drained wallet is what refuses
            idem_key="audit-contention-permit-7",
            requires_human_approval=True,
        )
        assert permit["requires_human_approval"] is True
        await _drain_wallet(ctx["agent_wallet_id"])
        body = _call_body(
            tool_name=tool_name,
            wallet_id=ctx["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key="audit-contention-approval-1",
        )

        _always_contended(monkeypatch)
        resp = await client.post(
            "/mcp/messages", json=body, headers=ctx["agent_headers"]
        )
        monkeypatch.undo()

        assert resp.status_code == 200, resp.text
        error = resp.json()["error"]
        assert error["message"] != "internal_error", error
        assert error["code"] == -32005, error
        assert runs["count"] == 0

        # The key was released, so the retry is not met by its own predecessor.
        idem = get_idempotency_service()
        assert (
            await idem.get_record(
                wallet_id=ctx["agent_wallet_id"],
                endpoint=GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
                idempotency_key="audit-contention-approval-1",
            )
            is None
        ), "the key was stranded in progress"

        # Take the retry the -32005 named. The spent approval refuses it, and
        # that refusal is the terminal, signed answer -- not another -32005.
        again = await client.post(
            "/mcp/messages", json=body, headers=ctx["agent_headers"]
        )
        assert again.status_code == 200, again.text
        retry_error = again.json()["error"]
        assert retry_error["message"] != "idempotency_in_progress", retry_error
        assert retry_error["code"] != -32005, retry_error
        assert retry_error["message"] == "human_approval_consumed", retry_error
        receipt = retry_error["data"]["receipt"]
        assert receipt["outcome"] == "denied", receipt
        assert receipt["reason_code"] == "human_approval_consumed", receipt
        assert receipt["credits_charged"] == "0", receipt
        assert receipt["signature"], receipt
        assert runs["count"] == 0
    finally:
        get_service_registry().unregister_local(tool_name)
