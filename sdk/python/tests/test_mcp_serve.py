"""The legacy serve command uses the FastMCP 1.x serving API."""

import asyncio
import json

import pytest

from b2a_sdk import mcp


@pytest.mark.parametrize("transport", ["stdio", "sse"])
def test_serve_async_registers_tool_and_starts_transport(monkeypatch, tmp_path, capsys, transport):
    fastmcp = pytest.importorskip("mcp.server.fastmcp").FastMCP
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        mcp,
        "generate_manifest",
        lambda **kwargs: {"tools": [{"name": "partner.search", "description": "Search"}]},
    )
    started = []

    async def run_stdio(self):
        started.append(("stdio", self))

    async def run_sse(self, mount_path=None):
        started.append(("sse", self))

    monkeypatch.setattr(fastmcp, "run_stdio_async", run_stdio)
    monkeypatch.setattr(fastmcp, "run_sse_async", run_sse)

    asyncio.run(mcp.serve_async(transport=transport, port=8765, api_url="https://unused.invalid"))

    output = capsys.readouterr()
    if transport == "stdio":
        assert output.out == ""
        assert "Starting MCP server" in output.err

    assert len(started) == 1
    assert started[0][0] == transport
    server = started[0][1]
    registered_tools = asyncio.run(server.list_tools())
    assert [(tool.name, tool.description) for tool in registered_tools] == [
        ("partner.search", "Search")
    ]
    assert {"permit_id", "idempotency_key"} <= registered_tools[0].inputSchema["properties"].keys()
    assert not {"permit_id", "idempotency_key"} & set(
        registered_tools[0].inputSchema.get("required", [])
    )
    assert registered_tools[0].inputSchema["properties"]["kwargs"]["type"] == "object"
    if transport == "sse":
        assert server.settings.port == 8765


@pytest.mark.parametrize(
    "status_code, detail, permit_id, idempotency_key",
    [
        (200, None, None, None),
        (200, None, "permit-123", "invoke-123"),
        (403, "other_denial", None, None),
        (403, "permit_required", None, None),
    ],
)
def test_serve_async_tool_invokes_gateway_with_arguments(
    monkeypatch, tmp_path, status_code, detail, permit_id, idempotency_key
):
    fastmcp = pytest.importorskip("mcp.server.fastmcp").FastMCP
    httpx = pytest.importorskip("httpx")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("B2A_WALLET_ID", "test-wallet")
    monkeypatch.setenv("B2A_API_KEY", "test-key")
    monkeypatch.setattr(
        mcp,
        "generate_manifest",
        lambda **kwargs: {"tools": [{"name": "partner.search", "description": "Search"}]},
    )
    registered = []
    started = []
    requests = []
    add_tool = fastmcp.add_tool

    def capture_tool(self, handler, **kwargs):
        registered.append(handler)
        return add_tool(self, handler, **kwargs)

    async def run_stdio(self):
        started.append(self)

    def respond(request):
        requests.append(request)
        body = (
            {"content": [{"type": "text", "text": "ok"}]} if detail is None else {"detail": detail}
        )
        return httpx.Response(status_code, json=body)

    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(httpx, "AsyncClient", lambda: client_type(transport=transport))
    monkeypatch.setattr(fastmcp, "add_tool", capture_tool)
    monkeypatch.setattr(fastmcp, "run_stdio_async", run_stdio)

    asyncio.run(mcp.serve_async(api_url="https://unused.invalid"))
    assert len(registered) == 1
    assert len(started) == 1
    caller_context = {}
    if permit_id is not None:
        caller_context = {"permit_id": permit_id, "idempotency_key": idempotency_key}
    if status_code == 200:
        result = asyncio.run(
            started[0].call_tool(
                "partner.search", {"kwargs": {"query": "sample"}, **caller_context}
            )
        )
        assert result.isError is False
        assert result.content[0].text == "ok"
    elif detail == "permit_required":
        result = asyncio.run(
            started[0].call_tool("partner.search", {"kwargs": {"query": "sample"}})
        )
        assert result.isError is True
        assert result.structuredContent["error"] == "permit_required"
        assert "permit_id" in result.content[0].text
    else:
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            asyncio.run(registered[0](query="sample", **caller_context))
        assert exc_info.value.response.status_code == 403
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == "https://unused.invalid/mcp/tools/partner.search/invoke"
    assert requests[0].headers["X-API-Key"] == "test-key"
    mcp_context = {"wallet_id": "test-wallet"}
    if permit_id is not None:
        mcp_context.update({"permit_id": permit_id, "idempotency_key": idempotency_key})
    assert json.loads(requests[0].content) == {
        "name": "partner.search",
        "arguments": {"query": "sample"},
        "mcp_context": mcp_context,
    }


def test_serve_async_rejects_unknown_transport(monkeypatch, tmp_path):
    fastmcp = pytest.importorskip("mcp.server.fastmcp").FastMCP
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mcp, "generate_manifest", lambda **kwargs: {"tools": []})

    async def forbidden(self):
        pytest.fail("unsupported transport must not start a listener")

    monkeypatch.setattr(fastmcp, "run_stdio_async", forbidden)
    monkeypatch.setattr(fastmcp, "run_sse_async", forbidden)

    with pytest.raises(ValueError, match="Unknown transport: websocket"):
        asyncio.run(mcp.serve_async(transport="websocket", api_url="https://unused.invalid"))
