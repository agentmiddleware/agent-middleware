"""A failing local tool answers -32006 with no exception text (issue #349).

``docs/failure-semantics.md`` maps the ``failed_refunded`` outcome to
``-32006`` and reserves ``-32603`` for failures the pipeline did not
classify. A local tool that raised was answered ``-32603`` on both JSON-RPC
endpoints, and ``/mcp`` copied the tool's exception text into the message.
The call here is valid for the tool's schema; the tool itself raises, so the
failure is reached after the charge and refunded.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.service_registry import get_service_registry
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet

TOOL_TEXT = "local tool failure text that must stay server side"


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as value:
        yield value


@pytest.fixture
def standard_mcp_enabled(monkeypatch):
    monkeypatch.setenv("ENABLE_STANDARD_MCP_ENDPOINT", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.setenv("ENABLE_STANDARD_MCP_ENDPOINT", "false")
    get_settings.cache_clear()


@pytest.fixture
def failing_tool():
    tool_name = "local-failure-code-tool"
    runs = {"count": 0}

    def boom(message: str = "ok") -> dict[str, Any]:
        runs["count"] += 1
        raise RuntimeError(TOOL_TEXT)

    get_service_registry().register_local(
        service_id=tool_name,
        name="Local failure code tool",
        description="Raises after dispatch to exercise failed_refunded",
        category=ServiceCategory.AGENT_COMMS,
        func=boom,
        credits_per_unit=2.0,
        unit_name="call",
    )
    try:
        yield tool_name, runs
    finally:
        get_service_registry().unregister_local(tool_name)


async def _permit(client: AsyncClient, ctx: dict[str, Any], tool_name: str) -> str:
    permit = await create_tool_permit(
        client,
        wallet_id=ctx["agent_wallet_id"],
        key_id=ctx["key_id"],
        tool_name=tool_name,
        idem_key=f"{tool_name}-permit",
    )
    return permit["permit_id"]


def _assert_tool_failure(error: dict[str, Any]) -> None:
    assert error["code"] == -32006, error
    assert error["message"] == "tool_execution_failed", error
    assert error["data"]["receipt"]["outcome"] == "failed_refunded", error
    assert error["data"]["receipt"]["reason_code"] == "tool_execution_failed", error
    assert TOOL_TEXT not in str(error), error


@pytest.mark.anyio
async def test_messages_endpoint_live_and_replay(
    client: AsyncClient, clean_database: None, failing_tool
) -> None:
    tool_name, runs = failing_tool
    ctx = await provision_agent_wallet(client)
    permit_id = await _permit(client, ctx, tool_name)
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": {"message": "hi"},
            "mcpContext": {
                "wallet_id": ctx["agent_wallet_id"],
                "permit_id": permit_id,
                "idempotency_key": "local-failure-messages-1",
            },
        },
    }

    live = await client.post("/mcp/messages", json=body, headers=ctx["agent_headers"])
    replay = await client.post("/mcp/messages", json=body, headers=ctx["agent_headers"])

    assert live.status_code == 200, live.text
    _assert_tool_failure(live.json()["error"])
    assert replay.status_code == 200, replay.text
    _assert_tool_failure(replay.json()["error"])
    assert (
        replay.json()["error"]["data"]["receipt"]["receipt_id"]
        == live.json()["error"]["data"]["receipt"]["receipt_id"]
    )
    assert runs["count"] == 1


@pytest.mark.anyio
async def test_standard_endpoint_live_and_replay(
    client: AsyncClient, clean_database: None, standard_mcp_enabled, failing_tool
) -> None:
    tool_name, runs = failing_tool
    ctx = await provision_agent_wallet(client)
    body = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": {"message": "hi"}},
    }
    headers = {
        **ctx["agent_headers"],
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Idempotency-Key": "local-failure-standard-1",
    }

    live = await client.post("/mcp", json=body, headers=headers)
    replay = await client.post("/mcp", json=body, headers=headers)

    assert live.status_code == 200, live.text
    assert TOOL_TEXT not in live.text
    _assert_tool_failure(live.json()["error"])
    assert replay.status_code == 200, replay.text
    assert TOOL_TEXT not in replay.text
    _assert_tool_failure(replay.json()["error"])
    assert runs["count"] == 1


@pytest.mark.anyio
async def test_rest_invoke_keeps_500_with_canned_error(
    client: AsyncClient, clean_database: None, failing_tool
) -> None:
    tool_name, _runs = failing_tool
    ctx = await provision_agent_wallet(client)
    permit_id = await _permit(client, ctx, tool_name)
    resp = await client.post(
        f"/mcp/tools/{tool_name}/invoke",
        json={
            "name": tool_name,
            "arguments": {"message": "hi"},
            "mcp_context": {
                "wallet_id": ctx["agent_wallet_id"],
                "permit_id": permit_id,
                "idempotency_key": "local-failure-rest-1",
            },
        },
        headers=ctx["agent_headers"],
    )

    assert resp.status_code == 500, resp.text
    assert TOOL_TEXT not in resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "tool_execution_failed", detail
    assert detail["receipt"]["outcome"] == "failed_refunded", detail
