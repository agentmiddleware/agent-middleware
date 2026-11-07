"""A queued CrewAI call must retain concurrent permit replay semantics."""

import asyncio
import threading

import httpx
import pytest

from b2a_sdk.client import AgentMiddlewareClient
from tests.test_wrapper_permit_retry_contract import CALL, Gateway, _prepare


@pytest.mark.asyncio
async def test_queued_concurrent_call_replays_permit_after_first_is_cached(monkeypatch):
    first_invoked = threading.Event()
    gateway = Gateway(lose_first=False)

    async def handle(request):
        response = await gateway(request)
        if request.url.path == "/mcp/messages":
            first_invoked.set()
        return response

    async with AgentMiddlewareClient(
        "synthetic",
        base_url="http://local.invalid",
        transport=httpx.MockTransport(handle),
    ) as client:
        tool, call = await _prepare(monkeypatch, "crewai", client)
        original = tool._ensure_sync_runner
        submissions = 0

        def queue_after_first_invoke():
            nonlocal submissions
            submissions += 1
            loop = original()
            if submissions == 2:
                assert first_invoked.wait(timeout=5)
            return loop

        monkeypatch.setattr(tool, "_ensure_sync_runner", queue_after_first_invoke)
        first, second = await asyncio.gather(call(**CALL), call(**CALL))

    assert first == second
    assert len(gateway.permit_bodies) == 2
    assert gateway.permit_bodies[0] == gateway.permit_bodies[1]
    assert len(gateway.charges) == 1
