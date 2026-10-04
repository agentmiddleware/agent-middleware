from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.runtime_mode import is_simulation
from app.db.database import get_session_factory
from app.db.models import PolicyBundleModel
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.audit_log import list_audit_events
from app.services.policies import evaluate_wallet_policy
from app.services.pricing import PROOF_SURFACE_CATEGORIES
from app.services.service_registry import get_service_registry


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _wallet(client: AsyncClient, agent_id: str = "policy-agent") -> str:
    headers = {"X-API-Key": "test-key"}
    sponsor = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": f"{agent_id} sponsor",
            "email": f"{agent_id}@example.com",
            "initial_credits": 10000,
            "require_kyc": False,
        },
        headers=headers,
    )
    assert sponsor.status_code == 201
    agent = await client.post(
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": sponsor.json()["wallet_id"],
            "agent_id": agent_id,
            "budget_credits": 1000,
        },
        headers=headers,
    )
    assert agent.status_code == 201
    return agent.json()["wallet_id"]


@pytest.mark.anyio
async def test_policy_crud_requires_bootstrap_admin(client, clean_database):
    wallet_id = await _wallet(client, "policy-crud")
    created = await client.post(
        "/v1/policies",
        json={
            "wallet_id": wallet_id,
            "name": "Strict agent policy",
            "allowed_tools": ["policy-echo"],
            "allowed_service_categories": ["agent_comms"],
            "max_cost_per_action": 3,
        },
        headers={"X-API-Key": "test-key"},
    )
    assert created.status_code == 201
    policy = created.json()
    assert policy["policy_id"].startswith("polb-")

    listed = await client.get(
        f"/v1/policies?wallet_id={wallet_id}",
        headers={"X-API-Key": "test-key"},
    )
    assert listed.status_code == 200
    assert listed.json()["total"] == 1

    patched = await client.patch(
        f"/v1/policies/{policy['policy_id']}",
        json={"max_cost_per_action": 1, "is_active": False},
        headers={"X-API-Key": "test-key"},
    )
    assert patched.status_code == 200
    assert patched.json()["max_cost_per_action"] == 1.0
    assert patched.json()["is_active"] is False

    key = await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id, "key_name": "runtime"},
        headers={"X-API-Key": "test-key"},
    )
    assert key.status_code == 201
    denied = await client.get(
        f"/v1/policies?wallet_id={wallet_id}",
        headers={"X-API-Key": key.json()["api_key"]},
    )
    assert denied.status_code == 403


@pytest.mark.anyio
async def test_mcp_policy_denies_disallowed_tool_before_charge(client, clean_database):
    registry = get_service_registry()

    def blocked_tool() -> dict:
        return {"ran": True}

    registry.register_local(
        service_id="policy-blocked-tool",
        name="Policy Blocked Tool",
        description="Should be blocked by policy",
        category=ServiceCategory.AGENT_COMMS,
        func=blocked_tool,
        credits_per_unit=2.0,
        unit_name="call",
    )
    try:
        wallet_id = await _wallet(client, "policy-mcp")
        policy = await client.post(
            "/v1/policies",
            json={
                "wallet_id": wallet_id,
                "name": "Only another tool",
                "allowed_tools": ["some-other-tool"],
            },
            headers={"X-API-Key": "test-key"},
        )
        assert policy.status_code == 201

        response = await client.post(
            "/mcp/messages",
            json={
                "jsonrpc": "2.0",
                "id": "policy-deny-1",
                "method": "tools/call",
                "params": {
                    "name": "policy-blocked-tool",
                    "arguments": {},
                    "mcpContext": {"wallet_id": wallet_id},
                },
            },
            headers={"X-API-Key": "test-key"},
        )
        assert response.status_code == 200
        assert response.json()["error"]["message"] == "tool_not_allowed"

        ledger = await client.get(
            f"/v1/billing/ledger/{wallet_id}",
            headers={"X-API-Key": "test-key"},
        )
        assert ledger.status_code == 200
        assert all(
            "policy-blocked-tool" not in entry.get("description", "")
            for entry in ledger.json()["entries"]
        )

        events = await list_audit_events(
            wallet_id=wallet_id, tool="policy-blocked-tool"
        )
        assert len(events) == 1
        assert events[0].ok is False
        assert events[0].error == "tool_not_allowed"
        assert events[0].metadata["policy_id"] == policy.json()["policy_id"]
    finally:
        registry.unregister_local("policy-blocked-tool")


