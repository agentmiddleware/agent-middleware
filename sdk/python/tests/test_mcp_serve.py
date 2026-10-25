"""The legacy serve command uses the FastMCP 1.x serving API."""

import asyncio
import json
import sys

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
    assert "permit_id" in registered_tools[0].inputSchema["properties"]
    assert "idempotency_key" not in registered_tools[0].inputSchema["properties"]
    assert "permit_id" not in registered_tools[0].inputSchema.get("required", [])
    assert registered_tools[0].inputSchema["properties"]["kwargs"]["type"] == "object"
    if transport == "sse":
        assert server.settings.port == 8765


@pytest.mark.parametrize(
    "status_code, detail, permit_id, idempotency_key, legacy_result_format",
    [
        (200, None, None, None, False),
        (200, None, "permit-123", "invoke-123", False),
        (200, None, None, None, True),
        (403, "other_denial", None, None, False),
        (403, "permit_required", None, None, False),
        (403, {"error": "permit_required"}, None, None, False),
        (400, "idempotency_key_required", "permit-123", "invoke-123", False),
        (
            202,
            {
                "error": "human_approval_pending",
                "approval": {"approval_id": "approval-123", "expires_at": "2026-10-24T00:00:00Z"},
            },
            "permit-123",
            "invoke-123",
            False,
        ),
        (202, "human_approval_pending", "permit-123", "invoke-123", False),
    ],
)
def test_serve_async_tool_invokes_gateway_with_arguments(
    monkeypatch, tmp_path, status_code, detail, permit_id, idempotency_key, legacy_result_format
):
    fastmcp = pytest.importorskip("mcp.server.fastmcp").FastMCP
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
    registered = []
    started = []
    requests = []
    add_tool = fastmcp.add_tool

    def capture_tool(self, handler, **kwargs):
        registered.append(handler)
        return add_tool(self, handler, **kwargs)

    async def run_stdio(self):
        started.append(self)

    def get_context(self):
        metadata = (
            {"io.agentmiddleware/idempotency_key": idempotency_key}
            if idempotency_key is not None
            else {}
        )
        return pytest.importorskip("mcp.server.fastmcp").Context(
            request_context=request_context(
                request_id="call-1",
                meta=request_meta.model_validate(metadata),
                session=None,
                lifespan_context=None,
            ),
            fastmcp=self,
        )

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
    monkeypatch.setattr(fastmcp, "get_context", get_context)
    monkeypatch.setattr(fastmcp, "run_stdio_async", run_stdio)

    asyncio.run(
        mcp.serve_async(api_url="https://unused.invalid", legacy_result_format=legacy_result_format)
    )
    assert len(registered) == 1
    assert len(started) == 1
    caller_context = {"permit_id": permit_id} if permit_id is not None else {}
    if status_code == 200:
        result = asyncio.run(
            started[0].call_tool(
                "partner.search", {"kwargs": {"query": "sample"}, **caller_context}
            )
        )
        if legacy_result_format:
            assert json.loads(result[0].text) == {"content": [{"type": "text", "text": "ok"}]}
        else:
            assert result.isError is False
            assert result.content[0].text == "ok"
    elif detail == "permit_required" or detail == {"error": "permit_required"}:
        result = asyncio.run(
            started[0].call_tool("partner.search", {"kwargs": {"query": "sample"}})
        )
        assert result.isError is True
        assert result.structuredContent["error"] == "permit_required"
        assert "permit_id" in result.content[0].text
        assert "idempotency_key" in result.content[0].text
        assert "idempotency_key" in result.structuredContent["guidance"]
    elif status_code == 400:
        result = asyncio.run(
            registered[0](ctx=started[0].get_context(), query="sample", **caller_context)
        )
        assert result.isError is True
        assert result.structuredContent["error"] == "idempotency_key_required"
        assert "idempotency_key" in result.content[0].text
        assert "idempotency_key" in result.structuredContent["guidance"]
    elif status_code == 202:
        result = asyncio.run(
            started[0].call_tool(
                "partner.search", {"kwargs": {"query": "sample"}, **caller_context}
            )
        )
        assert result.isError is True
        assert result.structuredContent["error"] == "human_approval_pending"
        approval = detail["approval"] if isinstance(detail, dict) else {}
        assert result.structuredContent["approval"] == approval
        if approval:
            assert "approval-123" in result.content[0].text
        assert "invoke-123" not in result.content[0].text
        assert (
            "same permit_id and io.agentmiddleware/idempotency_key"
            in result.structuredContent["guidance"]
        )
    else:
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            asyncio.run(
                registered[0](ctx=started[0].get_context(), query="sample", **caller_context)
            )
        assert exc_info.value.response.status_code == 403
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == "https://unused.invalid/mcp/tools/partner.search/invoke"
    assert requests[0].headers["X-API-Key"] == "test-key"
    mcp_context = {"wallet_id": "test-wallet"}
    if permit_id is not None:
        mcp_context["permit_id"] = permit_id
    mcp_context["idempotency_key"] = requests[0].headers["Idempotency-Key"]
    if idempotency_key is not None:
        assert requests[0].headers["Idempotency-Key"] == idempotency_key
    assert json.loads(requests[0].content) == {
        "name": "partner.search",
        "arguments": {"query": "sample"},
        "mcp_context": mcp_context,
    }


