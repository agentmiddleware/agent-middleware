"""Human approval gate (Sentinel) on the governed invoke path.

Covers the full contract:

- Simulated approvals auto-approve in local/dev, marked simulated, and are
  refused in production-like environments (fail closed at permit creation AND
  at the invoke gate).
- Real mode drives Sentinel: pending returns a retryable -32005 without
  consuming the idempotency key or charging; approval lets the SAME key
  proceed to a charged, receipted invoke; rejection and local expiry produce
  terminal denied receipts that replay.
- approval_id is carried on the signed receipt (conditionally signed, so
  tampering fails verification) and in the signed audit metadata.
- Permit signatures cover requires_human_approval only when set, so flipping
  the stored flag in either direction invalidates the permit.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import logging

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

import app.services.human_approval as human_approval_module
from app.core.config import get_settings
from app.db.database import get_session_factory
from app.db.models import HumanApprovalModel, PermitModel
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.human_approval import (
    HumanApprovalError,
    HumanApprovalService,
    HumanApprovalUnavailableError,
)
from app.services.idempotency import (
    GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
    get_idempotency_service,
)
from app.services.service_registry import get_service_registry
from tests.test_trust_helpers import (
    BOOTSTRAP_HEADERS,
    create_tool_permit,
    provision_agent_wallet,
)

TOOL = "approval-echo"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def registered_tool():
    registry = get_service_registry()
    executions = {"count": 0}

    def approval_echo(message: str = "ok") -> dict:
        executions["count"] += 1
        return {"message": message}

    registry.register_local(
        service_id=TOOL,
        name="Approval Echo",
        description="Governed human-approval test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=approval_echo,
        credits_per_unit=2.0,
        unit_name="call",
    )
    yield executions
    registry.unregister_local(TOOL)


@pytest.fixture
def fresh_service(monkeypatch):
    """Give each test an isolated HumanApprovalService instance."""
    service = HumanApprovalService()
    monkeypatch.setattr(human_approval_module, "_service", service)
    return service


def _sentinel_env(monkeypatch, *, simulated: bool, configured: bool = True):
    settings = get_settings()
    monkeypatch.setattr(settings, "SIMULATION_MODE_HUMAN_APPROVAL", simulated)
    monkeypatch.setattr(
        settings, "SENTINEL_API_URL", "https://sentinel.test" if configured else ""
    )
    monkeypatch.setattr(
        settings, "SENTINEL_API_KEY", "sk_test_" + "0" * 64 if configured else ""
    )
    monkeypatch.setattr(settings, "SENTINEL_WAIT_SECONDS", 0.0)
    return settings


class FakeSentinel:
    """Stands in for SentinelClient; decisions are scripted per test."""

    def __init__(self, status: str = "pending") -> None:
        self.status = status
        self.created: list[dict] = []
        self.polls = 0
        self.fail_with: Exception | None = None

    async def create_approval(self, **kwargs):
        if self.fail_with is not None:
            raise self.fail_with
        self.created.append(kwargs)
        return {"action_id": f"act_{uuid.uuid4().hex[:16]}", "status": "pending"}

    async def get_approval(self, action_id: str):
        if self.fail_with is not None:
            raise self.fail_with
        self.polls += 1
        payload = {"action_id": action_id, "status": self.status}
        if self.status in {"approved", "rejected"}:
            payload["decided_by"] = "reviewer@example.com"
            payload["reason"] = f"scripted {self.status}"
        return payload

    async def wait_approval(self, action_id: str, timeout: float):
        return await self.get_approval(action_id)


class DedupSentinel:
    """Sentinel stand-in that deduplicates creates by provider key."""

    def __init__(self) -> None:
        self.by_key: dict[str, str] = {}
        self.creates: list[tuple[str, dict]] = []

    async def create_approval(self, **kwargs):
        key = kwargs["idempotency_key"]
        self.creates.append((key, kwargs["arguments"]))
        action = self.by_key.setdefault(key, f"act_{len(self.by_key)}")
        return {"action_id": action, "status": "pending"}

    async def get_approval(self, action_id):
        return {"action_id": action_id, "status": "pending"}


def _invoke_body(provisioned, permit, idem_key: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": f"call-{idem_key}",
        "method": "tools/call",
        "params": {
            "name": TOOL,
            "arguments": {"message": "hello"},
            "mcpContext": {
                "wallet_id": provisioned["agent_wallet_id"],
                "permit_id": permit["permit_id"],
                "idempotency_key": idem_key,
            },
        },
    }


async def _approval_permit(client, provisioned, *, idem_key: str) -> dict:
    resp = await client.post(
        "/v1/permits",
        json={
            "issuer_wallet_id": provisioned["agent_wallet_id"],
            "subject_wallet_id": provisioned["agent_wallet_id"],
            "subject_key_id": provisioned["key_id"],
            "allowed_tools": [TOOL],
            "scopes": [f"tool:{TOOL}:invoke", "billing:charge"],
            "max_credits": 50,
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=30)
            ).isoformat(),
            "requires_human_approval": True,
        },
        headers={**BOOTSTRAP_HEADERS, "Idempotency-Key": idem_key},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _ledger_debits(client, wallet_id: str) -> int:
    resp = await client.get(
        f"/v1/billing/ledger/{wallet_id}", headers=BOOTSTRAP_HEADERS
    )
    assert resp.status_code == 200
    return sum(1 for entry in resp.json()["entries"] if entry["action"] == "debit")


# ---------------------------------------------------------------------------
# Simulation mode
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_simulated_approval_auto_approves_and_is_marked(
    client, clean_database, registered_tool, fresh_service, monkeypatch
):
    _sentinel_env(monkeypatch, simulated=True, configured=False)

    provisioned = await provision_agent_wallet(client)
    permit = await _approval_permit(client, provisioned, idem_key="sim-permit-1")
    assert permit["requires_human_approval"] is True

    resp = await client.post(
        "/mcp/messages",
        json=_invoke_body(provisioned, permit, "sim-invoke-1"),
        headers=provisioned["agent_headers"],
    )
    assert resp.status_code == 200
    payload = resp.json()
    receipt = payload["result"]["receipt"]
    assert receipt["outcome"] == "success"
    assert receipt["approval_id"], "success receipt must carry the approval id"

    # The stored approval row is explicitly marked simulated.
    factory = get_session_factory()
    async with factory() as session:
        row = (
            await session.execute(
                select(HumanApprovalModel).where(
                    HumanApprovalModel.approval_id == receipt["approval_id"]
                )
            )
        ).scalar_one()
        assert row.simulated is True
        # Single-use: the approval is consumed once it authorizes the invoke.
        assert row.status == "consumed"
        assert row.decided_by == "simulation"

    # Audit metadata records the approval and its simulated nature.
    events = await client.get(
        "/v1/audit/events",
        params={"wallet_id": provisioned["agent_wallet_id"], "event": "mcp.invoke"},
        headers=BOOTSTRAP_HEADERS,
    )
    assert events.status_code == 200
    matching = [
        e
        for e in events.json()["events"]
        if e["metadata"].get("approval_id") == receipt["approval_id"]
    ]
    assert matching, "audit event must link the approval"
    assert matching[0]["metadata"]["approval_simulated"] is True


@pytest.mark.anyio
async def test_simulated_mode_fails_closed_in_production_like_env(
    client, clean_database, registered_tool, fresh_service, monkeypatch
):
    settings = _sentinel_env(monkeypatch, simulated=True, configured=False)

    provisioned = await provision_agent_wallet(client)

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    resp = await client.post(
        "/v1/permits",
        json={
            "issuer_wallet_id": provisioned["agent_wallet_id"],
            "subject_wallet_id": provisioned["agent_wallet_id"],
            "subject_key_id": provisioned["key_id"],
            "allowed_tools": [TOOL],
            "scopes": [f"tool:{TOOL}:invoke", "billing:charge"],
            "max_credits": 50,
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=30)
            ).isoformat(),
            "requires_human_approval": True,
        },
        headers={**BOOTSTRAP_HEADERS, "Idempotency-Key": "prod-permit-1"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "human_approval_not_configured"

    # Even a permit minted before the environment became production-like is
    # not honored: the invoke-time gate re-checks and denies.
    monkeypatch.setattr(settings, "ENVIRONMENT", "local")
    permit = await _approval_permit(client, provisioned, idem_key="prod-permit-2")
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")

    invoke = await client.post(
        "/mcp/messages",
        json=_invoke_body(provisioned, permit, "prod-invoke-1"),
        headers=provisioned["agent_headers"],
    )
    assert invoke.status_code == 200
    error = invoke.json()["error"]
    assert error["code"] == -32003
    assert error["message"] == "human_approval_not_configured"
    assert error["data"]["receipt"]["outcome"] == "denied"


@pytest.mark.anyio
async def test_real_mode_without_config_rejects_permit_creation(
    client, clean_database, fresh_service, monkeypatch
):
    _sentinel_env(monkeypatch, simulated=False, configured=False)

    provisioned = await provision_agent_wallet(client)
    resp = await client.post(
        "/v1/permits",
        json={
            "issuer_wallet_id": provisioned["agent_wallet_id"],
            "subject_wallet_id": provisioned["agent_wallet_id"],
            "subject_key_id": provisioned["key_id"],
            "allowed_tools": [TOOL],
            "scopes": [f"tool:{TOOL}:invoke", "billing:charge"],
            "max_credits": 50,
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=30)
            ).isoformat(),
            "requires_human_approval": True,
        },
        headers={**BOOTSTRAP_HEADERS, "Idempotency-Key": "unconfigured-permit"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "human_approval_not_configured"


@pytest.mark.anyio
async def test_plain_permits_are_unaffected(
    client, clean_database, registered_tool, fresh_service, monkeypatch
):
    """A permit without the flag never touches the approval service."""
    _sentinel_env(monkeypatch, simulated=False, configured=False)

    async def _boom(**_kwargs):  # pragma: no cover - must not run
        raise AssertionError("approval service must not be consulted")

    monkeypatch.setattr(fresh_service, "ensure_approval", _boom)

    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name=TOOL,
        idem_key="plain-permit-1",
    )
    assert permit["requires_human_approval"] is False

    resp = await client.post(
        "/mcp/messages",
        json=_invoke_body(provisioned, permit, "plain-invoke-1"),
        headers=provisioned["agent_headers"],
    )
    assert resp.status_code == 200
    assert resp.json()["result"]["receipt"]["outcome"] == "success"
    assert resp.json()["result"]["receipt"]["approval_id"] is None


# ---------------------------------------------------------------------------
# Real mode against a scripted Sentinel
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_pending_then_approved_full_loop(
    client, clean_database, registered_tool, fresh_service, monkeypatch
):
    _sentinel_env(monkeypatch, simulated=False)
    fake = FakeSentinel(status="pending")
    monkeypatch.setattr(fresh_service, "_sentinel", lambda: fake)

    provisioned = await provision_agent_wallet(client)
    permit = await _approval_permit(client, provisioned, idem_key="real-permit-1")
    body = _invoke_body(provisioned, permit, "real-invoke-1")

    # First invoke: approval created in Sentinel, gate returns pending.
    first = await client.post(
        "/mcp/messages", json=body, headers=provisioned["agent_headers"]
    )
    assert first.status_code == 200
    error = first.json()["error"]
    assert error["code"] == -32005
    assert error["message"] == "human_approval_pending"
    assert error["data"]["approval_status"] == "pending"
    assert error["data"]["approval_id"].startswith("appr-")
    assert error["data"]["sentinel_action_id"].startswith("act_")
    assert len(fake.created) == 1
    # The request forwarded to Sentinel carries the governed context.
    sent = fake.created[0]
    assert sent["function_name"] == TOOL
    assert sent["arguments"]["permit_id"] == permit["permit_id"]

    # Nothing charged, no receipt written, and the idempotency key is free.
    assert await _ledger_debits(client, provisioned["agent_wallet_id"]) == 0

    # Retry while still pending: no second Sentinel approval is created.
    second = await client.post(
        "/mcp/messages", json=body, headers=provisioned["agent_headers"]
    )
    assert second.json()["error"]["message"] == "human_approval_pending"
    assert len(fake.created) == 1
    assert fake.polls == 1

    # Human approves in Sentinel; the SAME invoke (same key) now executes.
    fake.status = "approved"
    third = await client.post(
        "/mcp/messages", json=body, headers=provisioned["agent_headers"]
    )
    assert third.status_code == 200, third.text
    result = third.json()["result"]
    receipt = result["receipt"]
    assert receipt["outcome"] == "success"
    assert receipt["approval_id"] == error["data"]["approval_id"]
    assert receipt["ledger_entry_id"]
    assert await _ledger_debits(client, provisioned["agent_wallet_id"]) == 1

    # The receipt (with approval_id in its signed payload) verifies.
    verify = await client.post(
        "/v1/receipts/verify",
        json={"receipt_id": receipt["receipt_id"]},
        headers=BOOTSTRAP_HEADERS,
    )
    assert verify.status_code == 200
    assert verify.json()["valid"] is True

    # Replay of the completed invoke returns the same receipt, no new charge.
    replay = await client.post(
        "/mcp/messages", json=body, headers=provisioned["agent_headers"]
    )
    assert replay.json()["result"]["receipt"]["receipt_id"] == receipt["receipt_id"]
    assert await _ledger_debits(client, provisioned["agent_wallet_id"]) == 1


@pytest.mark.anyio
async def test_rejected_approval_is_terminal_and_replays(
    client, clean_database, registered_tool, fresh_service, monkeypatch
):
    _sentinel_env(monkeypatch, simulated=False)
    fake = FakeSentinel(status="pending")
    monkeypatch.setattr(fresh_service, "_sentinel", lambda: fake)

    provisioned = await provision_agent_wallet(client)
    permit = await _approval_permit(client, provisioned, idem_key="reject-permit-1")
    body = _invoke_body(provisioned, permit, "reject-invoke-1")

    pending = await client.post(
        "/mcp/messages", json=body, headers=provisioned["agent_headers"]
    )
    approval_id = pending.json()["error"]["data"]["approval_id"]

    fake.status = "rejected"
    denied = await client.post(
        "/mcp/messages", json=body, headers=provisioned["agent_headers"]
    )
    error = denied.json()["error"]
    assert error["code"] == -32003
    assert error["message"] == "human_approval_rejected"
    receipt = error["data"]["receipt"]
    assert receipt["outcome"] == "denied"
    assert receipt["approval_id"] == approval_id
    assert receipt["credits_charged"] == "0"
    assert await _ledger_debits(client, provisioned["agent_wallet_id"]) == 0

    # Terminal: the same key replays the denial without re-consulting Sentinel.
    polls_before = fake.polls
    replay = await client.post(
        "/mcp/messages", json=body, headers=provisioned["agent_headers"]
    )
    assert replay.json()["error"]["message"] == "human_approval_rejected"
    assert fake.polls == polls_before


@pytest.mark.anyio
async def test_locally_expired_approval_denies(
    client, clean_database, registered_tool, fresh_service, monkeypatch
):
    """Sentinel keeps timed-out approvals pending forever; we expire locally."""
    _sentinel_env(monkeypatch, simulated=False)
    fake = FakeSentinel(status="pending")
    monkeypatch.setattr(fresh_service, "_sentinel", lambda: fake)

    provisioned = await provision_agent_wallet(client)
    permit = await _approval_permit(client, provisioned, idem_key="expire-permit-1")
    body = _invoke_body(provisioned, permit, "expire-invoke-1")

    pending = await client.post(
        "/mcp/messages", json=body, headers=provisioned["agent_headers"]
    )
    approval_id = pending.json()["error"]["data"]["approval_id"]

    # Move the local expiry into the past.
    factory = get_session_factory()
    async with factory() as session:
        await session.execute(
            text(
                "UPDATE human_approvals SET expires_at = :past WHERE approval_id = :aid"
            ),
            {"past": datetime(2000, 1, 1), "aid": approval_id},
        )
        await session.commit()

    # Even though Sentinel would now say "approved", local expiry wins —
    # the check runs before any remote poll.
    fake.status = "approved"
    denied = await client.post(
        "/mcp/messages", json=body, headers=provisioned["agent_headers"]
    )
    error = denied.json()["error"]
    assert error["message"] == "human_approval_expired"
    assert error["data"]["receipt"]["outcome"] == "denied"
    assert await _ledger_debits(client, provisioned["agent_wallet_id"]) == 0


@pytest.mark.anyio
async def test_sentinel_unreachable_is_retryable_not_terminal(
    client, clean_database, registered_tool, fresh_service, monkeypatch
):
    import httpx

    _sentinel_env(monkeypatch, simulated=False)
    fake = FakeSentinel(status="pending")
    fake.fail_with = httpx.ConnectError("no route to sentinel")
    monkeypatch.setattr(fresh_service, "_sentinel", lambda: fake)

    provisioned = await provision_agent_wallet(client)
    permit = await _approval_permit(client, provisioned, idem_key="outage-permit-1")
    body = _invoke_body(provisioned, permit, "outage-invoke-1")

    down = await client.post(
        "/mcp/messages", json=body, headers=provisioned["agent_headers"]
    )
    error = down.json()["error"]
    assert error["code"] == -32005
    assert error["message"] == "human_approval_unavailable"
    assert await _ledger_debits(client, provisioned["agent_wallet_id"]) == 0

    # Sentinel recovers: the same key proceeds instead of replaying an error.
    fake.fail_with = None
    fake.status = "approved"
    pending = await client.post(
        "/mcp/messages", json=body, headers=provisioned["agent_headers"]
    )
    # First successful contact creates the approval (pending), then approves.
    assert pending.json()["error"]["message"] == "human_approval_pending"
    done = await client.post(
        "/mcp/messages", json=body, headers=provisioned["agent_headers"]
    )
    assert done.json()["result"]["receipt"]["outcome"] == "success"


# ---------------------------------------------------------------------------
# Signature coverage
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_tampering_with_permit_flag_invalidates_signature(
    client, clean_database, registered_tool, fresh_service, monkeypatch
):
    _sentinel_env(monkeypatch, simulated=True, configured=False)

    provisioned = await provision_agent_wallet(client)
    permit = await _approval_permit(client, provisioned, idem_key="tamper-permit-1")

    # Flip the stored flag off — as if a DB tamperer disabled the gate.
    factory = get_session_factory()
    async with factory() as session:
        model = await session.get(PermitModel, permit["permit_id"])
        model.requires_human_approval = False
        session.add(model)
        await session.commit()

    resp = await client.post(
        "/mcp/messages",
        json=_invoke_body(provisioned, permit, "tamper-invoke-1"),
        headers=provisioned["agent_headers"],
    )
    error = resp.json()["error"]
    assert error["message"] == "permit_signature_invalid"


@pytest.mark.anyio
async def test_tampering_with_receipt_approval_id_invalidates_signature(
    client, clean_database, registered_tool, fresh_service, monkeypatch
):
    _sentinel_env(monkeypatch, simulated=True, configured=False)

    provisioned = await provision_agent_wallet(client)
    permit = await _approval_permit(client, provisioned, idem_key="rtamper-permit-1")
    resp = await client.post(
        "/mcp/messages",
        json=_invoke_body(provisioned, permit, "rtamper-invoke-1"),
        headers=provisioned["agent_headers"],
    )
    receipt = resp.json()["result"]["receipt"]

    factory = get_session_factory()
    async with factory() as session:
        await session.execute(
            text("UPDATE receipts SET approval_id = NULL WHERE receipt_id = :rid"),
            {"rid": receipt["receipt_id"]},
        )
        await session.commit()

    verify = await client.post(
        "/v1/receipts/verify",
        json={"receipt_id": receipt["receipt_id"]},
        headers=BOOTSTRAP_HEADERS,
    )
    assert verify.status_code == 200
    assert verify.json()["valid"] is False


# ---------------------------------------------------------------------------
# Service-level unit coverage
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_service_raises_unavailable_on_poll_failure(
    clean_database, fresh_service, monkeypatch, client
):
    import httpx

    _sentinel_env(monkeypatch, simulated=False)
    fake = FakeSentinel(status="pending")
    monkeypatch.setattr(fresh_service, "_sentinel", lambda: fake)

    provisioned = await provision_agent_wallet(client)
    permit = await _approval_permit(client, provisioned, idem_key="svc-permit-1")

    check = await fresh_service.ensure_approval(
        wallet_id=provisioned["agent_wallet_id"],
        permit_id=permit["permit_id"],
        tool_name=TOOL,
        idempotency_key="svc-key-1",
        arguments={},
        estimated_credits=Decimal("2"),
    )
    assert check.status == "pending"

    fake.fail_with = httpx.ReadTimeout("slow sentinel")
    with pytest.raises(HumanApprovalUnavailableError):
        await fresh_service.ensure_approval(
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            tool_name=TOOL,
            idempotency_key="svc-key-1",
            arguments={},
            estimated_credits=Decimal("2"),
        )


@pytest.mark.anyio
async def test_service_4xx_from_sentinel_is_terminal(
    clean_database, fresh_service, monkeypatch, client
):
    import httpx

    _sentinel_env(monkeypatch, simulated=False)
    request = httpx.Request("POST", "https://sentinel.test/v1/approvals")
    response = httpx.Response(400, request=request, text="approvers must be set")
    fake = FakeSentinel()
    fake.fail_with = httpx.HTTPStatusError("400", request=request, response=response)
    monkeypatch.setattr(fresh_service, "_sentinel", lambda: fake)

    provisioned = await provision_agent_wallet(client)
    permit = await _approval_permit(client, provisioned, idem_key="svc4xx-permit-1")

    with pytest.raises(HumanApprovalError) as excinfo:
        await fresh_service.ensure_approval(
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            tool_name=TOOL,
            idempotency_key="svc4xx-key-1",
            arguments={},
            estimated_credits=Decimal("2"),
        )
    assert excinfo.value.reason == "human_approval_request_rejected"


def test_sim_flag_is_registered_with_runtime_mode():
    from app.core.runtime_mode import SERVICE_NAMES, simulation_settings_field

    assert "human_approval" in SERVICE_NAMES
    assert (
        simulation_settings_field("human_approval") == "SIMULATION_MODE_HUMAN_APPROVAL"
    )


# ---------------------------------------------------------------------------
# Hardening regressions (adversarial review of the original gate commit)
# ---------------------------------------------------------------------------


async def _approve_pending(client, provisioned, fake, body):
    """Drive one invoke to pending, approve it in Sentinel, return the id."""
    pending = await client.post(
        "/mcp/messages", json=body, headers=provisioned["agent_headers"]
    )
    error = pending.json()["error"]
    assert error["message"] == "human_approval_pending", pending.text
    data = error["data"]
    fake.status = "approved"
    return data


@pytest.mark.anyio
async def test_argument_swap_after_approval_is_denied(
    client, clean_database, registered_tool, fresh_service, monkeypatch
):
    """The human approves args X; a retry of the same key with args Y must not
    ride that approval. This is the core guarantee of the gate."""
    _sentinel_env(monkeypatch, simulated=False)
    fake = FakeSentinel(status="pending")
    monkeypatch.setattr(fresh_service, "_sentinel", lambda: fake)

    provisioned = await provision_agent_wallet(client)
    permit = await _approval_permit(client, provisioned, idem_key="swap-permit-1")

    benign = _invoke_body(provisioned, permit, "swap-invoke-1")
    benign["params"]["arguments"] = {"text": "benign — the human sees this"}
    await _approve_pending(client, provisioned, fake, benign)

    # Same idempotency key, DIFFERENT arguments — must be denied before any
    # charge or execution (the exact bypass reproduced against a live Sentinel).
    malicious = _invoke_body(provisioned, permit, "swap-invoke-1")
    malicious["params"]["arguments"] = {"text": "MALICIOUS — never approved"}
    resp = await client.post(
        "/mcp/messages", json=malicious, headers=provisioned["agent_headers"]
    )
    error = resp.json()["error"]
    assert error["code"] == -32003
    assert error["message"] == "human_approval_request_mismatch"
    assert error["data"]["receipt"]["outcome"] == "denied"
    assert error["data"]["receipt"]["credits_charged"] == "0"
    assert await _ledger_debits(client, provisioned["agent_wallet_id"]) == 0


@pytest.mark.anyio
async def test_price_change_after_approval_is_denied(
    client, clean_database, registered_tool, fresh_service, monkeypatch
):
    """The human approves one price; a same-key retry at a higher current
    price must not spend that approval or charge the wallet."""
    _sentinel_env(monkeypatch, simulated=False)
    fake = FakeSentinel(status="pending")
    monkeypatch.setattr(fresh_service, "_sentinel", lambda: fake)

    provisioned = await provision_agent_wallet(client)
    permit = await _approval_permit(client, provisioned, idem_key="price-permit-1")
    body = _invoke_body(provisioned, permit, "price-invoke-1")

    await _approve_pending(client, provisioned, fake, body)
    assert fake.created[0]["arguments"]["estimated_credits"] == "2.0"

    service = get_service_registry().get_local(TOOL)
    assert service is not None
    service["credits_per_unit"] = 20.0

    resp = await client.post(
        "/mcp/messages", json=body, headers=provisioned["agent_headers"]
    )
    error = resp.json()["error"]
    assert error["code"] == -32003
    assert error["message"] == "human_approval_request_mismatch"
    assert error["data"]["receipt"]["outcome"] == "denied"
    assert error["data"]["receipt"]["credits_charged"] == "0"
    assert await _ledger_debits(client, provisioned["agent_wallet_id"]) == 0


@pytest.mark.anyio
async def test_one_approval_cannot_charge_twice_across_transports(
    client, clean_database, registered_tool, fresh_service, monkeypatch
):
    """A single human decision authorizes exactly one invoke, even though the
    same logical (wallet, permit, tool, key) reaches the pipeline over both
    JSON-RPC and REST transports."""
    _sentinel_env(monkeypatch, simulated=False)
    fake = FakeSentinel(status="pending")
    monkeypatch.setattr(fresh_service, "_sentinel", lambda: fake)

    provisioned = await provision_agent_wallet(client)
    permit = await _approval_permit(client, provisioned, idem_key="xport-permit-1")
    body = _invoke_body(provisioned, permit, "xport-invoke-1")

    await _approve_pending(client, provisioned, fake, body)

    # First consumption over JSON-RPC succeeds.
    first = await client.post(
        "/mcp/messages", json=body, headers=provisioned["agent_headers"]
    )
    first_result = first.json()["result"]
    receipt = first_result["receipt"]
    assert receipt["outcome"] == "success"
    assert registered_tool["count"] == 1
    assert await _ledger_debits(client, provisioned["agent_wallet_id"]) == 1

    # Same logical request over REST replays the signed terminal result. It
    # must not re-enter the approval gate, execute the tool, or charge again.
    approvals_created = len(fake.created)
    approval_polls = fake.polls
    rest = await client.post(
        f"/mcp/tools/{TOOL}/invoke",
        json={
            "name": TOOL,
            "arguments": {"message": "hello"},
            "mcp_context": {
                "wallet_id": provisioned["agent_wallet_id"],
                "permit_id": permit["permit_id"],
                "idempotency_key": "xport-invoke-1",
            },
        },
        headers=provisioned["agent_headers"],
    )
    assert rest.status_code == 200
    rest_result = rest.json()
    assert {key: value for key, value in rest_result.items() if value is not None} == (
        first_result
    )
    assert rest_result["receipt"]["receipt_id"] == receipt["receipt_id"]
    assert len(fake.created) == approvals_created == 1
    assert fake.polls == approval_polls == 1
    assert registered_tool["count"] == 1
    assert await _ledger_debits(client, provisioned["agent_wallet_id"]) == 1
    record = await get_idempotency_service().get_record(
        wallet_id=provisioned["agent_wallet_id"],
        endpoint=GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
        idempotency_key="xport-invoke-1",
    )
    assert record is not None
    assert record.status_code == 200
    assert record.response_reference == receipt["receipt_id"]

    verify = await client.post(
        "/v1/receipts/verify",
        json={"receipt_id": receipt["receipt_id"]},
        headers=BOOTSTRAP_HEADERS,
    )
    assert verify.status_code == 200
    assert verify.json()["valid"] is True


@pytest.mark.anyio
async def test_stored_simulated_approval_denied_in_production(
    client, clean_database, registered_tool, fresh_service, monkeypatch
):
    """A simulated auto-approval minted in dev must never authorize a real
    invoke after the same database is promoted to a production-like env."""
    settings = _sentinel_env(monkeypatch, simulated=True, configured=False)

    provisioned = await provision_agent_wallet(client)
    permit = await _approval_permit(client, provisioned, idem_key="promote-permit-1")

    # Mint the simulated approval row in local/dev by calling the service
    # directly, so no tool executes and nothing is charged. The service still
    # consumes the row; the returned check reports the pre-consume decision.
    check = await fresh_service.ensure_approval(
        wallet_id=provisioned["agent_wallet_id"],
        permit_id=permit["permit_id"],
        tool_name=TOOL,
        idempotency_key="promote-invoke-1",
        arguments={"message": "hello"},
        estimated_credits=Decimal("2"),
    )
    assert check.status == "approved" and check.simulated is True

    # Promote: production-like env, real Sentinel config.
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "SIMULATION_MODE_HUMAN_APPROVAL", False)
    monkeypatch.setattr(settings, "SENTINEL_API_URL", "https://sentinel.test")
    monkeypatch.setattr(settings, "SENTINEL_API_KEY", "sk_live_" + "0" * 64)

    with pytest.raises(HumanApprovalError) as excinfo:
        await fresh_service.ensure_approval(
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            tool_name=TOOL,
            idempotency_key="promote-invoke-1",
            arguments={"message": "hello"},
            estimated_credits=Decimal("2"),
        )
    assert excinfo.value.reason == "human_approval_not_configured"


@pytest.mark.anyio
async def test_non_json_sentinel_response_is_retryable_not_stranded(
    client, clean_database, registered_tool, fresh_service, monkeypatch
):
    """A 200 with a non-JSON body (proxy error page) must surface as a
    retryable outage, not strand the caller's idempotency key."""
    import httpx

    _sentinel_env(monkeypatch, simulated=False)

    # Drive a REAL SentinelClient through httpx.MockTransport so the test
    # exercises create_approval's actual response-decoding path (a change that
    # reverted _decode_json to resp.json() would fail here).
    from app.services.human_approval import SentinelClient

    def _html(_request):
        return httpx.Response(200, text="<html>502 Bad Gateway</html>")

    html_client = SentinelClient("https://sentinel.test", "sk_test_" + "0" * 64)
    html_client._client = httpx.AsyncClient(
        base_url="https://sentinel.test",
        transport=httpx.MockTransport(_html),
    )
    monkeypatch.setattr(fresh_service, "_sentinel", lambda: html_client)

    provisioned = await provision_agent_wallet(client)
    permit = await _approval_permit(client, provisioned, idem_key="html-permit-1")
    body = _invoke_body(provisioned, permit, "html-invoke-1")

    resp = await client.post(
        "/mcp/messages", json=body, headers=provisioned["agent_headers"]
    )
    error = resp.json()["error"]
    assert error["code"] == -32005
    assert error["message"] == "human_approval_unavailable"

    # Key not poisoned: once Sentinel is healthy the same key proceeds. The
    # first healthy contact creates the approval (pending); a retry then polls
    # it to approved and executes.
    fake = FakeSentinel(status="pending")
    monkeypatch.setattr(fresh_service, "_sentinel", lambda: fake)
    recovered = await client.post(
        "/mcp/messages", json=body, headers=provisioned["agent_headers"]
    )
    assert recovered.json()["error"]["message"] == "human_approval_pending"
    fake.status = "approved"
    ok = await client.post(
        "/mcp/messages", json=body, headers=provisioned["agent_headers"]
    )
    assert ok.json()["result"]["receipt"]["outcome"] == "success"