@pytest.mark.anyio
async def test_mcp_policy_enforces_daily_spend_limit(client, clean_database):
    registry = get_service_registry()
    registry.register_local(
        service_id="daily-cap-tool",
        name="Daily Cap Tool",
        description="Allowed by tool list but over the daily spend cap",
        category=ServiceCategory.AGENT_COMMS,
        func=lambda: {"ran": True},
        credits_per_unit=2.0,
        unit_name="call",
    )
    try:
        wallet_id = await _wallet(client, "policy-daily")
        policy = await client.post(
            "/v1/policies",
            json={
                "wallet_id": wallet_id,
                "name": "Tiny daily cap",
                "allowed_tools": ["daily-cap-tool"],
                "daily_spend_limit": 1,
            },
            headers={"X-API-Key": "test-key"},
        )
        assert policy.status_code == 201

        # The tool costs 2 credits; the very first call (0 used + 2) exceeds the
        # declared daily cap of 1, so it must be denied before any charge.
        response = await client.post(
            "/mcp/messages",
            json={
                "jsonrpc": "2.0",
                "id": "daily-cap-1",
                "method": "tools/call",
                "params": {
                    "name": "daily-cap-tool",
                    "arguments": {},
                    "mcpContext": {"wallet_id": wallet_id},
                },
            },
            headers={"X-API-Key": "test-key"},
        )
        assert response.status_code == 200
        assert response.json()["error"]["message"] == "daily_spend_limit_exceeded"

        ledger = await client.get(
            f"/v1/billing/ledger/{wallet_id}",
            headers={"X-API-Key": "test-key"},
        )
        assert all(
            "daily-cap-tool" not in entry.get("description", "")
            for entry in ledger.json()["entries"]
        )
    finally:
        registry.unregister_local("daily-cap-tool")


