"""Contract: durable agent comms (DB + /v1/agent-comms)."""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.config import get_settings
from app.db.database import get_session_factory
from app.db.models import AgentCommsMessageModel
from app.main import app
from tests.test_trust_helpers import provision_agent_wallet

HEADERS = {"X-API-Key": "test-key"}

# 32 raw bytes, strict base64 — same non-secret test material CI uses.
TEST_SIGNING_KEY = "dGVzdC1zaWduaW5nLWtleS1tYXRlcmlhbC0zMmJ5dGU="


@pytest.fixture(autouse=True)
def _restore_agent_comms_sim():
    settings = get_settings()
    saved = settings.SIMULATION_MODE_AGENT_COMMS
    yield
    settings.SIMULATION_MODE_AGENT_COMMS = saved


@pytest.mark.anyio
async def test_durable_send_persists_row_and_inbox_lists():
    settings = get_settings()
    settings.SIMULATION_MODE_AGENT_COMMS = False
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        s = await client.post(
            "/v1/comms/agents",
            json={"name": "durable-sender", "capabilities": ["x"]},
            headers=HEADERS,
        )
        r = await client.post(
            "/v1/comms/agents",
            json={"name": "durable-receiver", "capabilities": ["y"]},
            headers=HEADERS,
        )
        sender_id = s.json()["agent_id"]
        receiver_id = r.json()["agent_id"]

        send = await client.post(
            "/v1/agent-comms/send",
            json={
                "from_agent": sender_id,
                "to_agent": receiver_id,
                "subject": "hello",
                "body": {"k": "v"},
            },
            headers=HEADERS,
        )
        assert send.status_code == 202
        send_body = send.json()
        mid = send_body["message_id"]
        assert send_body["payload_hash"] is not None
        assert len(send_body["payload_hash"]) == 64

    factory = get_session_factory()
    async with factory() as session:
        row = (
            await session.execute(
                select(AgentCommsMessageModel).where(
                    AgentCommsMessageModel.message_id == mid
                )
            )
        ).scalar_one_or_none()
    assert row is not None
    assert row.from_agent == sender_id
    assert row.to_agent == receiver_id
    assert row.payload_hash == send_body["payload_hash"]

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        inbox = await client.get(
            "/v1/agent-comms/inbox",
            params={"agent_id": receiver_id},
            headers=HEADERS,
        )
    assert inbox.status_code == 200
    data = inbox.json()
    assert data["total"] >= 1
    match = next(m for m in data["messages"] if m["message_id"] == mid)
    assert match["payload_hash"] == row.payload_hash
    assert match["delivered_at"] is not None


@pytest.mark.anyio
async def test_simulation_skips_db_row_for_send():
    settings = get_settings()
    settings.SIMULATION_MODE_AGENT_COMMS = True
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        s = await client.post(
            "/v1/comms/agents",
            json={"name": "sim-sender", "capabilities": ["x"]},
            headers=HEADERS,
        )
        r = await client.post(
            "/v1/comms/agents",
            json={"name": "sim-receiver", "capabilities": ["y"]},
            headers=HEADERS,
        )
        sender_id = s.json()["agent_id"]
        receiver_id = r.json()["agent_id"]
        send = await client.post(
            "/v1/agent-comms/send",
            json={
                "from_agent": sender_id,
                "to_agent": receiver_id,
                "subject": "hi",
                "body": {"a": 1},
            },
            headers=HEADERS,
        )
        assert send.status_code == 202
        assert send.json()["payload_hash"] is None
        mid = send.json()["message_id"]

    factory = get_session_factory()
    async with factory() as session:
        row = (
            await session.execute(
                select(AgentCommsMessageModel).where(
                    AgentCommsMessageModel.message_id == mid
                )
            )
        ).scalar_one_or_none()
    assert row is None


