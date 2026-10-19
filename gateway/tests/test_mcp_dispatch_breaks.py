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
    UpstreamMcpDeliveryUncertainError,
    UpstreamMcpResponseRejectedError,
    UpstreamMcpReturnedError,
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
            MCP_UPSTREAM_CALL_TIMEOUT_SECONDS=3.0,
            MCP_UPSTREAM_MAX_RESPONSE_BYTES=4096,
        )
    )


def _scripted_upstream(
    follow_up: str,
    *,
    tool_result: dict[str, Any] | None = None,
    call_failure: str | None = None,
    trace: list[str] | None = None,
) -> tuple[httpx.AsyncClient, list[dict[str, Any]]]:
    """Script initialize, the tool call, and a hostile tools/list.

    ``follow_up`` is what a second tools/list would receive. The paid
    call is only correct when that request is never sent.
    """
    seen: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads((await request.aread()).decode("utf-8"))
        method = payload.get("method")
        if trace is not None:
            trace.append(method)
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
            if call_failure == "transport":
                raise httpx.ReadTimeout("upstream timeout")
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
            if call_failure == "invalid_json":
                return httpx.Response(
                    200,
                    headers={"Content-Type": "application/json"},
                    content=b"{broken",
                )
            result = tool_result or {
                "content": [{"type": "text", "text": "paid-result"}],
                "structuredContent": {"ok": True},
                "isError": False,
            }
            if call_failure == "secret":
                result = {
                    "content": [{"type": "text", "text": "secret-partner-token"}],
                    "structuredContent": {"ok": True},
                    "isError": False,
                }
            if call_failure == "oversized":
                result = {
                    "content": [{"type": "text", "text": "x" * 8000}],
                    "structuredContent": {"ok": True},
                    "isError": False,
                }
            body = {
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": result,
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
            # This unsolicited list can advertise a changed schema after
            # the call. It must not replace the schema found at discovery.
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
                                "required": [
                                    "different"
                                    if follow_up == "output_schema"
                                    else "ok"
                                ],
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


def _mark_discovered(
    adapter: UpstreamMcpAdapter, *, output_schema: dict[str, Any] | None
) -> None:
    adapter._discovered_tool = DiscoveredUpstreamTool(
        name="partner.write",
        description="Partner write",
        input_schema={"type": "object"},
        output_schema=output_schema,
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "follow_up",
    ["unknown_method", "output_schema", "oversized"],
)
async def test_paid_tool_call_ignores_a_hostile_follow_up_list(
    follow_up: str,
) -> None:
    trace: list[str] = []
    client, seen = _scripted_upstream(follow_up, trace=trace)
    adapter = UpstreamMcpAdapter(
        _configuration(),
        http_client=client,
        resolver=_global_resolver,
    )
    _mark_discovered(adapter, output_schema={"type": "object", "required": ["ok"]})
    claimed = False

    async def before_dispatch() -> None:
        nonlocal claimed
        claimed = True
        trace.append("claim")

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
    assert trace.index("claim") < trace.index("tools/call")
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
    assert result.payload["structuredContent"] == {"ok": True}
    assert result.payload["isError"] is False
    assert result.is_error is False


@pytest.mark.anyio
async def test_tool_without_advertised_output_schema_keeps_text_result() -> None:
    tool_result = {
        "content": [{"type": "text", "text": "paid-result"}],
        "isError": False,
    }
    client, seen = _scripted_upstream("unknown_method", tool_result=tool_result)
    adapter = UpstreamMcpAdapter(
        _configuration(), http_client=client, resolver=_global_resolver
    )
    _mark_discovered(adapter, output_schema=None)

    async def before_dispatch() -> None:
        return None

    try:
        result = await adapter.call_tool(
            {},
            invocation_id="inv-no-schema",
            idempotency_key="idem-no-schema",
            before_dispatch=before_dispatch,
        )
    finally:
        await client.aclose()

    assert result.payload == tool_result
    assert [entry["method"] for entry in seen].count("tools/call") == 1
    assert "tools/list" not in [entry["method"] for entry in seen]


@pytest.mark.anyio
@pytest.mark.parametrize("structured", [None, {"wrong": True}])
async def test_known_output_schema_rejects_missing_or_invalid_content(
    structured: dict[str, Any] | None,
) -> None:
    tool_result: dict[str, Any] = {
        "content": [{"type": "text", "text": "paid-result"}],
        "isError": False,
    }
    if structured is not None:
        tool_result["structuredContent"] = structured
    client, seen = _scripted_upstream("unknown_method", tool_result=tool_result)
    adapter = UpstreamMcpAdapter(
        _configuration(), http_client=client, resolver=_global_resolver
    )
    _mark_discovered(adapter, output_schema={"type": "object", "required": ["ok"]})

    async def before_dispatch() -> None:
        return None

    try:
        # The SDK raises after receiving a result that fails its discovered
        # schema. The adapter's existing error contract is delivery-uncertain.
        with pytest.raises(UpstreamMcpDeliveryUncertainError):
            await adapter.call_tool(
                {},
                invocation_id="inv-invalid-output",
                idempotency_key="idem-invalid-output",
                before_dispatch=before_dispatch,
            )
    finally:
        await client.aclose()

    methods = [entry["method"] for entry in seen]
    assert methods.count("tools/call") == 1
    assert "tools/list" not in methods


@pytest.mark.anyio
async def test_tool_level_error_keeps_its_error_result_with_known_schema() -> None:
    tool_result = {
        "content": [{"type": "text", "text": "partner rejected"}],
        "isError": True,
    }
    client, seen = _scripted_upstream("unknown_method", tool_result=tool_result)
    adapter = UpstreamMcpAdapter(
        _configuration(), http_client=client, resolver=_global_resolver
    )
    _mark_discovered(adapter, output_schema={"type": "object", "required": ["ok"]})

    async def before_dispatch() -> None:
        return None

    try:
        with pytest.raises(UpstreamMcpReturnedError) as exc_info:
            await adapter.call_tool(
                {},
                invocation_id="inv-error",
                idempotency_key="idem-error",
                before_dispatch=before_dispatch,
            )
    finally:
        await client.aclose()

    assert exc_info.value.result.payload == tool_result
    assert [entry["method"] for entry in seen].count("tools/call") == 1
    assert "tools/list" not in [entry["method"] for entry in seen]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("failure", "error_type", "code"),
    [
        ("oversized", UpstreamMcpResponseRejectedError, "upstream_response_too_large"),
        ("secret", UpstreamMcpResponseRejectedError, "upstream_response_rejected"),
        (
            "invalid_json",
            UpstreamMcpDeliveryUncertainError,
            "upstream_delivery_uncertain",
        ),
        ("transport", UpstreamMcpDeliveryUncertainError, "upstream_delivery_uncertain"),
    ],
)
async def test_failed_call_preserves_dispatch_classification_without_relisting(
    failure: str, error_type: type[Exception], code: str
) -> None:
    trace: list[str] = []
    client, seen = _scripted_upstream(
        "unknown_method", call_failure=failure, trace=trace
    )
    adapter = UpstreamMcpAdapter(
        _configuration(), http_client=client, resolver=_global_resolver
    )
    _mark_discovered(adapter, output_schema={"type": "object", "required": ["ok"]})

    async def before_dispatch() -> None:
        trace.append("claim")

    try:
        with pytest.raises(error_type) as exc_info:
            await adapter.call_tool(
                {},
                invocation_id=f"inv-{failure}",
                idempotency_key=f"idem-{failure}",
                before_dispatch=before_dispatch,
            )
    finally:
        await client.aclose()

    assert exc_info.value.code == code
    methods = [entry["method"] for entry in seen]
    assert trace.index("claim") < trace.index("tools/call")
    assert methods.count("tools/call") == 1
    assert "tools/list" not in methods
