"""
Tests for the Agent Communications endpoints.
Validates agent registration, messaging, polling, and capability-based handoffs.
"""

import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app
from tests.test_trust_helpers import provision_agent_wallet


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def api_headers():
    return {"X-API-Key": "test-key"}


# --- Agent Registration ---


@pytest.mark.anyio
async def test_register_agent(client, api_headers):
    resp = await client.post(
        "/v1/comms/agents",
        json={
            "name": "test-iot-agent",
            "capabilities": ["iot-monitoring", "mqtt-bridging"],
            "webhook_url": "https://my-agent.example.com/webhook",
        },
        headers=api_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert "agent_id" in data
    assert data["name"] == "test-iot-agent"
    assert "api_key" in data
    assert data["capabilities"] == ["iot-monitoring", "mqtt-bridging"]


@pytest.mark.anyio
async def test_list_agents(client, api_headers):
    await client.post(
        "/v1/comms/agents",
        json={"name": "list-test", "capabilities": ["testing"]},
        headers=api_headers,
    )
    resp = await client.get("/v1/comms/agents", headers=api_headers)
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1


@pytest.mark.anyio
async def test_filter_agents_by_capability(client, api_headers):
    await client.post(
        "/v1/comms/agents",
        json={"name": "video-agent", "capabilities": ["video-transcription"]},
        headers=api_headers,
    )
    resp = await client.get(
        "/v1/comms/agents?capability=video-transcription",
        headers=api_headers,
    )
    assert resp.status_code == 200
    agents = resp.json()["agents"]
    assert all("video-transcription" in a["capabilities"] for a in agents)


# --- Messaging ---


@pytest.mark.anyio
async def test_send_message(client, api_headers):
    # Register sender and receiver
    sender = await client.post(
        "/v1/comms/agents",
        json={"name": "sender", "capabilities": ["sending"]},
        headers=api_headers,
    )
    receiver = await client.post(
        "/v1/comms/agents",
        json={"name": "receiver", "capabilities": ["receiving"]},
        headers=api_headers,
    )

    sender_id = sender.json()["agent_id"]
    receiver_id = receiver.json()["agent_id"]

    resp = await client.post(
        f"/v1/comms/messages?from_agent={sender_id}",
        json={
            "to_agent": receiver_id,
            "message_type": "request",
            "subject": "Process this data",
            "body": {"data_id": "xyz-123"},
        },
        headers=api_headers,
    )
    assert resp.status_code == 202
    assert resp.json()["from_agent"] == sender_id
    assert resp.json()["to_agent"] == receiver_id


@pytest.mark.anyio
async def test_poll_inbox(client, api_headers):
    # Register and send
    reg = await client.post(
        "/v1/comms/agents",
        json={"name": "poller", "capabilities": ["polling"]},
        headers=api_headers,
    )
    agent_id = reg.json()["agent_id"]

    resp = await client.get(
        f"/v1/comms/messages/{agent_id}/inbox",
        headers=api_headers,
    )
    assert resp.status_code == 200
    assert "messages" in resp.json()


# --- Handoff ---


@pytest.mark.anyio
async def test_handoff_no_agent_found(client, api_headers):
    resp = await client.post(
        "/v1/comms/handoff?from_agent=some-agent",
        json={
            "capability": "nonexistent-capability",
            "context": {"task": "impossible"},
        },
        headers=api_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "no_agent_found"


@pytest.mark.anyio
async def test_handoff_with_matching_agent(client, api_headers):
    # Register specialist
    await client.post(
        "/v1/comms/agents",
        json={"name": "transcriber", "capabilities": ["transcription"]},
        headers=api_headers,
    )

    resp = await client.post(
        "/v1/comms/handoff?from_agent=requester",
        json={
            "capability": "transcription",
            "context": {"video_id": "abc-123"},
        },
        headers=api_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "handoff_sent"
    assert resp.json()["target_agent_name"] == "transcriber"


# --- Cross-agent ownership (impersonation prevention) ---


async def _register_foreign_owned_agent(client, api_headers, name: str) -> str:
    """Register an agent and rewrite its owner_key so the caller no longer
    owns it — sets up a 'caller does not own this agent' scenario without
    needing two independently-valid API keys configured."""
    from app.core.dependencies import get_agent_comms

    resp = await client.post(
        "/v1/comms/agents",
        json={"name": name, "capabilities": ["x"]},
        headers=api_headers,
    )
    assert resp.status_code == 201
    agent_id = resp.json()["agent_id"]
    comms = get_agent_comms()
    agent = await comms.registry.get(agent_id)
    agent.owner_key = "owner-from-some-other-tenant-key"
    return agent_id


@pytest.mark.anyio
async def test_send_denied_when_caller_does_not_own_from_agent(client, api_headers):
    from_agent = await _register_foreign_owned_agent(
        client, api_headers, "foreign-sender"
    )
    receiver = await client.post(
        "/v1/comms/agents",
        json={"name": "rx-cross-send", "capabilities": ["y"]},
        headers=api_headers,
    )
    receiver_id = receiver.json()["agent_id"]
    resp = await client.post(
        f"/v1/comms/messages?from_agent={from_agent}",
        json={"to_agent": receiver_id, "subject": "spoof", "body": {"x": 1}},
        headers=api_headers,
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "access_denied"


@pytest.mark.anyio
async def test_inbox_denied_when_caller_does_not_own_agent(client, api_headers):
    agent_id = await _register_foreign_owned_agent(client, api_headers, "foreign-inbox")
    resp = await client.get(f"/v1/comms/messages/{agent_id}/inbox", headers=api_headers)
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "access_denied"


@pytest.mark.anyio
async def test_ack_denied_when_caller_does_not_own_agent(client, api_headers):
    agent_id = await _register_foreign_owned_agent(client, api_headers, "foreign-ack")
    resp = await client.post(
        f"/v1/comms/messages/{agent_id}/ack/any-msg-id", headers=api_headers
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "access_denied"


@pytest.mark.anyio
async def test_handoff_denied_when_caller_does_not_own_from_agent(client, api_headers):
    from_agent = await _register_foreign_owned_agent(
        client, api_headers, "foreign-handoff"
    )
    resp = await client.post(
        f"/v1/comms/handoff?from_agent={from_agent}",
        json={"capability": "x", "context": {"why": "spoof"}},
        headers=api_headers,
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "access_denied"


# --- Cross-tenant isolation with wallet-scoped keys ---
#
# The tests above prove ownership with the single bootstrap key. These use two
# independently provisioned wallet-scoped keys (and the JWTs minted from them)
# so tenant isolation is exercised the way a real second tenant would hit it.

# 32 raw bytes, strict base64 — same non-secret test material CI uses.
TEST_SIGNING_KEY = "dGVzdC1zaWduaW5nLWtleS1tYXRlcmlhbC0zMmJ5dGU="


@pytest.fixture
def signing_key(monkeypatch):
    """Configure JWT signing on the live settings object.

    Patching the cached settings (rather than clearing the cache) keeps the
    proof-surface flag the autouse fixture set on it.
    """
    from app.core.config import get_settings

    monkeypatch.setattr(
        get_settings(), "TRUST_SIGNING_PRIVATE_KEY_B64", TEST_SIGNING_KEY
    )


async def _register(client, headers, name: str, capabilities=None) -> str:
    resp = await client.post(
        "/v1/comms/agents",
        json={"name": name, "capabilities": capabilities or ["x"]},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["agent_id"]


async def _mint_access_token(client, api_key: str) -> str:
    resp = await client.post("/v1/auth/token", json={"api_key": api_key})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _assert_no_leak(resp, *secrets: str) -> None:
    for secret in secrets:
        assert secret not in resp.text


@pytest.mark.proof
@pytest.mark.anyio
async def test_wallet_cannot_drive_another_wallets_agent(client, clean_database):
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)
    a_agent = await _register(client, a["agent_headers"], "a-owned")
    a_peer = await _register(client, a["agent_headers"], "a-peer")
    sent = await client.post(
        f"/v1/comms/messages?from_agent={a_peer}",
        json={"to_agent": a_agent, "subject": "a-private", "body": {"s": "a-only"}},
        headers=a["agent_headers"],
    )
    assert sent.status_code == 202
    message_id = sent.json()["message_id"]

    denied = [
        await client.post(
            f"/v1/comms/messages?from_agent={a_agent}",
            json={"to_agent": a_peer, "subject": "spoof", "body": {}},
            headers=b["agent_headers"],
        ),
        await client.get(
            f"/v1/comms/messages/{a_agent}/inbox", headers=b["agent_headers"]
        ),
        await client.post(
            f"/v1/comms/messages/{a_agent}/ack/{message_id}",
            headers=b["agent_headers"],
        ),
        await client.post(
            f"/v1/comms/handoff?from_agent={a_agent}",
            json={"capability": "x", "context": {}},
            headers=b["agent_headers"],
        ),
    ]
    for resp in denied:
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "access_denied"
        _assert_no_leak(resp, "a-only", a["agent_wallet_id"])

    # The owner still reads and acknowledges its own inbox.
    inbox = await client.get(
        f"/v1/comms/messages/{a_agent}/inbox", headers=a["agent_headers"]
    )
    assert inbox.status_code == 200
    assert [m["message_id"] for m in inbox.json()["messages"]] == [message_id]
    ack = await client.post(
        f"/v1/comms/messages/{a_agent}/ack/{message_id}",
        headers=a["agent_headers"],
    )
    assert ack.status_code == 200


@pytest.mark.proof
@pytest.mark.anyio
async def test_unregistered_agent_ids_fail_closed_for_wallet_callers(
    client, clean_database
):
    """An unclaimed agent id must not be spoofable as a sender or readable as
    an inbox: registration, not first use, establishes ownership."""
    b = await provision_agent_wallet(client)
    b_agent = await _register(client, b["agent_headers"], "b-own")
    ghost = "agent-never-registered"

    responses = [
        await client.post(
            f"/v1/comms/messages?from_agent={ghost}",
            json={"to_agent": b_agent, "subject": "spoof", "body": {}},
            headers=b["agent_headers"],
        ),
        await client.get(
            f"/v1/comms/messages/{ghost}/inbox", headers=b["agent_headers"]
        ),
        await client.post(
            f"/v1/comms/messages/{ghost}/ack/any-msg", headers=b["agent_headers"]
        ),
        await client.post(
            f"/v1/comms/handoff?from_agent={ghost}",
            json={"capability": "x", "context": {}},
            headers=b["agent_headers"],
        ),
    ]
    for resp in responses:
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"]["error"] == "agent_not_found"

    # Nothing was delivered under the spoofed sender id.
    inbox = await client.get(
        f"/v1/comms/messages/{b_agent}/inbox", headers=b["agent_headers"]
    )
    assert inbox.status_code == 200
    assert inbox.json()["count"] == 0


@pytest.mark.proof
@pytest.mark.anyio
async def test_ownerless_agent_denied_to_wallet_callers(
    client, api_headers, clean_database
):
    """A record with no owner (legacy data) must fail closed for tenants."""
    from app.core.dependencies import get_agent_comms

    b = await provision_agent_wallet(client)
    orphan = await _register(client, api_headers, "orphan")
    agent = await get_agent_comms().registry.get(orphan)
    agent.owner_key = ""

    responses = [
        await client.post(
            f"/v1/comms/messages?from_agent={orphan}",
            json={"to_agent": orphan, "subject": "spoof", "body": {}},
            headers=b["agent_headers"],
        ),
        await client.get(
            f"/v1/comms/messages/{orphan}/inbox", headers=b["agent_headers"]
        ),
        await client.post(
            f"/v1/comms/messages/{orphan}/ack/any-msg", headers=b["agent_headers"]
        ),
        await client.post(
            f"/v1/comms/handoff?from_agent={orphan}",
            json={"capability": "x", "context": {}},
            headers=b["agent_headers"],
        ),
    ]
    for resp in responses:
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "access_denied"

    # A bootstrap admin can still operate the owner-less record.
    admin = await client.get(f"/v1/comms/messages/{orphan}/inbox", headers=api_headers)
    assert admin.status_code == 200


@pytest.mark.proof
@pytest.mark.anyio
async def test_list_agents_scoped_to_caller(client, api_headers, clean_database):
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)
    a_agent = await _register(
        client, a["agent_headers"], "a-secret-agent", ["cap-scoped-list"]
    )
    b_agent = await _register(client, b["agent_headers"], "b-agent", ["cap-b"])

    for path in ("/v1/comms/agents", "/v1/comms/agents?capability=cap-scoped-list"):
        resp = await client.get(path, headers=b["agent_headers"])
        assert resp.status_code == 200
        ids = [x["agent_id"] for x in resp.json()["agents"]]
        assert a_agent not in ids
        _assert_no_leak(resp, a_agent, "a-secret-agent")

    own = await client.get("/v1/comms/agents", headers=b["agent_headers"])
    assert [x["agent_id"] for x in own.json()["agents"]] == [b_agent]
    assert own.json()["total"] == 1

    owner = await client.get(
        "/v1/comms/agents?capability=cap-scoped-list", headers=a["agent_headers"]
    )
    assert [x["agent_id"] for x in owner.json()["agents"]] == [a_agent]

    # Bootstrap admins keep the full registry view.
    admin = await client.get("/v1/comms/agents", headers=api_headers)
    admin_ids = {x["agent_id"] for x in admin.json()["agents"]}
    assert {a_agent, b_agent} <= admin_ids


@pytest.mark.proof
@pytest.mark.anyio
async def test_jwt_callers_from_different_wallets_do_not_share_ownership(
    client, clean_database, signing_key
):
    """Every EdDSA access token starts with the same header bytes. Ownership
    keyed to a token prefix let any JWT drive any JWT-registered agent."""
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)
    a_jwt = await _mint_access_token(client, a["agent_headers"]["X-API-Key"])
    b_jwt = await _mint_access_token(client, b["agent_headers"]["X-API-Key"])
    a_jwt_headers = {"X-API-Key": a_jwt}
    b_jwt_headers = {"X-API-Key": b_jwt}

    a_agent = await _register(client, a_jwt_headers, "a-jwt-agent")
    a_peer = await _register(client, a_jwt_headers, "a-jwt-peer")
    sent = await client.post(
        f"/v1/comms/messages?from_agent={a_peer}",
        json={"to_agent": a_agent, "subject": "a-private", "body": {"s": "a-only"}},
        headers=a_jwt_headers,
    )
    assert sent.status_code == 202

    denied = [
        await client.get(f"/v1/comms/messages/{a_agent}/inbox", headers=b_jwt_headers),
        await client.post(
            f"/v1/comms/messages?from_agent={a_agent}",
            json={"to_agent": a_peer, "subject": "spoof", "body": {}},
            headers=b_jwt_headers,
        ),
        await client.post(
            f"/v1/comms/handoff?from_agent={a_agent}",
            json={"capability": "x", "context": {}},
            headers=b_jwt_headers,
        ),
    ]
    for resp in denied:
        assert resp.status_code == 403, resp.text
        _assert_no_leak(resp, "a-only")

    listed = await client.get("/v1/comms/agents", headers=b_jwt_headers)
    assert a_agent not in [x["agent_id"] for x in listed.json()["agents"]]

    # The owning wallet keeps access, whether it presents the JWT or the key
    # the JWT was minted from.
    for headers in (a_jwt_headers, a["agent_headers"]):
        inbox = await client.get(f"/v1/comms/messages/{a_agent}/inbox", headers=headers)
        assert inbox.status_code == 200
        assert inbox.json()["count"] == 1
