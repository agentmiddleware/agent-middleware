"""simulate_session keeps the virtual balance the server reports (issue #361)."""

from __future__ import annotations

import httpx
import pytest

from b2a_sdk.client import AgentMiddlewareClient


def _client(handler) -> AgentMiddlewareClient:
    return AgentMiddlewareClient(
        api_key="test-key", base_url="http://test", transport=httpx.MockTransport(handler)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_body", "expected"),
    [
        ({"session_id": "sim-1", "virtual_balance": 10000.0}, 10000.0),
        ({"session_id": "sim-1", "virtual_balance": "250.5"}, 250.5),
        ({"session_id": "sim-1"}, None),
    ],
)
async def test_simulate_session_exposes_server_initial_balance(session_body, expected) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=session_body)
        return httpx.Response(200, json={})

    async with _client(handler) as client:
        async with client.simulate_session("wallet-1") as sim:
            assert sim.initial_balance == expected
            assert sim.total_cost == 0.0
