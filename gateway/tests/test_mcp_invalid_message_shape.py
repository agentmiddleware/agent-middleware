"""A /mcp body that is not one JSON-RPC message gets a fixed answer (#351).

The SDK refused a JSON-RPC batch (and any other body that fails its message
model) with the raw pydantic validation dump, including the library's version
link, and ``"server-error"`` as the id. JSON-RPC 2.0 asks for ``id: null``
when the request id cannot be determined.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import app
from tests.test_standard_mcp_endpoint import MCP_HEADERS
from tests.test_trust_helpers import provision_agent_wallet


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


def _list_request(rpc_id: int) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "method": "tools/list", "params": {}}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            [_list_request(1), _list_request(2)],
            "Invalid Request: batch requests are not supported",
        ),
        ([], "Invalid Request: batch requests are not supported"),
        ({}, "Invalid Request: body is not a JSON-RPC 2.0 message"),
        (5, "Invalid Request: body is not a JSON-RPC 2.0 message"),
        (
            {"jsonrpc": "1.0", "id": 1, "method": "tools/list"},
            "Invalid Request: body is not a JSON-RPC 2.0 message",
        ),
    ],
)
async def test_non_message_body_answers_invalid_request(
    client: AsyncClient, clean_database: None, standard_mcp_enabled, body, message
) -> None:
    ctx = await provision_agent_wallet(client)
    resp = await client.post(
        "/mcp", json=body, headers={**ctx["agent_headers"], **MCP_HEADERS}
    )

    assert resp.status_code == 400, resp.text
    payload = resp.json()
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] is None
    assert payload["error"]["code"] == -32600
    assert payload["error"]["message"] == message
    assert "pydantic" not in resp.text
    assert "server-error" not in resp.text


@pytest.mark.anyio
async def test_single_message_still_reaches_the_server(
    client: AsyncClient, clean_database: None, standard_mcp_enabled
) -> None:
    ctx = await provision_agent_wallet(client)
    resp = await client.post(
        "/mcp", json=_list_request(3), headers={**ctx["agent_headers"], **MCP_HEADERS}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == 3
    assert "tools" in resp.json()["result"]