@pytest.mark.anyio
async def test_durable_send_denied_when_caller_does_not_own_from_agent():
    """Authenticated callers cannot impersonate an agent they do not own on
    the durable send path."""
    from app.core.dependencies import get_agent_comms

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        sender = await client.post(
            "/v1/comms/agents",
            json={"name": "durable-foreign-sender", "capabilities": ["x"]},
            headers=HEADERS,
        )
        sender_id = sender.json()["agent_id"]
        # Rewrite owner_key so test-key no longer owns this agent.
        comms = get_agent_comms()
        agent = await comms.registry.get(sender_id)
        agent.owner_key = "owner-from-some-other-tenant-key"

        receiver = await client.post(
            "/v1/comms/agents",
            json={"name": "durable-foreign-receiver", "capabilities": ["y"]},
            headers=HEADERS,
        )
        receiver_id = receiver.json()["agent_id"]

        resp = await client.post(
            "/v1/agent-comms/send",
            json={
                "from_agent": sender_id,
                "to_agent": receiver_id,
                "subject": "spoof",
                "body": {"a": 1},
            },
            headers=HEADERS,
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "access_denied"


# --- Cross-tenant isolation with wallet-scoped keys ---


async def _register(client, headers, name: str) -> str:
    resp = await client.post(
        "/v1/comms/agents",
        json={"name": name, "capabilities": ["durable-x"]},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["agent_id"]


@pytest.mark.proof
@pytest.mark.anyio
async def test_durable_unregistered_ids_fail_closed_for_wallet_callers(
    clean_database,
):
    """Real mode stores a message to an unregistered recipient (status
    failed). No tenant may read that inbox, or send as an unclaimed id."""
    settings = get_settings()
    settings.SIMULATION_MODE_AGENT_COMMS = False
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        a = await provision_agent_wallet(client)
        b = await provision_agent_wallet(client)
        a_sender = await _register(client, a["agent_headers"], "durable-a-sender")
        ghost = "agent-durable-ghost"

        sent = await client.post(
            "/v1/agent-comms/send",
            json={
                "from_agent": a_sender,
                "to_agent": ghost,
                "subject": "a-private",
                "body": {"s": "a-only"},
            },
            headers=a["agent_headers"],
        )
        assert sent.status_code == 202
        assert sent.json()["status"] == "failed"

        inbox = await client.get(
            "/v1/agent-comms/inbox",
            params={"agent_id": ghost},
            headers=b["agent_headers"],
        )
        assert inbox.status_code == 404, inbox.text
        assert inbox.json()["detail"]["error"] == "agent_not_found"
        assert "a-only" not in inbox.text

        spoof = await client.post(
            "/v1/agent-comms/send",
            json={
                "from_agent": ghost,
                "to_agent": a_sender,
                "subject": "spoof",
                "body": {"s": "spoof"},
            },
            headers=b["agent_headers"],
        )
        assert spoof.status_code == 404, spoof.text
        assert spoof.json()["detail"]["error"] == "agent_not_found"

        # Nothing landed in A's inbox under the spoofed id; the owner can
        # still list it.
        own = await client.get(
            "/v1/agent-comms/inbox",
            params={"agent_id": a_sender},
            headers=a["agent_headers"],
        )
        assert own.status_code == 200
        assert own.json()["total"] == 0


@pytest.mark.proof
@pytest.mark.anyio
async def test_durable_wallet_cannot_read_or_send_as_foreign_agent(clean_database):
    settings = get_settings()
    settings.SIMULATION_MODE_AGENT_COMMS = False
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        a = await provision_agent_wallet(client)
        b = await provision_agent_wallet(client)
        a_agent = await _register(client, a["agent_headers"], "durable-a-agent")
        a_peer = await _register(client, a["agent_headers"], "durable-a-peer")
        sent = await client.post(
            "/v1/agent-comms/send",
            json={
                "from_agent": a_peer,
                "to_agent": a_agent,
                "subject": "a-private",
                "body": {"s": "a-only"},
            },
            headers=a["agent_headers"],
        )
        assert sent.status_code == 202

        denied = [
            await client.get(
                "/v1/agent-comms/inbox",
                params={"agent_id": a_agent},
                headers=b["agent_headers"],
            ),
            await client.post(
                "/v1/agent-comms/send",
                json={
                    "from_agent": a_agent,
                    "to_agent": a_peer,
                    "subject": "spoof",
                    "body": {},
                },
                headers=b["agent_headers"],
            ),
        ]
        for resp in denied:
            assert resp.status_code == 403
            assert resp.json()["detail"]["error"] == "access_denied"
            assert "a-only" not in resp.text
            assert a["agent_wallet_id"] not in resp.text

        own = await client.get(
            "/v1/agent-comms/inbox",
            params={"agent_id": a_agent},
            headers=a["agent_headers"],
        )
        assert own.status_code == 200
        assert own.json()["total"] == 1


@pytest.mark.proof
@pytest.mark.anyio
async def test_durable_inbox_denied_to_jwt_from_another_wallet(
    clean_database, monkeypatch
):
    """JWT callers in different wallets must not share one ownership
    identity (every EdDSA token shares its first 20 characters)."""
    monkeypatch.setattr(
        get_settings(), "TRUST_SIGNING_PRIVATE_KEY_B64", TEST_SIGNING_KEY
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        a = await provision_agent_wallet(client)
        b = await provision_agent_wallet(client)
        tokens = {}
        for name, wallet in (("a", a), ("b", b)):
            minted = await client.post(
                "/v1/auth/token",
                json={"api_key": wallet["agent_headers"]["X-API-Key"]},
            )
            assert minted.status_code == 200, minted.text
            tokens[name] = minted.json()["access_token"]
        a_bearer = {"Authorization": f"Bearer {tokens['a']}"}
        b_bearer = {"Authorization": f"Bearer {tokens['b']}"}

        # Registration accepts the JWT through the X-API-Key compatibility path.
        a_agent = await _register(
            client, {"X-API-Key": tokens["a"]}, "durable-a-jwt-agent"
        )

        foreign = await client.get(
            "/v1/agent-comms/inbox",
            params={"agent_id": a_agent},
            headers=b_bearer,
        )
        assert foreign.status_code == 403, foreign.text
        assert foreign.json()["detail"]["error"] == "access_denied"

        spoof = await client.post(
            "/v1/agent-comms/send",
            json={
                "from_agent": a_agent,
                "to_agent": a_agent,
                "subject": "spoof",
                "body": {},
            },
            headers=b_bearer,
        )
        assert spoof.status_code == 403, spoof.text

        own = await client.get(
            "/v1/agent-comms/inbox",
            params={"agent_id": a_agent},
            headers=a_bearer,
        )
        assert own.status_code == 200
