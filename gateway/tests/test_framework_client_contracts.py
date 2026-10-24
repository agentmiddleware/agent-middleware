"""B2AClient must speak the routes that are actually mounted.

The legacy client used to invent a decision, post telemetry and messages in
shapes the routers reject, omit the wallet on AWI session create, treat a
missing balance as zero, and copy caller text straight into a URL or header.
"""

from __future__ import annotations

import json

import httpx
import pytest

from framework_integrations import B2AClient


def _client(handler, **kwargs) -> B2AClient:
    client = B2AClient(
        api_url=kwargs.pop("api_url", "http://b2a.test"),
        api_key=kwargs.pop("api_key", "k"),
        wallet_id=kwargs.pop("wallet_id", "wal-1"),
        **kwargs,
    )
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


@pytest.mark.anyio
async def test_decide_returns_the_server_action_not_the_first_option():
    """The route answers with `action`. The first option is not a decision."""
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(
            200,
            json={
                "decision_id": "d1",
                "agent_id": "wal-1",
                "action": "ship",
                "reasoning": "ready",
                "confidence": 0.9,
            },
        )

    client = _client(handler)
    try:
        choice = await client.decide({"goal": "release"}, ["hold", "ship"])
    finally:
        await client.close()

    assert choice == "ship"
    assert sent[0].url.path == "/v1/ai/decide"
    body = json.loads(sent[0].content)
    assert body == {
        "agent_id": "wal-1",
        "context": {"goal": "release"},
        "options": ["hold", "ship"],
    }


@pytest.mark.anyio
async def test_decide_refuses_a_response_with_no_action():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"decision": "hold"})

    client = _client(handler)
    try:
        with pytest.raises(ValueError, match="action"):
            await client.decide({"goal": "release"}, ["hold", "ship"])
    finally:
        await client.close()


@pytest.mark.anyio
async def test_emit_telemetry_posts_a_batch_the_route_accepts():
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(202, json={"ingested": 1, "failed": 0, "batch_id": "b1"})

    client = _client(handler)
    try:
        named = await client.emit_telemetry("task_completed", {"step": 1})
        typed = await client.emit_telemetry("error", {"code": 500})
    finally:
        await client.close()

    assert named["batch_id"] == "b1"
    assert typed["batch_id"] == "b1"
    first, second = (json.loads(request.content) for request in sent)
    assert first == {
        "events": [
            {
                "event_type": "custom",
                "source": "framework_integrations",
                "message": "task_completed",
                "metadata": {"step": 1, "event": "task_completed"},
            }
        ]
    }
    assert second["events"][0]["event_type"] == "error"
    assert second["events"][0]["message"] == "error"
    assert all(request.url.path == "/v1/telemetry/events" for request in sent)


@pytest.mark.anyio
async def test_emit_telemetry_surfaces_batch_ingestion_failure():
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(
            202,
            json={"ingested": 0, "failed": 1, "batch_id": "b1", "errors": [{}]},
        )

    client = _client(handler)
    try:
        with pytest.raises(RuntimeError, match="telemetry ingestion failed"):
            await client.emit_telemetry("task_completed")
    finally:
        await client.close()

    assert len(sent) == 1
    assert sent[0].url.path == "/v1/telemetry/events"


@pytest.mark.anyio
async def test_emit_telemetry_rejects_a_missing_failure_count():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"ingested": 1, "batch_id": "b1"})

    client = _client(handler)
    try:
        with pytest.raises(ValueError, match="failed count"):
            await client.emit_telemetry("task_completed")
    finally:
        await client.close()


@pytest.mark.anyio
async def test_send_message_uses_the_comms_query_and_body():
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(202, json={"message_id": "msg-1"})

    client = _client(handler)
    try:
        await client.send_message("agent-2", {"hello": "world"}, "HIGH")
    finally:
        await client.close()

    request = sent[0]
    assert request.url.path == "/v1/comms/messages"
    assert request.url.params["from_agent"] == "wal-1"
    assert json.loads(request.content) == {
        "to_agent": "agent-2",
        "message_type": "request",
        "priority": "high",
        "subject": "agent message",
        "body": {"hello": "world"},
    }


