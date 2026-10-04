"""Durable JSON reload and process-restart regressions (GH-513 / IP-001)."""

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.core.auth import AuthContext, get_auth_context
from app.core.config import get_settings
from app.core.durable_state import DurableStateStore
from app.routers import ai
from app.services import agent_intelligence


class FakeLLM:
    async def chat(self, *args, **kwargs):
        return SimpleNamespace(
            content=json.dumps(
                {
                    "reasoning": "synthetic",
                    "action": "wait",
                    "confidence": 0.9,
                    "diagnosis": "synthetic",
                    "fix": "synthetic",
                }
            )
        )


@pytest.mark.proof
@pytest.mark.anyio
@pytest.mark.parametrize("surface", ["decisions", "heal"])
@pytest.mark.parametrize("restart", [False, True])
async def test_durable_ai_roundtrip_preserves_response_types(
    tmp_path, monkeypatch, surface, restart
):
    settings = get_settings()
    monkeypatch.setattr(settings, "STATE_BACKEND", "sqlite")
    monkeypatch.setattr(settings, "SQLITE_URL", str(tmp_path / "state.db"))
    state = DurableStateStore()
    monkeypatch.setattr(agent_intelligence, "get_durable_state", lambda: state)
    service = agent_intelligence.AgentIntelligence()
    service._state = state
    service._llm = FakeLLM()
    monkeypatch.setattr(agent_intelligence, "_agent_intelligence", service)
    local_app = FastAPI()
    local_app.include_router(ai.router)
    local_app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        source="api_key",
        raw_key="synthetic",
        wallet_id="agt-synthetic",
        key_id="key-synthetic",
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=local_app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            if surface == "decisions":
                created = await client.post(
                    "/v1/ai/decide", json={"agent_id": "bot", "context": {}}
                )
                read_path = "/v1/ai/decisions/bot"
            else:
                created = await client.post(
                    "/v1/ai/heal", json={"issue": "synthetic", "context": {}}
                )
                read_path = f"/v1/ai/heal/{created.json()['heal_id']}"
            assert created.status_code == 200
            before = await client.get(read_path)
            assert before.status_code == 200
            if restart:
                replacement = agent_intelligence.AgentIntelligence()
                replacement._state = state
                replacement._llm = FakeLLM()
                monkeypatch.setattr(
                    agent_intelligence, "_agent_intelligence", replacement
                )
                restarted = await client.get(read_path)
                assert restarted.status_code == 200
                assert restarted.json() == before.json()
            # Any later initialized AI request reloads JSON from the durable backend.
            recalled = await client.post(
                "/v1/ai/memory/recall", json={"agent_id": "bot"}
            )
            assert recalled.status_code == 200
            after = await client.get(read_path)
            assert after.status_code == 200, (
                f"Durable reload changed HTTP200 to HTTP{after.status_code}"
            )
            assert after.json() == before.json()
            local_app.dependency_overrides[get_auth_context] = lambda: AuthContext(
                source="api_key",
                raw_key="synthetic-other",
                wallet_id="agt-other",
                key_id="key-other",
            )
            foreign = await client.get(read_path)
            if surface == "decisions":
                assert foreign.status_code == 200
                assert foreign.json() == []
            else:
                assert foreign.status_code == 404
    finally:
        await state.close()
