"""Exercise the legacy client against responses emitted by the gateway app."""

import json

import httpx
import pytest

from app.main import app
from app.services import agent_intelligence as agent_intelligence_module
from app.services.llm import LLMResponse
from framework_integrations.client import B2AClient
from tests.test_trust_helpers import provision_agent_wallet


class _LocalLLM:
    async def chat(self, messages, system=None, tools=None, tool_choice=None):
        return LLMResponse(
            content=json.dumps(
                {"reasoning": "selected option", "action": "wait", "confidence": 1}
            ),
            model="local",
            usage={"prompt_tokens": 0, "completion_tokens": 0},
        )


@pytest.mark.proof
@pytest.mark.anyio
async def test_legacy_client_reads_gateway_wallet_and_decision(
    monkeypatch, clean_database
):
    service = agent_intelligence_module.AgentIntelligence()
    service._llm = _LocalLLM()
    monkeypatch.setattr(agent_intelligence_module, "_agent_intelligence", service)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as gateway:
        identity = await provision_agent_wallet(gateway)
        key = identity["agent_headers"]["X-API-Key"]
        wallet_id = identity["agent_wallet_id"]
        legacy = B2AClient(api_url="http://test", api_key=key, wallet_id=wallet_id)
        await legacy._client.aclose()
        legacy._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )
        try:
            wallet = await gateway.get(
                f"/v1/billing/wallets/{wallet_id}", headers=identity["agent_headers"]
            )
            assert wallet.status_code == 200
            assert await legacy.get_balance() == float(wallet.json()["balance"])
            assert (
                await legacy.decide({"case": "gateway"}, ["charge", "wait"]) == "wait"
            )
            charge = await legacy.charge(
                "iot_bridge", idempotency_key="gateway-contract-1"
            )
            assert charge["wallet_id"] == wallet_id
            telemetry = await legacy.emit_telemetry("task_completed", {"latency_ms": 5})
            assert telemetry["ingested"] == 1
            session = await legacy.create_awi_session(
                "https://example.com/start", max_steps=12
            )
            assert session["wallet_id"] == wallet_id
        finally:
            await legacy.close()

        unauthorized = B2AClient(
            api_url="http://test", api_key="invalid-test-key", wallet_id=wallet_id
        )
        await unauthorized._client.aclose()
        unauthorized._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )
        try:
            with pytest.raises(httpx.HTTPStatusError) as exc:
                await unauthorized.get_balance()
            assert exc.value.response.status_code == 403
        finally:
            await unauthorized.close()
