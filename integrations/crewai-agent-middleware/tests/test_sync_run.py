"""CrewAI calls tools synchronously, often from a worker thread."""

from __future__ import annotations

import asyncio
import threading

import httpx
import pytest

from crewai_b2a import B2AClient, CrewAIB2ATool


def _tool(handler, *, sync: bool = True) -> tuple[CrewAIB2ATool, B2AClient]:
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
    if sync:
        tool._sync_client = B2AClient(
            api_key="test-key",
            base_url="http://127.0.0.1:9",
            transport=transport,
        )
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
        tool.close()
        asyncio.run(client.close())


@pytest.mark.parametrize("async_first", [False, True])
def test_sync_and_async_calls_keep_borrowed_client_on_caller_loop(async_first):
    loops: list[asyncio.AbstractEventLoop] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        loops.append(asyncio.get_running_loop())
        return httpx.Response(200, json={"balance": 3})

    tool, client = _tool(handler)

    async def call_async() -> None:
        owner = asyncio.get_running_loop()
        assert await tool._arun("balance") == "Balance: 3 credits"
        assert loops[-1] is owner

    if async_first:
        asyncio.run(call_async())
        assert tool._run("balance") == "Balance: 3 credits"
    else:
        assert tool._run("balance") == "Balance: 3 credits"
        asyncio.run(call_async())
    loop = tool._sync_runner_loop
    thread = tool._sync_runner_thread
    assert loops.count(loop) == 1
    assert thread is not None and thread.is_alive()
    tool.close()
    assert loop is not None and loop.is_closed()
    assert not thread.is_alive()
    assert not client._client.is_closed
    asyncio.run(client.close())


def test_borrowed_client_with_loop_bound_transport_stays_on_owner_loop(monkeypatch):
    tool, client = _tool(
        lambda request: httpx.Response(200, json={"balance": 3}), sync=False
    )

    async def run() -> None:
        owner = asyncio.get_running_loop()
        original = client.get_balance

        async def loop_bound(wallet_id):
            assert asyncio.get_running_loop() is owner
            return await original(wallet_id)

        monkeypatch.setattr(client, "get_balance", loop_bound)
        assert await tool._arun("balance") == "Balance: 3 credits"
        tool.close()
        await client.close()

    asyncio.run(run())


def test_context_manager_closes_owned_client_on_runner_loop(monkeypatch):
    loops: list[asyncio.AbstractEventLoop] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        loops.append(asyncio.get_running_loop())
        return httpx.Response(200, json={"balance": 3})

    client = B2AClient(
        api_key="test-key",
        base_url="http://127.0.0.1:9",
        transport=httpx.MockTransport(handler),
    )
    original_close = client.close

    async def record_close() -> None:
        loops.append(asyncio.get_running_loop())
        await original_close()

    monkeypatch.setattr(client, "close", record_close)
    monkeypatch.setattr("crewai_b2a.tool.B2AClient", lambda **kwargs: client)
    with CrewAIB2ATool(api_key="test-key", wallet_id="wallet-1") as tool:
        assert tool._run("balance") == "Balance: 3 credits"
        loop = tool._sync_runner_loop
        thread = tool._sync_runner_thread
        assert loops == [loop]
    assert loops == [loop, loop]
    assert loop is not None and loop.is_closed()
    assert thread is not None and not thread.is_alive()
    assert client._client.is_closed
    assert tool.client is None
    tool.close()