def test_registered_tool_keeps_retry_metadata_separate_from_tool_inputs(monkeypatch, tmp_path):
    fastmcp_module = pytest.importorskip("mcp.server.fastmcp")
    fastmcp = fastmcp_module.FastMCP
    request_context = pytest.importorskip("mcp.shared.context").RequestContext
    request_meta = pytest.importorskip("mcp.types").RequestParams.Meta
    tool_error = pytest.importorskip("mcp.server.fastmcp.exceptions").ToolError
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
    metadata = {"io.agentmiddleware/idempotency_key": "retry-1"}

    async def run_stdio(self):
        servers.append(self)

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

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

    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(httpx, "AsyncClient", lambda: client_type(transport=transport))
    monkeypatch.setattr(fastmcp, "get_context", get_context)
    monkeypatch.setattr(fastmcp, "run_stdio_async", run_stdio)

    asyncio.run(mcp.serve_async(api_url="https://unused.invalid"))
    arguments = {"kwargs": {"query": "sample", "idempotency_key": "tool-input"}}
    for _ in range(2):
        result = asyncio.run(servers[0].call_tool("partner.search", arguments))
        assert result.isError is False
    metadata["io.agentmiddleware/idempotency_key"] = " "
    with pytest.raises(tool_error, match="idempotency_key"):
        asyncio.run(servers[0].call_tool("partner.search", arguments))
    metadata.clear()
    missing_key = asyncio.run(
        servers[0].call_tool("partner.search", {**arguments, "permit_id": "permit-1"})
    )
    assert missing_key.isError is True
    assert missing_key.structuredContent["error"] == "idempotency_key_required"

    assert len(requests) == 2
    assert [request.headers["Idempotency-Key"] for request in requests] == [
        "retry-1",
        "retry-1",
    ]
    assert [json.loads(request.content)["arguments"] for request in requests] == [
        {"query": "sample", "idempotency_key": "tool-input"},
        {"query": "sample", "idempotency_key": "tool-input"},
    ]
    assert all(
        json.loads(request.content)["mcp_context"]["idempotency_key"] == "retry-1"
        for request in requests
    )


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


def test_serve_cli_enables_legacy_result_format(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "argv", ["b2a-mcp", "serve", "--legacy-result-format"])
    monkeypatch.setattr(mcp, "serve", lambda **kwargs: calls.append(kwargs))

    mcp.main()

    assert calls == [
        {
            "transport": "stdio",
            "port": 8001,
            "api_url": mcp.DEFAULT_API_URL,
            "legacy_result_format": True,
        }
    ]
