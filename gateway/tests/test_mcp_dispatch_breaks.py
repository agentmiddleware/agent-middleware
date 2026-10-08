"""Adversarial checks for one upstream MCP dispatch.

A paid tools/call must stand on its own response. The SDK client helper
also sends tools/list when it has not cached an output schema, and that
second request runs only after the tool has already succeeded. These
tests use the real client session against a scripted upstream so a
hostile follow-up cannot be hidden by a fake session.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import httpx
import pytest

from app.services.upstream_mcp import (
    DiscoveredUpstreamTool,
    UpstreamMcpAdapter,
    UpstreamMcpConfiguration,
)
from app.core.config import Settings


async def _global_resolver(_host: str, _port: int | None) -> Sequence[str]:
    return ("93.184.216.34",)


def _configuration() -> UpstreamMcpConfiguration:
    return UpstreamMcpConfiguration.from_settings(
        Settings(
            _env_file=None,
            ENVIRONMENT="local",
            MCP_UPSTREAM_ENABLED=True,
            MCP_UPSTREAM_URL="https://partner.example/mcp",
            MCP_UPSTREAM_TOOL_NAME="partner.write",
            MCP_UPSTREAM_PUBLIC_TOOL_ID="partner.notes.write",
            MCP_UPSTREAM_BEARER_TOKEN="secret-partner-token",
            MCP_UPSTREAM_CREDITS_PER_CALL="7.5",
            MCP_UPSTREAM_MAX_RESPONSE_BYTES=4096,
        )
    )


def _scripted_upstream(
    follow_up: str,
) -> tuple[httpx.AsyncClient, list[dict[str, Any]]]:
    """Script initialize, the tool call, and a hostile tools/list.

    ``follow_up`` is what a second tools/list would receive. The paid
    call is only correct when that request is never sent.
    """
    seen: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads((await request.aread()).decode("utf-8"))
        method = payload.get("method")
        seen.append(
            {
                "method": method,
                "session_id": request.headers.get("mcp-session-id"),
                "params": payload.get("params"),
            }
        )
        if payload.get("id") is None:
            return httpx.Response(202)
        if method == "initialize":
            body = {
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "serverInfo": {"name": "partner", "version": "1.0"},
                },
            }
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "application/json",
                    "mcp-session-id": "partner-session-1",
                },
                content=json.dumps(body).encode("utf-8"),
            )
        if method == "tools/call":
            if request.headers.get("mcp-session-id") != "partner-session-1":
                body = {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "error": {
                        "code": -32000,
                        "message": "session required",
                    },
                }
                return httpx.Response(
                    200,
                    headers={"Content-Type": "application/json"},
                    content=json.dumps(body).encode("utf-8"),
                )
            body = {
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "content": [{"type": "text", "text": "paid-result"}],
                    "isError": False,
                },
            }
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "application/json",
                    "mcp-session-id": "partner-session-1",
                },
                content=json.dumps(body).encode("utf-8"),
            )
        if method == "tools/list":
            if follow_up == "unknown_method":
                body = {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "error": {"code": -32601, "message": "Method not found"},
                }
                return httpx.Response(
                    200,
                    headers={"Content-Type": "application/json"},
                    content=json.dumps(body).encode("utf-8"),
                )
            # A successful list that publishes an output schema. The tool
            # result above has no structured content, so a client that
            # validates against this list rejects a body it already has.
            body = {
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "tools": [
                        {
                            "name": "partner.write",
                            "inputSchema": {"type": "object"},
                            "outputSchema": {
                                "type": "object",
                                "required": ["ok"],
                            },
                        }
                    ]
                },
            }
            encoded = json.dumps(body).encode("utf-8")
            if follow_up == "oversized":
                encoded = encoded + (b" " * 8000)
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                content=encoded,
            )
        raise AssertionError(f"unexpected MCP method: {method}")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer secret-partner-token"},
        follow_redirects=False,
    )
    return client, seen


def _mark_discovered(adapter: UpstreamMcpAdapter) -> None:
    adapter._discovered_tool = DiscoveredUpstreamTool(
        name="partner.write",
        description="Partner write",
        input_schema={"type": "object"},
        output_schema={"type": "object", "required": ["ok"]},
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "follow_up",
    ["unknown_method", "output_schema", "oversized"],
)
async def test_paid_tool_call_ignores_a_hostile_follow_up_list(
    follow_up: str,
) -> None:
    client, seen = _scripted_upstream(follow_up)
    adapter = UpstreamMcpAdapter(
        _configuration(),
        http_client=client,
        resolver=_global_resolver,
    )
    _mark_discovered(adapter)
    claimed = False

    async def before_dispatch() -> None:
        nonlocal claimed
        claimed = True

    try:
        result = await adapter.call_tool(
            {"note": "hello"},
            invocation_id="inv-paid",
            idempotency_key="idem-paid",
            before_dispatch=before_dispatch,
        )
    finally:
        await client.aclose()

    methods = [entry["method"] for entry in seen]
    assert claimed is True
    assert methods == [
        "initialize",
        "notifications/initialized",
        "tools/call",
    ]
    call = next(entry for entry in seen if entry["method"] == "tools/call")
    assert call["session_id"] == "partner-session-1"
    assert call["params"]["_meta"] == {
        "io.agentmiddleware/invocation_id": "inv-paid",
        "io.agentmiddleware/idempotency_key": "idem-paid",
    }
    assert result.payload["content"] == [{"type": "text", "text": "paid-result"}]
    assert result.payload["isError"] is False
    assert result.is_error is False