def test_close_cancels_in_flight_sync_call_within_timeout(monkeypatch):
    started = threading.Event()
    cancelled = threading.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    client = B2AClient(
        api_key="test-key",
        base_url="http://127.0.0.1:9",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr("crewai_b2a.tool.B2AClient", lambda **kwargs: client)
    tool = CrewAIB2ATool(api_key="test-key", wallet_id="wallet-1")
    calls: list[str] = []
    caller = threading.Thread(
        target=lambda: calls.append(tool._run("balance")), daemon=True
    )
    caller.start()
    assert started.wait(timeout=2)
    loop = tool._sync_runner_loop
    runner = tool._sync_runner_thread

    close_errors: list[Exception] = []

    def close_tool() -> None:
        try:
            tool.close()
        except Exception as exc:  # noqa: BLE001
            close_errors.append(exc)

    closer = threading.Thread(target=close_tool, daemon=True)
    closer.start()
    closer.join(timeout=5)
    assert not closer.is_alive(), "close() waited indefinitely for the active call"
    assert not close_errors
    caller.join(timeout=2)
    assert not caller.is_alive()
    assert cancelled.wait(timeout=1)
    assert calls and calls[0].startswith("Error:")
    assert loop is not None and loop.is_closed()
    assert runner is not None and not runner.is_alive()
    assert client._client.is_closed


def test_first_submit_waits_until_runner_is_running(monkeypatch):
    entered = threading.Event()
    resume = threading.Event()
    original = asyncio.BaseEventLoop.run_forever

    def paused_start(loop):
        if threading.current_thread().name == "crewai-b2a-loop":
            entered.set()
            assert resume.wait(timeout=5)
        return original(loop)

    monkeypatch.setattr(asyncio.BaseEventLoop, "run_forever", paused_start)
    tool, client = _tool(lambda request: httpx.Response(200, json={"balance": 3}))
    results: list[str] = []
    caller = threading.Thread(target=lambda: results.append(tool._run("balance")))
    caller.start()
    try:
        assert entered.wait(timeout=2)
        assert results == []
    finally:
        resume.set()
        caller.join(timeout=5)
        tool.close()
        asyncio.run(client.close())
    assert not caller.is_alive()
    assert results == ["Balance: 3 credits"]


def test_close_between_runner_lookup_and_submit_rejects_without_hanging(monkeypatch):
    tool, client = _tool(lambda request: httpx.Response(200, json={"balance": 3}))
    entered = threading.Event()
    resume = threading.Event()
    original = tool._ensure_sync_runner

    def pause_after_lookup():
        loop = original()
        entered.set()
        assert resume.wait(timeout=5)
        return loop

    monkeypatch.setattr(tool, "_ensure_sync_runner", pause_after_lookup)
    results: list[str] = []
    caller = threading.Thread(target=lambda: results.append(tool._run("balance")))
    caller.start()
    try:
        assert entered.wait(timeout=2)
        tool.close()
    finally:
        resume.set()
        caller.join(timeout=5)
        asyncio.run(client.close())
    assert not caller.is_alive()
    assert results == ["Error: CrewAI tool is closed"]


def test_failed_close_can_be_retried(monkeypatch):
    tool = CrewAIB2ATool(api_key="test-key", wallet_id="wallet-1")
    client = B2AClient(
        api_key="test-key",
        base_url="http://127.0.0.1:9",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"balance": 3})
        ),
    )
    monkeypatch.setattr("crewai_b2a.tool.B2AClient", lambda **kwargs: client)
    assert tool._run("balance") == "Balance: 3 credits"
    loop = tool._sync_runner_loop
    original_close = client.close
    attempts = 0

    async def fail_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("close failed")
        await original_close()

    monkeypatch.setattr(client, "close", fail_once)
    with pytest.raises(RuntimeError, match="close failed"):
        tool.close()
    assert loop is not None and loop.is_running()
    tool.close()
    assert attempts == 2
    assert loop.is_closed()
    assert client._client.is_closed


def test_close_retry_finishes_a_stopped_runner(monkeypatch):
    tool, client = _tool(lambda request: httpx.Response(200, json={"balance": 3}))
    assert tool._run("balance") == "Balance: 3 credits"
    loop = tool._sync_runner_loop
    thread = tool._sync_runner_thread
    assert thread is not None
    original_join = thread.join

    def fail_after_join(timeout=None):
        original_join(timeout)
        monkeypatch.setattr(thread, "join", original_join)
        raise RuntimeError("join interrupted")

    monkeypatch.setattr(thread, "join", fail_after_join)
    with pytest.raises(RuntimeError, match="join interrupted"):
        tool.close()
    assert loop is not None and not loop.is_running() and not loop.is_closed()
    tool.close()
    assert loop.is_closed()
    asyncio.run(client.close())


def test_sync_call_inside_running_loop_does_not_block_it():
    seen = threading.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.set()
        await asyncio.Event().wait()
        return httpx.Response(200, json={"balance": 3})

    tool, client = _tool(handler, sync=False)
    result: list[str] = []

    def run_loop() -> None:
        async def run() -> None:
            result.append(tool._run("balance"))

        asyncio.run(run())

    caller = threading.Thread(target=run_loop, daemon=True)
    caller.start()
    caller.join(timeout=2)
    try:
        assert not caller.is_alive(), "sync call blocked its caller's event loop"
        assert result and "running event loop" in result[0]
        assert not seen.is_set()
    finally:
        tool.close()
        asyncio.run(client.close())


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
