"""gtm-62 drift backport: delivery_uncertain as a model-readable tool result.

On the standard MCP surface (POST /mcp) an uncertain outcome used to arrive
as a JSON-RPC error, a channel that reads as retryable, so a model that only
sees the message tends to call the tool again and risk a second charged
dispatch. The outcome is already charged and receipted, so it is now
returned as a CallToolResult with isError set, telling the model not to
resend and carrying the receipt plus outcome metadata for reconciliation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.service_registry import get_service_registry
from app.services.upstream_mcp import UpstreamMcpDeliveryUncertainError
from tests.test_trust_helpers import provision_agent_wallet

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def standard_mcp_enabled(monkeypatch):
    monkeypatch.setenv("ENABLE_STANDARD_MCP_ENDPOINT", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.setenv("ENABLE_STANDARD_MCP_ENDPOINT", "false")
    get_settings.cache_clear()


def _rpc(method: str, request_id: int | None = 1, params: dict | None = None) -> dict:
    body: dict = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if request_id is not None:
        body["id"] = request_id
    return body


@dataclass
class AmbiguousExecutor:
    """Executor that raises delivery_uncertain after the dispatch checkpoint."""

    dispatch_count: int = 0
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def call_tool(
        self,
        arguments: dict[str, Any],
        *,
        invocation_id: str,
        idempotency_key: str,
        before_dispatch: Callable[[], Awaitable[None]],
    ):
        self.calls.append({"arguments": arguments, "idempotency_key": idempotency_key})
        await before_dispatch()
        self.dispatch_count += 1
        raise UpstreamMcpDeliveryUncertainError()


@pytest.mark.anyio
async def test_uncertain_outcome_returns_is_error_result_not_jsonrpc_error(
    client, standard_mcp_enabled, clean_database
):
    tool_name = "gtm62-uncertain-tool"
    executor = AmbiguousExecutor()
    registry = get_service_registry()
    registry.register_upstream(
        service_id=tool_name,
        name="GTM62 Uncertain Tool",
        description="Uncertain delivery fixture",
        category=ServiceCategory.AGENT_COMMS,
        executor=executor,
        input_schema={"type": "object", "properties": {}},
        output_schema=None,
        credits_per_unit=2.0,
        upstream_tool_name=tool_name,
        upstream_origin="https://test.example.com",
    )
    try:
        provisioned = await provision_agent_wallet(client)
        headers = {
            **provisioned["agent_headers"],
            **MCP_HEADERS,
            "Idempotency-Key": "gtm62-uncertain-1",
        }
        call = _rpc("tools/call", params={"name": tool_name, "arguments": {}})
        resp = await client.post("/mcp", json=call, headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "error" not in body, body
        result = body["result"]
        assert result["isError"] is True
        text = result["content"][0]["text"]
        assert "Delivery uncertain" in text
        assert "Do not resend or retry" in text
        receipt = result["receipt"]
        assert receipt["outcome"] == "delivery_uncertain"
        assert receipt["reason_code"] == "delivery_uncertain"
        assert receipt["wallet_id"] == provisioned["agent_wallet_id"]
        # The receipt also rides the spec's extension point, with the
        # outcome alongside it.
        meta = result["_meta"]
        assert meta["io.agentmiddleware/receipt"] == receipt
        assert meta["outcome"] == "delivery_uncertain"
        assert executor.dispatch_count == 1

        # A retry with the same key replays the uncertain receipt instead
        # of dispatching a second time.
        replay = await client.post("/mcp", json=call, headers=headers)
        assert replay.status_code == 200, replay.text
        replay_body = replay.json()
        assert "error" not in replay_body, replay_body
        replay_result = replay_body["result"]
        assert replay_result["isError"] is True
        assert replay_result["receipt"]["receipt_id"] == receipt["receipt_id"]
        assert executor.dispatch_count == 1
    finally:
        registry.unregister_local(tool_name)
