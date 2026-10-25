"""The legacy serve command uses the FastMCP 1.x serving API."""

import asyncio
import json
import sys

import pytest

from b2a_sdk import mcp
from b2a_sdk.errors import TransportError


def _serve_tool_bridge(monkeypatch, tmp_path, respond):
    """Start serve_async against a mock gateway and return the registered handler."""
    fastmcp = pytest.importorskip("mcp.server.fastmcp").FastMCP
    httpx = pytest.importorskip("httpx")
    monkeypatch.chdir(tmp_path)
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
        return None

    def wrapped(request):
        requests.append(request)
        return respond(request)

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda: client_type(transport=httpx.MockTransport(wrapped))
    )
    monkeypatch.setattr(fastmcp, "add_tool", capture_tool)
    monkeypatch.setattr(fastmcp, "run_stdio_async", run_stdio)
    asyncio.run(mcp.serve_async(api_url="https://unused.invalid"))
    return registered[0], requests, httpx


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
    assert "permit_id" not in registered_tools[0].inputSchema["properties"]
    assert "idempotency_key" in registered_tools[0].inputSchema["properties"]
    assert "idempotency_key" not in registered_tools[0].inputSchema.get("required", [])
    assert registered_tools[0].inputSchema["properties"]["kwargs"]["type"] == "object"
    if transport == "sse":
        assert server.settings.port == 8765


@pytest.mark.parametrize(
    "status_code, body, idempotency_key, legacy_result_format",
    [
        (200, {"result": {"content": [{"type": "text", "text": "ok"}]}}, None, False),
        (200, {"result": {"content": [{"type": "text", "text": "ok"}]}}, "invoke-123", False),
        (200, {"result": {"content": [{"type": "text", "text": "ok"}]}}, None, True),
        (403, {"detail": "other_denial"}, None, False),
        (
            403,
            {"detail": {"error": "permit_scope_denied", "receipt": {"outcome": "denied"}}},
            None,
            False,
        ),
        (429, {"detail": {"error": "rate_limited", "retry_after": 1}}, None, False),
        (400, {"detail": "idempotency_key_required"}, None, False),
        (
            400,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "error": {
                    "code": -32602,
                    "message": "invalid_idempotency_key",
                    "data": {"reason": "blank"},
                },
            },
            None,
            False,
        ),
        (
            200,
            {
                "error": {
                    "code": -32003,
                    "message": "human_approval_pending",
                    "data": {"approval": {"approval_id": "approval-123"}},
                }
            },
            "invoke-123",
            False,
        ),
    ],
)
def test_serve_async_tool_invokes_gateway_with_arguments(
    monkeypatch, tmp_path, status_code, body, idempotency_key, legacy_result_format
):
    fastmcp = pytest.importorskip("mcp.server.fastmcp").FastMCP
    httpx = pytest.importorskip("httpx")
    monkeypatch.chdir(tmp_path)
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
        return httpx.Response(status_code, json=body)

    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(httpx, "AsyncClient", lambda: client_type(transport=transport))
    monkeypatch.setattr(fastmcp, "add_tool", capture_tool)
    monkeypatch.setattr(fastmcp, "run_stdio_async", run_stdio)

    asyncio.run(
        mcp.serve_async(api_url="https://unused.invalid", legacy_result_format=legacy_result_format)
    )
    assert len(registered) == 1
    assert len(started) == 1
    caller_context = {"idempotency_key": idempotency_key} if idempotency_key else {}
    if "result" in body:
        result = asyncio.run(
            started[0].call_tool(
                "partner.search", {"kwargs": {"query": "sample"}, **caller_context}
            )
        )
        if legacy_result_format:
            assert json.loads(result[0].text) == body["result"]
        else:
            assert result.isError is False
            assert result.content[0].text == "ok"
    else:
        result = asyncio.run(
            started[0].call_tool(
                "partner.search", {"kwargs": {"query": "sample"}, **caller_context}
            )
        )
        assert result.isError is True
        if status_code >= 400 and "error" in body:
            assert result.structuredContent == body
        elif "error" in body:
            assert result.structuredContent == body["error"]
        else:
            assert result.structuredContent["detail"] == body["detail"]
            assert result.structuredContent["error"] == (
                body["detail"].get("error") if isinstance(body["detail"], dict) else body["detail"]
            )
        assert json.loads(result.content[0].text) == result.structuredContent
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == "https://unused.invalid/mcp"
    assert requests[0].headers["X-API-Key"] == "test-key"
    assert requests[0].headers["Accept"] == "application/json, text/event-stream"
    assert requests[0].headers.get("Idempotency-Key") == idempotency_key
    assert json.loads(requests[0].content) == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "partner.search", "arguments": {"query": "sample"}},
    }


