"""Legacy client request contracts and malformed-response checks.

Successful response behavior is exercised against the gateway app in
``gateway/tests/test_framework_client_gateway_contract.py``.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from framework_integrations.client import B2AClient

_API = "http://127.0.0.1:9"


class _RequestCaptured(Exception):
    """Stop a mock request before fabricating a gateway response."""


def _client(monkeypatch: pytest.MonkeyPatch, handler) -> B2AClient:
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr("framework_integrations.client.httpx.AsyncClient", factory)
    return B2AClient(api_url=_API, api_key="key-tenant-a", wallet_id="wallet-tenant-a")


def _capture(seen: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise _RequestCaptured

    return handler


@pytest.mark.parametrize("payload", [{"reasoning": "unsure"}, {"action": " "}, []])
def test_decide_rejects_a_response_without_a_nonblank_action(monkeypatch, payload):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=payload)

    client = _client(monkeypatch, handler)

    async def run() -> None:
        try:
            await client.decide({"case": 1}, ["charge", "wait"])
        finally:
            await client.close()

    with pytest.raises(ValueError, match="action"):
        asyncio.run(run())
    assert len(seen) == 1


def test_emit_telemetry_posts_gateway_event_bodies(monkeypatch):
    seen: list[httpx.Request] = []
    client = _client(monkeypatch, _capture(seen))

    async def run() -> None:
        try:
            with pytest.raises(_RequestCaptured):
                await client.emit_telemetry("task_completed", {"latency_ms": 5})
            with pytest.raises(_RequestCaptured):
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
    assert json.loads(seen[0].content) == {
        "event_type": "custom",
        "source": "framework_integrations",
        "message": "task_completed",
        "severity": "info",
        "metadata": {"latency_ms": 5, "event": "task_completed"},
    }
    assert json.loads(seen[1].content) == {
        "event_type": "error",
        "source": "agent",
        "message": "disk full",
        "severity": "high",
        "metadata": {"code": 7},
    }
    assert seen[0].headers["x-api-key"] == "key-tenant-a"


@pytest.mark.parametrize("severity", [["high"], {"level": "high"}])
def test_emit_telemetry_defaults_non_string_severity(monkeypatch, severity):
    seen: list[httpx.Request] = []
    client = _client(monkeypatch, _capture(seen))

    async def run() -> None:
        try:
            with pytest.raises(_RequestCaptured):
                await client.emit_telemetry("warning", {"severity": severity})
        finally:
            await client.close()

    asyncio.run(run())
    assert len(seen) == 1
    assert json.loads(seen[0].content)["severity"] == "info"


@pytest.mark.parametrize(
    ("event", "properties"),
    [
        ("", None),
        (" \t", {"message": ""}),
        (None, {"message": " \t"}),
    ],
)
def test_emit_telemetry_rejects_blank_effective_message_before_sending(
    monkeypatch, event, properties
):
    seen: list[httpx.Request] = []
    client = _client(monkeypatch, _capture(seen))

    async def run() -> None:
        try:
            await client.emit_telemetry(event, properties)
        finally:
            await client.close()

    with pytest.raises(ValueError, match="telemetry message"):
        asyncio.run(run())
    assert seen == []


def test_send_message_matches_the_comms_request_contract(monkeypatch):
    seen: list[httpx.Request] = []
    client = _client(monkeypatch, _capture(seen))

    async def run() -> None:
        try:
            await client.send_message(
                "agent-2", {"subject": "ping", "text": "hello"}, priority="high"
            )
        finally:
            await client.close()

    with pytest.raises(_RequestCaptured):
        asyncio.run(run())
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
    client = _client(monkeypatch, _capture(seen))

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
    client = _client(monkeypatch, _capture(seen))

    async def run() -> None:
        try:
            await client.create_awi_session("https://example.com/start", max_steps=12)
        finally:
            await client.close()

    with pytest.raises(_RequestCaptured):
        asyncio.run(run())
    request = seen[0]
    assert request.url.path == "/v1/awi/sessions"
    assert request.headers["x-api-key"] == "key-tenant-a"
    assert json.loads(request.content) == {
        "target_url": "https://example.com/start",
        "wallet_id": "wallet-tenant-a",
        "max_steps": 12,
    }


@pytest.mark.parametrize("max_steps", [0, True, 1001])
def test_create_awi_session_rejects_invalid_steps_before_sending(
    monkeypatch, max_steps
):
    seen: list[httpx.Request] = []
    client = _client(monkeypatch, _capture(seen))

    async def run() -> None:
        try:
            await client.create_awi_session(
                "https://example.com/start", max_steps=max_steps
            )
        finally:
            await client.close()

    with pytest.raises(ValueError, match="max_steps"):
        asyncio.run(run())
    assert seen == []


@pytest.mark.parametrize(
    "payload",
    [
        {"wallet_id": "wallet-tenant-a"},
        {"balance": True},
        {"balance": "4"},
        [],
        b'{"balance": Infinity}',
    ],
)
def test_get_balance_rejects_invalid_gateway_data(monkeypatch, payload):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if isinstance(payload, bytes):
            return httpx.Response(200, content=payload)
        return httpx.Response(200, json=payload)

    client = _client(monkeypatch, handler)

    async def run() -> None:
        try:
            await client.get_balance()
        finally:
            await client.close()

    with pytest.raises(ValueError, match="balance"):
        asyncio.run(run())
    assert len(seen) == 1


@pytest.mark.parametrize("key", [" ", "k" * 129])
def test_charge_rejects_invalid_idempotency_key_before_sending(monkeypatch, key):
    seen: list[httpx.Request] = []
    client = _client(monkeypatch, _capture(seen))

    async def run() -> None:
        try:
            await client.charge("api_call", idempotency_key=key)
        finally:
            await client.close()

    with pytest.raises(ValueError, match="idempotency_key"):
        asyncio.run(run())
    assert seen == []


def test_charge_sends_caller_owned_idempotency_key(monkeypatch):
    seen: list[httpx.Request] = []
    client = _client(monkeypatch, _capture(seen))

    async def run() -> None:
        try:
            await client.charge("api_call", idempotency_key="k" * 128)
        finally:
            await client.close()

    with pytest.raises(_RequestCaptured):
        asyncio.run(run())
    assert len(seen) == 1
    assert seen[0].headers["idempotency-key"] == "k" * 128
    assert seen[0].url.params["wallet_id"] == "wallet-tenant-a"
    assert seen[0].headers["x-api-key"] == "key-tenant-a"