@pytest.mark.anyio
async def test_sentinel_idempotency_key_is_deterministic_per_invoke(
    client, clean_database, registered_tool, fresh_service, monkeypatch
):
    """The provider Idempotency-Key must be stable across attempts for one
    invoke, so a lost-response retry dedups instead of paging twice."""
    from app.services.human_approval import (
        invoke_request_hash,
        sentinel_idempotency_key,
    )

    hx = invoke_request_hash("t", {"a": 1}, Decimal("2"))
    hy = invoke_request_hash("t", {"a": 2}, Decimal("2"))
    hz = invoke_request_hash("t", {"a": 1}, Decimal("20"))
    k1 = sentinel_idempotency_key("w", "p", "t", "k", hx)
    k2 = sentinel_idempotency_key("w", "p", "t", "k", hx)
    k3 = sentinel_idempotency_key("w", "p", "t", "different", hx)
    # Same idempotency key + DIFFERENT args must diverge, so a differing-args
    # retry gets its own Sentinel approval instead of riding the human's
    # original decision (the transient-create bypass).
    k4 = sentinel_idempotency_key("w", "p", "t", "k", hy)
    # The same applies when the reviewed price changes with identical args.
    k5 = sentinel_idempotency_key("w", "p", "t", "k", hz)
    assert k1 == k2 and k1 not in {k3, k4, k5} and k1.startswith("mw-")

    _sentinel_env(monkeypatch, simulated=False)
    fake = FakeSentinel(status="pending")
    monkeypatch.setattr(fresh_service, "_sentinel", lambda: fake)

    provisioned = await provision_agent_wallet(client)
    permit = await _approval_permit(client, provisioned, idem_key="detkey-permit-1")
    body = _invoke_body(provisioned, permit, "detkey-invoke-1")

    await client.post("/mcp/messages", json=body, headers=provisioned["agent_headers"])
    sent_key = fake.created[0]["idempotency_key"]
    assert sent_key == sentinel_idempotency_key(
        provisioned["agent_wallet_id"],
        permit["permit_id"],
        TOOL,
        "detkey-invoke-1",
        invoke_request_hash(TOOL, {"message": "hello"}, Decimal("2")),
    )
    # Independent of the per-attempt random approval_id.
    assert "appr-" not in sent_key


