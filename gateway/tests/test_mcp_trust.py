from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.config import get_settings
from app.db.database import get_session_factory
from app.db.models import (
    IdempotencyRecordModel,
    LedgerEntryModel,
    ReceiptModel,
    WalletModel,
)
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.idempotency import (
    GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
    IdempotencyInProgressError,
    get_idempotency_service,
)
from app.services.permits import get_permit_service
from app.services.receipts import ReceiptService
from app.services.service_registry import get_service_registry
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
async def test_governed_mcp_invoke_returns_receipt_and_replay_does_not_double_charge(
    client,
    clean_database,
):
    provisioned = await provision_agent_wallet(client)
    registry = get_service_registry()

    def trust_echo(message: str = "ok") -> dict:
        return {"message": message}

    registry.register_local(
        service_id="trust-echo",
        name="Trust Echo",
        description="Governed trust test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=trust_echo,
        credits_per_unit=2.0,
        unit_name="call",
    )
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name="trust-echo",
        )
        body = {
            "jsonrpc": "2.0",
            "id": "trust-call-1",
            "method": "tools/call",
            "params": {
                "name": "trust-echo",
                "arguments": {"message": "hello"},
                "mcpContext": {
                    "wallet_id": provisioned["agent_wallet_id"],
                    "permit_id": permit["permit_id"],
                    "idempotency_key": "trust-invoke-1",
                },
            },
        }
        first = await client.post(
            "/mcp/messages",
            json=body,
            headers=provisioned["agent_headers"],
        )
        assert first.status_code == 200
        first_payload = first.json()
        receipt = first_payload["result"]["receipt"]
        assert receipt["permit_id"] == permit["permit_id"]
        assert receipt["ledger_entry_id"]
        assert receipt["outcome"] == "success"

        replay = await client.post(
            "/mcp/messages",
            json=body,
            headers=provisioned["agent_headers"],
        )
        assert replay.status_code == 200
        assert replay.json()["result"]["receipt"]["receipt_id"] == receipt["receipt_id"]

        ledger_resp = await client.get(
            f"/v1/billing/ledger/{provisioned['agent_wallet_id']}",
            headers=provisioned["agent_headers"],
        )
        debits = [
            entry
            for entry in ledger_resp.json()["entries"]
            if entry["service_category"] == "agent_comms"
            and "trust-echo" in entry["description"]
        ]
        assert len(debits) == 1
        stored_permit = await get_permit_service().get_permit(permit["permit_id"])
        assert stored_permit is not None
        assert stored_permit.spent_credits == 2
    finally:
        registry.unregister_local("trust-echo")


@pytest.mark.anyio
async def test_frozen_wallet_denial_returns_receipt_and_replays_without_charge(
    client,
    clean_database,
):
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    registry = get_service_registry()
    calls = {"count": 0}

    def frozen_guard_tool() -> dict:
        calls["count"] += 1
        return {"ran": True}

    registry.register_local(
        service_id="frozen-guard-tool",
        name="Frozen Guard Tool",
        description="Must not execute for a frozen wallet",
        category=ServiceCategory.AGENT_COMMS,
        func=frozen_guard_tool,
        credits_per_unit=2.0,
        unit_name="call",
    )
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=wallet_id,
            key_id=provisioned["key_id"],
            tool_name="frozen-guard-tool",
            idem_key="frozen-guard-permit-create-1",
        )

        factory = get_session_factory()
        async with factory() as session:
            wallet = (
                await session.execute(
                    select(WalletModel).where(WalletModel.wallet_id == wallet_id)
                )
            ).scalar_one()
            balance_before = wallet.balance
            lifetime_debits_before = wallet.lifetime_debits
            hourly_spent_before = wallet.hourly_spent
            daily_spent_before = wallet.daily_spent
            wallet.status = "frozen"
            await session.commit()

        body = {
            "jsonrpc": "2.0",
            "id": "frozen-guard-call-1",
            "method": "tools/call",
            "params": {
                "name": "frozen-guard-tool",
                "arguments": {},
                "mcpContext": {
                    "wallet_id": wallet_id,
                    "permit_id": permit["permit_id"],
                    "idempotency_key": "frozen-guard-invoke-1",
                },
            },
        }
        first = await client.post(
            "/mcp/messages",
            json=body,
            headers=provisioned["agent_headers"],
        )
        replay = await client.post(
            "/mcp/messages",
            json=body,
            headers=provisioned["agent_headers"],
        )

        assert first.status_code == 200
        first_error = first.json()["error"]
        assert first_error["code"] == -32003
        assert first_error["message"] == "wallet_frozen"
        receipt = first_error["data"]["receipt"]
        assert receipt["outcome"] == "denied"
        assert receipt["ledger_entry_id"] is None
        assert receipt["credits_charged"] == "0"

        assert replay.status_code == 200
        assert replay.json()["error"] == first_error
        assert calls["count"] == 0

        stored_permit = await get_permit_service().get_permit(permit["permit_id"])
        assert stored_permit is not None
        assert stored_permit.spent_credits == 0

        valid, reason, _ = await ReceiptService().verify_receipt(receipt["receipt_id"])
        assert (valid, reason) == (True, None)

        async with factory() as session:
            wallet = (
                await session.execute(
                    select(WalletModel).where(WalletModel.wallet_id == wallet_id)
                )
            ).scalar_one()
            assert wallet.balance == balance_before
            assert wallet.lifetime_debits == lifetime_debits_before
            assert wallet.hourly_spent == hourly_spent_before
            assert wallet.daily_spent == daily_spent_before
    finally:
        registry.unregister_local("frozen-guard-tool")


