"""
Tests for Pillar 13: Interactive Testing Sandboxes.
Validates headless puzzle environments and generalization scoring.
"""

import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.services.audit_log import list_audit_events
from tests.test_trust_helpers import provision_agent_wallet


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


HEADERS = {"X-API-Key": "test-key"}


@pytest.mark.anyio
async def test_create_pattern_environment(client):
    """Create a pattern-discovery sandbox."""
    resp = await client.post(
        "/v1/sandbox/environments",
        json={
            "env_type": "pattern",
            "difficulty": "medium",
            "seed": 42,
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["env_type"] == "pattern"
    assert data["difficulty"] == "medium"
    assert data["env_id"].startswith("env-")


@pytest.mark.anyio
async def test_create_navigation_environment(client):
    """Create a navigation sandbox."""
    resp = await client.post(
        "/v1/sandbox/environments",
        json={
            "env_type": "navigation",
            "difficulty": "hard",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201
    assert resp.json()["env_type"] == "navigation"


@pytest.mark.anyio
async def test_create_api_mock_environment(client):
    """Create an API mock sandbox."""
    resp = await client.post(
        "/v1/sandbox/environments",
        json={
            "env_type": "api_mock",
            "difficulty": "easy",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201
    assert resp.json()["env_type"] == "api_mock"


@pytest.mark.anyio
async def test_create_adversarial_environment(client):
    """Create an adversarial sandbox."""
    resp = await client.post(
        "/v1/sandbox/environments",
        json={
            "env_type": "adversarial",
            "difficulty": "extreme",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201
    assert resp.json()["env_type"] == "adversarial"


@pytest.mark.anyio
async def test_submit_action(client):
    """Agent can submit actions to the environment."""
    create = await client.post(
        "/v1/sandbox/environments",
        json={
            "env_type": "pattern",
            "difficulty": "easy",
        },
        headers=HEADERS,
    )
    env_id = create.json()["env_id"]

    resp = await client.post(
        f"/v1/sandbox/environments/{env_id}/actions",
        json={
            "action": {"type": "observe", "value": "grid"},
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "step" in data
    assert "reward" in data
    assert "feedback" in data
    assert data["action_accepted"] is True


@pytest.mark.anyio
async def test_solve_pattern_environment(client):
    """Agent can solve a pattern puzzle by guessing the transform."""
    create = await client.post(
        "/v1/sandbox/environments",
        json={
            "env_type": "pattern",
            "difficulty": "easy",
            "seed": 100,
        },
        headers=HEADERS,
    )
    env_id = create.json()["env_id"]

    resp = await client.post(
        f"/v1/sandbox/environments/{env_id}/actions",
        json={
            "action": {"type": "submit_transform", "value": "rotate"},
        },
        headers=HEADERS,
    )
    data = resp.json()
    # Depending on seed, may or may not solve, but should accept action
    assert data["action_accepted"] is True


@pytest.mark.anyio
async def test_evaluate_environment(client):
    """Evaluation returns generalization score."""
    create = await client.post(
        "/v1/sandbox/environments",
        json={
            "env_type": "pattern",
            "difficulty": "medium",
        },
        headers=HEADERS,
    )
    env_id = create.json()["env_id"]

    # Do a few actions first
    await client.post(
        f"/v1/sandbox/environments/{env_id}/actions",
        json={
            "action": {"type": "observe"},
        },
        headers=HEADERS,
    )

    resp = await client.post(
        f"/v1/sandbox/environments/{env_id}/evaluate", headers=HEADERS
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "generalization_score" in data
    assert "efficiency" in data
    assert "solved" in data
    assert data["steps_used"] >= 1


@pytest.mark.anyio
async def test_list_environments(client):
    """Can list all sandbox environments."""
    await client.post(
        "/v1/sandbox/environments",
        json={
            "env_type": "pattern",
        },
        headers=HEADERS,
    )

    resp = await client.get("/v1/sandbox/environments", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1


@pytest.mark.anyio
async def test_get_environment_by_id(client):
    """Can retrieve a specific environment."""
    create = await client.post(
        "/v1/sandbox/environments",
        json={
            "env_type": "navigation",
        },
        headers=HEADERS,
    )
    env_id = create.json()["env_id"]

    resp = await client.get(f"/v1/sandbox/environments/{env_id}", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["env_id"] == env_id


@pytest.mark.anyio
async def test_environment_not_found(client):
    resp = await client.get("/v1/sandbox/environments/env-nonexistent", headers=HEADERS)
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_state_hides_rules(client):
    """Environment state should NOT expose hidden rules."""
    create = await client.post(
        "/v1/sandbox/environments",
        json={
            "env_type": "pattern",
            "difficulty": "hard",
        },
        headers=HEADERS,
    )
    data = create.json()
    state = data["state"]
    assert "hidden_rules" not in str(state).lower() or "hidden_rules" not in state


@pytest.mark.anyio
async def test_sandbox_requires_api_key(client):
    resp = await client.post(
        "/v1/sandbox/environments",
        json={
            "env_type": "pattern",
        },
    )
    assert resp.status_code in (401, 403)


@pytest.mark.anyio
async def test_sandbox_lifecycle_records_governance_audit_events(
    client,
    clean_database,
):
    create = await client.post(
        "/v1/sandbox/environments",
        json={
            "env_type": "pattern",
            "difficulty": "easy",
            "seed": 123,
        },
        headers={**HEADERS, "X-Request-ID": "req-sandbox-create"},
    )
    assert create.status_code == 201
    env_id = create.json()["env_id"]

    action = await client.post(
        f"/v1/sandbox/environments/{env_id}/actions",
        json={"action": {"type": "observe", "value": "grid"}},
        headers={**HEADERS, "X-Request-ID": "req-sandbox-action"},
    )
    assert action.status_code == 200

    evaluate = await client.post(
        f"/v1/sandbox/environments/{env_id}/evaluate",
        headers={**HEADERS, "X-Request-ID": "req-sandbox-evaluate"},
    )
    assert evaluate.status_code == 200

    events = await list_audit_events(tool="sandbox", limit=10)
    by_request_id = {event.request_id: event for event in events}

    assert by_request_id["req-sandbox-create"].event == "sandbox.environment.create"
    assert by_request_id["req-sandbox-create"].endpoint == "/v1/sandbox/environments"
    assert by_request_id["req-sandbox-create"].ok is True
    assert by_request_id["req-sandbox-create"].metadata["env_id"] == env_id
    assert by_request_id["req-sandbox-create"].metadata["env_type"] == "pattern"

    assert by_request_id["req-sandbox-action"].event == "sandbox.action.submit"
    assert (
        by_request_id["req-sandbox-action"].endpoint
        == f"/v1/sandbox/environments/{env_id}/actions"
    )
    assert by_request_id["req-sandbox-action"].metadata["action_accepted"] is True
    assert by_request_id["req-sandbox-action"].metadata["step"] == action.json()["step"]

    assert by_request_id["req-sandbox-evaluate"].event == "sandbox.evaluate"
    assert (
        by_request_id["req-sandbox-evaluate"].endpoint
        == f"/v1/sandbox/environments/{env_id}/evaluate"
    )
    assert (
        by_request_id["req-sandbox-evaluate"].metadata["generalization_score"]
        == (evaluate.json()["generalization_score"])
    )


# --------------------------------------------------------------------------
# Tenant isolation: puzzle environments are owned by the creating wallet.
# --------------------------------------------------------------------------


async def _create_env(client, headers) -> str:
    resp = await client.post(
        "/v1/sandbox/environments",
        json={"env_type": "pattern", "difficulty": "easy", "seed": 7},
        headers=headers,
    )
    assert resp.status_code == 201
    return resp.json()["env_id"]


@pytest.mark.proof
@pytest.mark.anyio
async def test_sandbox_reads_require_api_key(client):
    """Neither GET may be served to an unauthenticated caller."""
    env_id = await _create_env(client, HEADERS)

    listing = await client.get("/v1/sandbox/environments")
    assert listing.status_code == 401
    assert env_id not in listing.text

    single = await client.get(f"/v1/sandbox/environments/{env_id}")
    assert single.status_code == 401
    assert "grid" not in single.text


@pytest.mark.proof
@pytest.mark.anyio
async def test_sandbox_env_not_accessible_across_tenants(client, clean_database):
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)
    env_id = await _create_env(client, a["agent_headers"])
    missing_id = "env-doesnotexist"

    def _same_as_missing(foreign, missing) -> None:
        # A foreign environment must be indistinguishable from a missing one
        # (no existence oracle) and must never echo the owner's wallet.
        assert foreign.status_code == 404
        assert missing.status_code == 404
        assert foreign.text.replace(env_id, "ID") == missing.text.replace(
            missing_id, "ID"
        )
        assert a["agent_wallet_id"] not in foreign.text

    # Wallet B cannot read A's environment.
    _same_as_missing(
        await client.get(
            f"/v1/sandbox/environments/{env_id}", headers=b["agent_headers"]
        ),
        await client.get(
            f"/v1/sandbox/environments/{missing_id}", headers=b["agent_headers"]
        ),
    )

    # Wallet B cannot act on A's environment.
    action = {"action": {"type": "submit_transform", "value": "rotate"}}
    _same_as_missing(
        await client.post(
            f"/v1/sandbox/environments/{env_id}/actions",
            json=action,
            headers=b["agent_headers"],
        ),
        await client.post(
            f"/v1/sandbox/environments/{missing_id}/actions",
            json=action,
            headers=b["agent_headers"],
        ),
    )

    # Wallet B cannot evaluate (and thereby complete) A's environment.
    _same_as_missing(
        await client.post(
            f"/v1/sandbox/environments/{env_id}/evaluate", headers=b["agent_headers"]
        ),
        await client.post(
            f"/v1/sandbox/environments/{missing_id}/evaluate",
            headers=b["agent_headers"],
        ),
    )

    # B's attempts left A's environment untouched, and the owner still works.
    owner_view = await client.get(
        f"/v1/sandbox/environments/{env_id}", headers=a["agent_headers"]
    )
    assert owner_view.status_code == 200
    assert owner_view.json()["action_count"] == 0
    assert owner_view.json()["state"]["step"] == 0
    assert owner_view.json()["completed_at"] is None

    owner_action = await client.post(
        f"/v1/sandbox/environments/{env_id}/actions",
        json={"action": {"type": "observe", "value": "grid"}},
        headers=a["agent_headers"],
    )
    assert owner_action.status_code == 200
    assert owner_action.json()["step"] == 1

    owner_eval = await client.post(
        f"/v1/sandbox/environments/{env_id}/evaluate", headers=a["agent_headers"]
    )
    assert owner_eval.status_code == 200
    assert owner_eval.json()["env_id"] == env_id

    # Bootstrap admins keep cross-tenant access.
    admin_view = await client.get(f"/v1/sandbox/environments/{env_id}", headers=HEADERS)
    assert admin_view.status_code == 200
    assert admin_view.json()["action_count"] == 1


@pytest.mark.proof
@pytest.mark.anyio
async def test_sandbox_admin_created_env_not_visible_to_wallet_keys(
    client, clean_database
):
    a = await provision_agent_wallet(client)
    admin_env = await _create_env(client, HEADERS)

    assert (
        await client.get(
            f"/v1/sandbox/environments/{admin_env}", headers=a["agent_headers"]
        )
    ).status_code == 404
    assert (
        await client.post(
            f"/v1/sandbox/environments/{admin_env}/evaluate",
            headers=a["agent_headers"],
        )
    ).status_code == 404

    listing = await client.get("/v1/sandbox/environments", headers=a["agent_headers"])
    assert listing.status_code == 200
    assert admin_env not in listing.text


@pytest.mark.proof
@pytest.mark.anyio
async def test_sandbox_list_environments_scoped_to_caller(client, clean_database):
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)
    env_a = await _create_env(client, a["agent_headers"])
    env_b = await _create_env(client, b["agent_headers"])

    b_list = await client.get("/v1/sandbox/environments", headers=b["agent_headers"])
    assert b_list.status_code == 200
    assert [e["env_id"] for e in b_list.json()["environments"]] == [env_b]
    assert b_list.json()["total"] == 1
    assert env_a not in b_list.text

    a_list = await client.get("/v1/sandbox/environments", headers=a["agent_headers"])
    assert a_list.status_code == 200
    assert [e["env_id"] for e in a_list.json()["environments"]] == [env_a]
    assert env_b not in a_list.text

    # Bootstrap admins still enumerate every environment.
    admin_list = await client.get("/v1/sandbox/environments", headers=HEADERS)
    assert admin_list.status_code == 200
    admin_ids = {e["env_id"] for e in admin_list.json()["environments"]}
    assert {env_a, env_b} <= admin_ids
