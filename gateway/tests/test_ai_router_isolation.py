"""Cross-tenant isolation regression tests for the agent-intelligence router.

``/v1/ai`` (a frozen proof surface) used to key decisions and memory on a
caller-asserted ``agent_id`` and store heal results with no owner, so any
valid API key could read or write another tenant's agent decisions, memory
and learned patterns, and fetch any heal by id. Agent data is now namespaced
by the caller's wallet and heals carry their owning wallet; bootstrap admins
keep their existing (un-namespaced) behavior.
"""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services import agent_intelligence as agent_intelligence_module
from app.services.llm import LLMResponse
from tests.test_trust_helpers import BOOTSTRAP_HEADERS, provision_agent_wallet

AGENT_ID = "shared-bot"


class _FakeLLM:
    """Deterministic LLM stand-in: one JSON body every caller can parse."""

    async def chat(self, messages, system=None, tools=None, tool_choice=None):
        return LLMResponse(
            content=json.dumps(
                {
                    "reasoning": "fake-reasoning",
                    "action": "fake-action",
                    "confidence": 0.9,
                    "diagnosis": "fake-diagnosis",
                    "fix": "fake-fix",
                }
            ),
            model="fake",
            usage={"prompt_tokens": 0, "completion_tokens": 0},
        )


@pytest.fixture
def ai_service(monkeypatch):
    """A fresh agent-intelligence singleton per test with a stubbed LLM."""
    service = agent_intelligence_module.AgentIntelligence()
    service._llm = _FakeLLM()
    monkeypatch.setattr(agent_intelligence_module, "_agent_intelligence", service)
    return service


@pytest.fixture
async def client(ai_service):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _decide(client: AsyncClient, headers: dict, marker: str) -> str:
    resp = await client.post(
        "/v1/ai/decide",
        json={"agent_id": AGENT_ID, "context": {"marker": marker}},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The response echoes the caller's own agent id, never a namespaced key.
    assert body["agent_id"] == AGENT_ID
    return body["decision_id"]


async def _recall(client: AsyncClient, headers: dict, agent_id: str = AGENT_ID):
    return await client.post(
        "/v1/ai/memory/recall",
        json={"agent_id": agent_id, "limit": 100},
        headers=headers,
    )


@pytest.mark.proof
@pytest.mark.anyio
async def test_ai_decisions_not_readable_across_tenants(client, clean_database):
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)

    a_decision = await _decide(client, a["agent_headers"], "tenant-a-context")

    # B asks for the same agent id and sees nothing of A's.
    foreign = await client.get(
        f"/v1/ai/decisions/{AGENT_ID}", headers=b["agent_headers"]
    )
    assert foreign.status_code == 200
    assert foreign.json() == []
    assert a_decision not in foreign.text

    # The owner still reads its own decision.
    own = await client.get(f"/v1/ai/decisions/{AGENT_ID}", headers=a["agent_headers"])
    assert own.status_code == 200
    assert [d["decision_id"] for d in own.json()] == [a_decision]
    assert own.json()[0]["agent_id"] == AGENT_ID


@pytest.mark.proof
@pytest.mark.anyio
async def test_ai_decisions_not_writable_across_tenants(client, clean_database):
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)

    a_decision = await _decide(client, a["agent_headers"], "tenant-a-context")
    b_decision = await _decide(client, b["agent_headers"], "tenant-b-context")

    # B deciding under the same agent id never lands in A's history (and
    # cannot evict it through the per-agent cap).
    own_a = await client.get(f"/v1/ai/decisions/{AGENT_ID}", headers=a["agent_headers"])
    assert [d["decision_id"] for d in own_a.json()] == [a_decision]
    own_b = await client.get(f"/v1/ai/decisions/{AGENT_ID}", headers=b["agent_headers"])
    assert [d["decision_id"] for d in own_b.json()] == [b_decision]


@pytest.mark.proof
@pytest.mark.anyio
async def test_ai_memory_not_readable_or_writable_across_tenants(
    client, clean_database
):
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)

    stored = await client.post(
        "/v1/ai/memory",
        json={"agent_id": AGENT_ID, "key": "api_token", "value": "secret-of-a"},
        headers=a["agent_headers"],
    )
    assert stored.status_code == 201
    # The response echoes the caller's own agent id, never a namespaced key.
    assert stored.json() == {
        "status": "stored",
        "agent_id": AGENT_ID,
        "key": "api_token",
    }

    # B cannot recall A's memory under the shared agent id.
    foreign = await _recall(client, b["agent_headers"])
    assert foreign.status_code == 200
    assert foreign.json() == {"memories": []}
    assert "secret-of-a" not in foreign.text

    # Prefixing the agent id with A's wallet id does not reach A's namespace.
    spoofed = await _recall(
        client, b["agent_headers"], f"{a['agent_wallet_id']}:{AGENT_ID}"
    )
    assert spoofed.status_code == 200
    assert spoofed.json() == {"memories": []}
    assert "secret-of-a" not in spoofed.text

    # B writing under the same agent id does not poison A's memory.
    poisoned = await client.post(
        "/v1/ai/memory",
        json={"agent_id": AGENT_ID, "key": "api_token", "value": "poison-from-b"},
        headers=b["agent_headers"],
    )
    assert poisoned.status_code == 201

    own = await _recall(client, a["agent_headers"])
    assert own.status_code == 200
    assert [m["value"] for m in own.json()["memories"]] == ["secret-of-a"]
    assert "poison-from-b" not in own.text