@pytest.mark.anyio
async def test_velocity_auto_freeze_returns_receipted_denial_and_releases_budget(
    client,
    clean_database,
):
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    registry = get_service_registry()
    calls = {"count": 0}

    def auto_freeze_guard_tool() -> dict:
        calls["count"] += 1
        return {"ran": True}

    registry.register_local(
        service_id="auto-freeze-guard-tool",
        name="Auto Freeze Guard Tool",
        description="Must not execute when the charge auto-freezes its wallet",
        category=ServiceCategory.AGENT_COMMS,
        func=auto_freeze_guard_tool,
        credits_per_unit=2.0,
        unit_name="call",
    )
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=wallet_id,
            key_id=provisioned["key_id"],
            tool_name="auto-freeze-guard-tool",
            idem_key="auto-freeze-guard-permit-create-1",
        )

        factory = get_session_factory()
        async with factory() as session:
            wallet = await session.get(WalletModel, wallet_id)
            assert wallet is not None
            assert wallet.status == "active"
            balance_before = wallet.balance
            lifetime_debits_before = wallet.lifetime_debits
            hourly_spent_before = Decimal("2")
            daily_spent_before = wallet.daily_spent
            wallet.hourly_limit = Decimal("1")
            wallet.hourly_spent = hourly_spent_before
            wallet.velocity_alerts_triggered = get_settings().VELOCITY_FREEZE_THRESHOLD
            await session.commit()

        body = {
            "jsonrpc": "2.0",
            "id": "auto-freeze-guard-call-1",
            "method": "tools/call",
            "params": {
                "name": "auto-freeze-guard-tool",
                "arguments": {},
                "mcpContext": {
                    "wallet_id": wallet_id,
                    "permit_id": permit["permit_id"],
                    "idempotency_key": "auto-freeze-guard-invoke-1",
                },
            },
        }
        first = await client.post(
            "/mcp/messages",
            json=body,
            headers=provisioned["agent_headers"],
        )
        replay = await client.post(
            "/mcp/messages",
            json=body,
            headers=provisioned["agent_headers"],
        )

        assert first.status_code == 200
        first_error = first.json()["error"]
        assert first_error["code"] == -32003
        assert first_error["message"] == "wallet_frozen"
        receipt = first_error["data"]["receipt"]
        assert receipt["outcome"] == "denied"
        assert receipt["ledger_entry_id"] is None
        assert receipt["credits_charged"] == "0"
        assert replay.status_code == 200
        assert replay.json()["error"] == first_error
        assert calls["count"] == 0

        stored_permit = await get_permit_service().get_permit(permit["permit_id"])
        assert stored_permit is not None
        assert stored_permit.spent_credits == 0

        valid, reason, _ = await ReceiptService().verify_receipt(receipt["receipt_id"])
        assert (valid, reason) == (True, None)

        async with factory() as session:
            wallet = await session.get(WalletModel, wallet_id)
            assert wallet is not None
            assert wallet.status == "frozen"
            assert wallet.balance == balance_before
            assert wallet.lifetime_debits == lifetime_debits_before
            assert wallet.hourly_spent == hourly_spent_before
            assert wallet.daily_spent == daily_spent_before
    finally:
        registry.unregister_local("auto-freeze-guard-tool")


@pytest.mark.anyio
async def test_http_invoke_tool_governed_flow_returns_receipt(client, clean_database):
    provisioned = await provision_agent_wallet(client)
    registry = get_service_registry()
    registry.register_local(
        service_id="http-invoke-tool",
        name="HTTP Invoke Tool",
        description="Governed HTTP invoke test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=lambda message="ok": {"message": message},
        credits_per_unit=2.0,
        unit_name="call",
    )
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name="http-invoke-tool",
        )
        resp = await client.post(
            "/mcp/tools/http-invoke-tool/invoke",
            json={
                "name": "http-invoke-tool",
                "arguments": {"message": "hi"},
                "mcp_context": {
                    "wallet_id": provisioned["agent_wallet_id"],
                    "permit_id": permit["permit_id"],
                    "idempotency_key": "http-invoke-1",
                },
            },
            headers=provisioned["agent_headers"],
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["isError"] is False
        assert body["receipt"]["permit_id"] == permit["permit_id"]
        assert body["receipt"]["outcome"] == "success"

        # Out-of-scope tool over the same HTTP route is denied with 403.
        registry.register_local(
            service_id="http-blocked-tool",
            name="HTTP Blocked Tool",
            description="Outside the permit",
            category=ServiceCategory.AGENT_COMMS,
            func=lambda: {"ran": True},
            credits_per_unit=2.0,
            unit_name="call",
        )
        denied = await client.post(
            "/mcp/tools/http-blocked-tool/invoke",
            json={
                "name": "http-blocked-tool",
                "arguments": {},
                "mcp_context": {
                    "wallet_id": provisioned["agent_wallet_id"],
                    "permit_id": permit["permit_id"],
                    "idempotency_key": "http-denied-1",
                },
            },
            headers=provisioned["agent_headers"],
        )
        assert denied.status_code == 403
        assert denied.json()["detail"]["error"] == "permit_tool_not_allowed"
    finally:
        registry.unregister_local("http-invoke-tool")
        registry.unregister_local("http-blocked-tool")


@pytest.mark.anyio
async def test_governed_mcp_requires_idempotency_key(client, clean_database):
    provisioned = await provision_agent_wallet(client)
    registry = get_service_registry()
    registry.register_local(
        service_id="missing-idem-tool",
        name="Missing Idempotency Tool",
        description="Governed trust test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=lambda: {"ok": True},
        credits_per_unit=2.0,
        unit_name="call",
    )
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="missing-idem-tool",
    )
    try:
        resp = await client.post(
            "/mcp/messages",
            json={
                "jsonrpc": "2.0",
                "id": "trust-call-2",
                "method": "tools/call",
                "params": {
                    "name": "missing-idem-tool",
                    "arguments": {},
                    "mcpContext": {
                        "wallet_id": provisioned["agent_wallet_id"],
                        "permit_id": permit["permit_id"],
                    },
                },
            },
            headers=provisioned["agent_headers"],
        )
    finally:
        registry.unregister_local("missing-idem-tool")
    assert resp.status_code == 200
    assert resp.json()["error"]["message"] == "idempotency_key_required"


