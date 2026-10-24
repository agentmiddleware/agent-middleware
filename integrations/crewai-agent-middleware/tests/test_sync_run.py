"""CrewAI calls tools synchronously, often from a worker thread."""

from __future__ import annotations

import asyncio
import threading

import httpx

from crewai_b2a import B2AClient, CrewAIB2ATool


def _tool(handler) -> tuple[CrewAIB2ATool, B2AClient]:
    transport = httpx.MockTransport(handler)
    client = B2AClient(
        api_key="test-key",
        base_url="http://127.0.0.1:9",
        transport=transport,
    )
    tool = CrewAIB2ATool(
        api_key="test-key",
        wallet_id="wallet-1",
        base_url="http://127.0.0.1:9",
    )
    tool.client = client
    return tool, client


def test_sync_run_from_a_worker_thread_calls_the_api():
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"balance": 3})

    tool, client = _tool(handler)
    box: list[str] = []

    def work() -> None:
        box.append(tool._run("balance"))
        box.append(tool._run("balance"))

    thread = threading.Thread(target=work)
    thread.start()
    thread.join(timeout=10)

    try:
        assert not thread.is_alive()
        assert box == ["Balance: 3 credits", "Balance: 3 credits"]
        assert seen == [
            "/v1/billing/wallets/wallet-1",
            "/v1/billing/wallets/wallet-1",
        ]
    finally:
        if hasattr(tool, "_sync_await"):
            tool._sync_await(client.close())
            tool._stop_sync_runner()


def test_blank_tool_name_and_bad_arguments_do_not_mint_a_permit():
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(500, json={"detail": "should not be called"})

    tool, client = _tool(handler)

    async def run() -> tuple[str, str]:
        try:
            blank = await tool._arun(
                operation="call_tool",
                tool_name="  ",
                idempotency_key="invoke-1",
                permit_idempotency_key="permit-1",
                arguments={},
            )
            bad_args = await tool._arun(
                operation="call_tool",
                tool_name="partner.search",
                idempotency_key="invoke-1",
                permit_idempotency_key="permit-1",
                arguments=["nope"],
            )
            return blank, bad_args
        finally:
            await client.close()

    blank, bad_args = asyncio.run(run())
    assert "tool_name" in blank
    assert "arguments" in bad_args
    assert seen == []


def test_permit_body_is_stored_while_holding_the_cache_lock():
    tool, client = _tool(lambda request: httpx.Response(404))
    seen: dict[str, bool] = {}

    class LockedDict(dict):
        def get(self, key, default=None):
            seen["get_locked"] = tool._permit_lock.locked()
            return super().get(key, default)

        def __setitem__(self, key, value) -> None:
            seen["set_locked"] = tool._permit_lock.locked()
            super().__setitem__(key, value)

    tool._permit_requests = LockedDict()
    try:
        first = tool._permit_request("partner.search", "permit-key")
        second = tool._permit_request("partner.search", "permit-key")
    finally:
        asyncio.run(client.close())

    assert first is second
    assert seen == {"get_locked": True, "set_locked": True}