@pytest.mark.anyio
async def test_send_message_uses_registered_sender_id_when_wallet_differs():
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.url.path.startswith("/v1/billing/wallets/"):
            return httpx.Response(200, json={"balance": 1})
        return httpx.Response(202, json={"message_id": "msg-1"})

    client = _client(handler, sender_agent_id="agent-1")
    try:
        await client.send_message("agent-2", {"hello": "world"})
        await client.get_balance()
    finally:
        await client.close()

    assert sent[0].url.params["from_agent"] == "agent-1"
    assert sent[1].url.path == "/v1/billing/wallets/wal-1"


@pytest.mark.anyio
@pytest.mark.parametrize("sender_agent_id", ["", "agent\r\nbad", 123])
async def test_send_message_rejects_invalid_sender_id_before_send(sender_agent_id):
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(202, json={"message_id": "msg-1"})

    client = _client(handler, sender_agent_id=sender_agent_id)
    try:
        with pytest.raises(ValueError, match="sender_agent_id"):
            await client.send_message("agent-2", {"hello": "world"})
    finally:
        await client.close()

    assert sent == []


@pytest.mark.anyio
async def test_create_awi_session_sends_the_configured_wallet():
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(201, json={"session_id": "awi-1"})

    client = _client(handler)
    try:
        await client.create_awi_session("https://example.com", max_steps=5)
    finally:
        await client.close()

    assert json.loads(sent[0].content) == {
        "target_url": "https://example.com",
        "max_steps": 5,
        "wallet_id": "wal-1",
    }


@pytest.mark.anyio
async def test_get_balance_does_not_invent_zero():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"wallet_id": "wal-1"})

    client = _client(handler)
    try:
        with pytest.raises(ValueError, match="balance"):
            await client.get_balance()
    finally:
        await client.close()


@pytest.mark.anyio
async def test_get_balance_rejects_a_boolean_balance():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"balance": True})

    client = _client(handler)
    try:
        with pytest.raises(ValueError, match="balance"):
            await client.get_balance()
    finally:
        await client.close()


@pytest.mark.anyio
async def test_get_balance_rejects_an_integer_too_large_for_float():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"balance": 10**400})

    client = _client(handler)
    try:
        with pytest.raises(ValueError, match="balance is not a number"):
            await client.get_balance()
    finally:
        await client.close()


@pytest.mark.anyio
async def test_get_mcp_tools_rejects_missing_tools_key():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "manifest unavailable"})

    client = _client(handler)
    try:
        with pytest.raises(ValueError, match="tools manifest"):
            await client.get_mcp_tools()
    finally:
        await client.close()


@pytest.mark.anyio
async def test_trailing_slash_on_api_url_does_not_double_the_path():
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"name": "middleware"})

    client = _client(handler, api_url="http://b2a.test/")
    try:
        await client.discover()
    finally:
        await client.close()

    assert sent[0].url.path == "/v1/discover"


@pytest.mark.anyio
async def test_wallet_id_cannot_escape_the_balance_path():
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"balance": 1})

    client = _client(handler, wallet_id="wal/../../admin/secret")
    try:
        with pytest.raises(ValueError, match="wallet_id"):
            await client.get_balance()
    finally:
        await client.close()
    assert sent == []


@pytest.mark.anyio
async def test_header_fields_reject_line_breaks_before_send():
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={})

    client = _client(handler, api_key="k\r\nX-Evil: 1")
    try:
        with pytest.raises(ValueError, match="api_key"):
            await client.discover()
        with pytest.raises(ValueError, match="idempotency_key"):
            await client.charge("iot_bridge", idempotency_key="charge\nkey")
        with pytest.raises(ValueError, match="permit_id"):
            await client.execute_awi_action(
                "session",
                "navigate_to",
                {},
                permit_id="permit\r\nx",
                idempotency_key="awi-key",
            )
    finally:
        await client.close()
    assert sent == []