@pytest.mark.anyio
async def test_governed_mcp_rejects_in_progress_idempotency_without_charge(
    client,
    clean_database,
):
    provisioned = await provision_agent_wallet(client)
    calls = {"count": 0}
    registry = get_service_registry()

    def in_progress_tool() -> dict:
        calls["count"] += 1
        return {"ok": True}

    registry.register_local(
        service_id="in-progress-idem-tool",
        name="In Progress Idempotency Tool",
        description="Governed idempotency in-progress test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=in_progress_tool,
        credits_per_unit=2.0,
        unit_name="call",
    )
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="in-progress-idem-tool",
        idem_key="in-progress-mcp-permit-create-1",
    )
    body = {
        "jsonrpc": "2.0",
        "id": "in-progress-idem-call",
        "method": "tools/call",
        "params": {
            "name": "in-progress-idem-tool",
            "arguments": {},
            "mcpContext": {
                "wallet_id": provisioned["agent_wallet_id"],
                "permit_id": permit["permit_id"],
                "idempotency_key": "in-progress-mcp-key",
            },
        },
    }
    await get_idempotency_service().begin(
        wallet_id=provisioned["agent_wallet_id"],
        endpoint=GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
        idempotency_key="in-progress-mcp-key",
        request_payload={
            "tool_name": "in-progress-idem-tool",
            "arguments": {},
            "wallet_id": provisioned["agent_wallet_id"],
            "permit_id": permit["permit_id"],
        },
    )
    try:
        resp = await client.post(
            "/mcp/messages",
            json=body,
            headers=provisioned["agent_headers"],
        )
    finally:
        registry.unregister_local("in-progress-idem-tool")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["error"]["code"] == -32005
    assert payload["error"]["message"] == "idempotency_in_progress"
    assert calls["count"] == 0
    ledger_resp = await client.get(
        f"/v1/billing/ledger/{provisioned['agent_wallet_id']}",
        headers=provisioned["agent_headers"],
    )
    debits = [
        entry
        for entry in ledger_resp.json()["entries"]
        if entry["service_category"] == "agent_comms"
        and "in-progress-idem-tool" in entry["description"]
    ]
    assert debits == []


@pytest.mark.anyio
async def test_out_of_scope_governed_mcp_denial_returns_receipt(
    client,
    clean_database,
):
    provisioned = await provision_agent_wallet(client)
    registry = get_service_registry()
    registry.register_local(
        service_id="blocked-trust-tool",
        name="Blocked Trust Tool",
        description="Governed trust denial test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=lambda: {"ok": True},
        credits_per_unit=2.0,
        unit_name="call",
    )
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="allowed-trust-tool",
        idem_key="permit-denial-create-1",
    )
    try:
        resp = await client.post(
            "/mcp/messages",
            json={
                "jsonrpc": "2.0",
                "id": "trust-denial-1",
                "method": "tools/call",
                "params": {
                    "name": "blocked-trust-tool",
                    "arguments": {},
                    "mcpContext": {
                        "wallet_id": provisioned["agent_wallet_id"],
                        "permit_id": permit["permit_id"],
                        "idempotency_key": "trust-denial-invoke-1",
                    },
                },
            },
            headers=provisioned["agent_headers"],
        )

        replay = await client.post(
            "/mcp/messages",
            json={
                "jsonrpc": "2.0",
                "id": "trust-denial-1",
                "method": "tools/call",
                "params": {
                    "name": "blocked-trust-tool",
                    "arguments": {},
                    "mcpContext": {
                        "wallet_id": provisioned["agent_wallet_id"],
                        "permit_id": permit["permit_id"],
                        "idempotency_key": "trust-denial-invoke-1",
                    },
                },
            },
            headers=provisioned["agent_headers"],
        )
    finally:
        registry.unregister_local("blocked-trust-tool")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["error"]["message"] == "permit_tool_not_allowed"
    receipt = payload["error"]["data"]["receipt"]
    assert receipt["permit_id"] == permit["permit_id"]
    assert receipt["outcome"] == "denied"
    assert receipt["ledger_entry_id"] is None
    assert receipt["credits_charged"] == "0"

    assert replay.status_code == 200
    replay_payload = replay.json()
    assert "result" not in replay_payload
    assert replay_payload["error"]["message"] == "permit_tool_not_allowed"
    assert (
        replay_payload["error"]["data"]["receipt"]["receipt_id"]
        == receipt["receipt_id"]
    )


