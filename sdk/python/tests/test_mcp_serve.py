"""The legacy serve command uses the FastMCP 1.x serving API."""

import asyncio
import json
from types import SimpleNamespace

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
    assert [(tool.name, tool.description) for tool in asyncio.run(server.list_tools())] == [
        ("partner.search", "Search")
    ]
    if transport == "sse":
        assert server.settings.port == 8765


@pytest.mark.parametrize("status_code", [200, 403])
def test_serve_async_tool_invokes_gateway_with_arguments(monkeypatch, tmp_path, status_code):
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
    requests = []
    add_tool = fastmcp.add_tool

    def capture_tool(self, handler, **kwargs):
        registered.append(handler)
        return add_tool(self, handler, **kwargs)

    async def run_stdio(self):
        pass

    def respond(request):
        requests.append(request)
        return httpx.Response(status_code, json={"content": [{"type": "text", "text": "ok"}]})

    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(httpx, "AsyncClient", lambda: client_type(transport=transport))
    monkeypatch.setattr(fastmcp, "add_tool", capture_tool)
    monkeypatch.setattr(fastmcp, "run_stdio_async", run_stdio)

    asyncio.run(mcp.serve_async(api_url="https://unused.invalid"))
    assert len(registered) == 1
    context = SimpleNamespace(request_context=SimpleNamespace(meta=None))
    if status_code == 200:
        assert asyncio.run(
            registered[0](ctx=context, query="sample", idempotency_key="tool-input")
        ) == {"content": [{"type": "text", "text": "ok"}]}
    else:
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            asyncio.run(registered[0](ctx=context, query="sample", idempotency_key="tool-input"))
        assert exc_info.value.response.status_code == 403
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == "https://unused.invalid/mcp/tools/partner.search/invoke"
    assert requests[0].headers["X-API-Key"] == "test-key"
    assert requests[0].headers["Idempotency-Key"] != "tool-input"
    assert json.loads(requests[0].content) == {
        "name": "partner.search",
        "arguments": {"query": "sample", "idempotency_key": "tool-input"},
        "mcp_context": {"wallet_id": "test-wallet"},
    }


def test_registered_tool_keeps_retry_metadata_separate_from_tool_inputs(monkeypatch, tmp_path):
    fastmcp_module = pytest.importorskip("mcp.server.fastmcp")
    fastmcp = fastmcp_module.FastMCP
    request_context = pytest.importorskip("mcp.shared.context").RequestContext
    request_meta = pytest.importorskip("mcp.types").RequestParams.Meta
    httpx = pytest.importorskip("httpx")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("B2A_WALLET_ID", "test-wallet")
    monkeypatch.setenv("B2A_API_KEY", "test-key")
    monkeypatch.setattr(
        mcp,
        "generate_manifest",
        lambda **kwargs: {"tools": [{"name": "partner.search", "description": "Search"}]},
    )
    servers = []
    requests = []

    async def run_stdio(self):
        servers.append(self)

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    metadata = {"idempotency_key": "retry-1"}

    def get_context(self):
        return fastmcp_module.Context(
            request_context=request_context(
                request_id="call-1",
                meta=request_meta.model_validate(metadata),
                session=None,
                lifespan_context=None,
            ),
            fastmcp=self,
        )

    monkeypatch.setattr(httpx, "AsyncClient", lambda: client_type(transport=transport))
    monkeypatch.setattr(fastmcp, "get_context", get_context)
    monkeypatch.setattr(fastmcp, "run_stdio_async", run_stdio)

    asyncio.run(mcp.serve_async(api_url="https://unused.invalid"))
    for _ in range(2):
        asyncio.run(
            servers[0].call_tool(
                "partner.search", {"kwargs": {"query": "sample", "idempotency_key": "tool-input"}}
            )
        )
    tool_error = pytest.importorskip("mcp.server.fastmcp.exceptions").ToolError
    metadata["idempotency_key"] = " "
    with pytest.raises(tool_error, match="idempotency_key"):
        asyncio.run(
            servers[0].call_tool(
                "partner.search", {"kwargs": {"query": "sample", "idempotency_key": "tool-input"}}
            )
        )

    assert len(requests) == 2
    assert [request.headers["Idempotency-Key"] for request in requests] == [
        "retry-1",
        "retry-1",
    ]
    assert [json.loads(request.content)["arguments"] for request in requests] == [
        {"kwargs": {"query": "sample", "idempotency_key": "tool-input"}},
        {"kwargs": {"query": "sample", "idempotency_key": "tool-input"}},
    ]


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