def test_serve_async_repeats_caller_key_for_gateway_receipt_replay(monkeypatch, tmp_path):
    from uuid import uuid4

    fastmcp = pytest.importorskip("mcp.server.fastmcp").FastMCP
    httpx = pytest.importorskip("httpx")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("B2A_API_KEY", "test-key")
    monkeypatch.setattr(
        mcp,
        "generate_manifest",
        lambda **kwargs: {"tools": [{"name": "partner.search", "description": "Search"}]},
    )
    started = []
    requests = []
    receipt = {"receipt_id": str(uuid4()), "permit_id": str(uuid4())}

    async def run_stdio(self):
        started.append(self)

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={"result": {"content": [{"type": "text", "text": "ok"}], "receipt": receipt}},
        )

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda: client_type(transport=httpx.MockTransport(respond))
    )
    monkeypatch.setattr(fastmcp, "run_stdio_async", run_stdio)
    asyncio.run(mcp.serve_async(api_url="https://unused.invalid"))

    key = str(uuid4())
    arguments = {"kwargs": {"query": "sample"}, "idempotency_key": key}
    first = asyncio.run(started[0].call_tool("partner.search", arguments))
    replay = asyncio.run(started[0].call_tool("partner.search", arguments))

    assert first.receipt == replay.receipt == receipt
    assert len(requests) == 2
    assert [request.headers["Idempotency-Key"] for request in requests] == [key, key]
    assert requests[0].content == requests[1].content
    assert all("permit_id" not in json.loads(request.content)["params"] for request in requests)


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


def test_serve_async_rejects_non_ascii_idempotency_key_before_send(monkeypatch, tmp_path):
    """A non-ASCII key must not be assigned to an HTTP header (UnicodeEncodeError)."""
    httpx = pytest.importorskip("httpx")

    def respond(request):
        return httpx.Response(200, json={"result": {"content": [{"type": "text", "text": "ok"}]}})

    handler, requests, _httpx = _serve_tool_bridge(monkeypatch, tmp_path, respond)

    with pytest.raises(ValueError, match="printable ASCII"):
        asyncio.run(handler(idempotency_key="注文-123", kwargs={"query": "sample"}))
    with pytest.raises(ValueError, match="printable ASCII"):
        asyncio.run(handler(idempotency_key="  café-1  ", kwargs={"query": "sample"}))
    assert requests == []

    result = asyncio.run(handler(idempotency_key="  invoke-123  ", kwargs={"query": "sample"}))
    assert result.isError is False
    assert len(requests) == 1
    assert requests[0].headers["Idempotency-Key"] == "invoke-123"


def test_serve_async_passes_jsonrpc_error_object_unchanged(monkeypatch, tmp_path):
    """Gateway JSON-RPC errors keep code, message, data, and extra fields as sent."""
    httpx = pytest.importorskip("httpx")
    error = {
        "code": -32003,
        "message": "human_approval_pending",
        "data": {"approval": {"approval_id": "approval-123"}},
        "retryable": False,
    }

    def respond(request):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": error})

    handler, requests, _httpx = _serve_tool_bridge(monkeypatch, tmp_path, respond)
    result = asyncio.run(handler(idempotency_key="invoke-123", kwargs={"query": "sample"}))

    assert result.isError is True
    assert result.structuredContent == error
    assert json.loads(result.content[0].text) == error
    assert len(requests) == 1


@pytest.mark.parametrize("status_code", [500, 502, 503])
def test_serve_async_gateway_5xx_is_retryable_transport_error(monkeypatch, tmp_path, status_code):
    """HTTP 5xx is a transport failure so the caller can retry the same key."""
    httpx = pytest.importorskip("httpx")

    def respond(request):
        return httpx.Response(
            status_code,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32603, "message": "internal"},
            },
        )

    handler, requests, _httpx = _serve_tool_bridge(monkeypatch, tmp_path, respond)

    with pytest.raises(TransportError, match=str(status_code)) as exc_info:
        asyncio.run(handler(idempotency_key="invoke-123", kwargs={"query": "sample"}))

    assert exc_info.value.__class__ is TransportError
    assert len(requests) == 1
    assert requests[0].headers["Idempotency-Key"] == "invoke-123"