@pytest.mark.proof
@pytest.mark.anyio
async def test_ai_learned_patterns_scoped_to_caller(client, clean_database):
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)

    learned = await client.post(
        "/v1/ai/learn",
        json={"agent_id": AGENT_ID, "experience": {"note": "lesson-of-a"}},
        headers=a["agent_headers"],
    )
    assert learned.status_code == 200

    foreign = await _recall(client, b["agent_headers"])
    assert foreign.status_code == 200
    assert foreign.json() == {"memories": []}
    assert "lesson-of-a" not in foreign.text

    own = await _recall(client, a["agent_headers"])
    memories = own.json()["memories"]
    assert [m["key"] for m in memories] == ["learned_pattern"]
    assert memories[0]["value"]["experience"] == {"note": "lesson-of-a"}


@pytest.mark.proof
@pytest.mark.anyio
async def test_ai_heal_not_readable_across_tenants(client, clean_database):
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)

    created = await client.post(
        "/v1/ai/heal",
        json={"issue": "issue-of-a", "context": {"trace": "trace-of-a"}},
        headers=a["agent_headers"],
    )
    assert created.status_code == 200
    heal_id = created.json()["heal_id"]

    # B gets the same 404 as for an unknown heal: no existence oracle and
    # nothing of A's heal (or A's wallet) in the body.
    foreign = await client.get(f"/v1/ai/heal/{heal_id}", headers=b["agent_headers"])
    missing = await client.get("/v1/ai/heal/does-not-exist", headers=b["agent_headers"])
    assert foreign.status_code == 404
    assert foreign.json() == missing.json()
    assert "issue-of-a" not in foreign.text
    assert a["agent_wallet_id"] not in foreign.text

    # The owner and a bootstrap admin can still read it.
    own = await client.get(f"/v1/ai/heal/{heal_id}", headers=a["agent_headers"])
    assert own.status_code == 200
    assert own.json()["issue"] == "issue-of-a"
    assert a["agent_wallet_id"] not in own.text
    admin = await client.get(f"/v1/ai/heal/{heal_id}", headers=BOOTSTRAP_HEADERS)
    assert admin.status_code == 200
    assert admin.json()["heal_id"] == heal_id


@pytest.mark.proof
@pytest.mark.anyio
async def test_ai_admin_heal_not_readable_by_wallet_key(client, clean_database):
    b = await provision_agent_wallet(client)

    created = await client.post(
        "/v1/ai/heal",
        json={"issue": "issue-of-admin", "context": {}},
        headers=BOOTSTRAP_HEADERS,
    )
    assert created.status_code == 200
    heal_id = created.json()["heal_id"]

    foreign = await client.get(f"/v1/ai/heal/{heal_id}", headers=b["agent_headers"])
    assert foreign.status_code == 404
    assert "issue-of-admin" not in foreign.text


@pytest.mark.proof
@pytest.mark.anyio
async def test_ai_bootstrap_admin_flow_unchanged(client, clean_database):
    """The single bootstrap key keeps working end to end on both sides."""
    decision = await _decide(client, BOOTSTRAP_HEADERS, "admin-context")
    listed = await client.get(f"/v1/ai/decisions/{AGENT_ID}", headers=BOOTSTRAP_HEADERS)
    assert listed.status_code == 200
    assert [d["decision_id"] for d in listed.json()] == [decision]

    stored = await client.post(
        "/v1/ai/memory",
        json={"agent_id": AGENT_ID, "key": "k", "value": "admin-value"},
        headers=BOOTSTRAP_HEADERS,
    )
    assert stored.status_code == 201
    recalled = await _recall(client, BOOTSTRAP_HEADERS)
    assert [m["value"] for m in recalled.json()["memories"]] == ["admin-value"]

    query = await client.post(
        "/v1/ai/query", json={"question": "status?"}, headers=BOOTSTRAP_HEADERS
    )
    assert query.status_code == 200


@pytest.mark.proof
@pytest.mark.anyio
async def test_ai_wallet_data_not_visible_in_admin_namespace(client, clean_database):
    """Wallet-scoped agent data stays out of the shared bootstrap namespace."""
    a = await provision_agent_wallet(client)
    await _decide(client, a["agent_headers"], "tenant-a-context")
    await client.post(
        "/v1/ai/memory",
        json={"agent_id": AGENT_ID, "key": "api_token", "value": "secret-of-a"},
        headers=a["agent_headers"],
    )

    listed = await client.get(f"/v1/ai/decisions/{AGENT_ID}", headers=BOOTSTRAP_HEADERS)
    assert listed.json() == []
    recalled = await _recall(client, BOOTSTRAP_HEADERS)
    assert recalled.json() == {"memories": []}


@pytest.mark.proof
@pytest.mark.anyio
async def test_ai_endpoints_require_auth(client, clean_database):
    calls = [
        ("post", "/v1/ai/decide", {"agent_id": AGENT_ID, "context": {}}),
        ("get", f"/v1/ai/decisions/{AGENT_ID}", None),
        ("post", "/v1/ai/heal", {"issue": "x", "context": {}}),
        ("get", "/v1/ai/heal/anything", None),
        ("post", "/v1/ai/query", {"question": "x"}),
        ("post", "/v1/ai/memory", {"agent_id": AGENT_ID, "key": "k", "value": "v"}),
        ("post", "/v1/ai/memory/recall", {"agent_id": AGENT_ID}),
        ("post", "/v1/ai/learn", {"agent_id": AGENT_ID, "experience": {}}),
    ]
    for method, path, body in calls:
        if body is None:
            resp = await client.request(method, path)
        else:
            resp = await client.request(method, path, json=body)
        assert resp.status_code == 401, (method, path, resp.status_code)
