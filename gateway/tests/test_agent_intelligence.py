"""Unit tests for the agent-intelligence service (no network, no database).

The service talks to the LLM and the durable-state store; both are replaced
with in-memory fakes here so the suite stays fast and hermetic.
"""

import json

import pytest

from app.services import agent_intelligence as ai_module
from app.services.agent_intelligence import (
    AgentIntelligence,
    _agent_key,
    get_agent_intelligence,
)
from app.services.llm import LLMResponse


class _FakeState:
    """In-memory stand-in for the durable-state store."""

    def __init__(self):
        self.saved = {}

    async def _ensure_ready(self):
        return None

    async def load_json(self, key):
        return self.saved.get(key)

    async def save_json(self, key, value):
        self.saved[key] = value
        return True


class _FakeLLM:
    """Deterministic LLM stand-in; ``content`` is the next response body."""

    def __init__(self, content=""):
        self.content = content
        self.seen = []

    async def chat(self, messages, system=None, tools=None, tool_choice=None):
        self.seen.append({"messages": messages, "system": system})
        return LLMResponse(content=self.content, model="fake", usage={})


@pytest.fixture
def service(monkeypatch):
    """A fresh AgentIntelligence wired to fakes."""
    state = _FakeState()
    llm = _FakeLLM()
    monkeypatch.setattr(ai_module, "get_durable_state", lambda: state)
    monkeypatch.setattr(ai_module, "get_llm_service", lambda: llm)
    svc = AgentIntelligence()
    svc._llm = llm
    return svc


def _decision_payload(**overrides):
    payload = {"reasoning": "scale up", "action": "add-replica", "confidence": 0.8}
    payload.update(overrides)
    return json.dumps(payload)


@pytest.mark.anyio
async def test_decide_parses_json_and_stores(service):
    service._llm.content = _decision_payload()
    decision = await service.decide("bot", {"load": 0.9}, options=["add-replica"])

    assert decision.agent_id == "bot"
    assert decision.reasoning == "scale up"
    assert decision.action == "add-replica"
    assert decision.confidence == 0.8
    assert decision.context == {"load": 0.9}
    assert service.get_decisions("bot") == [decision]
    assert "agent_intelligence.decisions" in service._state.saved


@pytest.mark.anyio
async def test_decide_sends_options_to_llm(service):
    service._llm.content = _decision_payload()
    await service.decide("bot", {}, options=["a", "b"])

    prompt = service._llm.seen[0]["messages"][0]["content"]
    assert "- a" in prompt
    assert "- b" in prompt


@pytest.mark.anyio
async def test_decide_falls_back_on_non_json_response(service):
    """Edge case: a plain-text LLM reply becomes the action, not an error."""
    service._llm.content = "restart the worker\nsecond line"
    decision = await service.decide("bot", {})

    assert decision.reasoning == "restart the worker\nsecond line"
    assert decision.action == "restart the worker"
    assert decision.confidence == 0.5


@pytest.mark.anyio
async def test_decide_caps_history_at_100_per_agent(service):
    service._llm.content = _decision_payload(action="noop")
    for _ in range(105):
        await service.decide("bot", {})

    stored = service.get_decisions("bot", limit=200)
    assert len(stored) == 100


@pytest.mark.anyio
async def test_diagnose_and_heal_parses_json_and_needs_approval(service):
    service._llm.content = json.dumps({"diagnosis": "disk full", "fix": "rm cache"})
    result = await service.diagnose_and_heal("crash", {"trace": "..."})

    assert result.issue == "crash"
    assert result.diagnosis == "disk full"
    assert result.code_change == "rm cache"
    assert result.fix_applied is False
    assert result.success is False
    assert service.get_heal(result.heal_id) == result


@pytest.mark.anyio
async def test_diagnose_and_heal_falls_back_on_non_json_response(service):
    """Error case: an unparseable LLM reply is kept as the raw fix text."""
    service._llm.content = "not json at all"
    result = await service.diagnose_and_heal("crash", {})

    assert result.diagnosis == "Could not parse LLM response"
    assert result.code_change == "not json at all"


