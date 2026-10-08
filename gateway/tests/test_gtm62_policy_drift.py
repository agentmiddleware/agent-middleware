"""gtm-62 drift backports: risk-tier ceiling and fail-closed daily cap.

The gateway copy compared risk tiers by exact match and only recorded the
mismatch (it never denied), and it skipped the daily spend cap whenever past
spending or the cost estimate was unknown. These tests pin the canonical
behavior: a bundle's tier is a low < medium < high ceiling, unknown tier
labels fail closed, and a cap that cannot be checked denies instead of
passing.
"""

from __future__ import annotations

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


async def _bundle(
    client: AsyncClient,
    wallet_id: str,
    *,
    risk_tier: str = "medium",
    daily_spend_limit: float | None = None,
) -> str:
    created = await client.post(
        "/v1/policies",
        json={
            "wallet_id": wallet_id,
            "name": "Drift fixture",
            "allowed_tools": ["drift-tool"],
            "risk_tier": risk_tier,
            "daily_spend_limit": daily_spend_limit,
        },
        headers={"X-API-Key": "test-key"},
    )
    assert created.status_code == 201
    return created.json()["policy_id"]


async def _set_risk_column(policy_id: str, value: str) -> None:
    factory = get_session_factory()
    async with factory() as session:
        row = await session.get(PolicyBundleModel, policy_id)
        assert row is not None
        row.risk_tier = value
        session.add(row)
        await session.commit()


@pytest.mark.anyio
async def test_low_risk_call_passes_under_high_ceiling(client, clean_database):
    wallet_id = await _wallet(client, "tier-ceiling-low")
    await _bundle(client, wallet_id, risk_tier="high")
    evaluation = await evaluate_wallet_policy(
        wallet_id=wallet_id, tool_name="drift-tool", risk_tier="low"
    )
    assert evaluation.allowed is True
    assert evaluation.reason == "allowed"


@pytest.mark.anyio
async def test_equal_tier_passes(client, clean_database):
    wallet_id = await _wallet(client, "tier-ceiling-equal")
    await _bundle(client, wallet_id, risk_tier="medium")
    evaluation = await evaluate_wallet_policy(
        wallet_id=wallet_id, tool_name="drift-tool", risk_tier="medium"
    )
    assert evaluation.allowed is True
    assert evaluation.reason == "allowed"


@pytest.mark.anyio
async def test_high_risk_call_denied_under_low_ceiling(client, clean_database):
    wallet_id = await _wallet(client, "tier-ceiling-high")
    policy_id = await _bundle(client, wallet_id, risk_tier="low")
    evaluation = await evaluate_wallet_policy(
        wallet_id=wallet_id, tool_name="drift-tool", risk_tier="high"
    )
    assert evaluation.allowed is False
    assert evaluation.reason == "risk_tier_not_allowed"
    assert evaluation.policy_id == policy_id
    evaluated = evaluation.evaluated_constraints["evaluated"][-1]
    assert evaluated["requested_risk_tier"] == "high"


@pytest.mark.anyio
async def test_unknown_request_tier_fails_closed(client, clean_database):
    wallet_id = await _wallet(client, "tier-unknown-request")
    await _bundle(client, wallet_id, risk_tier="high")
    evaluation = await evaluate_wallet_policy(
        wallet_id=wallet_id, tool_name="drift-tool", risk_tier="critical"
    )
    assert evaluation.allowed is False
    assert evaluation.reason == "risk_tier_not_allowed"


@pytest.mark.anyio
async def test_unknown_stored_ceiling_fails_closed(client, clean_database):
    wallet_id = await _wallet(client, "tier-unknown-stored")
    policy_id = await _bundle(client, wallet_id, risk_tier="medium")
    await _set_risk_column(policy_id, "bogus")
    evaluation = await evaluate_wallet_policy(
        wallet_id=wallet_id, tool_name="drift-tool", risk_tier="low"
    )
    assert evaluation.allowed is False
    assert evaluation.reason == "risk_tier_not_allowed"


@pytest.mark.anyio
async def test_missing_request_tier_does_not_deny(client, clean_database):
    # Callers that carry no tier information keep the old behavior: a
    # bundle ceiling only constrains calls that declare a tier.
    wallet_id = await _wallet(client, "tier-no-request")
    await _bundle(client, wallet_id, risk_tier="low")
    evaluation = await evaluate_wallet_policy(
        wallet_id=wallet_id, tool_name="drift-tool"
    )
    assert evaluation.allowed is True
    assert evaluation.reason == "allowed"


@pytest.mark.anyio
async def test_unknown_past_spend_denies_under_cap(client, clean_database):
    wallet_id = await _wallet(client, "cap-unknown-spend")
    policy_id = await _bundle(client, wallet_id, daily_spend_limit=100)
    evaluation = await evaluate_wallet_policy(
        wallet_id=wallet_id,
        tool_name="drift-tool",
        estimated_cost=10,
        daily_spend_used=None,
    )
    assert evaluation.allowed is False
    assert evaluation.reason == "daily_spend_unknown"
    assert evaluation.policy_id == policy_id


@pytest.mark.anyio
async def test_unknown_estimate_denies_under_cap(client, clean_database):
    wallet_id = await _wallet(client, "cap-unknown-estimate")
    await _bundle(client, wallet_id, daily_spend_limit=100)
    evaluation = await evaluate_wallet_policy(
        wallet_id=wallet_id,
        tool_name="drift-tool",
        estimated_cost=None,
        daily_spend_used=5,
    )
    assert evaluation.allowed is False
    assert evaluation.reason == "daily_spend_unknown"


@pytest.mark.anyio
async def test_known_spend_under_cap_allowed(client, clean_database):
    wallet_id = await _wallet(client, "cap-known-ok")
    await _bundle(client, wallet_id, daily_spend_limit=100)
    evaluation = await evaluate_wallet_policy(
        wallet_id=wallet_id,
        tool_name="drift-tool",
        estimated_cost=10,
        daily_spend_used=5,
    )
    assert evaluation.allowed is True
    assert evaluation.reason == "allowed"


@pytest.mark.anyio
async def test_known_spend_over_cap_denied(client, clean_database):
    wallet_id = await _wallet(client, "cap-known-over")
    await _bundle(client, wallet_id, daily_spend_limit=100)
    evaluation = await evaluate_wallet_policy(
        wallet_id=wallet_id,
        tool_name="drift-tool",
        estimated_cost=10,
        daily_spend_used=95,
    )
    assert evaluation.allowed is False
    assert evaluation.reason == "daily_spend_limit_exceeded"


@pytest.mark.anyio
async def test_no_cap_unknown_spend_allowed(client, clean_database):
    # Without a daily cap there is nothing to check, so unknown spend
    # figures must not deny.
    wallet_id = await _wallet(client, "cap-absent")
    await _bundle(client, wallet_id)
    evaluation = await evaluate_wallet_policy(
        wallet_id=wallet_id,
        tool_name="drift-tool",
        estimated_cost=None,
        daily_spend_used=None,
    )
    assert evaluation.allowed is True
    assert evaluation.reason == "allowed"
