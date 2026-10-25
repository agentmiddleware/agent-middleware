"""The legacy serve command uses the FastMCP 1.x serving API."""

import asyncio
import json
import sys

import pytest

from b2a_sdk import mcp


def _serve_tool_bridge(monkeypatch, tmp_path, respond):
    """Start serve_async against a mock gateway.

    Returns the registered handler, the captured requests, httpx, and the
    FastMCP server so tests can also go through ``FastMCP.call_tool``.
    """
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
    servers = []
    add_tool = fastmcp.add_tool

    def capture_tool(self, handler, **kwargs):
        registered.append(handler)
        return add_tool(self, handler, **kwargs)

    async def run_stdio(self):
        servers.append(self)

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
    return registered[0], requests, httpx, servers[0]


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

    handler, requests, _httpx, _server = _serve_tool_bridge(monkeypatch, tmp_path, respond)

    with pytest.raises(ValueError, match="printable ASCII"):
        asyncio.run(handler(idempotency_key="注文-123", kwargs={"query": "sample"}))
    with pytest.raises(ValueError, match="printable ASCII"):
        asyncio.run(handler(idempotency_key="café-1", kwargs={"query": "sample"}))
    assert requests == []


@pytest.mark.parametrize("key", ["  invoke-123", "invoke-123  ", "\tinvoke-123", "invoke-123\n"])
def test_serve_async_rejects_edge_whitespace_idempotency_key(monkeypatch, tmp_path, key):
    """The gateway compares keys verbatim, so edge whitespace is refused, not trimmed.

    A trimmed retry would reach the gateway as a different key than the
    caller believes it sent, and HTTP drops header-edge whitespace anyway.
    """
    httpx = pytest.importorskip("httpx")
    tool_error = pytest.importorskip("mcp.server.fastmcp.exceptions").ToolError

    def respond(request):
        return httpx.Response(200, json={"result": {"content": [{"type": "text", "text": "ok"}]}})

    handler, requests, _httpx, server = _serve_tool_bridge(monkeypatch, tmp_path, respond)

    with pytest.raises(ValueError, match="leading or trailing whitespace"):
        asyncio.run(handler(idempotency_key=key, kwargs={"query": "sample"}))
    with pytest.raises(tool_error, match="leading or trailing whitespace"):
        asyncio.run(
            server.call_tool(
                "partner.search", {"kwargs": {"query": "sample"}, "idempotency_key": key}
            )
        )
    assert requests == []


def test_serve_async_sends_interior_space_idempotency_key_exactly(monkeypatch, tmp_path):
    """A key with interior spaces is valid and reaches the gateway byte for byte."""
    httpx = pytest.importorskip("httpx")

    def respond(request):
        return httpx.Response(200, json={"result": {"content": [{"type": "text", "text": "ok"}]}})

    _handler, requests, _httpx, server = _serve_tool_bridge(monkeypatch, tmp_path, respond)
    result = asyncio.run(
        server.call_tool(
            "partner.search", {"kwargs": {"query": "sample"}, "idempotency_key": "order 42 retry"}
        )
    )

    assert result.isError is False
    assert len(requests) == 1
    assert requests[0].headers["Idempotency-Key"] == "order 42 retry"


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

    handler, requests, _httpx, _server = _serve_tool_bridge(monkeypatch, tmp_path, respond)
    result = asyncio.run(handler(idempotency_key="invoke-123", kwargs={"query": "sample"}))

    assert result.isError is True
    assert result.structuredContent == error
    assert json.loads(result.content[0].text) == error
    assert len(requests) == 1


_CORRELATION_ID = "4f0c2a9e8b7d4c3fa1e2d3c4b5a69788"


@pytest.mark.parametrize(
    "status_code, body, headers, expected_code, expected_message",
    [
        (
            500,
            {
                "code": "internal_error",
                "message": "The request could not be completed.",
                "fix": "Try again later.",
                "correlation_id": _CORRELATION_ID,
            },
            {},
            "internal_error",
            "The request could not be completed.",
        ),
        (
            500,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "error": {
                    "code": -32603,
                    "message": "Internal error",
                    "data": {"correlation_id": _CORRELATION_ID},
                },
            },
            {},
            "internal_error",
            "Internal error",
        ),
        (
            503,
            {"code": "service_unavailable", "message": "Gateway is restarting."},
            {"X-Correlation-ID": _CORRELATION_ID},
            "service_unavailable",
            "Gateway is restarting.",
        ),
        (
            502,
            "<html>Bad Gateway</html>",
            {"X-Request-ID": _CORRELATION_ID},
            "internal_error",
            "POST /mcp failed: HTTP 502",
        ),
    ],
)
def test_serve_async_gateway_5xx_returns_retryable_structured_error(
    monkeypatch, tmp_path, status_code, body, headers, expected_code, expected_message
):
    """HTTP 5xx is returned as data so FastMCP cannot flatten it into a ToolError.

    The result keeps the gateway code and correlation id, and marks the call
    retryable with the same key, both from the handler and through
    ``FastMCP.call_tool``.
    """
    httpx = pytest.importorskip("httpx")

    def respond(request):
        if isinstance(body, dict):
            return httpx.Response(status_code, json=body, headers=headers)
        return httpx.Response(status_code, text=body, headers=headers)

    handler, requests, _httpx, server = _serve_tool_bridge(monkeypatch, tmp_path, respond)
    expected = {
        "error": expected_code,
        "code": expected_code,
        "message": expected_message,
        "correlation_id": _CORRELATION_ID,
        "http_status": status_code,
        "retryable": True,
    }

    direct = asyncio.run(handler(idempotency_key="invoke-123", kwargs={"query": "sample"}))
    via_fastmcp = asyncio.run(
        server.call_tool(
            "partner.search", {"kwargs": {"query": "sample"}, "idempotency_key": "invoke-123"}
        )
    )

    for result in (direct, via_fastmcp):
        assert result.isError is True
        assert result.structuredContent == expected
        assert json.loads(result.content[0].text) == expected
    assert len(requests) == 2
    assert [request.headers["Idempotency-Key"] for request in requests] == [
        "invoke-123",
        "invoke-123",
    ]


def test_serve_async_gateway_5xx_without_correlation_id_reports_none(monkeypatch, tmp_path):
    """No correlation id in body or headers is reported as null, never invented."""
    httpx = pytest.importorskip("httpx")

    def respond(request):
        return httpx.Response(500, json={"code": "internal_error"})

    _handler, _requests, _httpx, server = _serve_tool_bridge(monkeypatch, tmp_path, respond)
    result = asyncio.run(server.call_tool("partner.search", {"kwargs": {"query": "sample"}}))

    assert result.isError is True
    assert result.structuredContent["correlation_id"] is None
    assert result.structuredContent["retryable"] is True
