"""Tests for the legacy framework client (framework_integrations.client)."""

import asyncio
import json

import httpx
import pytest
from framework_integrations.client import B2AClient, B2AConfig


def _client(handler):
    client = B2AClient(B2AConfig(api_key="k", wallet_id="w1"))
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


async def _close(client):
    await client._client.aclose()


def test_charge_validation_happens_before_any_request():
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    client = _client(handler)
    try:
        with pytest.raises(ValueError, match="must not be blank"):
            asyncio.run(client.charge("svc", idempotency_key="  "))
        for bad in (-1.0, float("nan"), float("inf")):
            with pytest.raises(ValueError, match="units"):
                asyncio.run(client.charge("svc", units=bad, idempotency_key="k"))
        assert seen == []
    finally:
        asyncio.run(_close(client))


def test_charge_sends_wallet_service_units_and_key():
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        captured["headers"] = dict(request.headers)
        captured["body"] = request.content
        return httpx.Response(200, json={"charged": True})

    client = _client(handler)
    try:
        result = asyncio.run(
            client.charge(
                "svc",
                units=2.0,
                description="d",
                idempotency_key="key-1",
                request_path="/x",
            )
        )
        assert result == {"charged": True}
        assert captured["params"]["wallet_id"] == "w1"
        assert captured["params"]["service"] == "svc"
        assert captured["params"]["units"] == "2.0"
        assert captured["params"]["description"] == "d"
        assert captured["headers"]["idempotency-key"] == "key-1"
        assert captured["headers"]["x-api-key"] == "k"
    finally:
        asyncio.run(_close(client))


def test_decide_refuses_empty_options_before_any_request():
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"action": "b"})

    client = _client(handler)
    try:
        with pytest.raises(ValueError, match="options"):
            asyncio.run(client.decide({}, []))
        assert seen == []
        assert asyncio.run(client.decide({}, ["a", "b"])) == "b"
    finally:
        asyncio.run(_close(client))


def test_decide_refuses_a_response_without_an_action():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = _client(handler)
    try:
        with pytest.raises(ValueError, match="action"):
            asyncio.run(client.decide({"x": 1}, ["a", "b"]))
    finally:
        asyncio.run(_close(client))


def test_awi_execute_validation_and_headers():
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"ok": True})

    client = _client(handler)
    try:
        with pytest.raises(ValueError, match="permit_id"):
            asyncio.run(
                client.execute_awi_action(
                    "s", "click", {}, permit_id=" ", idempotency_key="k"
                )
            )
        with pytest.raises(ValueError, match="idempotency_key"):
            asyncio.run(
                client.execute_awi_action(
                    "s", "click", {}, permit_id="p", idempotency_key=" "
                )
            )
        with pytest.raises(ValueError, match="128"):
            asyncio.run(
                client.execute_awi_action(
                    "s", "click", {}, permit_id="p", idempotency_key="k" * 129
                )
            )
        assert captured == {}
        asyncio.run(
            client.execute_awi_action(
                "s", "click", {}, permit_id="p", idempotency_key="k"
            )
        )
        assert captured["headers"]["x-permit-id"] == "p"
        assert captured["headers"]["idempotency-key"] == "k"
    finally:
        asyncio.run(_close(client))


def test_thin_wrappers_shape_and_auth():
    """Balance, telemetry, messaging, heal, discovery and session creation all
    carry the API key and the wallet-scoped identity the server authorizes on."""
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured[request.url.path] = {
            "headers": dict(request.headers),
            "params": dict(request.url.params),
            "body": json.loads(request.content) if request.content else None,
        }
        if request.url.path.endswith("wallets/w1"):
            return httpx.Response(200, json={"balance": 7.5})
        return httpx.Response(200, json={"ok": True})

    client = _client(handler)
    try:
        assert asyncio.run(client.get_balance()) == 7.5
        asyncio.run(client.emit_telemetry("ev", {"a": 1}))
        asyncio.run(client.send_message("agent-2", {"hi": 1}))
        asyncio.run(client.heal("issue", {"ctx": 1}))
        asyncio.run(client.create_awi_session("https://example.com"))
        asyncio.run(client.discover())
        asyncio.run(client.get_mcp_tools())
        for path, data in captured.items():
            assert data["headers"]["x-api-key"] == "k", path
        telemetry = captured["/v1/telemetry/events/single"]["body"]
        assert telemetry["message"] == "ev"
        assert telemetry["metadata"] == {"a": 1, "event": "ev"}
        assert captured["/v1/comms/messages"]["params"]["from_agent"] == "w1"
    finally:
        asyncio.run(_close(client))
