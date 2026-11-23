"""The sync runner uses the caller's separately configured client."""

import asyncio

import httpx
import pytest

from crewai_b2a import B2AClient, CrewAIB2ATool


def test_sync_run_uses_configured_url_timeout_and_transport():
    requests: list[httpx.Request] = []
    clients: list[B2AClient] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, json={"detail": "unavailable"})

    def make_client() -> B2AClient:
        client = B2AClient(
            api_key="test-key",
            base_url="https://caller.example.invalid",
            timeout=2.75,
            transport=httpx.MockTransport(handler),
            bearer_token="test-token",
        )
        clients.append(client)
        return client

    borrowed_client = make_client()
    tool = CrewAIB2ATool(
        api_key="test-key",
        wallet_id="wallet",
        base_url="https://tool.example.invalid",
        client=borrowed_client,
        sync_client_factory=make_client,
    )
    try:
        result = tool._run(operation="balance")
        assert result.startswith("Error:")
        assert len(clients) == 2
        assert tool._sync_client is clients[1]
        assert tool._sync_client is not borrowed_client
        assert len(requests) == 1
        request = requests[0]
        assert request.url.host == "caller.example.invalid"
        assert request.url.path == "/v1/billing/wallets/wallet"
        assert request.extensions["timeout"]["connect"] == 2.75
        assert request.headers["authorization"] == "Bearer test-token"
    finally:
        tool.close()
        asyncio.run(borrowed_client.close())


def test_sync_run_refuses_borrowed_client_without_runner_factory():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503)

    borrowed_client = B2AClient(
        api_key="test-key",
        base_url="https://caller.example.invalid",
        transport=httpx.MockTransport(handler),
    )
    tool = CrewAIB2ATool(
        api_key="test-key",
        wallet_id="wallet",
        base_url="https://tool.example.invalid",
        client=borrowed_client,
    )
    try:
        assert tool._run(operation="balance") == (
            "Error: sync_client_factory is required for sync calls with a borrowed client"
        )
        assert requests == []
    finally:
        tool.close()
        asyncio.run(borrowed_client.close())


@pytest.mark.asyncio
async def test_sync_run_uses_factory_inside_running_loop():
    requests: list[httpx.Request] = []
    clients: list[B2AClient] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"balance": 3})

    def make_client() -> B2AClient:
        client = B2AClient(
            api_key="test-key",
            transport=httpx.MockTransport(handler),
        )
        clients.append(client)
        return client

    borrowed_client = make_client()
    tool = CrewAIB2ATool(
        api_key="test-key",
        wallet_id="wallet",
        client=borrowed_client,
        sync_client_factory=make_client,
    )
    try:
        assert tool._run(operation="balance") == "Balance: 3 credits"
        assert len(clients) == 2
        assert tool._sync_client is clients[1]
        assert requests[0].url.path == "/v1/billing/wallets/wallet"
    finally:
        tool.close()
        await borrowed_client.close()


@pytest.mark.asyncio
async def test_sync_run_refuses_borrowed_client_without_factory_inside_running_loop():
    borrowed_client = B2AClient(api_key="test-key")
    tool = CrewAIB2ATool(api_key="test-key", wallet_id="wallet", client=borrowed_client)
    try:
        assert tool._run(operation="balance") == (
            "Error: sync_client_factory is required for sync calls with a borrowed client"
        )
        assert tool._sync_runner_loop is None
    finally:
        tool.close()
        await borrowed_client.close()
