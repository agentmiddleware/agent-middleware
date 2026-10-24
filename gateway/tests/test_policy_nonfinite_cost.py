"""Non-finite money inputs must deny, never crash or bypass policy caps.

Regression tests for the fail-closed guard in
``app.services.policies.evaluate_wallet_policy``: a NaN estimated cost or
daily spend used to raise ``decimal.InvalidOperation`` out of the evaluator
(an unclassified 500 with no denial recorded), and a negative-infinity cost
is smaller than every cap so it silently passed both monetary gates. Both
now deny with ``policy_cost_invalid``. All HTTP boundaries already refuse
non-finite numbers, so this guard is defense in depth at the trust plane
for direct service callers and future inputs.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.database import get_session_factory
from app.db.models import PolicyBundleModel
from app.main import app
from app.services.policies import evaluate_wallet_policy


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _wallet(client: AsyncClient, agent_id: str) -> str:
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


async def _capped_wallet(client: AsyncClient, agent_id: str) -> str:
    wallet_id = await _wallet(client, agent_id)
    created = await client.post(
        "/v1/policies",
        json={
            "wallet_id": wallet_id,
            "name": "Capped",
            "max_cost_per_action": 5,
            "daily_spend_limit": 50,
        },
        headers={"X-API-Key": "test-key"},
    )
    assert created.status_code == 201
    return wallet_id


@pytest.mark.anyio
@pytest.mark.parametrize("field", ["estimated_cost", "daily_spend_used"])
@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ],
)
async def test_nonfinite_money_denies_without_comparison(
    client, clean_database, field, value
):
    wallet_id = await _capped_wallet(client, "policy-nonfinite")
    money = {"estimated_cost": 1.0, "daily_spend_used": 0.0}
    money[field] = value
    evaluation = await evaluate_wallet_policy(
        wallet_id=wallet_id, tool_name="some-tool", **money
    )
    assert evaluation.allowed is False
    assert evaluation.reason == "policy_cost_invalid"
    assert evaluation.policy_id is None
    assert evaluation.evaluated_constraints == {"policy_count": 1}


@pytest.mark.anyio
async def test_finite_costs_keep_existing_allow_and_deny_behavior(
    client, clean_database
):
    wallet_id = await _capped_wallet(client, "policy-finite-cost")
    allowed = await evaluate_wallet_policy(
        wallet_id=wallet_id,
        tool_name="some-tool",
        estimated_cost=2.0,
        daily_spend_used=10.0,
    )
    assert allowed.allowed is True
    assert allowed.reason == "allowed"

    at_action_cap = await evaluate_wallet_policy(
        wallet_id=wallet_id,
        tool_name="some-tool",
        estimated_cost=Decimal("5"),
        daily_spend_used=0.0,
    )
    assert at_action_cap.allowed is True

    at_daily_cap = await evaluate_wallet_policy(
        wallet_id=wallet_id,
        tool_name="some-tool",
        estimated_cost=Decimal("2"),
        daily_spend_used=Decimal("48"),
    )
    assert at_daily_cap.allowed is True

    over_max = await evaluate_wallet_policy(
        wallet_id=wallet_id,
        tool_name="some-tool",
        estimated_cost=6.0,
        daily_spend_used=0.0,
    )
    assert over_max.allowed is False
    assert over_max.reason == "max_cost_per_action_exceeded"

    over_daily = await evaluate_wallet_policy(
        wallet_id=wallet_id,
        tool_name="some-tool",
        estimated_cost=2.0,
        daily_spend_used=49.0,
    )
    assert over_daily.allowed is False
    assert over_daily.reason == "daily_spend_limit_exceeded"


@pytest.mark.anyio
async def test_unknown_cost_and_daily_spend_keep_current_contract(
    client, clean_database
):
    wallet_id = await _capped_wallet(client, "policy-none-cost")
    evaluation = await evaluate_wallet_policy(
        wallet_id=wallet_id,
        tool_name="some-tool",
        estimated_cost=None,
        daily_spend_used=0,
    )
    assert evaluation.allowed is True
    assert evaluation.reason == "allowed"

    missing_daily = await evaluate_wallet_policy(
        wallet_id=wallet_id,
        tool_name="some-tool",
        estimated_cost=None,
        daily_spend_used=None,
    )
    assert missing_daily.allowed is False
    assert missing_daily.reason == "daily_spend_unknown"


@pytest.mark.anyio
async def test_nonfinite_cost_with_no_bundles_stays_allowed(client, clean_database):
    # With no policy bundles there are no caps to evaluate, matching the
    # documented no-bundles-allow default.
    wallet_id = await _wallet(client, "policy-nan-nobundle")
    evaluation = await evaluate_wallet_policy(
        wallet_id=wallet_id,
        tool_name="some-tool",
        estimated_cost=float("nan"),
    )
    assert evaluation.allowed is True
    assert evaluation.reason == "allowed"

    daily_nonfinite = await evaluate_wallet_policy(
        wallet_id=wallet_id,
        tool_name="some-tool",
        daily_spend_used=Decimal("sNaN"),
    )
    assert daily_nonfinite.allowed is True
    assert daily_nonfinite.reason == "allowed"


@pytest.mark.anyio
async def test_explicit_empty_allowlist_denies_every_tool(client, clean_database):
    # Behavior pin for the "empty policy" case: an explicit empty allowlist
    # is deny-all, never unrestricted (NULL is the only unrestricted value).
    wallet_id = await _wallet(client, "policy-empty-allowlist")
    created = await client.post(
        "/v1/policies",
        json={
            "wallet_id": wallet_id,
            "name": "Deny all tools",
            "allowed_tools": [],
        },
        headers={"X-API-Key": "test-key"},
    )
    assert created.status_code == 201
    for tool_name in ("some-tool", ""):
        evaluation = await evaluate_wallet_policy(
            wallet_id=wallet_id, tool_name=tool_name
        )
        assert evaluation.allowed is False, tool_name
        assert evaluation.reason == "tool_not_allowed", tool_name


@pytest.mark.anyio
async def test_corrupt_allowlist_still_denies(client, clean_database):
    wallet_id = await _wallet(client, "policy-corrupt-allowlist")
    created = await client.post(
        "/v1/policies",
        json={"wallet_id": wallet_id, "name": "Tools", "allowed_tools": ["some-tool"]},
        headers={"X-API-Key": "test-key"},
    )
    assert created.status_code == 201
    factory = get_session_factory()
    async with factory() as session:
        policy = await session.get(PolicyBundleModel, created.json()["policy_id"])
        assert policy is not None
        policy.allowed_tools_json = "not-json"
        await session.commit()

    evaluation = await evaluate_wallet_policy(
        wallet_id=wallet_id, tool_name="some-tool"
    )
    assert evaluation.allowed is False
    assert evaluation.reason == "policy_constraint_corrupt"


@pytest.mark.anyio
async def test_risk_ceiling_and_approval_gate_still_apply(client, clean_database):
    wallet_id = await _wallet(client, "policy-risk-approval")
    created = await client.post(
        "/v1/policies",
        json={
            "wallet_id": wallet_id,
            "name": "Low risk with approval",
            "risk_tier": "low",
            "human_approval_required": True,
        },
        headers={"X-API-Key": "test-key"},
    )
    assert created.status_code == 201

    no_gate = await evaluate_wallet_policy(
        wallet_id=wallet_id, tool_name="some-tool", risk_tier="low"
    )
    assert no_gate.allowed is False
    assert no_gate.reason == "human_approval_required"

    high_risk = await evaluate_wallet_policy(
        wallet_id=wallet_id,
        tool_name="some-tool",
        risk_tier="high",
        approval_gate_active=True,
    )
    assert high_risk.allowed is False
    assert high_risk.reason == "risk_tier_not_allowed"

    approved = await evaluate_wallet_policy(
        wallet_id=wallet_id,
        tool_name="some-tool",
        risk_tier="low",
        approval_gate_active=True,
    )
    assert approved.allowed is True
    assert (
        approved.evaluated_constraints["evaluated"][0][
            "human_approval_satisfied_by_gate"
        ]
        is True
    )
