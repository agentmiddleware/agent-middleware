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
async def test_nan_estimated_cost_denies_instead_of_crashing(client, clean_database):
    wallet_id = await _capped_wallet(client, "policy-nan-cost")
    evaluation = await evaluate_wallet_policy(
        wallet_id=wallet_id,
        tool_name="some-tool",
        estimated_cost=float("nan"),
        daily_spend_used=0.0,
    )
    assert evaluation.allowed is False
    assert evaluation.reason == "policy_cost_invalid"


@pytest.mark.anyio
async def test_nan_daily_spend_denies_instead_of_crashing(client, clean_database):
    wallet_id = await _capped_wallet(client, "policy-nan-daily")
    evaluation = await evaluate_wallet_policy(
        wallet_id=wallet_id,
        tool_name="some-tool",
        estimated_cost=1.0,
        daily_spend_used=float("nan"),
    )
    assert evaluation.allowed is False
    assert evaluation.reason == "policy_cost_invalid"


@pytest.mark.anyio
async def test_decimal_nan_inputs_deny(client, clean_database):
    wallet_id = await _capped_wallet(client, "policy-decimal-nan")
    evaluation = await evaluate_wallet_policy(
        wallet_id=wallet_id,
        tool_name="some-tool",
        estimated_cost=Decimal("NaN"),
        daily_spend_used=Decimal("NaN"),
    )
    assert evaluation.allowed is False
    assert evaluation.reason == "policy_cost_invalid"


@pytest.mark.anyio
async def test_negative_infinity_cost_denies_instead_of_passing_caps(
    client, clean_database
):
    # -Infinity compares smaller than every cap, so without the guard it
    # sailed through both monetary gates with reason "allowed".
    wallet_id = await _capped_wallet(client, "policy-neginf-cost")
    evaluation = await evaluate_wallet_policy(
        wallet_id=wallet_id,
        tool_name="some-tool",
        estimated_cost=float("-inf"),
        daily_spend_used=0.0,
    )
    assert evaluation.allowed is False
    assert evaluation.reason == "policy_cost_invalid"


@pytest.mark.anyio
async def test_positive_infinity_cost_denies_with_invalid_reason(
    client, clean_database
):
    wallet_id = await _capped_wallet(client, "policy-inf-cost")
    evaluation = await evaluate_wallet_policy(
        wallet_id=wallet_id,
        tool_name="some-tool",
        estimated_cost=float("inf"),
        daily_spend_used=0.0,
    )
    assert evaluation.allowed is False
    assert evaluation.reason == "policy_cost_invalid"


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
async def test_unknown_cost_none_keeps_skip_the_check_behavior(client, clean_database):
    # None means "unknown, nothing to check" and stays allowed; only
    # malformed non-finite values deny.
    wallet_id = await _capped_wallet(client, "policy-none-cost")
    evaluation = await evaluate_wallet_policy(
        wallet_id=wallet_id, tool_name="some-tool", estimated_cost=None
    )
    assert evaluation.allowed is True
    assert evaluation.reason == "allowed"


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