@pytest.mark.anyio
async def test_governed_policy_denial_returns_receipt(client, clean_database):
    provisioned = await provision_agent_wallet(client)
    registry = get_service_registry()
    registry.register_local(
        service_id="policy-denied-trust-tool",
        name="Policy Denied Trust Tool",
        description="Governed trust policy denial test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=lambda: {"ok": True},
        credits_per_unit=2.0,
        unit_name="call",
    )
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="policy-denied-trust-tool",
        idem_key="policy-denial-permit-create-1",
    )
    policy_resp = await client.post(
        "/v1/policies",
        json={
            "wallet_id": provisioned["agent_wallet_id"],
            "name": "Governed deny one tool",
            "allowed_tools": ["some-other-tool"],
        },
        headers={"X-API-Key": "test-key"},
    )
    assert policy_resp.status_code == 201
    body = {
        "jsonrpc": "2.0",
        "id": "policy-denial-trust-call",
        "method": "tools/call",
        "params": {
            "name": "policy-denied-trust-tool",
            "arguments": {},
            "mcpContext": {
                "wallet_id": provisioned["agent_wallet_id"],
                "permit_id": permit["permit_id"],
                "idempotency_key": "policy-denial-trust-invoke-1",
            },
        },
    }
    try:
        resp = await client.post(
            "/mcp/messages",
            json=body,
            headers=provisioned["agent_headers"],
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["error"]["message"] == "tool_not_allowed"
        receipt = payload["error"]["data"]["receipt"]
        assert receipt["outcome"] == "denied"
        assert receipt["credits_charged"] == "0"

        replay = await client.post(
            "/mcp/messages",
            json=body,
            headers=provisioned["agent_headers"],
        )
        assert (
            replay.json()["error"]["data"]["receipt"]["receipt_id"]
            == receipt["receipt_id"]
        )
        stored_permit = await get_permit_service().get_permit(permit["permit_id"])
        assert stored_permit is not None
        assert stored_permit.spent_credits == 0
    finally:
        registry.unregister_local("policy-denied-trust-tool")


@pytest.mark.anyio
async def test_governed_tool_failure_returns_refunded_receipt(client, clean_database):
    provisioned = await provision_agent_wallet(client)
    registry = get_service_registry()

    def failing_tool() -> dict:
        raise RuntimeError("tool exploded")

    registry.register_local(
        service_id="failing-trust-tool",
        name="Failing Trust Tool",
        description="Governed trust failure receipt test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=failing_tool,
        credits_per_unit=2.0,
        unit_name="call",
    )
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="failing-trust-tool",
        idem_key="failing-trust-permit-create-1",
    )
    try:
        resp = await client.post(
            "/mcp/messages",
            json={
                "jsonrpc": "2.0",
                "id": "failing-trust-call",
                "method": "tools/call",
                "params": {
                    "name": "failing-trust-tool",
                    "arguments": {},
                    "mcpContext": {
                        "wallet_id": provisioned["agent_wallet_id"],
                        "permit_id": permit["permit_id"],
                        "idempotency_key": "failing-trust-invoke-1",
                    },
                },
            },
            headers=provisioned["agent_headers"],
        )
    finally:
        registry.unregister_local("failing-trust-tool")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["error"]["message"] == "tool exploded"
    receipt = payload["error"]["data"]["receipt"]
    assert receipt["outcome"] == "failed_refunded"
    assert receipt["ledger_entry_id"]
    assert receipt["credits_charged"] == "0"
    stored_permit = await get_permit_service().get_permit(permit["permit_id"])
    assert stored_permit is not None
    assert stored_permit.spent_credits == 0


@pytest.mark.anyio
async def test_governed_refund_failure_keeps_permit_budget_reserved(
    client,
    clean_database,
):
    provisioned = await provision_agent_wallet(client)
    registry = get_service_registry()
    calls = {"count": 0}

    def failing_tool() -> dict:
        calls["count"] += 1
        raise RuntimeError("tool exploded")

    async def failing_refund(self, **_kwargs):
        raise RuntimeError("refund down")

    registry.register_local(
        service_id="unrefunded-trust-tool",
        name="Unrefunded Trust Tool",
        description="Governed refund-failure permit-accounting test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=failing_tool,
        credits_per_unit=2.0,
        unit_name="call",
    )
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="unrefunded-trust-tool",
        max_credits=2,
        idem_key="unrefunded-trust-permit-create-1",
    )

    def body(idempotency_key: str, request_id: str) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {
                "name": "unrefunded-trust-tool",
                "arguments": {},
                "mcpContext": {
                    "wallet_id": provisioned["agent_wallet_id"],
                    "permit_id": permit["permit_id"],
                    "idempotency_key": idempotency_key,
                },
            },
        }

    try:
        with patch(
            "app.services.agent_money.AgentMoney.refund_charge",
            failing_refund,
        ):
            first = await client.post(
                "/mcp/messages",
                json=body("unrefunded-trust-invoke-1", "unrefunded-call-1"),
                headers=provisioned["agent_headers"],
            )
            replay = await client.post(
                "/mcp/messages",
                json=body("unrefunded-trust-invoke-1", "unrefunded-call-1"),
                headers=provisioned["agent_headers"],
            )
            fresh_key = await client.post(
                "/mcp/messages",
                json=body("unrefunded-trust-invoke-2", "unrefunded-call-2"),
                headers=provisioned["agent_headers"],
            )
    finally:
        registry.unregister_local("unrefunded-trust-tool")

    assert first.status_code == 200
    assert first.json()["error"]["code"] == -32603
    # The reason names the tool's own error; the refund store's text stays
    # server-side (server log and audit event), as on the upstream path.
    assert first.json()["error"]["message"] == "refund_failed; tool_error:tool exploded"
    assert "refund down" not in first.text
    assert replay.json()["error"] == first.json()["error"]
    receipt = first.json()["error"]["data"]["receipt"]
    assert receipt["outcome"] == "failed_unrefunded"
    assert receipt["ledger_entry_id"]
    assert Decimal(receipt["credits_charged"]) == Decimal("2")
    assert first.json()["error"]["data"]["refund_reconciliation"]["status"] == (
        "pending"
    )

    # The failed compensation leaves a real wallet debit, so the matching
    # permit reservation must remain consumed. A fresh idempotency key cannot
    # bypass max_credits and execute or charge the tool again.
    stored_permit = await get_permit_service().get_permit(permit["permit_id"])
    assert stored_permit is not None
    assert stored_permit.spent_credits == 2
    assert fresh_key.status_code == 200
    assert fresh_key.json()["error"]["message"] == "permit_budget_exceeded"
    assert calls["count"] == 1

    wallet_resp = await client.get(
        f"/v1/billing/wallets/{provisioned['agent_wallet_id']}",
        headers=provisioned["agent_headers"],
    )
    assert wallet_resp.status_code == 200
    assert Decimal(wallet_resp.json()["balance_exact"]) == Decimal("998")

    ledger_resp = await client.get(
        f"/v1/billing/ledger/{provisioned['agent_wallet_id']}",
        headers=provisioned["agent_headers"],
    )
    tool_entries = [
        entry
        for entry in ledger_resp.json()["entries"]
        if "unrefunded-trust-tool" in entry.get("description", "")
    ]
    assert [entry["action"] for entry in tool_entries] == ["debit"]


