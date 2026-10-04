"""Negative-path security invariants for the governed invocation pipeline.

These cover the gaps not already exercised elsewhere:
- an expired permit is denied at invocation time (no charge),
- a permit whose budget is too small is denied (no charge),
- a receipt stripped of its signature fails verification.

(Wrong-wallet, wrong-tool, missing-idempotency-key, replay-with-changed-payload,
tampered-receipt, and tampered-audit cases live in test_mcp_trust*.py,
test_idempotency.py, test_receipts.py, and test_audit_chain.py.)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.time import utc_now
from app.db.database import get_session_factory
from app.db.models import PermitModel, ReceiptModel
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.policies import evaluate_wallet_policy as real_evaluate_wallet_policy
from app.services.receipts import get_receipt_service
from app.services.service_registry import get_service_registry
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _register_tool(tool_name: str, credits_per_unit: float = 2.0) -> None:
    get_service_registry().register_local(
        service_id=tool_name,
        name=tool_name,
        description="negative-security test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=lambda message="ok": {"message": message},
        credits_per_unit=credits_per_unit,
        unit_name="call",
    )


def _mcp_call(wallet_id: str, permit_id: str, tool_name: str, idem: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": idem,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": {"message": "hi"},
            "mcpContext": {
                "wallet_id": wallet_id,
                "permit_id": permit_id,
                "idempotency_key": idem,
            },
        },
    }


async def _ledger_debits(client, provisioned, tool_name) -> list:
    ledger = await client.get(
        f"/v1/billing/ledger/{provisioned['agent_wallet_id']}",
        headers=provisioned["agent_headers"],
    )
    return [
        entry
        for entry in ledger.json()["entries"]
        if entry["service_category"] == "agent_comms"
        and tool_name in entry.get("description", "")
    ]


@pytest.mark.anyio
async def test_expired_permit_is_denied_without_charge(client, clean_database):
    provisioned = await provision_agent_wallet(client)
    tool_name = "expired-permit-tool"
    _register_tool(tool_name)
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
        )

        # Force the permit into the past after creation (creation rejects
        # already-expired permits, so we expire a previously-valid one).
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(PermitModel).where(PermitModel.permit_id == permit["permit_id"])
            )
            model = result.scalar_one()
            model.expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
            session.add(model)
            await session.commit()

        resp = await client.post(
            "/mcp/messages",
            json=_mcp_call(
                provisioned["agent_wallet_id"],
                permit["permit_id"],
                tool_name,
                "expired-invoke-1",
            ),
            headers=provisioned["agent_headers"],
        )
        assert resp.status_code == 200
        error = resp.json()["error"]
        assert error["message"] == "permit_expired"
        assert await _ledger_debits(client, provisioned, tool_name) == []
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_revoked_permit_is_denied_without_charge(client, clean_database):
    provisioned = await provision_agent_wallet(client)
    tool_name = "revoked-permit-tool"
    _register_tool(tool_name)
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
        )
        revoke = await client.post(
            f"/v1/permits/{permit['permit_id']}/revoke",
            headers={"X-API-Key": "test-key"},
        )
        assert revoke.status_code == 200

        resp = await client.post(
            "/mcp/messages",
            json=_mcp_call(
                provisioned["agent_wallet_id"],
                permit["permit_id"],
                tool_name,
                "revoked-invoke-1",
            ),
            headers=provisioned["agent_headers"],
        )
        assert resp.status_code == 200
        error = resp.json()["error"]
        assert error["message"] == "permit_revoked"
        # A signed denial receipt is still issued, with no ledger charge.
        assert error["data"]["receipt"]["outcome"] == "denied"
        assert error["data"]["receipt"]["ledger_entry_id"] is None
        assert await _ledger_debits(client, provisioned, tool_name) == []
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("terminal_state", "expected_reason"),
    [
        ("revoked", "permit_revoked"),
        ("expired", "permit_expired"),
    ],
)
async def test_terminal_permit_between_precheck_and_reservation_is_denied(
    client,
    clean_database,
    terminal_state,
    expected_reason,
):
    """Revocation/expiry racing after the precheck cannot reach charging."""
    provisioned = await provision_agent_wallet(client)
    tool_name = f"racing-{terminal_state}-permit-tool"
    _register_tool(tool_name)
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
        )

        async def transition_permit_during_policy(**kwargs):
            factory = get_session_factory()
            async with factory() as session:
                model = await session.get(PermitModel, permit["permit_id"])
                assert model is not None
                if terminal_state == "revoked":
                    model.status = "revoked"
                    model.revoked_at = utc_now()
                else:
                    model.expires_at = utc_now() - timedelta(seconds=1)
                session.add(model)
                await session.commit()
            return await real_evaluate_wallet_policy(**kwargs)

        with patch(
            "app.routers.mcp.evaluate_wallet_policy",
            new=transition_permit_during_policy,
        ):
            resp = await client.post(
                "/mcp/messages",
                json=_mcp_call(
                    provisioned["agent_wallet_id"],
                    permit["permit_id"],
                    tool_name,
                    f"racing-{terminal_state}-invoke-1",
                ),
                headers=provisioned["agent_headers"],
            )

        assert resp.status_code == 200
        error = resp.json()["error"]
        assert error["message"] == expected_reason
        assert error["data"]["receipt"]["outcome"] == "denied"
        assert await _ledger_debits(client, provisioned, tool_name) == []

        factory = get_session_factory()
        async with factory() as session:
            model = await session.get(PermitModel, permit["permit_id"])
            assert model is not None
            assert model.spent_credits == 0
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_budget_exceeded_permit_is_denied_without_charge(client, clean_database):
    provisioned = await provision_agent_wallet(client)
    tool_name = "budget-tool"
    _register_tool(tool_name, credits_per_unit=2.0)
    try:
        # Tool costs 2 credits; cap the permit below that so the very first
        # call exceeds the bounded budget.
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            max_credits=1,
        )

        resp = await client.post(
            "/mcp/messages",
            json=_mcp_call(
                provisioned["agent_wallet_id"],
                permit["permit_id"],
                tool_name,
                "budget-invoke-1",
            ),
            headers=provisioned["agent_headers"],
        )
        assert resp.status_code == 200
        error = resp.json()["error"]
        assert error["message"] == "permit_budget_exceeded"
        assert await _ledger_debits(client, provisioned, tool_name) == []
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_replay_not_served_to_unauthorized_caller(client, clean_database):
    """A caller who does not own the wallet must never be served that wallet's
    stored receipt via the idempotency replay short-circuit.

    Regression: idem.begin() used to run on the body-supplied wallet_id before
    the ownership check, so replaying another wallet's (body, key) returned that
    wallet's signed receipt to an unauthorized caller.
    """
    victim = await provision_agent_wallet(client)
    attacker = await provision_agent_wallet(client)
    tool_name = "replay-authz-tool"
    _register_tool(tool_name)
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=victim["agent_wallet_id"],
            key_id=victim["key_id"],
            tool_name=tool_name,
        )
        body = _mcp_call(
            victim["agent_wallet_id"], permit["permit_id"], tool_name, "shared-idem-1"
        )

        # Victim performs the real governed invoke and obtains a receipt.
        r1 = await client.post(
            "/mcp/messages", json=body, headers=victim["agent_headers"]
        )
        assert r1.status_code == 200
        victim_receipt = r1.json()["result"]["receipt"]["receipt_id"]

        # Attacker replays the victim's exact body + idempotency key with its own
        # key. It must be denied, and must not receive the victim's receipt.
        r2 = await client.post(
            "/mcp/messages", json=body, headers=attacker["agent_headers"]
        )
        assert r2.status_code == 200
        payload = r2.json()
        assert payload.get("result") is None
        assert payload["error"]["message"] == "wallet_access_denied"
        assert victim_receipt not in r2.text
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_denied_call_does_not_poison_idempotency_key(client, clean_database):
    """A denied cross-tenant attempt must not lock the victim's (wallet, key).

    Regression: the wallet_access_denied branch created its idempotency record
    before authorizing, then raised without closing it, so the legitimate
    owner's later use of that key failed permanently.
    """
    victim = await provision_agent_wallet(client)
    attacker = await provision_agent_wallet(client)
    tool_name = "poison-authz-tool"
    _register_tool(tool_name)
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=victim["agent_wallet_id"],
            key_id=victim["key_id"],
            tool_name=tool_name,
        )
        # Identical body for both callers; only the auth header differs.
        body = _mcp_call(
            victim["agent_wallet_id"], permit["permit_id"], tool_name, "poison-key-1"
        )

        atk = await client.post(
            "/mcp/messages", json=body, headers=attacker["agent_headers"]
        )
        assert atk.json()["error"]["message"] == "wallet_access_denied"

        # The rightful owner reuses that same key for its own real call. With the
        # denial no longer planting a record, this must succeed rather than
        # returning idempotency_in_progress / idempotency_key_reused.
        vic = await client.post(
            "/mcp/messages", json=body, headers=victim["agent_headers"]
        )
        assert vic.status_code == 200
        assert "error" not in vic.json(), vic.json().get("error")
        assert vic.json()["result"]["receipt"]["receipt_id"]
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_unsigned_receipt_fails_verification(client, clean_database):
    provisioned = await provision_agent_wallet(client)
    tool_name = "unsigned-receipt-tool"
    _register_tool(tool_name)
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
        )
        resp = await client.post(
            "/mcp/messages",
            json=_mcp_call(
                provisioned["agent_wallet_id"],
                permit["permit_id"],
                tool_name,
                "unsigned-invoke-1",
            ),
            headers=provisioned["agent_headers"],
        )
        assert resp.status_code == 200
        receipt_id = resp.json()["result"]["receipt"]["receipt_id"]

        # Strip the signature from the stored receipt: an unsigned receipt
        # must not verify.
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(ReceiptModel).where(ReceiptModel.receipt_id == receipt_id)
            )
            model = result.scalar_one()
            model.signature = ""
            session.add(model)
            await session.commit()

        valid, reason, _ = await get_receipt_service().verify_receipt(receipt_id)
        assert valid is False
        assert reason == "receipt_signature_invalid"
    finally:
        get_service_registry().unregister_local(tool_name)
