"""
Tests for Pillar 10: Sub-Wallet Provisioning (Swarm Delegation).
Validates hierarchical child wallets, spend caps, and reclaim.
"""

import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app
from tests.test_trust_helpers import BOOTSTRAP_HEADERS, provision_agent_wallet


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


HEADERS = {"X-API-Key": "test-key"}


async def _create_sponsor_and_agent(client):
    """Helper: create sponsor + agent wallet chain."""
    sponsor = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Swarm Corp",
            "email": "swarm@test.com",
            "initial_credits": 100000.0,
        },
        headers=HEADERS,
    )
    sponsor_id = sponsor.json()["wallet_id"]

    agent = await client.post(
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": sponsor_id,
            "agent_id": "master-builder-01",
            "budget_credits": 50000.0,
        },
        headers=HEADERS,
    )
    agent_id = agent.json()["wallet_id"]

    return sponsor_id, agent_id


@pytest.mark.anyio
async def test_create_child_wallet(client):
    """Agent can spawn a child wallet."""
    _, agent_id = await _create_sponsor_and_agent(client)

    resp = await client.post(
        "/v1/billing/wallets/child",
        json={
            "parent_wallet_id": agent_id,
            "child_agent_id": "code-writer-01",
            "budget_credits": 2000.0,
            "max_spend": 2000.0,
            "task_description": "Write unit tests",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["wallet_type"] == "child"
    assert data["balance"] == 2000.0
    assert data["max_spend"] == 2000.0
    assert data["child_agent_id"] == "code-writer-01"


@pytest.mark.anyio
async def test_child_wallet_deducts_from_parent(client):
    """Spawning a child deducts credits from parent."""
    _, agent_id = await _create_sponsor_and_agent(client)

    await client.post(
        "/v1/billing/wallets/child",
        json={
            "parent_wallet_id": agent_id,
            "child_agent_id": "tester-01",
            "budget_credits": 5000.0,
            "max_spend": 5000.0,
        },
        headers=HEADERS,
    )

    parent = await client.get(f"/v1/billing/wallets/{agent_id}", headers=HEADERS)
    assert parent.json()["balance"] == 45000.0  # 50K - 5K


@pytest.mark.anyio
async def test_reclaim_child_wallet(client):
    """Reclaim unspent credits from child back to parent."""
    _, agent_id = await _create_sponsor_and_agent(client)

    child_resp = await client.post(
        "/v1/billing/wallets/child",
        json={
            "parent_wallet_id": agent_id,
            "child_agent_id": "deployer-01",
            "budget_credits": 3000.0,
            "max_spend": 3000.0,
        },
        headers=HEADERS,
    )
    child_id = child_resp.json()["wallet_id"]

    reclaim = await client.post(
        f"/v1/billing/wallets/{child_id}/reclaim", headers=HEADERS
    )
    assert reclaim.status_code == 200
    data = reclaim.json()
    assert data["credits_reclaimed"] == 3000.0
    assert data["child_status"] == "closed"


@pytest.mark.anyio
async def test_swarm_budget_summary(client):
    """View hierarchical budget for agent's child swarm."""
    _, agent_id = await _create_sponsor_and_agent(client)

    for i in range(3):
        await client.post(
            "/v1/billing/wallets/child",
            json={
                "parent_wallet_id": agent_id,
                "child_agent_id": f"worker-{i}",
                "budget_credits": 1000.0,
                "max_spend": 1000.0,
            },
            headers=HEADERS,
        )

    resp = await client.get(f"/v1/billing/wallets/{agent_id}/swarm", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["active_children"] == 3
    assert data["total_delegated"] == 3000.0
    assert len(data["children"]) == 3


@pytest.mark.anyio
async def test_child_wallet_insufficient_parent_balance(client):
    """Cannot spawn child wallet exceeding parent balance."""
    _, agent_id = await _create_sponsor_and_agent(client)

    resp = await client.post(
        "/v1/billing/wallets/child",
        json={
            "parent_wallet_id": agent_id,
            "child_agent_id": "greedy-agent",
            "budget_credits": 999999.0,
            "max_spend": 999999.0,
        },
        headers=HEADERS,
    )
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_child_wallet_requires_api_key(client):
    resp = await client.post(
        "/v1/billing/wallets/child",
        json={
            "parent_wallet_id": "fake",
            "child_agent_id": "test",
            "budget_credits": 100.0,
            "max_spend": 100.0,
        },
    )
    assert resp.status_code in (401, 403)


@pytest.mark.anyio
async def test_scoped_key_cannot_spawn_reclaim_or_view_another_wallets_swarm(
    client,
):
    """A key scoped to wallet A cannot move or read wallet B's swarm budget.

    Spawning debits the parent and reclaiming credits it, so either one on a
    wallet the caller does not own would move another tenant's money. The
    swarm view lists every child and its balance. All three must refuse a
    wallet-scoped key for a different wallet and leave B's balances alone.
    """
    tenant_a = await provision_agent_wallet(client)
    tenant_b = await provision_agent_wallet(client)
    wallet_a = tenant_a["agent_wallet_id"]
    wallet_b = tenant_b["agent_wallet_id"]
    a_headers = tenant_a["agent_headers"]

    b_child_resp = await client.post(
        "/v1/billing/wallets/child",
        json={
            "parent_wallet_id": wallet_b,
            "child_agent_id": "b-worker",
            "budget_credits": 200.0,
            "max_spend": 200.0,
        },
        headers=tenant_b["agent_headers"],
    )
    assert b_child_resp.status_code == 201, b_child_resp.text
    b_child = b_child_resp.json()["wallet_id"]

    async def balance(wallet_id: str) -> float:
        resp = await client.get(
            f"/v1/billing/wallets/{wallet_id}", headers=BOOTSTRAP_HEADERS
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["balance"]

    b_before = await balance(wallet_b)
    b_child_before = await balance(b_child)
    assert b_before == 800.0
    assert b_child_before == 200.0

    spawn = await client.post(
        "/v1/billing/wallets/child",
        json={
            "parent_wallet_id": wallet_b,
            "child_agent_id": "a-stolen-worker",
            "budget_credits": 100.0,
            "max_spend": 100.0,
        },
        headers=a_headers,
    )
    assert spawn.status_code == 403, spawn.text
    assert spawn.json()["detail"]["error"] == "wallet_access_denied"

    reclaim = await client.post(
        f"/v1/billing/wallets/{b_child}/reclaim", headers=a_headers
    )
    assert reclaim.status_code == 403, reclaim.text
    assert reclaim.json()["detail"]["error"] == "wallet_access_denied"

    swarm = await client.get(f"/v1/billing/wallets/{wallet_b}/swarm", headers=a_headers)
    assert swarm.status_code == 403, swarm.text
    assert swarm.json()["detail"]["error"] == "wallet_access_denied"
    assert b_child not in swarm.text

    assert await balance(wallet_b) == b_before
    assert await balance(b_child) == b_child_before
    b_swarm = await client.get(
        f"/v1/billing/wallets/{wallet_b}/swarm", headers=BOOTSTRAP_HEADERS
    )
    assert b_swarm.status_code == 200, b_swarm.text
    assert b_swarm.json()["active_children"] == 1
    assert [c["wallet_id"] for c in b_swarm.json()["children"]] == [b_child]

    # The refusals are about ownership, not the routes: A's own key still
    # spawns from and reads its own wallet.
    own_spawn = await client.post(
        "/v1/billing/wallets/child",
        json={
            "parent_wallet_id": wallet_a,
            "child_agent_id": "a-worker",
            "budget_credits": 100.0,
            "max_spend": 100.0,
        },
        headers=a_headers,
    )
    assert own_spawn.status_code == 201, own_spawn.text
    own_swarm = await client.get(
        f"/v1/billing/wallets/{wallet_a}/swarm", headers=a_headers
    )
    assert own_swarm.status_code == 200, own_swarm.text
    assert own_swarm.json()["active_children"] == 1