async def _age_idempotency_record(wallet_id: str, idempotency_key: str) -> None:
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(IdempotencyRecordModel).where(
                IdempotencyRecordModel.wallet_id == wallet_id,
                IdempotencyRecordModel.endpoint == GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
                IdempotencyRecordModel.idempotency_key == idempotency_key,
            )
        )
        record = result.scalar_one()
        record.created_at = datetime.now(timezone.utc) - timedelta(hours=1)
        session.add(record)
        await session.commit()


@pytest.mark.anyio
async def test_governed_mcp_finalize_retries_on_transient_receipt_failure(
    client, clean_database
):
    """A transient failure writing the receipt must not turn a successful,
    already-charged tool call into an error -- the finalize sequence should
    retry and still return the real receipt."""
    provisioned = await provision_agent_wallet(client)
    registry = get_service_registry()

    def flaky_finalize_tool() -> dict:
        return {"ok": True}

    registry.register_local(
        service_id="flaky-finalize-tool",
        name="Flaky Finalize Tool",
        description="Governed trust finalize-retry test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=flaky_finalize_tool,
        credits_per_unit=2.0,
        unit_name="call",
    )
    call_count = {"n": 0}
    original_create_receipt = ReceiptService.create_receipt

    async def flaky_create_receipt(self, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] <= 2:
            raise RuntimeError("transient db hiccup")
        return await original_create_receipt(self, *args, **kwargs)

    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name="flaky-finalize-tool",
            idem_key="flaky-finalize-permit-create-1",
        )
        body = {
            "jsonrpc": "2.0",
            "id": "flaky-finalize-call",
            "method": "tools/call",
            "params": {
                "name": "flaky-finalize-tool",
                "arguments": {},
                "mcpContext": {
                    "wallet_id": provisioned["agent_wallet_id"],
                    "permit_id": permit["permit_id"],
                    "idempotency_key": "flaky-finalize-invoke-1",
                },
            },
        }
        with patch.object(ReceiptService, "create_receipt", flaky_create_receipt):
            resp = await client.post(
                "/mcp/messages", json=body, headers=provisioned["agent_headers"]
            )

        assert resp.status_code == 200
        payload = resp.json()
        receipt = payload["result"]["receipt"]
        assert receipt["outcome"] == "success"
        assert call_count["n"] == 3

        ledger_resp = await client.get(
            f"/v1/billing/ledger/{provisioned['agent_wallet_id']}",
            headers=provisioned["agent_headers"],
        )
        debits = [
            e
            for e in ledger_resp.json()["entries"]
            if e["service_category"] == "agent_comms"
        ]
        # Exactly one charge landed despite the two failed finalize attempts:
        # retries reuse the already-created receipt rather than recharging.
        assert len(debits) == 1
    finally:
        registry.unregister_local("flaky-finalize-tool")


