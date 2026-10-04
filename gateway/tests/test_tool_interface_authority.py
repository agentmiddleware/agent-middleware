"""The middleware disappears behind the tool interface.

An agent should discover a normal MCP tool, call it normally, and have
governance happen automatically because that is the only executable path.
These tests pin the recomposition that makes that true on the standard
``/mcp`` surface:

- A wallet policy demanding a human decision is *materialized* as an
  approval-gated auto-permit: the agent calls the tool with only name and
  arguments, gets a retryable ``pending_human_approval`` with an approval id
  and remediation, and the SAME call with the SAME idempotency key executes
  once a human approves. The agent never learns the permit API.
- The approval gate satisfies the policy's ``human_approval_required``
  constraint but waives nothing else — cost caps still deny.
- Denials are machine-actionable: the standard surface carries the same
  ``details`` as the governed endpoints, and mint failures name the gap and
  the way out (``authority_required`` + remediation).
- Discovery advertises the governance contract per tool.
- ``GET /v1/me/authority`` answers "what authority do I currently have?" in
  one wallet-scoped read.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

import app.routers.mcp_standard as mcp_standard_module
import app.services.human_approval as human_approval_module
from app.core.config import get_settings
from app.main import app
from app.routers.mcp import ToolPermissionDenied
from app.schemas.billing import ServiceCategory
from app.schemas.policies import PolicyBundleCreate
from app.services.human_approval import HumanApprovalService
from app.services.mcp_generator import McpGenerator
from app.services.policies import (
    create_policy_bundle,
    evaluate_wallet_policy,
    wallet_human_approval_required,
)
from app.services.service_registry import get_service_registry
from tests.test_trust_helpers import (
    BOOTSTRAP_HEADERS,
    create_tool_permit,
    provision_agent_wallet,
)

TOOL = "authority-echo"

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def standard_mcp_enabled(monkeypatch):
    monkeypatch.setenv("ENABLE_STANDARD_MCP_ENDPOINT", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.setenv("ENABLE_STANDARD_MCP_ENDPOINT", "false")
    get_settings.cache_clear()


@pytest.fixture
def registered_tool():
    registry = get_service_registry()
    registry.register_local(
        service_id=TOOL,
        name="Authority Echo",
        description="Tool-interface authority test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=lambda message="ok": {"message": message},
        credits_per_unit=2.0,
        unit_name="call",
    )
    yield
    registry.unregister_local(TOOL)


@pytest.fixture
def fresh_service(monkeypatch):
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

    async def create_approval(self, **kwargs):
        self.created.append(kwargs)
        return {"action_id": f"act_{uuid.uuid4().hex[:16]}", "status": "pending"}

    async def get_approval(self, action_id: str):
        self.polls += 1
        payload = {"action_id": action_id, "status": self.status}
        if self.status in {"approved", "rejected"}:
            payload["decided_by"] = "reviewer@example.com"
            payload["reason"] = f"scripted {self.status}"
        return payload

    async def wait_approval(self, action_id: str, timeout: float):
        return await self.get_approval(action_id)


def _tools_call(idem_key: str | None = None) -> tuple[dict, dict]:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": TOOL, "arguments": {"message": "hi"}},
    }
    headers = dict(MCP_HEADERS)
    if idem_key:
        headers["Idempotency-Key"] = idem_key
    return body, headers


async def _approval_policy(wallet_id: str):
    return await create_policy_bundle(
        PolicyBundleCreate(
            wallet_id=wallet_id,
            name="human approval for everything",
            human_approval_required=True,
        )
    )


# ---------------------------------------------------------------------------
# Policy -> materialized approval on the standard /mcp surface
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_policy_materializes_approval_gate_pending_then_approved(
    client,
    standard_mcp_enabled,
    clean_database,
    registered_tool,
    fresh_service,
    monkeypatch,
):
    """The vision's core loop: call -> pending approval -> approve -> retry -> receipt.

    The agent sends only the tool name and arguments (plus an idempotency
    key). It never sees /v1/permits or mcpContext.
    """
    _sentinel_env(monkeypatch, simulated=False)
    fake = FakeSentinel(status="pending")
    monkeypatch.setattr(fresh_service, "_sentinel", lambda: fake)

    provisioned = await provision_agent_wallet(client)
    await _approval_policy(provisioned["agent_wallet_id"])
    assert await wallet_human_approval_required(provisioned["agent_wallet_id"])

    body, headers = _tools_call(idem_key="authority-call-1")
    headers = {**provisioned["agent_headers"], **headers}

    first = await client.post("/mcp", json=body, headers=headers)
    assert first.status_code == 200
    error = first.json()["error"]
    assert error["code"] == -32005
    assert error["message"] == "human_approval_pending"
    data = error["data"]
    assert data["error"] == "authority_required"
    assert data["status"] == "pending_human_approval"
    assert data["approval_id"].startswith("appr-")
    assert data["remediation"]["type"] == "await_human_decision"
    assert len(fake.created) == 1

    # Retry while pending: the same approval is polled, no human re-paged.
    second = await client.post("/mcp", json=body, headers=headers)
    assert second.json()["error"]["message"] == "human_approval_pending"
    assert len(fake.created) == 1

    # Human approves; the SAME call with the SAME key executes and receipts.
    fake.status = "approved"
    third = await client.post("/mcp", json=body, headers=headers)
    assert third.status_code == 200, third.text
    result = third.json()["result"]
    receipt = result["receipt"]
    assert receipt["outcome"] == "success"
    assert receipt["approval_id"] == data["approval_id"]

    # The materialized permit is real, bounded, and approval-gated — the
    # cryptographic artifact of the policy decision, minted server-side.
    permit_resp = await client.get(
        f"/v1/permits/{receipt['permit_id']}", headers=provisioned["agent_headers"]
    )
    assert permit_resp.status_code == 200
    permit = permit_resp.json()
    assert permit["requires_human_approval"] is True
    assert permit["allowed_tools"] == [TOOL]

    # The permit outlives the whole approval window (plus execution margin),
    # so a decision made late in the window still executes instead of
    # stranding as permit_expired.
    settings = get_settings()
    issued_at = datetime.fromisoformat(permit["issued_at"])
    expires_at = datetime.fromisoformat(permit["expires_at"])
    lifetime = (expires_at - issued_at).total_seconds()
    assert lifetime >= (
        settings.SENTINEL_APPROVAL_TIMEOUT_SECONDS
        + settings.STANDARD_MCP_PERMIT_TTL_SECONDS
        - 1
    )


@pytest.mark.anyio
async def test_approval_policy_requires_idempotency_key(
    client,
    standard_mcp_enabled,
    clean_database,
    registered_tool,
    fresh_service,
    monkeypatch,
):
    """Without a client key every retry would page a human again. Refuse with
    a structured, machine-actionable error instead."""
    _sentinel_env(monkeypatch, simulated=True, configured=False)

    provisioned = await provision_agent_wallet(client)
    await _approval_policy(provisioned["agent_wallet_id"])

    body, headers = _tools_call(idem_key=None)
    resp = await client.post(
        "/mcp", json=body, headers={**provisioned["agent_headers"], **headers}
    )
    error = resp.json()["error"]
    assert error["code"] == -32003
    assert error["message"] == "idempotency_key_required_for_human_approval"
    assert error["data"]["error"] == "authority_required"
    assert error["data"]["remediation"]["type"] == "retry_with_idempotency_key"


@pytest.mark.anyio
async def test_rejected_approval_is_terminal_and_replays_on_standard_surface(
    client,
    standard_mcp_enabled,
    clean_database,
    registered_tool,
    fresh_service,
    monkeypatch,
):
    """A rejected decision denies with a signed receipt, and the same key
    replays the same denial instead of paging a human again."""
    _sentinel_env(monkeypatch, simulated=False)
    fake = FakeSentinel(status="rejected")
    monkeypatch.setattr(fresh_service, "_sentinel", lambda: fake)

    provisioned = await provision_agent_wallet(client)
    await _approval_policy(provisioned["agent_wallet_id"])

    body, headers = _tools_call(idem_key="authority-rejected-1")
    headers = {**provisioned["agent_headers"], **headers}

    # Approval creation always starts pending; the human's rejection lands
    # on the poll that the keyed retry performs.
    fake.status = "pending"
    first = await client.post("/mcp", json=body, headers=headers)
    assert first.json()["error"]["code"] == -32005

    fake.status = "rejected"
    second = await client.post("/mcp", json=body, headers=headers)
    error = second.json()["error"]
    assert error["code"] == -32003
    assert error["message"] == "human_approval_rejected"
    assert error["data"]["receipt"]["outcome"] == "denied"

    replay = await client.post("/mcp", json=body, headers=headers)
    assert replay.json()["error"]["message"] == "human_approval_rejected"
    assert len(fake.created) == 1


@pytest.mark.anyio
async def test_simulated_approval_auto_approves_on_standard_surface(
    client,
    standard_mcp_enabled,
    clean_database,
    registered_tool,
    fresh_service,
    monkeypatch,
):
    _sentinel_env(monkeypatch, simulated=True, configured=False)

    provisioned = await provision_agent_wallet(client)
    await _approval_policy(provisioned["agent_wallet_id"])

    body, headers = _tools_call(idem_key="authority-sim-1")
    resp = await client.post(
        "/mcp", json=body, headers={**provisioned["agent_headers"], **headers}
    )
    assert resp.status_code == 200, resp.text
    receipt = resp.json()["result"]["receipt"]
    assert receipt["outcome"] == "success"
    assert receipt["approval_id"], "simulated approval must still be recorded"


# ---------------------------------------------------------------------------
# The gate satisfies human_approval_required and waives nothing else
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_policy_gate_semantics(client, clean_database):
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    await create_policy_bundle(
        PolicyBundleCreate(
            wallet_id=wallet_id,
            name="approval plus cost cap",
            human_approval_required=True,
            max_cost_per_action=5.0,
        )
    )

    # No approval gate: fail closed, as before.
    ungated = await evaluate_wallet_policy(
        wallet_id=wallet_id, tool_name=TOOL, estimated_cost=1.0
    )
    assert ungated.allowed is False
    assert ungated.reason == "human_approval_required"

    # Gate active: the approval demand is satisfied and recorded.
    gated = await evaluate_wallet_policy(
        wallet_id=wallet_id,
        tool_name=TOOL,
        estimated_cost=1.0,
        approval_gate_active=True,
    )
    assert gated.allowed is True
    evaluated = gated.evaluated_constraints["evaluated"]
    assert evaluated[0]["human_approval_satisfied_by_gate"] is True

    # Gate active but over the cost cap: every OTHER constraint still binds.
    capped = await evaluate_wallet_policy(
        wallet_id=wallet_id,
        tool_name=TOOL,
        estimated_cost=50.0,
        approval_gate_active=True,
    )
    assert capped.allowed is False
    assert capped.reason == "max_cost_per_action_exceeded"


# ---------------------------------------------------------------------------
# Machine-actionable errors on the standard surface
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_budget_denial_names_the_gap_and_the_way_out(
    client, standard_mcp_enabled, clean_database
):
    """A tool the wallet cannot afford yields authority_required with the
    requested cost and remediation, not a bare reason string."""
    provisioned = await provision_agent_wallet(client)
    registry = get_service_registry()
    registry.register_local(
        service_id="authority-expensive",
        name="Expensive Tool",
        description="Costs more than the test wallet holds",
        category=ServiceCategory.AGENT_COMMS,
        func=lambda: {"ok": True},
        credits_per_unit=100000.0,
        unit_name="call",
    )
    try:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "authority-expensive", "arguments": {}},
        }
        resp = await client.post(
            "/mcp", json=body, headers={**provisioned["agent_headers"], **MCP_HEADERS}
        )
        error = resp.json()["error"]
        assert error["code"] == -32004
        assert error["message"] == "permit_budget_exceeds_wallet_balance"
        data = error["data"]
        assert data["error"] == "authority_required"
        assert data["reason_code"] == "permit_budget_exceeds_wallet_balance"
        assert data["requested"]["tool"] == "authority-expensive"
        assert float(data["requested"]["estimated_credits"]) == 100000.0
        remediation = data["remediation"]
        assert remediation["type"] == "fund_wallet_or_request_authority"
        assert "/v1/permit-requests" in remediation["request_authority"]
        assert "/v1/me/authority" in remediation["check_authority"]
    finally:
        registry.unregister_local("authority-expensive")


@pytest.mark.anyio
async def test_standard_surface_forwards_denial_details(
    client, standard_mcp_enabled, clean_database, registered_tool, monkeypatch
):
    """Parity with /mcp/messages: ToolPermissionDenied.details must survive
    the standard-surface boundary, not just the receipt."""
    provisioned = await provision_agent_wallet(client)

    details = {"required_credits": "5", "remaining_credits": "1"}
    receipt = {"receipt_id": "rcpt-test", "outcome": "denied"}

    async def _deny(*args, **kwargs):
        raise ToolPermissionDenied(
            "permit_budget_exceeded", receipt=receipt, details=details
        )

    monkeypatch.setattr(mcp_standard_module, "_handle_tools_call", _deny)

    body, headers = _tools_call(idem_key=None)
    resp = await client.post(
        "/mcp", json=body, headers={**provisioned["agent_headers"], **headers}
    )
    error = resp.json()["error"]
    assert error["code"] == -32003
    assert error["message"] == "permit_budget_exceeded"
    assert error["data"]["details"] == details
    assert error["data"]["receipt"] == receipt


# ---------------------------------------------------------------------------
# Discovery advertises the governance contract
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_discovery_annotations_carry_governance_contract(
    client, registered_tool, monkeypatch
):
    # The suite's ambient env is permissive (conftest); pin strict mode —
    # the production default — so the manifest advertises the contract.
    settings = get_settings()
    monkeypatch.setattr(settings, "TRUST_MODE_ENABLED", True)
    monkeypatch.setattr(settings, "ALLOW_LEGACY_UNPERMITTED_MCP", False)

    resp = await client.get("/mcp/tools.json")
    assert resp.status_code == 200
    tools = {t["name"]: t for t in resp.json()["tools"]}
    ann = tools[TOOL]["annotations"]
    assert ann["governed"] is True
    assert ann["receiptProvided"] is True
    assert ann["supportsIdempotency"] is True
    assert ann["approvalMayBeRequired"] is True
    assert ann["economicAction"] is True  # 2.0 credits per call

    registry = get_service_registry()
    registry.register_local(
        service_id="authority-free",
        name="Free Tool",
        description="Zero-cost tool",
        category=ServiceCategory.AGENT_COMMS,
        func=lambda: {"ok": True},
        credits_per_unit=0.0,
        unit_name="call",
    )
    try:
        resp = await client.get("/mcp/tools.json")
        free = {t["name"]: t for t in resp.json()["tools"]}["authority-free"]
        assert free["annotations"]["economicAction"] is False
        assert free["annotations"]["governed"] is True
    finally:
        registry.unregister_local("authority-free")


# ---------------------------------------------------------------------------
# GET /v1/me/authority
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_me_authority_summarizes_current_authority(
    client, clean_database, registered_tool
):
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    await _approval_policy(wallet_id)
    permit = await create_tool_permit(
        client,
        wallet_id=wallet_id,
        key_id=provisioned["key_id"],
        tool_name=TOOL,
        max_credits=50,
        idem_key="authority-permit-1",
    )

    resp = await client.get("/v1/me/authority", headers=provisioned["agent_headers"])
    assert resp.status_code == 200, resp.text
    summary = resp.json()
    assert summary["wallet_id"] == wallet_id
    assert float(summary["balance"]) == 1000.0
    assert float(summary["daily_spend_used"]) == 0.0
    assert summary["human_approval_required"] is True
    assert [p["human_approval_required"] for p in summary["policies"]] == [True]
    permit_ids = [p["permit_id"] for p in summary["active_permits"]]
    assert permit["permit_id"] in permit_ids
    assert summary["pending_permit_requests"] == []


@pytest.mark.anyio
async def test_me_authority_requires_wallet_key(client, clean_database):
    resp = await client.get("/v1/me/authority", headers=BOOTSTRAP_HEADERS)
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "wallet_key_required"


@pytest.mark.anyio
async def test_me_authority_requires_credentials(client, clean_database):
    resp = await client.get("/v1/me/authority")
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_me_authority_missing_wallet_is_404(client, clean_database, monkeypatch):
    provisioned = await provision_agent_wallet(client)

    async def _no_wallet(self, wallet_id):
        return None

    from app.services.agent_money import AgentMoney

    monkeypatch.setattr(AgentMoney, "get_wallet", _no_wallet)
    resp = await client.get("/v1/me/authority", headers=provisioned["agent_headers"])
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "wallet_not_found"


@pytest.mark.anyio
async def test_me_authority_excludes_expired_permits(
    client, clean_database, registered_tool
):
    """A permit past expires_at keeps status="active" in storage; the summary
    must not present it as current authority."""
    from app.db.database import get_session_factory
    from app.db.models import PermitModel

    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    live = await create_tool_permit(
        client,
        wallet_id=wallet_id,
        key_id=provisioned["key_id"],
        tool_name=TOOL,
        max_credits=5,
        idem_key="authority-live-permit",
    )
    stale = await create_tool_permit(
        client,
        wallet_id=wallet_id,
        key_id=provisioned["key_id"],
        tool_name=TOOL,
        max_credits=5,
        idem_key="authority-stale-permit",
    )
    factory = get_session_factory()
    async with factory() as session:
        await session.execute(
            update(PermitModel)
            .where(PermitModel.permit_id == stale["permit_id"])  # type: ignore[arg-type]
            .values(
                expires_at=datetime.now(timezone.utc).replace(tzinfo=None)
                - timedelta(minutes=5)
            )
        )
        await session.commit()

    resp = await client.get("/v1/me/authority", headers=provisioned["agent_headers"])
    assert resp.status_code == 200
    summary = resp.json()
    permit_ids = [p["permit_id"] for p in summary["active_permits"]]
    assert live["permit_id"] in permit_ids
    assert stale["permit_id"] not in permit_ids
    assert summary["active_permits_total"] == len(permit_ids)


@pytest.mark.anyio
async def test_me_authority_reports_totals_beyond_the_preview_cap(
    client, clean_database, registered_tool
):
    """More than 50 active permits: the preview is capped but the total says
    so, so a planner never mistakes truncation for absence."""
    from app.schemas.trust import PermitCreateRequest
    from app.services.permits import get_permit_service

    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    service = get_permit_service()
    for _ in range(52):
        await service.create_permit(
            PermitCreateRequest(
                issuer_wallet_id=wallet_id,
                subject_wallet_id=wallet_id,
                subject_key_id=provisioned["key_id"],
                allowed_tools=[TOOL],
                max_credits=1,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
            ),
            subject_key_id=provisioned["key_id"],
        )

    resp = await client.get("/v1/me/authority", headers=provisioned["agent_headers"])
    assert resp.status_code == 200
    summary = resp.json()
    assert len(summary["active_permits"]) == 50
    assert summary["active_permits_total"] == 52


@pytest.mark.anyio
async def test_annotations_are_honest_in_permissive_trust_mode(
    client, registered_tool, monkeypatch
):
    """A deployment that accepts ungoverned permit-less calls must not
    advertise governance guarantees it does not enforce — except for a
    require_permit tool, whose calls the router forces onto the governed
    path even in permissive mode."""
    settings = get_settings()
    monkeypatch.setattr(settings, "TRUST_MODE_ENABLED", True)
    monkeypatch.setattr(settings, "ALLOW_LEGACY_UNPERMITTED_MCP", True)

    registry = get_service_registry()
    registry.register_local(
        service_id="authority-permit-forced",
        name="Permit-Forced Tool",
        description="High-value tool that always requires a permit",
        category=ServiceCategory.AGENT_COMMS,
        func=lambda: {"ok": True},
        credits_per_unit=3.0,
        unit_name="call",
        require_permit=True,
    )
    try:
        resp = await client.get("/mcp/tools.json")
        tools = {t["name"]: t for t in resp.json()["tools"]}

        ann = tools[TOOL]["annotations"]
        assert ann["governed"] is False
        assert ann["receiptProvided"] is False
        assert ann["supportsIdempotency"] is False
        assert ann["approvalMayBeRequired"] is False
        # Pricing facts are configuration-independent.
        assert ann["economicAction"] is True

        forced = tools["authority-permit-forced"]["annotations"]
        assert forced["governed"] is True
        assert forced["receiptProvided"] is True
        assert forced["supportsIdempotency"] is True
        assert forced["approvalMayBeRequired"] is True
        assert forced["requirePermit"] is True
    finally:
        registry.unregister_local("authority-permit-forced")


def _priced_tool(**price_fields):
    from app.services.mcp_generator import McpGenerator

    return McpGenerator()._service_to_mcp_tool(
        {
            "service_id": "authority-priced",
            "description": "Tool under price normalization",
            "category": ServiceCategory.AGENT_COMMS.value,
            **price_fields,
        }
    )


@pytest.mark.parametrize(
    "price_fields",
    [
        # Exact field malformed in every way float() can fail or lie.
        {"credits_per_unit": 2.0, "credits_per_unit_exact": "nan"},
        {"credits_per_unit": 2.0, "credits_per_unit_exact": "inf"},
        {"credits_per_unit": 2.0, "credits_per_unit_exact": "-inf"},
        {"credits_per_unit": 2.0, "credits_per_unit_exact": "not-a-number"},
        # A non-numeric type raises TypeError rather than ValueError.
        {"credits_per_unit": 2.0, "credits_per_unit_exact": []},
        # An int too large for a float raises OverflowError, not ValueError.
        {"credits_per_unit": 2.0, "credits_per_unit_exact": 10**400},
        # No exact field: the same guards apply to credits_per_unit.
        {"credits_per_unit": float("nan")},
        {"credits_per_unit": "garbage"},
        {"credits_per_unit": 10**400},
    ],
)
def test_malformed_price_is_never_advertised_as_free(price_fields):
    """A price the plane cannot make sense of must fail conservative —
    advertising economicAction=false would tell an agent a consequential
    tool is free."""
    assert _priced_tool(**price_fields)["annotations"]["economicAction"] is True


def test_valid_zero_price_is_still_not_an_economic_action():
    """The conservative fallback must not swallow a legitimately free tool."""
    assert (
        _priced_tool(credits_per_unit=0.0, credits_per_unit_exact="0")["annotations"][
            "economicAction"
        ]
        is False
    )
    assert _priced_tool(credits_per_unit=0.0)["annotations"]["economicAction"] is False


def _strict_json(payload) -> str:
    """Serialize rejecting NaN/Infinity — i.e. as a strict RFC 8259 client would.

    ``json.dumps`` emits bare ``NaN``/``Infinity`` literals by default, which
    Go's ``encoding/json`` and Rust's ``serde`` refuse; ``allow_nan=False``
    surfaces that here instead of shipping an unparseable manifest.
    """
    import json

    return json.dumps(payload, allow_nan=False)


@pytest.mark.parametrize(
    "price_fields",
    [
        {"credits_per_unit": float("nan")},
        {"credits_per_unit": float("inf")},
        {"credits_per_unit": float("-inf")},
        {"credits_per_unit": 2.0, "credits_per_unit_exact": "nan"},
        {"credits_per_unit": 2.0, "credits_per_unit_exact": 10**400},
        {"credits_per_unit": 0.0, "credits_per_unit_exact": "nan"},
    ],
)
def test_public_manifest_stays_strict_json_for_malformed_prices(price_fields):
    """One bad price must not make the WHOLE manifest unparseable.

    NaN/Infinity are not valid JSON, so a strict client rejects the entire
    document — every other tool's discovery breaks with it. Exercised through
    the public generator, not the private converter.
    """
    registry = get_service_registry()
    registry.register_local(
        service_id="authority-malformed-price",
        name="Malformed Price Tool",
        description="Registry row carrying an unusable price",
        category=ServiceCategory.AGENT_COMMS,
        func=lambda: {"ok": True},
        unit_name="call",
        **{k: v for k, v in price_fields.items() if k == "credits_per_unit"},
    )
    try:
        record = registry.get_local("authority-malformed-price")
        if "credits_per_unit_exact" in price_fields:
            record["credits_per_unit_exact"] = price_fields["credits_per_unit_exact"]

        manifest = McpGenerator().generate_tools_json()
        # The whole manifest must serialize for a strict RFC 8259 client.
        _strict_json(manifest)

        ann = {t["name"]: t for t in manifest["tools"]}["authority-malformed-price"][
            "annotations"
        ]
        # The advertised price is the conservative fallback specifically —
        # not merely "some finite number". Pinning the value catches a
        # regression that reuses a declared 2.0 alongside a corrupt exact
        # price, which a finiteness-only assertion would wave through.
        assert isinstance(ann["creditsPerCall"], float)
        assert math.isfinite(ann["creditsPerCall"])
        assert ann["creditsPerCall"] == 1.0
        # An unusable exact price is omitted rather than published as garbage.
        assert "creditsPerCallExact" not in ann
        # Conservative: an unreadable price never reads as free.
        assert ann["economicAction"] is True
    finally:
        registry.unregister_local("authority-malformed-price")


def test_public_manifest_preserves_a_valid_exact_price():
    """Normalization must not disturb a well-formed exact decimal."""
    registry = get_service_registry()
    registry.register_local(
        service_id="authority-exact-price",
        name="Exact Price Tool",
        description="Registry row with a valid exact decimal price",
        category=ServiceCategory.AGENT_COMMS,
        func=lambda: {"ok": True},
        credits_per_unit=2.5,
        unit_name="call",
    )
    try:
        registry.get_local("authority-exact-price")["credits_per_unit_exact"] = "2.50"
        manifest = McpGenerator().generate_tools_json()
        _strict_json(manifest)
        ann = {t["name"]: t for t in manifest["tools"]}["authority-exact-price"][
            "annotations"
        ]
        assert ann["creditsPerCall"] == 2.5
        assert ann["creditsPerCallExact"] == "2.50"
        assert ann["economicAction"] is True
    finally:
        registry.unregister_local("authority-exact-price")


def test_approval_window_is_clamped_to_sentinel_bounds(monkeypatch):
    from app.services.human_approval import approval_window_seconds

    settings = get_settings()
    monkeypatch.setattr(settings, "SENTINEL_APPROVAL_TIMEOUT_SECONDS", 0)
    assert approval_window_seconds() == 1
    monkeypatch.setattr(settings, "SENTINEL_APPROVAL_TIMEOUT_SECONDS", 100_000)
    assert approval_window_seconds() == 86_400
    monkeypatch.setattr(settings, "SENTINEL_APPROVAL_TIMEOUT_SECONDS", 300)
    assert approval_window_seconds() == 300
