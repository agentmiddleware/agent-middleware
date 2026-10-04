"""Two boundary changes: denials that say how much, reads a wallet can do itself.

**Denial detail (P3).** A governed denial used to be a bare reason string. An
agent told only `permit_budget_exceeded` can retry and fail again; the same
denial carrying `{"required_credits": ..., "remaining_credits": ...}` tells it
what permit to ask for. Details describe the caller's own permit, so they
disclose nothing a permit holder cannot already read — and the two binding
mismatches (wrong wallet, wrong key) deliberately carry no values, because the
caller has not proved it is the subject.

**Wallet-scoped trust reads (P4).** Reading your own permits, audit events,
audit summary, chain verification, and failed-refund items required an
operator key. Those reads are now self-service, scoped to the caller's wallet.
The tests that matter here are the negative ones: a wallet key must still see
only its own rows, and the refund *retry* — which moves money — stays
operator-only.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.database import get_session_factory
from app.db.models import PermitModel
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.service_registry import get_service_registry
from tests.test_trust_helpers import (
    BOOTSTRAP_HEADERS,
    create_tool_permit,
    provision_agent_wallet,
)

TOOL = "denial-echo"
OTHER_TOOL = "denial-other"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def registered_tool():
    registry = get_service_registry()

    def echo(message: str = "ok") -> dict:
        return {"message": message}

    registry.register_local(
        service_id=TOOL,
        name="Denial Echo",
        description="Governed denial-detail test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=echo,
        credits_per_unit=5.0,
        unit_name="call",
    )
    yield
    registry.unregister_local(TOOL)


async def _invoke(client, headers, *, wallet_id, permit_id, tool=TOOL):
    return await client.post(
        f"/mcp/tools/{tool}/invoke",
        json={
            "name": tool,
            "arguments": {"message": "hi"},
            "mcp_context": {
                "wallet_id": wallet_id,
                "permit_id": permit_id,
                "idempotency_key": f"inv-{uuid.uuid4().hex[:12]}",
            },
        },
        headers=headers,
    )


# --- P3: denial detail ------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_denial_says_what_was_needed_and_what_is_left(
    client, clean_database, registered_tool
):
    agent = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=agent["agent_wallet_id"],
        key_id=agent["key_id"],
        tool_name=TOOL,
        max_credits=3,
        idem_key="permit-detail-budget",
    )

    resp = await _invoke(
        client,
        agent["agent_headers"],
        wallet_id=agent["agent_wallet_id"],
        permit_id=permit["permit_id"],
    )
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["error"] == "permit_budget_exceeded"
    assert detail["receipt"]["reason_code"] == "permit_budget_exceeded"
    assert detail["receipt"]["credits_charged"] == "0"
    numbers = detail["details"]
    # Enough to compute the permit to ask for next.
    assert Decimal(numbers["required_credits"]) == Decimal("5.0")
    assert Decimal(numbers["remaining_credits"]) == Decimal("3")
    assert Decimal(numbers["spent_credits"]) == Decimal("0")
    assert Decimal(numbers["max_credits"]) == Decimal("3")


@pytest.mark.asyncio
async def test_tool_and_scope_denials_name_what_was_allowed(
    client, clean_database, registered_tool
):
    agent = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=agent["agent_wallet_id"],
        key_id=agent["key_id"],
        tool_name=OTHER_TOOL,
        idem_key="permit-detail-tool",
    )

    resp = await _invoke(
        client,
        agent["agent_headers"],
        wallet_id=agent["agent_wallet_id"],
        permit_id=permit["permit_id"],
    )
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["error"] == "permit_tool_not_allowed"
    assert detail["details"]["requested_tool"] == TOOL
    assert detail["details"]["allowed_tools"] == [OTHER_TOOL]


@pytest.mark.asyncio
async def test_expired_denial_says_when_it_expired(
    client, clean_database, registered_tool
):
    agent = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=agent["agent_wallet_id"],
        key_id=agent["key_id"],
        tool_name=TOOL,
        idem_key="permit-detail-expiry",
    )
    factory = get_session_factory()
    async with factory() as session:
        model = await session.get(PermitModel, permit["permit_id"])
        model.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            minutes=5
        )
        session.add(model)
        await session.commit()

    resp = await _invoke(
        client,
        agent["agent_headers"],
        wallet_id=agent["agent_wallet_id"],
        permit_id=permit["permit_id"],
    )
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["error"] == "permit_expired"
    assert detail["details"]["expired_at"].endswith("Z")
    assert detail["details"]["checked_at"].endswith("Z")


@pytest.mark.asyncio
async def test_binding_mismatch_denials_carry_no_values(
    client, clean_database, registered_tool
):
    """A caller that failed to prove it is the subject learns only that."""
    agent = await provision_agent_wallet(client)
    stranger = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=stranger["agent_wallet_id"],
        key_id=stranger["key_id"],
        tool_name=TOOL,
        idem_key="permit-detail-binding",
    )

    verified = await client.post(
        "/v1/permits/verify",
        json={
            "permit_id": permit["permit_id"],
            "wallet_id": stranger["agent_wallet_id"],
            "tool": TOOL,
            "estimated_credits": 1,
        },
        headers=stranger["agent_headers"],
    )
    assert verified.json()["valid"] is True

    # Same permit, wrong subject wallet: the reason names the binding that
    # failed, and nothing about the wallet or key it is actually bound to.
    # Asked as an operator, since a foreign wallet key is refused the permit
    # outright (403) before any verdict is computed — the detail change never
    # widens who can ask.
    mismatch = await client.post(
        "/v1/permits/verify",
        json={
            "permit_id": permit["permit_id"],
            "wallet_id": agent["agent_wallet_id"],
            "tool": TOOL,
            "estimated_credits": 1,
        },
        headers=BOOTSTRAP_HEADERS,
    )
    body = mismatch.json()
    assert body["valid"] is False
    assert body["reason"] == "permit_wallet_mismatch"
    assert body["details"] == {"bound_to": "subject_wallet_id"}
    assert stranger["agent_wallet_id"] not in str(body["details"])

    # And a foreign wallet key cannot reach the verdict at all.
    assert (
        await client.post(
            "/v1/permits/verify",
            json={
                "permit_id": permit["permit_id"],
                "wallet_id": agent["agent_wallet_id"],
                "tool": TOOL,
                "estimated_credits": 1,
            },
            headers=agent["agent_headers"],
        )
    ).status_code == 403


@pytest.mark.asyncio
async def test_verify_reports_no_details_for_a_valid_permit(
    client, clean_database, registered_tool
):
    agent = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=agent["agent_wallet_id"],
        key_id=agent["key_id"],
        tool_name=TOOL,
        idem_key="permit-detail-valid",
    )
    resp = await client.post(
        "/v1/permits/verify",
        json={
            "permit_id": permit["permit_id"],
            "wallet_id": agent["agent_wallet_id"],
            "tool": TOOL,
            "estimated_credits": 1,
        },
        headers=agent["agent_headers"],
    )
    body = resp.json()
    assert body["valid"] is True
    assert body["details"] is None


@pytest.mark.asyncio
async def test_verify_names_missing_action_context_instead_of_a_binding_reason(
    client, clean_database, registered_tool
):
    """An incomplete verify request must say what it left out.

    ``verify`` decides an action, so it needs the action. Asked without a
    wallet or a tool it used to be evaluated against empty strings and answer
    ``permit_wallet_mismatch`` — which reads as "this permit is not yours" to
    the permit's own subject, and sent at least one validation run chasing a
    binding bug that did not exist.
    """
    agent = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=agent["agent_wallet_id"],
        key_id=agent["key_id"],
        tool_name=TOOL,
        idem_key="permit-verify-context",
    )

    async def verify(body):
        resp = await client.post(
            "/v1/permits/verify",
            json={"permit_id": permit["permit_id"], **body},
            headers=agent["agent_headers"],
        )
        assert resp.status_code == 200
        return resp.json()

    both_missing = await verify({})
    assert both_missing["valid"] is False
    assert both_missing["reason"] == "permit_verify_context_missing"
    assert both_missing["details"] == {"missing": ["wallet_id", "tool"]}

    no_tool = await verify({"wallet_id": agent["agent_wallet_id"]})
    assert no_tool["valid"] is False
    assert no_tool["reason"] == "permit_verify_context_missing"
    assert no_tool["details"] == {"missing": ["tool"]}

    no_wallet = await verify({"tool": TOOL, "estimated_credits": 1})
    assert no_wallet["valid"] is False
    assert no_wallet["reason"] == "permit_verify_context_missing"
    assert no_wallet["details"] == {"missing": ["wallet_id"]}

    # The complete question still answers normally...
    complete = await verify(
        {
            "wallet_id": agent["agent_wallet_id"],
            "tool": TOOL,
            "estimated_credits": 1,
        }
    )
    assert complete["valid"] is True

    # ...and a real denial is never rewritten as missing context: this permit
    # exists and is bound correctly, the tool is simply not in scope.
    out_of_scope = await verify(
        {
            "wallet_id": agent["agent_wallet_id"],
            "tool": OTHER_TOOL,
            "estimated_credits": 1,
        }
    )
    assert out_of_scope["valid"] is False
    assert out_of_scope["reason"] == "permit_tool_not_allowed"


@pytest.mark.asyncio
async def test_verify_never_admits_an_action_it_was_not_told(client, clean_database):
    """An empty allowlist must not turn an incomplete request into `valid: true`.

    A permit may name no tools at all, and its scopes may include the scope an
    empty tool name generates (``tool::invoke``). Asked without ``tool``, the
    allowlist check has nothing to reject and the generated scope matches, so a
    verdict derived from the empty string would admit a call the caller never
    described. Missing context is decided before any of that.
    """
    agent = await provision_agent_wallet(client)
    permit = await client.post(
        "/v1/permits",
        json={
            "issuer_wallet_id": agent["agent_wallet_id"],
            "subject_wallet_id": agent["agent_wallet_id"],
            "subject_key_id": agent["key_id"],
            "allowed_tools": [],
            "scopes": ["tool::invoke", "billing:charge"],
            "max_credits": 50,
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=30)
            ).isoformat(),
        },
        headers={**BOOTSTRAP_HEADERS, "Idempotency-Key": "permit-empty-allowlist"},
    )
    assert permit.status_code == 201

    resp = await client.post(
        "/v1/permits/verify",
        json={"permit_id": permit.json()["permit_id"]},
        headers=agent["agent_headers"],
    )
    assert resp.status_code == 200
    assert resp.json()["valid"] is False
    assert resp.json()["reason"] == "permit_verify_context_missing"
    assert resp.json()["details"] == {"missing": ["wallet_id", "tool"]}


@pytest.mark.asyncio
async def test_verify_still_reports_permit_not_found_without_context(
    client, clean_database
):
    """A missing permit is reported as missing, whatever the request omitted."""
    agent = await provision_agent_wallet(client)
    resp = await client.post(
        "/v1/permits/verify",
        json={"permit_id": f"permit-{uuid.uuid4().hex[:16]}"},
        headers=agent["agent_headers"],
    )
    assert resp.status_code == 200
    assert resp.json()["valid"] is False
    assert resp.json()["reason"] == "permit_not_found"


@pytest.mark.asyncio
async def test_jsonrpc_denial_carries_details_alongside_the_receipt(
    client, clean_database, registered_tool
):
    agent = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=agent["agent_wallet_id"],
        key_id=agent["key_id"],
        tool_name=TOOL,
        max_credits=1,
        idem_key="permit-detail-jsonrpc",
    )

    resp = await client.post(
        "/mcp/messages",
        json={
            "jsonrpc": "2.0",
            "id": "req-1",
            "method": "tools/call",
            "params": {
                "name": TOOL,
                "arguments": {"message": "hi"},
                "mcpContext": {
                    "wallet_id": agent["agent_wallet_id"],
                    "permit_id": permit["permit_id"],
                    "idempotency_key": "inv-jsonrpc-1",
                },
            },
        },
        headers=agent["agent_headers"],
    )
    error = resp.json()["error"]
    assert error["code"] == -32003
    assert error["message"] == "permit_budget_exceeded"
    assert Decimal(error["data"]["details"]["required_credits"]) == Decimal("5.0")
    # The denial receipt is still there; details sit beside it, not instead.
    assert error["data"]["receipt"]["outcome"] == "denied"
    assert error["data"]["receipt"]["reason_code"] == "permit_budget_exceeded"


# --- P4: wallet-scoped trust reads ------------------------------------------


@pytest.mark.asyncio
async def test_wallet_key_lists_its_own_permits_without_an_admin_key(
    client, clean_database, registered_tool
):
    agent = await provision_agent_wallet(client)
    stranger = await provision_agent_wallet(client)
    mine = await create_tool_permit(
        client,
        wallet_id=agent["agent_wallet_id"],
        key_id=agent["key_id"],
        tool_name=TOOL,
        idem_key="permit-scope-mine",
    )
    await create_tool_permit(
        client,
        wallet_id=stranger["agent_wallet_id"],
        key_id=stranger["key_id"],
        tool_name=TOOL,
        idem_key="permit-scope-theirs",
    )

    listed = await client.get("/v1/permits", headers=agent["agent_headers"])
    assert listed.status_code == 200
    body = listed.json()
    assert [p["permit_id"] for p in body["permits"]] == [mine["permit_id"]]
    assert body["total"] == 1

    # Naming someone else's wallet is still refused.
    assert (
        await client.get(
            f"/v1/permits?wallet_id={stranger['agent_wallet_id']}",
            headers=agent["agent_headers"],
        )
    ).status_code == 403

    # The operator view still spans every wallet.
    admin = await client.get("/v1/permits", headers=BOOTSTRAP_HEADERS)
    assert admin.json()["total"] == 2


@pytest.mark.asyncio
async def test_wallet_key_reads_its_own_audit_events_and_summary(
    client, clean_database, registered_tool
):
    agent = await provision_agent_wallet(client)
    stranger = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=agent["agent_wallet_id"],
        key_id=agent["key_id"],
        tool_name=TOOL,
        idem_key="permit-audit-scope",
    )
    await _invoke(
        client,
        agent["agent_headers"],
        wallet_id=agent["agent_wallet_id"],
        permit_id=permit["permit_id"],
    )

    events = await client.get("/v1/audit/events", headers=agent["agent_headers"])
    assert events.status_code == 200
    wallets = {e["wallet_id"] for e in events.json()["events"]}
    assert wallets == {agent["agent_wallet_id"]}

    summary = await client.get("/v1/audit/summary", headers=agent["agent_headers"])
    assert summary.status_code == 200
    assert set(summary.json()["by_wallet"]) <= {agent["agent_wallet_id"]}

    # A different wallet sees its own (empty) view, never this one's.
    theirs = await client.get("/v1/audit/summary", headers=stranger["agent_headers"])
    assert agent["agent_wallet_id"] not in theirs.json()["by_wallet"]

    # Summarized events are scoped the same way.
    scoped_summary = await client.get(
        "/v1/audit/events?summary=true", headers=agent["agent_headers"]
    )
    assert scoped_summary.status_code == 200


@pytest.mark.asyncio
async def test_wallet_key_verifies_its_own_chain(
    client, clean_database, registered_tool
):
    agent = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=agent["agent_wallet_id"],
        key_id=agent["key_id"],
        tool_name=TOOL,
        idem_key="permit-chain-scope",
    )
    await _invoke(
        client,
        agent["agent_headers"],
        wallet_id=agent["agent_wallet_id"],
        permit_id=permit["permit_id"],
    )

    verified = await client.post(
        "/v1/audit/verify-chain", json={}, headers=agent["agent_headers"]
    )
    assert verified.status_code == 200
    assert verified.json()["valid"] is True

    # Naming another wallet is still refused.
    stranger = await provision_agent_wallet(client)
    assert (
        await client.post(
            "/v1/audit/verify-chain",
            json={"wallet_id": stranger["agent_wallet_id"]},
            headers=agent["agent_headers"],
        )
    ).status_code == 403


@pytest.mark.asyncio
async def test_wallet_key_sees_only_its_own_refund_items_and_cannot_retry(
    client, clean_database, registered_tool
):
    agent = await provision_agent_wallet(client)

    listed = await client.get(
        "/v1/receipts/reconciliation/refunds", headers=agent["agent_headers"]
    )
    assert listed.status_code == 200
    assert all(
        item["wallet_id"] == agent["agent_wallet_id"] for item in listed.json()["items"]
    )

    # The operator queue is still reachable with an admin key.
    assert (
        await client.get(
            "/v1/receipts/reconciliation/refunds", headers=BOOTSTRAP_HEADERS
        )
    ).status_code == 200

    # Retry moves money, so it stays operator-only.
    retried = await client.post(
        "/v1/receipts/reconciliation/refunds/rcpt-nope/retry",
        headers=agent["agent_headers"],
    )
    assert retried.status_code == 403