@pytest.mark.anyio
async def test_reconcile_stuck_records_repairs_charged_but_unfinalized_record(
    client, clean_database
):
    """If finalize exhausts all retries after the receipt was already
    written (e.g. a crash right before idempotency-complete), the record is
    left charged-but-stuck. reconcile_stuck_records must repair it from the
    receipt so a later retry replays cleanly instead of hanging forever."""
    provisioned = await provision_agent_wallet(client)
    registry = get_service_registry()

    def crash_after_receipt_tool() -> dict:
        return {"ok": True}

    registry.register_local(
        service_id="crash-after-receipt-tool",
        name="Crash After Receipt Tool",
        description="Governed trust reconciliation test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=crash_after_receipt_tool,
        credits_per_unit=2.0,
        unit_name="call",
    )

    async def always_fail_complete(self, *args, **kwargs):
        raise RuntimeError("process died before idempotency-complete")

    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name="crash-after-receipt-tool",
            idem_key="crash-after-receipt-permit-create-1",
        )
        idempotency_key = "crash-after-receipt-invoke-1"
        body = {
            "jsonrpc": "2.0",
            "id": "crash-after-receipt-call",
            "method": "tools/call",
            "params": {
                "name": "crash-after-receipt-tool",
                "arguments": {},
                "mcpContext": {
                    "wallet_id": provisioned["agent_wallet_id"],
                    "permit_id": permit["permit_id"],
                    "idempotency_key": idempotency_key,
                },
            },
        }
        idem = get_idempotency_service()
        with patch.object(type(idem), "complete", always_fail_complete):
            resp = await client.post(
                "/mcp/messages", json=body, headers=provisioned["agent_headers"]
            )
        # All 3 finalize attempts failed at the complete() step -> surfaces as
        # a JSON-RPC error, not a silent success.
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == -32603

        await _age_idempotency_record(provisioned["agent_wallet_id"], idempotency_key)

        repaired, needs_review = await idem.reconcile_stuck_records(idle_seconds=900)
        assert repaired == 1
        assert needs_review == 0

        # The stuck record now replays instead of raising IdempotencyInProgressError.
        replay = await client.post(
            "/mcp/messages", json=body, headers=provisioned["agent_headers"]
        )
        assert replay.status_code == 200
        replay_payload = replay.json()
        assert "error" not in replay_payload or replay_payload.get("error") is None
        assert replay_payload["result"]["reconciled"] is True
        assert replay_payload["result"]["receipt_id"]

        # Exactly one charge landed for the whole scenario.
        ledger_resp = await client.get(
            f"/v1/billing/ledger/{provisioned['agent_wallet_id']}",
            headers=provisioned["agent_headers"],
        )
        debits = [
            e
            for e in ledger_resp.json()["entries"]
            if e["service_category"] == "agent_comms"
        ]
        assert len(debits) == 1
    finally:
        registry.unregister_local("crash-after-receipt-tool")


@pytest.mark.anyio
async def test_reconcile_stuck_records_flags_charge_with_no_receipt_for_review(
    client, clean_database
):
    """If the charge landed but the receipt itself was never created (a more
    severe crash), there is nothing safe to auto-repair -- the original tool
    response was never persisted. reconcile_stuck_records must leave it
    untouched and report it separately rather than fabricate a receipt."""
    provisioned = await provision_agent_wallet(client)
    registry = get_service_registry()

    def crash_before_receipt_tool() -> dict:
        return {"ok": True}

    registry.register_local(
        service_id="crash-before-receipt-tool",
        name="Crash Before Receipt Tool",
        description="Governed trust reconciliation test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=crash_before_receipt_tool,
        credits_per_unit=2.0,
        unit_name="call",
    )

    async def always_fail_create_receipt(self, *args, **kwargs):
        raise RuntimeError("process died before receipt could be written")

    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name="crash-before-receipt-tool",
            idem_key="crash-before-receipt-permit-create-1",
        )
        idempotency_key = "crash-before-receipt-invoke-1"
        body = {
            "jsonrpc": "2.0",
            "id": "crash-before-receipt-call",
            "method": "tools/call",
            "params": {
                "name": "crash-before-receipt-tool",
                "arguments": {},
                "mcpContext": {
                    "wallet_id": provisioned["agent_wallet_id"],
                    "permit_id": permit["permit_id"],
                    "idempotency_key": idempotency_key,
                },
            },
        }
        with patch.object(ReceiptService, "create_receipt", always_fail_create_receipt):
            resp = await client.post(
                "/mcp/messages", json=body, headers=provisioned["agent_headers"]
            )
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == -32603

        await _age_idempotency_record(provisioned["agent_wallet_id"], idempotency_key)

        idem = get_idempotency_service()
        repaired, needs_review = await idem.reconcile_stuck_records(idle_seconds=900)
        assert repaired == 0
        assert needs_review == 1

        # Still stuck: a retry with the same key correctly still fails closed
        # rather than silently fabricating success.
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.wallet_id == provisioned["agent_wallet_id"],
                    IdempotencyRecordModel.endpoint
                    == GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
                    IdempotencyRecordModel.idempotency_key == idempotency_key,
                )
            )
            record = result.scalar_one()
            assert record.response_json is None
        with pytest.raises(IdempotencyInProgressError):
            await idem.begin(
                wallet_id=provisioned["agent_wallet_id"],
                endpoint=GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
                idempotency_key=idempotency_key,
                request_payload={
                    "tool_name": "crash-before-receipt-tool",
                    "arguments": {},
                    "wallet_id": provisioned["agent_wallet_id"],
                    "permit_id": permit["permit_id"],
                },
            )
    finally:
        registry.unregister_local("crash-before-receipt-tool")