@pytest.mark.anyio
async def test_consume_is_single_winner(
    clean_database, fresh_service, monkeypatch, client
):
    """Unit: an approved, unexpired approval consumes exactly once; an expired
    one never consumes."""
    from datetime import datetime

    from app.db.models import HumanApprovalModel
    from app.services.human_approval import APPROVAL_STATUS_APPROVED

    _sentinel_env(monkeypatch, simulated=False)
    provisioned = await provision_agent_wallet(client)
    permit = await _approval_permit(client, provisioned, idem_key="consume-permit-1")

    factory = get_session_factory()
    async with factory() as session:
        session.add(
            HumanApprovalModel(
                approval_id="appr-consume-live",
                wallet_id=provisioned["agent_wallet_id"],
                permit_id=permit["permit_id"],
                tool=TOOL,
                idempotency_key="consume-live",
                status=APPROVAL_STATUS_APPROVED,
                request_hash="x",
                requested_at=datetime(2026, 1, 1),
                expires_at=datetime(2999, 1, 1),
            )
        )
        session.add(
            HumanApprovalModel(
                approval_id="appr-consume-expired",
                wallet_id=provisioned["agent_wallet_id"],
                permit_id=permit["permit_id"],
                tool=TOOL,
                idempotency_key="consume-expired",
                status=APPROVAL_STATUS_APPROVED,
                request_hash="x",
                requested_at=datetime(2000, 1, 1),
                expires_at=datetime(2000, 1, 2),
            )
        )
        await session.commit()

    assert await fresh_service._consume("appr-consume-live") is True
    assert await fresh_service._consume("appr-consume-live") is False  # already spent
    assert await fresh_service._consume("appr-consume-expired") is False  # window gone