@pytest.mark.anyio
async def test_billing_policy_denies_disallowed_category(client, clean_database):
    wallet_id = await _wallet(client, "policy-billing")
    policy = await client.post(
        "/v1/policies",
        json={
            "wallet_id": wallet_id,
            "name": "No IoT",
            "allowed_service_categories": ["agent_comms"],
        },
        headers={"X-API-Key": "test-key"},
    )
    assert policy.status_code == 201

    response = await client.post(
        f"/v1/billing/charge?wallet_id={wallet_id}&service=iot_bridge&units=1",
        headers={"X-API-Key": "test-key", "X-Request-ID": "policy-billing-deny"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "service_category_not_allowed"

    events = await list_audit_events(
        wallet_id=wallet_id,
        request_id="policy-billing-deny",
    )
    assert len(events) == 1
    assert events[0].error == "service_category_not_allowed"
    assert events[0].metadata["policy_id"] == policy.json()["policy_id"]


@pytest.mark.anyio
async def test_billing_policy_denies_over_cost_charge(client, clean_database):
    wallet_id = await _wallet(client, "policy-billing-cost")
    policy = await client.post(
        "/v1/policies",
        json={
            "wallet_id": wallet_id,
            "name": "Cheap actions only",
            "allowed_service_categories": ["agent_comms"],
            "max_cost_per_action": 1,
        },
        headers={"X-API-Key": "test-key"},
    )
    assert policy.status_code == 201

    response = await client.post(
        f"/v1/billing/charge?wallet_id={wallet_id}&service=agent_comms&units=2",
        headers={"X-API-Key": "test-key", "X-Request-ID": "policy-cost-deny"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "max_cost_per_action_exceeded"


@pytest.mark.anyio
async def test_planner_policy_rejects_actions(client, clean_database):
    wallet_id = await _wallet(client, "policy-planner")
    policy = await client.post(
        "/v1/policies",
        json={
            "wallet_id": wallet_id,
            "name": "Only agent comms",
            "allowed_service_categories": ["agent_comms"],
        },
        headers={"X-API-Key": "test-key"},
    )
    assert policy.status_code == 201

    response = await client.post(
        "/v1/planner/optimize",
        json={
            "state": {
                "wallet_id": wallet_id,
                "agent_id": "a1",
                "task_id": "t1",
                "request_id": "policy-planner-1",
                "wallet_balance": 100,
                "daily_spend_used": 0,
                "daily_limit": 100,
                "rate_limit_headroom": 1,
                "service_health": {"iot_bridge": "healthy", "agent_comms": "healthy"},
                "simulation_flags": {"iot_bridge": False, "agent_comms": False},
                "auth_scope": ["invoke"],
                "task_context": {
                    "tier": "high",
                    "candidate_actions": [
                        {
                            "id": "bad-iot",
                            "service": "iot_bridge",
                            "credit_cost": 1,
                            "latency_ms": 10,
                            "risk_score": 0.01,
                            "expected_value": 10,
                            "reliability": 1,
                        },
                        {
                            "id": "good-comms",
                            "service": "agent_comms",
                            "credit_cost": 1,
                            "latency_ms": 10,
                            "risk_score": 0.01,
                            "expected_value": 5,
                            "reliability": 1,
                        },
                    ],
                },
                "remaining_budget": 10,
                "slo_window_seconds": 1,
            },
            "max_actions": 2,
        },
        headers={"X-API-Key": "test-key", "X-Request-ID": "policy-planner-1"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["policy_reasons"]["bad-iot"] == "service_category_not_allowed"
    assert body["rejected_actions"][0]["policy_id"] == policy.json()["policy_id"]
    assert body["governance"]["policy_ids"] == [policy.json()["policy_id"]]


@pytest.mark.anyio
async def test_policy_requiring_real_effects_denies_frozen_proof_surfaces(
    client, clean_database
):
    """
    A wallet policy demanding real effects must reject every frozen
    proof-surface category, because the tools registered under them are
    preview stubs rather than real integrations.

    This pins the coupling app/routers/mcp.py depends on: runtime_mode reports
    the category as simulated, and evaluate_wallet_policy turns that into a
    denial. While protocol_gen and sandbox carried no SIMULATION_MODE_* flag,
    is_simulation raised UnknownServiceError, the invoke path fell back to
    simulation=False, and this denial silently never fired for them.

    is_simulation() is called rather than hardcoding True so that dropping a
    category from _SERVICE_TO_SETTING fails here too.
    """
    wallet_id = await _wallet(client, "real-effects")
    created = await client.post(
        "/v1/policies",
        json={
            "wallet_id": wallet_id,
            "name": "Real effects only",
            "require_real_effects": True,
        },
        headers={"X-API-Key": "test-key"},
    )
    assert created.status_code == 201

    for category in sorted(c.value for c in PROOF_SURFACE_CATEGORIES):
        evaluation = await evaluate_wallet_policy(
            wallet_id=wallet_id,
            tool_name="preview-stub",
            service_category=category,
            simulation=is_simulation(category),
        )
        assert evaluation.allowed is False, category
        assert evaluation.reason == "real_effects_required", category


# --- Corrupt list columns fail closed ---------------------------------------
#
# A NULL allowed_tools_json / allowed_service_categories_json means "no
# restriction on this dimension". A value that is present but is not a JSON
# array of strings used to decode to None as well, so a corrupted restrictive
# bundle silently allowed every tool (or category). These pin the fail-closed
# reading: present-but-undecodable is a denial, never an unrestricted bundle.

# Values the API never writes (it stores json.dumps(list[str]) or NULL). The
# first four used to read as "no restriction"; the non-string lists read as an
# allowlist of things that are not tool names.
_CORRUPT_LIST_VALUES = [
    "not-json",
    '{"a": 1}',
    '"policy-echo"',
    "null",
    "[1, 2]",
    '["policy-echo", null]',
]


async def _set_policy_column(policy_id: str, column: str, value: str | None) -> None:
    factory = get_session_factory()
    async with factory() as session:
        row = await session.get(PolicyBundleModel, policy_id)
        assert row is not None
        setattr(row, column, value)
        session.add(row)
        await session.commit()


async def _restrictive_policy(client: AsyncClient, wallet_id: str) -> str:
    created = await client.post(
        "/v1/policies",
        json={
            "wallet_id": wallet_id,
            "name": "Echo only, comms only",
            "allowed_tools": ["policy-echo"],
            "allowed_service_categories": ["agent_comms"],
        },
        headers={"X-API-Key": "test-key"},
    )
    assert created.status_code == 201
    return created.json()["policy_id"]


@pytest.mark.anyio
@pytest.mark.parametrize("corrupt", _CORRUPT_LIST_VALUES)
async def test_corrupt_allowed_tools_column_denies(client, clean_database, corrupt):
    wallet_id = await _wallet(client, "policy-corrupt-tools")
    policy_id = await _restrictive_policy(client, wallet_id)
    await _set_policy_column(policy_id, "allowed_tools_json", corrupt)

    # Even the tool the bundle meant to allow is refused: the allowlist can no
    # longer be read, so nothing it would have permitted can be shown.
    for tool_name in ("policy-echo", "anything-else"):
        evaluation = await evaluate_wallet_policy(
            wallet_id=wallet_id,
            tool_name=tool_name,
            service_category="agent_comms",
        )
        assert evaluation.allowed is False, tool_name
        assert evaluation.reason == "policy_constraint_corrupt", tool_name
        assert evaluation.policy_id == policy_id
        evaluated = evaluation.evaluated_constraints["evaluated"][-1]
        assert evaluated["corrupt_constraint"] == "allowed_tools"
        # The undecodable value itself is never echoed into audit metadata.
        assert corrupt not in str(evaluation.evaluated_constraints)


@pytest.mark.anyio
@pytest.mark.parametrize("corrupt", [*_CORRUPT_LIST_VALUES, "[null]"])
async def test_corrupt_allowed_categories_column_denies(
    client, clean_database, corrupt
):
    wallet_id = await _wallet(client, "policy-corrupt-cats")
    policy_id = await _restrictive_policy(client, wallet_id)
    await _set_policy_column(policy_id, "allowed_service_categories_json", corrupt)

    # service_category=None matters for "[null]": None in [None] used to pass.
    for category in ("agent_comms", "iot_bridge", None):
        evaluation = await evaluate_wallet_policy(
            wallet_id=wallet_id,
            tool_name="policy-echo",
            service_category=category,
        )
        assert evaluation.allowed is False, category
        assert evaluation.reason == "policy_constraint_corrupt", category
        assert evaluation.policy_id == policy_id
        evaluated = evaluation.evaluated_constraints["evaluated"][-1]
        assert evaluated["corrupt_constraint"] == "allowed_service_categories"


@pytest.mark.anyio
async def test_null_list_columns_still_mean_no_restriction(client, clean_database):
    wallet_id = await _wallet(client, "policy-null-lists")
    policy_id = await _restrictive_policy(client, wallet_id)
    await _set_policy_column(policy_id, "allowed_tools_json", None)
    await _set_policy_column(policy_id, "allowed_service_categories_json", None)

    evaluation = await evaluate_wallet_policy(
        wallet_id=wallet_id,
        tool_name="any-tool",
        service_category="iot_bridge",
    )
    assert evaluation.allowed is True
    assert evaluation.reason == "allowed"

    # A well-formed list keeps its ordinary allowlist meaning.
    await _set_policy_column(policy_id, "allowed_tools_json", '["policy-echo"]')
    allowed = await evaluate_wallet_policy(wallet_id=wallet_id, tool_name="policy-echo")
    assert allowed.allowed is True
    denied = await evaluate_wallet_policy(wallet_id=wallet_id, tool_name="other-tool")
    assert denied.allowed is False
    assert denied.reason == "tool_not_allowed"


@pytest.mark.anyio
async def test_corrupt_bundle_on_one_wallet_does_not_touch_another(
    client, clean_database
):
    corrupt_wallet = await _wallet(client, "policy-corrupt-a")
    other_wallet = await _wallet(client, "policy-corrupt-b")
    policy_id = await _restrictive_policy(client, corrupt_wallet)
    await _set_policy_column(policy_id, "allowed_tools_json", "not-json")

    other = await evaluate_wallet_policy(
        wallet_id=other_wallet,
        tool_name="policy-echo",
        service_category="agent_comms",
    )
    assert other.allowed is True
    assert other.policy_id is None


@pytest.mark.anyio
async def test_corrupt_bundle_reads_back_as_deny_all_not_unrestricted(
    client, clean_database
):
    """The read path stays tolerant (no 500), but it must not report a corrupt
    allowlist as null: null means "unrestricted", and a consumer of the
    response model (the enterprise IGA bridge) enforces exactly what it says."""
    wallet_id = await _wallet(client, "policy-corrupt-read")
    policy_id = await _restrictive_policy(client, wallet_id)
    await _set_policy_column(policy_id, "allowed_tools_json", "not-json")
    await _set_policy_column(policy_id, "allowed_service_categories_json", '{"a": 1}')

    got = await client.get(
        f"/v1/policies/{policy_id}", headers={"X-API-Key": "test-key"}
    )
    assert got.status_code == 200
    assert got.json()["allowed_tools"] == []
    assert got.json()["allowed_service_categories"] == []

    listed = await client.get(
        f"/v1/policies?wallet_id={wallet_id}", headers={"X-API-Key": "test-key"}
    )
    assert listed.status_code == 200
    assert listed.json()["policies"][0]["allowed_tools"] == []


@pytest.mark.anyio
async def test_wallet_key_cannot_clear_a_corrupt_restriction(client, clean_database):
    """Unauthorized path: the agent the bundle restricts cannot turn the
    corrupt column into NULL (unrestricted) itself."""
    wallet_id = await _wallet(client, "policy-corrupt-unauth")
    policy_id = await _restrictive_policy(client, wallet_id)
    await _set_policy_column(policy_id, "allowed_tools_json", "not-json")

    key = await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id, "key_name": "runtime"},
        headers={"X-API-Key": "test-key"},
    )
    assert key.status_code == 201
    patched = await client.patch(
        f"/v1/policies/{policy_id}",
        json={"allowed_tools": None},
        headers={"X-API-Key": key.json()["api_key"]},
    )
    assert patched.status_code == 403

    evaluation = await evaluate_wallet_policy(
        wallet_id=wallet_id,
        tool_name="policy-echo",
        service_category="agent_comms",
    )
    assert evaluation.allowed is False
    assert evaluation.reason == "policy_constraint_corrupt"


@pytest.mark.anyio
async def test_mcp_corrupt_policy_denies_before_charge(client, clean_database):
    registry = get_service_registry()
    registry.register_local(
        service_id="policy-echo",
        name="Policy Echo",
        description="Allowed by the bundle until its allowlist was corrupted",
        category=ServiceCategory.AGENT_COMMS,
        func=lambda: {"ran": True},
        credits_per_unit=2.0,
        unit_name="call",
    )
    try:
        wallet_id = await _wallet(client, "policy-corrupt-mcp")
        policy_id = await _restrictive_policy(client, wallet_id)
        await _set_policy_column(policy_id, "allowed_tools_json", "not-json")

        response = await client.post(
            "/mcp/messages",
            json={
                "jsonrpc": "2.0",
                "id": "policy-corrupt-1",
                "method": "tools/call",
                "params": {
                    "name": "policy-echo",
                    "arguments": {},
                    "mcpContext": {"wallet_id": wallet_id},
                },
            },
            headers={"X-API-Key": "test-key"},
        )
        assert response.status_code == 200
        assert response.json()["error"]["message"] == "policy_constraint_corrupt"

        ledger = await client.get(
            f"/v1/billing/ledger/{wallet_id}",
            headers={"X-API-Key": "test-key"},
        )
        assert ledger.status_code == 200
        assert all(
            "policy-echo" not in entry.get("description", "")
            for entry in ledger.json()["entries"]
        )

        events = await list_audit_events(wallet_id=wallet_id, tool="policy-echo")
        assert len(events) == 1
        assert events[0].ok is False
        assert events[0].error == "policy_constraint_corrupt"
        assert events[0].metadata["policy_id"] == policy_id
    finally:
        registry.unregister_local("policy-echo")


# policy_bundles stores both limits as Numeric(18, 8) (ten integer digits),
# name as String(255) and risk_tier as String(20). Starlette parses the bare
# JSON literals Infinity/-Infinity/NaN, and ge=0 alone let +Infinity through.
UNSTORABLE_LIMITS = [float("inf"), float("-inf"), float("nan"), 1e20, 1e10]
UNSTORABLE_LIMIT_IDS = ["Infinity", "-Infinity", "NaN", "1e20", "1e10"]
# The largest float the bounds accept; it still fits Numeric(18, 8).
LARGEST_STORABLE_LIMIT = 9_999_999_999.0
ADMIN = {"X-API-Key": "test-key"}


def _raw_json(payload: dict) -> dict:
    """httpx kwargs that send Infinity/NaN as the bare JSON literals."""
    return {
        "content": json.dumps(payload),
        "headers": {**ADMIN, "Content-Type": "application/json"},
    }


async def _policy_total(client: AsyncClient, wallet_id: str) -> int:
    listed = await client.get(f"/v1/policies?wallet_id={wallet_id}", headers=ADMIN)
    assert listed.status_code == 200
    return listed.json()["total"]


@pytest.mark.anyio
@pytest.mark.parametrize("field", ["max_cost_per_action", "daily_spend_limit"])
@pytest.mark.parametrize("value", UNSTORABLE_LIMITS, ids=UNSTORABLE_LIMIT_IDS)
async def test_policy_create_refuses_unstorable_limits(
    client, clean_database, field, value
):
    wallet_id = await _wallet(client, "policy-unstorable")
    resp = await client.post(
        "/v1/policies",
        **_raw_json({"wallet_id": wallet_id, "name": "Unstorable", field: value}),
    )
    assert resp.status_code == 422, resp.text
    assert await _policy_total(client, wallet_id) == 0


@pytest.mark.anyio
@pytest.mark.parametrize("field", ["max_cost_per_action", "daily_spend_limit"])
@pytest.mark.parametrize("value", UNSTORABLE_LIMITS, ids=UNSTORABLE_LIMIT_IDS)
async def test_policy_patch_refuses_unstorable_limits(
    client, clean_database, field, value
):
    wallet_id = await _wallet(client, "policy-unstorable-patch")
    created = await client.post(
        "/v1/policies",
        json={
            "wallet_id": wallet_id,
            "name": "Patched",
            "max_cost_per_action": 3,
            "daily_spend_limit": 30,
        },
        headers=ADMIN,
    )
    assert created.status_code == 201
    policy_id = created.json()["policy_id"]

    resp = await client.patch(f"/v1/policies/{policy_id}", **_raw_json({field: value}))
    assert resp.status_code == 422, resp.text
    stored = await client.get(f"/v1/policies/{policy_id}", headers=ADMIN)
    assert stored.json()["max_cost_per_action"] == 3.0
    assert stored.json()["daily_spend_limit"] == 30.0


@pytest.mark.anyio
@pytest.mark.parametrize(
    "overrides",
    [
        {"risk_tier": "HIGH"},
        {"risk_tier": "med"},
        {"risk_tier": "x" * 300},
        {"name": "n" * 256},
    ],
    ids=["tier-uppercase", "tier-abbreviated", "tier-300-chars", "name-256-chars"],
)
async def test_policy_refuses_unknown_risk_tier_and_overlong_name(
    client, clean_database, overrides
):
    """risk_tier is one of the planner's tiers (low/medium/high); anything
    else never matched a requested tier, and over 20 characters it failed
    the Postgres insert (500). name is String(255)."""
    wallet_id = await _wallet(client, "policy-tier")
    resp = await client.post(
        "/v1/policies",
        json={"wallet_id": wallet_id, "name": "Tiered", **overrides},
        headers=ADMIN,
    )
    assert resp.status_code == 422, resp.text
    assert await _policy_total(client, wallet_id) == 0

    created = await client.post(
        "/v1/policies",
        json={"wallet_id": wallet_id, "name": "Tiered", "risk_tier": "low"},
        headers=ADMIN,
    )
    assert created.status_code == 201
    policy_id = created.json()["policy_id"]
    patched = await client.patch(
        f"/v1/policies/{policy_id}", json=overrides, headers=ADMIN
    )
    assert patched.status_code == 422, patched.text
    stored = await client.get(f"/v1/policies/{policy_id}", headers=ADMIN)
    assert stored.json()["risk_tier"] == "low"
    assert stored.json()["name"] == "Tiered"


@pytest.mark.anyio
async def test_policy_fields_at_storage_limits_are_accepted(client, clean_database):
    wallet_id = await _wallet(client, "policy-limits")
    created = await client.post(
        "/v1/policies",
        json={
            "wallet_id": wallet_id,
            "name": "n" * 255,
            "risk_tier": "high",
            "max_cost_per_action": LARGEST_STORABLE_LIMIT,
            "daily_spend_limit": 0,
        },
        headers=ADMIN,
    )
    assert created.status_code == 201, created.text
    policy = created.json()
    assert policy["name"] == "n" * 255
    assert policy["risk_tier"] == "high"
    assert policy["max_cost_per_action"] == LARGEST_STORABLE_LIMIT
    assert policy["daily_spend_limit"] == 0.0

    patched = await client.patch(
        f"/v1/policies/{policy['policy_id']}",
        json={
            "risk_tier": "medium",
            "daily_spend_limit": LARGEST_STORABLE_LIMIT,
        },
        headers=ADMIN,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["risk_tier"] == "medium"
    assert patched.json()["daily_spend_limit"] == LARGEST_STORABLE_LIMIT
