"""Adversarial probes for the MCP HTTP routers.

Named cases: a malformed initialize, a tool call with no session, an
oversized body, and an unknown method. Refusals must stay client errors.
A refusal must not run a governed tool.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.service_registry import get_service_registry
from tests.test_trust_helpers import BOOTSTRAP_HEADERS, provision_agent_wallet

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
BOOTSTRAP_MCP_HEADERS = {**BOOTSTRAP_HEADERS, **MCP_HEADERS}
TOOL = "router-break.effect"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def standard_mcp_enabled(monkeypatch):
    monkeypatch.setenv("ENABLE_STANDARD_MCP_ENDPOINT", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.setenv("ENABLE_STANDARD_MCP_ENDPOINT", "false")
    get_settings.cache_clear()


@pytest.fixture
def effect_tool():
    calls: list[Any] = []

    def effect(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"ok": True}

    registry = get_service_registry()
    registry.register_local(
        service_id=TOOL,
        name="Router break effect",
        description="Must not run on a refused MCP message",
        category=ServiceCategory.AGENT_COMMS,
        func=effect,
        credits_per_unit=2.0,
        unit_name="call",
    )
    try:
        yield calls
    finally:
        registry.unregister_local(TOOL)


def _initialize(params: Any, request_id: Any = 1) -> dict[str, Any]:
    body: dict[str, Any] = {
        "jsonrpc": "2.0",
        "method": "initialize",
        "params": params,
    }
    if request_id is not None:
        body["id"] = request_id
    return body


_VALID_INIT_PARAMS = {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "probe", "version": "0"},
}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"null",
        b"[]",
        b"{",
        b"\xff",
        b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":NaN,"capabilities":{},"clientInfo":{"name":"p","version":"0"}}}',
        json.dumps(
            _initialize(
                {
                    "protocolVersion": None,
                    "capabilities": {},
                    "clientInfo": {"name": "p", "version": "0"},
                }
            )
        ).encode(),
        json.dumps(
            _initialize(
                {
                    "protocolVersion": True,
                    "capabilities": {},
                    "clientInfo": {"name": "p", "version": "0"},
                }
            )
        ).encode(),
        json.dumps(
            _initialize(
                {
                    "protocolVersion": 1.5,
                    "capabilities": {},
                    "clientInfo": {"name": "p", "version": "0"},
                }
            )
        ).encode(),
        json.dumps(
            _initialize(
                {
                    "protocolVersion": {"nested": "2025-06-18"},
                    "capabilities": {},
                    "clientInfo": {"name": "p", "version": "0"},
                }
            )
        ).encode(),
        json.dumps(
            _initialize(
                {
                    "protocolVersion": ["2025-06-18"],
                    "capabilities": {},
                    "clientInfo": {"name": "p", "version": "0"},
                }
            )
        ).encode(),
        json.dumps(
            _initialize(
                {
                    "protocolVersion": "",
                    "capabilities": [],
                    "clientInfo": "nope",
                }
            )
        ).encode(),
        json.dumps(_initialize(None)).encode(),
        json.dumps(_initialize([])).encode(),
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}).encode(),
        json.dumps(_initialize(_VALID_INIT_PARAMS, request_id=None)).encode(),
        json.dumps(_initialize(_VALID_INIT_PARAMS, request_id=True)).encode(),
        json.dumps(_initialize(_VALID_INIT_PARAMS, request_id=[1])).encode(),
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": None,
                "params": _VALID_INIT_PARAMS,
            }
        ).encode(),
        json.dumps(
            {"jsonrpc": "1.0", "id": 1, "method": "initialize", "params": {}}
        ).encode(),
    ],
)
async def test_malformed_initialize_is_not_a_server_error(
    client, standard_mcp_enabled, effect_tool, body: bytes
):
    response = await client.post(
        "/mcp",
        content=body,
        headers=BOOTSTRAP_MCP_HEADERS,
    )
    assert response.status_code < 500, response.text
    assert effect_tool == []


@pytest.mark.anyio
async def test_tool_call_without_session_does_not_skip_auth_or_crash(
    client, standard_mcp_enabled, clean_database, effect_tool
):
    provisioned = await provision_agent_wallet(client)
    wallet_headers = {**provisioned["agent_headers"], **MCP_HEADERS}
    call = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": TOOL, "arguments": {"text": "run"}},
    }

    missing_key = await client.post(
        "/mcp",
        json=call,
        headers={**MCP_HEADERS, "Mcp-Session-Id": "forged-session"},
    )
    assert missing_key.status_code == 401
    assert effect_tool == []

    no_session = await client.post("/mcp", json=call, headers=wallet_headers)
    assert no_session.status_code < 500, no_session.text
    assert "Mcp-Session-Id" not in no_session.headers
    assert (
        no_session.json()["result"]["receipt"]["wallet_id"]
        == (provisioned["agent_wallet_id"])
    )
    assert effect_tool == [{"text": "run"}]

    forged = await client.post(
        "/mcp",
        # Distinct arguments: an identical un-keyed call would replay the
        # first receipt under the derived retry key (SEL-484).
        json={
            **call,
            "id": 8,
            "params": {"name": TOOL, "arguments": {"text": "run-2"}},
        },
        headers={**wallet_headers, "Mcp-Session-Id": "forged-session"},
    )
    assert forged.status_code < 500, forged.text
    assert (
        forged.json()["result"]["receipt"]["wallet_id"]
        == (provisioned["agent_wallet_id"])
    )
    assert effect_tool == [{"text": "run"}, {"text": "run-2"}]

    for bad_id in (None, True, [], {}):
        body: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": TOOL, "arguments": {"text": "nope"}},
        }
        if bad_id is not None:
            body["id"] = bad_id
        refused = await client.post("/mcp", json=body, headers=wallet_headers)
        assert refused.status_code < 500, refused.text
    assert effect_tool == [{"text": "run"}, {"text": "run-2"}]


@pytest.mark.anyio
async def test_oversized_standard_mcp_body_is_refused_before_the_tool(
    client, standard_mcp_enabled, effect_tool
):
    limit = get_settings().MAX_REQUEST_BODY_BYTES
    padding = "x" * (limit + 1)
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": TOOL, "arguments": {"text": padding}},
        }
    ).encode()
    assert len(body) > limit
    response = await client.post(
        "/mcp",
        content=body,
        headers=BOOTSTRAP_MCP_HEADERS,
    )
    assert response.status_code == 413
    assert effect_tool == []

    # Chunked transfer omits Content-Length. The measured stream still
    # has to stop.
    chunked = await client.post(
        "/mcp",
        content=body,
        headers={**BOOTSTRAP_MCP_HEADERS, "Content-Length": ""},
    )
    # An empty Content-Length is not a size. Either the limit or the
    # header parser may refuse it, and neither may run the tool.
    assert chunked.status_code in {400, 411, 413}, chunked.text
    assert chunked.status_code < 500
    assert effect_tool == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "method",
    [
        "resources/list",
        "prompts/list",
        "logging/setLevel",
        "tools/execute",
        "INITIALIZE",
        "initialize ",
        "tools/call\x00",
        "",
        "notifications/initialized",
    ],
)
async def test_unknown_method_is_not_a_server_error(
    client, standard_mcp_enabled, effect_tool, method: str
):
    params: dict[str, Any] = {}
    if method == "logging/setLevel":
        params = {"level": "debug"}
    if method == "notifications/initialized":
        body: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
    else:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }
    response = await client.post(
        "/mcp",
        json=body,
        headers=BOOTSTRAP_MCP_HEADERS,
    )
    assert response.status_code < 500, response.text
    if method == "notifications/initialized":
        assert response.status_code == 202
    elif method in {
        "resources/list",
        "prompts/list",
        "logging/setLevel",
    }:
        # Known MCP methods this server does not implement.
        payload = response.json()
        assert payload["error"]["code"] == -32601, response.text
    elif method in {"tools/execute", "INITIALIZE"}:
        # Methods the SDK does not model are rejected as invalid params.
        # They still must not run a tool.
        payload = response.json()
        assert payload["error"]["code"] == -32602, response.text
    assert effect_tool == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [
        {"jsonrpc": "2.0", "id": 1, "method": 1, "params": {}},
        {"jsonrpc": "2.0", "id": 1, "method": None, "params": {}},
        {"jsonrpc": "2.0", "id": 1, "method": ["tools/call"], "params": {}},
        {"jsonrpc": "2.0", "id": 1, "params": {"name": TOOL}},
    ],
)
async def test_non_string_method_is_not_a_server_error(
    client, standard_mcp_enabled, effect_tool, body: dict
):
    response = await client.post(
        "/mcp",
        json=body,
        headers=BOOTSTRAP_MCP_HEADERS,
    )
    assert response.status_code < 500, response.text
    assert effect_tool == []