@pytest.mark.anyio
async def test_refresh_decision_does_not_revive_consumed_approval(
    clean_database, fresh_service, monkeypatch, client
):
    """A slow concurrent retry that polls Sentinel and persists an approved
    decision must not revive a row another request already consumed — that
    would let its atomic consume win a second time (one approval, two charges).
    Regression for the Cursor Bugbot finding on #199."""
    from datetime import datetime

    from app.db.models import HumanApprovalModel
    from app.services.human_approval import (
        APPROVAL_STATUS_APPROVED,
        APPROVAL_STATUS_CONSUMED,
    )

    _sentinel_env(monkeypatch, simulated=False)
    provisioned = await provision_agent_wallet(client)
    permit = await _approval_permit(client, provisioned, idem_key="revive-permit-1")

    common = dict(
        approval_id="appr-revive",
        wallet_id=provisioned["agent_wallet_id"],
        permit_id=permit["permit_id"],
        tool=TOOL,
        idempotency_key="revive",
        request_hash="x",
        sentinel_action_id="act_revive",
        requested_at=datetime(2026, 1, 1),
        expires_at=datetime(2999, 1, 1),
    )

    factory = get_session_factory()
    async with factory() as session:
        session.add(HumanApprovalModel(status=APPROVAL_STATUS_APPROVED, **common))
        await session.commit()

    # R1 consumes the single-use approval.
    assert await fresh_service._consume("appr-revive") is True

    # R2 arrives with a stale in-memory 'approved' decision and tries to
    # persist it. The conditional write (guarded on status='pending') is a
    # no-op against the now-consumed row.
    stale = HumanApprovalModel(
        status=APPROVAL_STATUS_APPROVED, decided_by="late", reason="late", **common
    )
    assert await fresh_service._persist_decision(stale) is False

    # The row stays consumed and cannot be consumed again.
    assert await fresh_service._consume("appr-revive") is False
    async with factory() as session:
        row = await session.get(HumanApprovalModel, "appr-revive")
        assert row.status == APPROVAL_STATUS_CONSUMED