@pytest.mark.anyio
async def test_reconcile_stuck_records_preserves_failed_outcome(client, clean_database):
    """A crash can leave a stuck idempotency record whose matching receipt
    records a FAILURE (e.g. the tool raised, the charge was refunded, and
    _finalize_governed_denial crashed before idem.complete()). Reconciliation
    must replay that as the failure it was, not a fabricated 200 success."""
    provisioned = await provision_agent_wallet(client)
    registry = get_service_registry()

    def exploding_tool() -> dict:
        raise RuntimeError("tool exploded before finalize crash")

    registry.register_local(
        service_id="crash-in-denial-tool",
        name="Crash In Denial Finalize Tool",
        description="Governed trust reconciliation outcome-fidelity test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=exploding_tool,
        credits_per_unit=2.0,
        unit_name="call",
    )

    idem = get_idempotency_service()
    original_complete = type(idem).complete

    async def complete_only_fails_for_this_key(self, *, idempotency_key, **kwargs):
        if idempotency_key == "crash-in-denial-invoke-1":
            raise RuntimeError("process died before idempotency-complete")
        return await original_complete(self, idempotency_key=idempotency_key, **kwargs)

    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name="crash-in-denial-tool",
            idem_key="crash-in-denial-permit-create-1",
        )
        idempotency_key = "crash-in-denial-invoke-1"
        body = {
            "jsonrpc": "2.0",
            "id": "crash-in-denial-call",
            "method": "tools/call",
            "params": {
                "name": "crash-in-denial-tool",
                "arguments": {},
                "mcpContext": {
                    "wallet_id": provisioned["agent_wallet_id"],
                    "permit_id": permit["permit_id"],
                    "idempotency_key": idempotency_key,
                },
            },
        }
        with patch.object(type(idem), "complete", complete_only_fails_for_this_key):
            resp = await client.post(
                "/mcp/messages", json=body, headers=provisioned["agent_headers"]
            )
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == -32603

        await _age_idempotency_record(provisioned["agent_wallet_id"], idempotency_key)

        repaired, needs_review = await idem.reconcile_stuck_records(idle_seconds=900)
        assert repaired == 1
        assert needs_review == 0

        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.wallet_id == provisioned["agent_wallet_id"],
                    IdempotencyRecordModel.endpoint
                    == GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
                    IdempotencyRecordModel.idempotency_key == idempotency_key,
                )
            )
            record = result.scalar_one()
            import json as _json

            reconciled = _json.loads(record.response_json)
            # The bug: this used to always be a bare 200 success regardless
            # of what the receipt actually recorded.
            assert reconciled["outcome"] == "failed_refunded"
            assert reconciled["isError"] is True
            assert record.status_code == 500

        # The wallet was refunded -- no net charge from the whole scenario.
        ledger_resp = await client.get(
            f"/v1/billing/ledger/{provisioned['agent_wallet_id']}",
            headers=provisioned["agent_headers"],
        )
        entries = [
            e
            for e in ledger_resp.json()["entries"]
            if e["service_category"] == "agent_comms"
        ]
        net = sum(e["amount"] for e in entries)
        assert net == 0
    finally:
        registry.unregister_local("crash-in-denial-tool")


@pytest.mark.anyio
async def test_insufficient_funds_returns_receipt_and_replays_without_charge(
    client,
    clean_database,
):
    """A balance shortfall signs an `insufficient_funds` receipt at 402.

    The tool must never execute, nothing may be debited, the reserved permit
    budget must be released, and the replay must return the same envelope.
    This was the one terminal receipt outcome with no test asserting it.
    """

    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    registry = get_service_registry()
    calls = {"count": 0}

    def broke_guard_tool() -> dict:
        calls["count"] += 1
        return {"ran": True}

    registry.register_local(
        service_id="broke-guard-tool",
        name="Broke Guard Tool",
        description="Must not execute when the balance cannot cover the charge",
        category=ServiceCategory.AGENT_COMMS,
        func=broke_guard_tool,
        credits_per_unit=2.0,
        unit_name="call",
    )
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=wallet_id,
            key_id=provisioned["key_id"],
            tool_name="broke-guard-tool",
            idem_key="broke-guard-permit-create-1",
        )

        factory = get_session_factory()
        async with factory() as session:
            wallet = (
                await session.execute(
                    select(WalletModel).where(WalletModel.wallet_id == wallet_id)
                )
            ).scalar_one()
            wallet.balance = Decimal("1")
            balance_before = wallet.balance
            lifetime_debits_before = wallet.lifetime_debits
            await session.commit()

        body = {
            "jsonrpc": "2.0",
            "id": "broke-guard-call-1",
            "method": "tools/call",
            "params": {
                "name": "broke-guard-tool",
                "arguments": {},
                "mcpContext": {
                    "wallet_id": wallet_id,
                    "permit_id": permit["permit_id"],
                    "idempotency_key": "broke-guard-invoke-1",
                },
            },
        }
        first = await client.post(
            "/mcp/messages",
            json=body,
            headers=provisioned["agent_headers"],
        )
        replay = await client.post(
            "/mcp/messages",
            json=body,
            headers=provisioned["agent_headers"],
        )

        assert first.status_code == 200
        first_error = first.json()["error"]
        assert first_error["code"] == -32004
        assert first_error["message"] == "insufficient_funds"
        receipt = first_error["data"]["receipt"]
        assert receipt["outcome"] == "insufficient_funds"
        assert receipt["ledger_entry_id"] is None
        assert receipt["credits_charged"] == "0"

        assert replay.status_code == 200
        assert replay.json()["error"] == first_error
        assert calls["count"] == 0

        stored_permit = await get_permit_service().get_permit(permit["permit_id"])
        assert stored_permit is not None
        assert stored_permit.spent_credits == 0

        valid, reason, _ = await ReceiptService().verify_receipt(receipt["receipt_id"])
        assert (valid, reason) == (True, None)

        async with factory() as session:
            wallet = (
                await session.execute(
                    select(WalletModel).where(WalletModel.wallet_id == wallet_id)
                )
            ).scalar_one()
            assert wallet.balance == balance_before
            assert wallet.lifetime_debits == lifetime_debits_before
    finally:
        registry.unregister_local("broke-guard-tool")