def test_get_heal_unknown_id_returns_none(service):
    assert service.get_heal("missing") is None


@pytest.mark.anyio
async def test_query_returns_llm_answer(service):
    service._llm.content = "all systems nominal"
    assert await service.query("status?") == "all systems nominal"


@pytest.mark.anyio
async def test_query_includes_data_context(service):
    service._llm.content = "ok"
    await service.query("status?", data_context={"agents": 2})

    contexts = [m for m in service._llm.seen[0]["messages"] if m["role"] == "system"]
    assert len(contexts) == 1
    assert "2" in contexts[0]["content"]


@pytest.mark.anyio
async def test_remember_and_recall_with_key_filter_and_limit(service):
    await service.remember("bot", "color", "blue")
    await service.remember("bot", "color", "red")
    await service.remember("bot", "size", "large")

    assert [m["value"] for m in await service.recall("bot", key="color")] == [
        "blue",
        "red",
    ]
    assert [m["value"] for m in await service.recall("bot", limit=1)] == ["large"]
    assert await service.recall("unknown-agent") == []


@pytest.mark.anyio
async def test_wallet_namespacing_isolates_decisions_and_memory(service):
    service._llm.content = _decision_payload()
    await service.decide("bot", {"by": "a"}, owner_wallet_id="spn-aaa")
    await service.decide("bot", {"by": "b"}, owner_wallet_id="spn-bbb")

    own = service.get_decisions("bot", owner_wallet_id="spn-aaa")
    assert [d.context for d in own] == [{"by": "a"}]
    other = service.get_decisions("bot", owner_wallet_id="spn-bbb")
    assert [d.context for d in other] == [{"by": "b"}]
    assert service.get_decisions("bot") == []

    await service.remember("bot", "k", "v-a", owner_wallet_id="spn-aaa")
    assert await service.recall("bot", owner_wallet_id="spn-bbb") == []
    assert [
        m["value"] for m in await service.recall("bot", owner_wallet_id="spn-aaa")
    ] == ["v-a"]


@pytest.mark.anyio
async def test_learn_stores_pattern_and_returns_insight(service):
    service._llm.content = "retry storms follow deploys"
    insight = await service.learn("bot", {"event": "deploy"})

    assert insight == "retry storms follow deploys"
    memories = await service.recall("bot", key="learned_pattern")
    assert len(memories) == 1
    assert memories[0]["value"]["experience"] == {"event": "deploy"}
    assert memories[0]["value"]["insight"] == "retry storms follow deploys"


@pytest.mark.anyio
async def test_initialize_restores_persisted_state(service):
    service._llm.content = _decision_payload()
    await service.decide("bot", {})
    await service.remember("bot", "k", "v")

    fresh = AgentIntelligence()
    fresh._state = service._state
    fresh._llm = service._llm
    await fresh.initialize()

    assert [d.action for d in fresh.get_decisions("bot")] == ["add-replica"]
    assert [m["value"] for m in await fresh.recall("bot")] == ["v"]


def test_agent_key_scopes_by_wallet():
    assert _agent_key("bot", None) == "bot"
    assert _agent_key("bot", "spn-aaa") == "spn-aaa:bot"
    assert _agent_key("bot", "spn-aaa") != _agent_key("bot", "spn-bbb")


def test_singleton_returns_same_instance(monkeypatch):
    monkeypatch.setattr(ai_module, "_agent_intelligence", None)
    monkeypatch.setattr(ai_module, "get_durable_state", _FakeState)
    monkeypatch.setattr(ai_module, "get_llm_service", lambda: _FakeLLM())
    try:
        assert get_agent_intelligence() is get_agent_intelligence()
    finally:
        monkeypatch.setattr(ai_module, "_agent_intelligence", None)
