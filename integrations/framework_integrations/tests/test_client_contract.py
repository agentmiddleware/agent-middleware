"""Contract tests for the legacy framework HTTP client.

These pin the gateway shapes the tools actually call. A mock transport records
every request so a wrong body fails the test without a server.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from framework_integrations.client import B2AClient

_API = "http://127.0.0.1:9"


def _client(monkeypatch: pytest.MonkeyPatch, handler) -> B2AClient:
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr("framework_integrations.client.httpx.AsyncClient", factory)
    return B2AClient(
        api_url=_API,
        api_key="key-tenant-a",
        wallet_id="wallet-tenant-a",
    )


def test_decide_returns_the_server_action_not_the_first_option(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "decision_id": "d1",
                "agent_id": "wallet-tenant-a",
                "reasoning": "waiting is cheaper",
                "action": "wait",
                "confidence": 0.8,
                "timestamp": "2026-01-01T00:00:00Z",
            },
        )

    client = _client(monkeypatch, handler)

    async def run() -> str:
        try:
            return await client.decide({"case": 1}, ["charge", "wait"])
        finally:
            await client.close()

    assert asyncio.run(run()) == "wait"
    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/v1/ai/decide"
    assert seen[0].headers["x-api-key"] == "key-tenant-a"
    assert body["agent_id"] == "wallet-tenant-a"
    assert body["options"] == ["charge", "wait"]


def test_decide_with_no_options_still_returns_the_server_action(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "action": "stay",
                "decision_id": "d2",
                "agent_id": "wallet-tenant-a",
                "reasoning": "no options were offered",
                "confidence": 1,
                "timestamp": "2026-01-01T00:00:00Z",
            },
        )

    client = _client(monkeypatch, handler)

    async def run() -> str:
        try:
            return await client.decide({}, [])
        finally:
            await client.close()

    assert asyncio.run(run()) == "stay"


def test_decide_without_an_action_does_not_invent_one(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"reasoning": "unsure"})

    client = _client(monkeypatch, handler)

    async def run() -> None:
        try:
            await client.decide({"case": 1}, ["charge", "wait"])
        finally:
            await client.close()

    with pytest.raises(ValueError, match="action"):
        asyncio.run(run())
    assert len(seen) == 1


def test_emit_telemetry_posts_one_gateway_event(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            202,
            json={"ingested": 1, "failed": 0, "batch_id": "b1", "errors": []},
        )

    client = _client(monkeypatch, handler)

    async def run() -> None:
        try:
            await client.emit_telemetry("task_completed", {"latency_ms": 5})
            await client.emit_telemetry(
                "error",
                {
                    "message": "disk full",
                    "source": "agent",
                    "severity": "high",
                    "code": 7,
                },
            )
        finally:
            await client.close()

    asyncio.run(run())
    assert [request.url.path for request in seen] == [
        "/v1/telemetry/events/single",
        "/v1/telemetry/events/single",
    ]
    custom = json.loads(seen[0].content)
    assert custom == {
        "event_type": "custom",
        "source": "framework_integrations",
        "message": "task_completed",
        "severity": "info",
        "metadata": {"latency_ms": 5, "event": "task_completed"},
    }
    error = json.loads(seen[1].content)
    assert error == {
        "event_type": "error",
        "source": "agent",
        "message": "disk full",
        "severity": "high",
        "metadata": {"code": 7},
    }
    assert seen[0].headers["x-api-key"] == "key-tenant-a"


def test_send_message_matches_the_comms_contract(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            202,
            json={
                "message_id": "m1",
                "from_agent": "wallet-tenant-a",
                "to_agent": "agent-2",
                "message_type": "request",
                "priority": "high",
                "subject": "ping",
                "delivery_status": "queued",
                "created_at": "2026-01-01T00:00:00Z",
                "delivered_at": None,
            },
        )

    client = _client(monkeypatch, handler)

    async def run() -> dict:
        try:
            return await client.send_message(
                "agent-2",
                {"subject": "ping", "text": "hello"},
                priority="high",
            )
        finally:
            await client.close()

    result = asyncio.run(run())
    assert result["message_id"] == "m1"
    request = seen[0]
    assert request.url.path == "/v1/comms/messages"
    assert request.url.params["from_agent"] == "wallet-tenant-a"
    assert request.headers["x-api-key"] == "key-tenant-a"
    assert json.loads(request.content) == {
        "to_agent": "agent-2",
        "message_type": "request",
        "priority": "high",
        "subject": "ping",
        "body": {"subject": "ping", "text": "hello"},
    }


def test_send_message_rejects_a_bad_priority_before_sending(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    client = _client(monkeypatch, handler)

    async def run() -> None:
        try:
            await client.send_message("agent-2", {"text": "hello"}, priority="urgent")
        finally:
            await client.close()

    with pytest.raises(ValueError, match="priority"):
        asyncio.run(run())
    assert seen == []


def test_create_awi_session_sends_the_configured_wallet(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"session_id": "sess-1"})

    client = _client(monkeypatch, handler)

    async def run() -> dict:
        try:
            return await client.create_awi_session(
                "https://example.com/start", max_steps=12
            )
        finally:
            await client.close()

    result = asyncio.run(run())
    assert result["session_id"] == "sess-1"
    request = seen[0]
    assert request.url.path == "/v1/awi/sessions"
    assert request.headers["x-api-key"] == "key-tenant-a"
    assert json.loads(request.content) == {
        "target_url": "https://example.com/start",
        "wallet_id": "wallet-tenant-a",
        "max_steps": 12,
    }


def test_create_awi_session_rejects_a_step_count_the_gateway_will_not_accept(
    monkeypatch,
):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"session_id": "sess-1"})

    client = _client(monkeypatch, handler)

    async def run() -> None:
        try:
            await client.create_awi_session("https://example.com/start", max_steps=0)
        finally:
            await client.close()

    with pytest.raises(ValueError, match="max_steps"):
        asyncio.run(run())
    assert seen == []


def test_get_balance_does_not_treat_a_missing_balance_as_zero(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/billing/wallets/wallet-tenant-a"
        assert request.headers["x-api-key"] == "key-tenant-a"
        return httpx.Response(200, json={"wallet_id": "wallet-tenant-a"})

    client = _client(monkeypatch, handler)

    async def run() -> None:
        try:
            await client.get_balance()
        finally:
            await client.close()

    with pytest.raises(ValueError, match="balance"):
        asyncio.run(run())


def test_get_balance_rejects_a_boolean_and_keeps_a_real_zero(monkeypatch):
    payloads: list[dict] = [{"balance": True}, {"balance": 0}, {"balance": 4.5}]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payloads.pop(0))

    client = _client(monkeypatch, handler)

    async def run() -> list[float]:
        try:
            with pytest.raises(ValueError, match="balance"):
                await client.get_balance()
            return [await client.get_balance(), await client.get_balance()]
        finally:
            await client.close()

    assert asyncio.run(run()) == [0.0, 4.5]


def test_charge_rejects_an_overlong_idempotency_key_before_sending(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ledger_id": "led-1"})

    client = _client(monkeypatch, handler)

    async def run() -> None:
        try:
            with pytest.raises(ValueError, match="128"):
                await client.charge("api_call", idempotency_key="k" * 129)
            assert seen == []
            await client.charge("api_call", idempotency_key="k" * 128)
            assert len(seen) == 1
            assert seen[0].headers["idempotency-key"] == "k" * 128
            assert seen[0].url.params["wallet_id"] == "wallet-tenant-a"
            assert seen[0].headers["x-api-key"] == "key-tenant-a"
        finally:
            await client.close()

    asyncio.run(run())