async def _simulate_crash_before_ledger_link(
    wallet_id: str, idempotency_key: str, *, drop_receipt: bool
) -> str:
    """Reproduce the crash window between the committed debit and the link.

    A governed local call charges with ``operation_key=record_id`` and only
    afterwards writes ``ledger_entry_id`` onto the idempotency record. A crash
    in that gap leaves the debit committed and the record unlinked, which is
    the state this helper recreates from a call that actually completed.
    Returns the record id.
    """
    factory = get_session_factory()
    async with factory() as session:
        record = (
            await session.execute(
                select(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.wallet_id == wallet_id,
                    IdempotencyRecordModel.endpoint
                    == GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
                    IdempotencyRecordModel.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one()
        record_id = record.record_id
        # The debit stays committed and still carries operation_key=record_id.
        record.response_json = None
        record.response_reference = None
        record.ledger_entry_id = None
        record.created_at = datetime.now(timezone.utc) - timedelta(hours=1)
        session.add(record)
        if drop_receipt:
            for receipt in (
                (
                    await session.execute(
                        select(ReceiptModel).where(
                            ReceiptModel.idempotency_record_id == record_id
                        )
                    )
                )
                .scalars()
                .all()
            ):
                await session.delete(receipt)
        await session.commit()
    return record_id


async def _invoke_local_governed_tool(client, provisioned, *, suffix: str) -> str:
    """Drive one successful governed local tool call; returns its idem key.

    The caller is responsible for unregistering ``crash-before-link-tool-*``;
    leaving it registered leaks into unrelated tool-listing tests.
    """
    registry = get_service_registry()
    tool = f"crash-before-link-tool-{suffix}"

    def _tool() -> dict:
        return {"ok": True}

    registry.register_local(
        service_id=tool,
        name="Crash Before Link Tool",
        description="Governed trust reconciliation test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=_tool,
        credits_per_unit=2.0,
        unit_name="call",
    )
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name=tool,
        idem_key=f"crash-before-link-permit-{suffix}",
    )
    idempotency_key = f"crash-before-link-invoke-{suffix}"
    resp = await client.post(
        "/mcp/messages",
        json={
            "jsonrpc": "2.0",
            "id": f"crash-before-link-{suffix}",
            "method": "tools/call",
            "params": {
                "name": tool,
                "arguments": {},
                "mcpContext": {
                    "wallet_id": provisioned["agent_wallet_id"],
                    "permit_id": permit["permit_id"],
                    "idempotency_key": idempotency_key,
                },
            },
        },
        headers=provisioned["agent_headers"],
    )
    assert resp.status_code == 200
    return idempotency_key


@pytest.mark.anyio
async def test_reconcile_links_orphaned_local_debit_and_repairs_it(
    client, clean_database
):
    """A local governed crash before the ledger link must not be invisible.

    The unstarted pass is scoped to operation_kind="upstream_mcp" and the
    stuck pass requires ledger_entry_id IS NOT NULL, so a local record with a
    committed debit and no link matched neither: it stuck forever and
    needs_review stayed 0. The debit carries operation_key=record_id, so the
    reconciler can adopt it and repair from the receipt.
    """
    provisioned = await provision_agent_wallet(client)
    try:
        idempotency_key = await _invoke_local_governed_tool(
            client, provisioned, suffix="repair"
        )
        await _simulate_crash_before_ledger_link(
            provisioned["agent_wallet_id"], idempotency_key, drop_receipt=False
        )

        idem = get_idempotency_service()
        repaired, needs_review = await idem.reconcile_stuck_records(idle_seconds=900)
        assert (repaired, needs_review) == (1, 0)
    finally:
        get_service_registry().unregister_local("crash-before-link-tool-repair")


@pytest.mark.anyio
async def test_reconcile_flags_orphaned_local_debit_without_a_receipt(
    client, clean_database
):
    """The same crash window with no receipt yet must be surfaced, not lost.

    This is the case that most needs an operator: real money moved and there
    is no evidence row to rebuild the response from. It previously counted
    zero on both sides, so nothing ever revealed it.
    """
    provisioned = await provision_agent_wallet(client)
    try:
        idempotency_key = await _invoke_local_governed_tool(
            client, provisioned, suffix="review"
        )
        await _simulate_crash_before_ledger_link(
            provisioned["agent_wallet_id"], idempotency_key, drop_receipt=True
        )

        idem = get_idempotency_service()
        repaired, needs_review = await idem.reconcile_stuck_records(idle_seconds=900)
        assert (repaired, needs_review) == (0, 1)
    finally:
        get_service_registry().unregister_local("crash-before-link-tool-review")


@pytest.mark.anyio
async def test_reconcile_refuses_a_credit_as_compensation_proof(client, clean_database):
    """Only a debit proves the local charge happened.

    The linking pass adopts a ledger entry as evidence that money moved under
    this identity. A credit carrying the same operation key proves the
    opposite, so it must not be linked -- the record stays exactly as found
    and reconciliation reports nothing, the same as when no entry exists.
    """
    provisioned = await provision_agent_wallet(client)
    try:
        idempotency_key = await _invoke_local_governed_tool(
            client, provisioned, suffix="credit"
        )
        record_id = await _simulate_crash_before_ledger_link(
            provisioned["agent_wallet_id"], idempotency_key, drop_receipt=False
        )

        factory = get_session_factory()
        async with factory() as session:
            entry = (
                await session.execute(
                    select(LedgerEntryModel).where(
                        LedgerEntryModel.operation_key == record_id
                    )
                )
            ).scalar_one()
            # Same key, opposite direction: a refund-shaped row, not a charge.
            entry.amount = abs(entry.amount)
            entry.action = "credit"
            session.add(entry)
            await session.commit()

        idem = get_idempotency_service()
        repaired, needs_review = await idem.reconcile_stuck_records(idle_seconds=900)
        assert (repaired, needs_review) == (0, 0)

        async with factory() as session:
            record = await session.get(IdempotencyRecordModel, record_id)
            assert record is not None
            assert record.ledger_entry_id is None
            assert record.response_json is None
    finally:
        get_service_registry().unregister_local("crash-before-link-tool-credit")