@pytest.mark.anyio
async def test_transient_create_with_different_args_gets_separate_approval(
    client, clean_database, registered_tool, fresh_service, monkeypatch
):
    """Codex P1: if Sentinel commits a create but the response is lost before
    the local binding is persisted, a retry with the SAME idempotency key but
    DIFFERENT arguments must not dedup onto the human's original approval. The
    request hash is part of the provider key, so the differing-args retry gets
    its own approval — the human never reviews args they didn't approve."""

    from app.services.human_approval import invoke_request_hash

    _sentinel_env(monkeypatch, simulated=False)
    provisioned = await provision_agent_wallet(client)
    permit = await _approval_permit(client, provisioned, idem_key="transient-permit-1")

    dedup = DedupSentinel()
    monkeypatch.setattr(fresh_service, "_sentinel", lambda: dedup)

    # Attempt 1 (args X): Sentinel commits the create, then the local persist
    # fails — the lost-response / crash-before-binding window. No binding row is
    # written, and the gate catch-all fails closed and releases the key.
    calls = {"n": 0}
    real_persist = fresh_service._persist

    async def flaky_persist(model):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("lost response after sentinel create")
        return await real_persist(model)

    monkeypatch.setattr(fresh_service, "_persist", flaky_persist)

    body_x = _invoke_body(provisioned, permit, "transient-1")
    body_x["params"]["arguments"] = {"text": "X the human reviews"}
    r1 = await client.post(
        "/mcp/messages", json=body_x, headers=provisioned["agent_headers"]
    )
    assert r1.json()["error"]["code"] == -32005

    # Attempt 2: SAME idempotency key, args Y. Persist now succeeds.
    body_y = _invoke_body(provisioned, permit, "transient-1")
    body_y["params"]["arguments"] = {"text": "Y never reviewed"}
    r2 = await client.post(
        "/mcp/messages", json=body_y, headers=provisioned["agent_headers"]
    )
    assert r2.json()["error"]["message"] == "human_approval_pending"

    # The two attempts sent DIFFERENT provider keys and got DIFFERENT approvals,
    # so Y is never bound to X's human decision.
    keys = {k for k, _ in dedup.creates}
    assert len(keys) == 2, dedup.creates
    x_hash = invoke_request_hash(TOOL, {"text": "X the human reviews"}, Decimal("2"))
    y_hash = invoke_request_hash(TOOL, {"text": "Y never reviewed"}, Decimal("2"))
    assert x_hash != y_hash


@pytest.mark.anyio
async def test_transient_create_with_different_price_gets_separate_approval(
    client, clean_database, registered_tool, fresh_service, monkeypatch
):
    """If Sentinel commits a create before local persistence fails, a retry
    at a different price must use a distinct provider key and approval."""
    _sentinel_env(monkeypatch, simulated=False)
    provisioned = await provision_agent_wallet(client)
    permit = await _approval_permit(
        client, provisioned, idem_key="transient-price-permit-1"
    )

    dedup = DedupSentinel()
    monkeypatch.setattr(fresh_service, "_sentinel", lambda: dedup)

    calls = {"n": 0}
    real_persist = fresh_service._persist

    async def flaky_persist(model):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("lost response after sentinel create")
        return await real_persist(model)

    monkeypatch.setattr(fresh_service, "_persist", flaky_persist)

    body = _invoke_body(provisioned, permit, "transient-price-1")
    first = await client.post(
        "/mcp/messages", json=body, headers=provisioned["agent_headers"]
    )
    assert first.json()["error"]["code"] == -32005

    service = get_service_registry().get_local(TOOL)
    assert service is not None
    service["credits_per_unit"] = 20.0

    second = await client.post(
        "/mcp/messages", json=body, headers=provisioned["agent_headers"]
    )
    assert second.json()["error"]["message"] == "human_approval_pending"

    assert [created[1]["estimated_credits"] for created in dedup.creates] == [
        "2.0",
        "20.0",
    ]
    assert len({created[0] for created in dedup.creates}) == 2, dedup.creates


@pytest.mark.anyio
async def test_malformed_sentinel_create_never_logs_the_approval_url(
    clean_database, fresh_service, monkeypatch, client, caplog
):
    """A malformed create response is logged by shape, never by value."""
    _sentinel_env(monkeypatch, simulated=False)
    fake = FakeSentinel(status="pending")
    token = "SENTINEL-MAGIC-LINK-TOKEN-9c1d"

    async def malformed_create(**kwargs):
        return {"status": "pending", "approval_url": f"https://sentinel.test/a/{token}"}

    monkeypatch.setattr(fake, "create_approval", malformed_create)
    monkeypatch.setattr(fresh_service, "_sentinel", lambda: fake)

    provisioned = await provision_agent_wallet(client)
    permit = await _approval_permit(client, provisioned, idem_key="malformed-permit-1")
    with caplog.at_level(logging.ERROR, logger="app.services.human_approval"):
        with pytest.raises(HumanApprovalUnavailableError):
            await fresh_service.ensure_approval(
                wallet_id=provisioned["agent_wallet_id"],
                permit_id=permit["permit_id"],
                tool_name=TOOL,
                idempotency_key="malformed-key-1",
                arguments={},
                estimated_credits=Decimal("2"),
            )
    assert "sentinel_create_malformed_response" in caplog.text
    assert "approval_url" in caplog.text
    assert token not in caplog.text
    assert "sentinel.test" not in caplog.text
