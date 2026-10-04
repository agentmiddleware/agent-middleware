"""Standard MCP endpoint (POST /mcp): SDK-owned lifecycle, gating, auto-permits.

The protocol surface (initialize, version negotiation, notifications, ping,
JSON-RPC framing and errors) is owned by the official MCP SDK; these tests
pin the trust-plane contract layered on top of it — auth gating, origin
validation, server-minted permits, exactly-once replay, and signed receipts.
"""

from __future__ import annotations

import hashlib

import pytest
from httpx import ASGITransport, AsyncClient
from mcp.types import LATEST_PROTOCOL_VERSION

from app.core.config import get_settings
from app.main import app
from app.routers import mcp_standard as standard_mcp_router
from app.schemas.billing import ServiceCategory
from app.services.idempotency import (
    IdempotencyInProgressError,
    get_idempotency_service,
)
from app.services.mcp_generator import McpGenerator
from app.services.service_registry import get_service_registry
from tests.test_trust_helpers import BOOTSTRAP_HEADERS, provision_agent_wallet

# The SDK's streamable HTTP transport requires an explicit Accept header.
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
BOOTSTRAP_MCP_HEADERS = {**BOOTSTRAP_HEADERS, **MCP_HEADERS}


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


def _rpc(method: str, request_id: int | None = 1, params: dict | None = None) -> dict:
    body: dict = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if request_id is not None:
        body["id"] = request_id
    return body


def _initialize(protocol_version: str = "2025-06-18") -> dict:
    return _rpc(
        "initialize",
        params={
            "protocolVersion": protocol_version,
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "0.0.0"},
        },
    )


@pytest.mark.anyio
async def test_discovery_preserves_typed_tools_alongside_no_argument_tool(
    client, standard_mcp_enabled
):
    from mcp.types import Tool

    registry = get_service_registry()

    def ready() -> dict:
        raise AssertionError("discovery must not invoke a tool")

    def echo(message: str) -> dict:
        raise AssertionError("discovery must not invoke a tool")

    handlers = {"discovery.ready": ready, "discovery.echo": echo}
    for name, handler in handlers.items():
        registry.register_local(
            service_id=name,
            name=name,
            description="Synthetic discovery fixture",
            category=ServiceCategory.PLATFORM_FEE,
            func=handler,
        )
    try:
        typed_schema = registry.get_local("discovery.echo")["input_schema"]
        assert registry.get_local("discovery.ready")["input_schema"] is None
        standard = await client.post(
            "/mcp", json=_rpc("tools/list"), headers=BOOTSTRAP_MCP_HEADERS
        )
        assert standard.status_code == 200
        assert "result" in standard.json(), standard.text
        manifest = await client.get("/mcp/tools.json")
        assert manifest.status_code == 200
        for tools in (standard.json()["result"]["tools"], manifest.json()["tools"]):
            by_name = {tool["name"]: Tool.model_validate(tool) for tool in tools}
            assert by_name["discovery.ready"].inputSchema == {
                "type": "object",
                "properties": {},
            }
            assert by_name["discovery.echo"].inputSchema == typed_schema
    finally:
        for name in handlers:
            registry.unregister_local(name)


@pytest.mark.anyio
async def test_endpoint_disabled_by_default(client):
    resp = await client.post("/mcp", json=_initialize(), headers=BOOTSTRAP_MCP_HEADERS)
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_endpoint_requires_credentials(client, standard_mcp_enabled):
    resp = await client.post("/mcp", json=_initialize(), headers=MCP_HEADERS)
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_initialize_negotiates_requested_protocol(client, standard_mcp_enabled):
    resp = await client.post(
        "/mcp",
        json=_initialize("2025-03-26"),
        headers=BOOTSTRAP_MCP_HEADERS,
    )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["capabilities"]["tools"] == {"listChanged": False}
    assert result["serverInfo"]["name"] == McpGenerator.MANIFEST_NAME
    assert "Transaction-integrity boundary" in result["instructions"]
    assert "delivery_uncertain" in result["instructions"]
    assert "legacy compatibility identifier" in result["instructions"]


@pytest.mark.anyio
async def test_initialize_unsupported_version_offers_latest(
    client, standard_mcp_enabled
):
    resp = await client.post(
        "/mcp",
        json=_initialize("1999-01-01"),
        headers=BOOTSTRAP_MCP_HEADERS,
    )
    assert resp.json()["result"]["protocolVersion"] == LATEST_PROTOCOL_VERSION


@pytest.mark.anyio
async def test_notifications_are_accepted_without_reply(client, standard_mcp_enabled):
    resp = await client.post(
        "/mcp",
        json=_rpc("notifications/initialized", request_id=None),
        headers=BOOTSTRAP_MCP_HEADERS,
    )
    assert resp.status_code == 202
    assert resp.content == b""


@pytest.mark.anyio
async def test_ping_returns_empty_result(client, standard_mcp_enabled):
    resp = await client.post("/mcp", json=_rpc("ping"), headers=BOOTSTRAP_MCP_HEADERS)
    assert resp.json()["result"] == {}


@pytest.mark.anyio
async def test_tools_list_returns_tools_array(client, standard_mcp_enabled):
    resp = await client.post(
        "/mcp", json=_rpc("tools/list"), headers=BOOTSTRAP_MCP_HEADERS
    )
    assert resp.status_code == 200
    assert isinstance(resp.json()["result"]["tools"], list)


@pytest.mark.anyio
async def test_unknown_method_is_method_not_found(client, standard_mcp_enabled):
    resp = await client.post(
        "/mcp", json=_rpc("resources/list"), headers=BOOTSTRAP_MCP_HEADERS
    )
    assert resp.json()["error"]["code"] == -32601


@pytest.mark.anyio
async def test_batch_requests_are_rejected(client, standard_mcp_enabled):
    resp = await client.post("/mcp", json=[_rpc("ping")], headers=BOOTSTRAP_MCP_HEADERS)
    assert resp.status_code == 400
    assert "error" in resp.json()


@pytest.mark.anyio
async def test_missing_accept_header_is_not_acceptable(client, standard_mcp_enabled):
    resp = await client.post(
        "/mcp",
        json=_rpc("ping"),
        headers={**BOOTSTRAP_HEADERS, "Accept": "text/plain"},
    )
    assert resp.status_code == 406


@pytest.mark.anyio
async def test_get_and_delete_are_method_not_allowed(client, standard_mcp_enabled):
    resp = await client.get("/mcp", headers=BOOTSTRAP_MCP_HEADERS)
    assert resp.status_code == 405
    assert resp.headers["allow"] == "POST"
    resp = await client.request("DELETE", "/mcp", headers=BOOTSTRAP_MCP_HEADERS)
    assert resp.status_code == 405


@pytest.mark.anyio
async def test_cross_origin_browser_calls_are_rejected(client, standard_mcp_enabled):
    resp = await client.post(
        "/mcp",
        json=_rpc("ping"),
        headers={**BOOTSTRAP_MCP_HEADERS, "Origin": "https://evil.example"},
    )
    assert resp.status_code == 403


@pytest.mark.anyio
@pytest.mark.parametrize("origin", ["https://test", "http://test:444"])
async def test_standard_mcp_rejects_same_host_different_origin(
    client, standard_mcp_enabled, origin
):
    resp = await client.post(
        "/mcp",
        json=_rpc("ping"),
        headers={**BOOTSTRAP_MCP_HEADERS, "Origin": origin},
    )

    assert resp.status_code == 403


@pytest.mark.anyio
async def test_standard_mcp_uses_public_url_not_proxy_observed_scheme(
    client, standard_mcp_enabled, monkeypatch
):
    monkeypatch.setenv("PUBLIC_URL", "https://api.example.com")
    get_settings.cache_clear()
    try:
        insecure = await client.post(
            "/mcp",
            json=_rpc("ping"),
            headers={
                **BOOTSTRAP_MCP_HEADERS,
                "Host": "api.example.com",
                "Origin": "http://api.example.com",
            },
        )
        canonical = await client.post(
            "/mcp",
            json=_rpc("ping"),
            headers={
                **BOOTSTRAP_MCP_HEADERS,
                "Host": "api.example.com",
                "Origin": "https://api.example.com",
            },
        )
    finally:
        monkeypatch.setenv("PUBLIC_URL", "")
        get_settings.cache_clear()

    assert insecure.status_code == 403
    assert canonical.status_code == 200


@pytest.mark.anyio
async def test_tools_call_requires_wallet_scoped_key(client, standard_mcp_enabled):
    resp = await client.post(
        "/mcp",
        json=_rpc("tools/call", params={"name": "anything", "arguments": {}}),
        headers=BOOTSTRAP_MCP_HEADERS,
    )
    error = resp.json()["error"]
    assert error["code"] == -32003
    assert "wallet_scoped_key_required" in error["message"]


@pytest.mark.anyio
async def test_tools_call_unknown_tool(client, standard_mcp_enabled, clean_database):
    provisioned = await provision_agent_wallet(client)
    resp = await client.post(
        "/mcp",
        json=_rpc("tools/call", params={"name": "no-such-tool", "arguments": {}}),
        headers={**provisioned["agent_headers"], **MCP_HEADERS},
    )
    assert resp.json()["error"]["code"] == -32001


@pytest.mark.anyio
async def test_tools_call_auto_mints_bounded_permit_and_charges_once(
    client, standard_mcp_enabled, clean_database
):
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    registry = get_service_registry()
    registry.register_local(
        service_id="standard-echo",
        name="Standard Echo",
        description="Standard MCP endpoint test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=lambda message="ok": {"message": message},
        credits_per_unit=2.0,
        unit_name="call",
    )
    try:
        call = _rpc(
            "tools/call",
            params={"name": "standard-echo", "arguments": {"message": "hi"}},
        )
        headers = {
            **provisioned["agent_headers"],
            **MCP_HEADERS,
            "Idempotency-Key": "standard-mcp-call-1",
        }
        resp = await client.post("/mcp", json=call, headers=headers)
        assert resp.status_code == 200
        result = resp.json()["result"]
        assert result["isError"] is False
        receipt = result["receipt"]
        assert receipt["wallet_id"] == wallet_id
        # The receipt also rides the spec's extension point.
        assert result["_meta"]["io.agentmiddleware/receipt"] == receipt
        permit_id = receipt["permit_id"]

        # The auto-minted permit is a real signed permit, bounded to this
        # tool and the caller's own wallet/key.
        permit_resp = await client.get(
            f"/v1/permits/{permit_id}", headers=provisioned["agent_headers"]
        )
        assert permit_resp.status_code == 200
        permit = permit_resp.json()
        assert permit["issuer_wallet_id"] == wallet_id
        assert permit["subject_wallet_id"] == wallet_id
        assert permit["subject_key_id"] == provisioned["key_id"]
        assert permit["allowed_tools"] == ["standard-echo"]
        assert float(permit["max_credits"]) == 2.0

        # Replaying the same idempotency key returns the original receipt
        # without a second charge.
        replay = await client.post("/mcp", json=call, headers=headers)
        assert replay.status_code == 200
        assert replay.json()["result"]["receipt"]["receipt_id"] == receipt["receipt_id"]

        ledger = await client.get(
            f"/v1/billing/ledger/{wallet_id}", headers=provisioned["agent_headers"]
        )
        debits = [
            entry
            for entry in ledger.json()["entries"]
            if "standard-echo" in entry["description"]
        ]
        assert len(debits) == 1
    finally:
        registry.unregister_local("standard-echo")


@pytest.mark.anyio
async def test_tools_call_reports_auto_permit_contention_as_retryable(
    client,
    standard_mcp_enabled,
    clean_database,
):
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    tool_name = "standard-idempotency-in-progress"
    client_key = "standard-mcp-contention-1"
    calls = 0

    def tool() -> dict[str, bool]:
        nonlocal calls
        calls += 1
        return {"ok": True}

    registry = get_service_registry()
    registry.register_local(
        service_id=tool_name,
        name="Standard Contention",
        description="Standard MCP retry contract test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=tool,
        credits_per_unit=2.0,
        unit_name="call",
    )
    mint_key = "smcp-" + hashlib.sha256(client_key.encode("utf-8")).hexdigest()[:48]
    await get_idempotency_service().begin(
        wallet_id=wallet_id,
        endpoint=standard_mcp_router._AUTO_PERMIT_ENDPOINT,
        idempotency_key=mint_key,
        request_payload={
            "kind": "standard-mcp-auto-permit",
            "tool": tool_name,
            "wallet_id": wallet_id,
            "subject_key_id": provisioned["key_id"],
        },
    )
    try:
        response = await client.post(
            "/mcp",
            json=_rpc("tools/call", params={"name": tool_name, "arguments": {}}),
            headers={
                **provisioned["agent_headers"],
                **MCP_HEADERS,
                "Idempotency-Key": client_key,
            },
        )
    finally:
        registry.unregister_local(tool_name)

    assert response.status_code == 200
    assert response.json()["error"] == {
        "code": -32005,
        "message": "idempotency_in_progress",
    }
    assert calls == 0


@pytest.mark.anyio
async def test_tools_call_reports_governed_invoke_contention_with_existing_code(
    client,
    standard_mcp_enabled,
    clean_database,
    monkeypatch: pytest.MonkeyPatch,
):
    provisioned = await provision_agent_wallet(client)
    tool_name = "standard-governed-idempotency-in-progress"

    async def report_contention(*_args, **_kwargs):
        raise IdempotencyInProgressError("idempotency_in_progress")

    registry = get_service_registry()
    registry.register_local(
        service_id=tool_name,
        name="Standard Governed Contention",
        description="Standard MCP governed contention contract test",
        category=ServiceCategory.AGENT_COMMS,
        func=lambda: {"ok": True},
        credits_per_unit=2.0,
        unit_name="call",
    )
    monkeypatch.setattr(standard_mcp_router, "_handle_tools_call", report_contention)
    try:
        response = await client.post(
            "/mcp",
            json=_rpc("tools/call", params={"name": tool_name, "arguments": {}}),
            headers={
                **provisioned["agent_headers"],
                **MCP_HEADERS,
                "Idempotency-Key": "standard-governed-contention-1",
            },
        )
    finally:
        registry.unregister_local(tool_name)

    assert response.status_code == 200
    assert response.json()["error"] == {
        "code": -32005,
        "message": "idempotency_in_progress",
    }


# ── Bodies the reply could not carry are refused before dispatch ─────────────
# The SDK parses the envelope, but a JSON "\ud800" escape decodes to a lone
# surrogate its serializer cannot encode: as an ``id`` it failed the response
# after the governed tools/call had run and charged the wallet.


@pytest.fixture
def strict_counted_tool():
    calls: list[str] = []

    def _run(text: str = "") -> dict[str, str]:
        calls.append(text)
        return {"text": text}

    registry = get_service_registry()
    registry.register_local(
        service_id="standard-echo-strict",
        name="Standard Echo Strict",
        description="Counts executions",
        category=ServiceCategory.AGENT_COMMS,
        func=_run,
        credits_per_unit=2.0,
        unit_name="call",
    )
    try:
        yield calls
    finally:
        registry.unregister_local("standard-echo-strict")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "raw",
    [
        '{"jsonrpc":"2.0","id":"\\ud800","method":"tools/call","params":{"name":"standard-echo-strict","arguments":{"text":"hi"}}}',
        '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"standard-echo-strict","arguments":{"text":"\\ud800"}}}',
        '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"standard-echo-strict","arguments":{"text":NaN}}}',
    ],
    ids=["surrogate-id", "surrogate-argument", "nan-argument"],
)
async def test_unechoable_body_is_refused_before_any_charge(
    client, standard_mcp_enabled, clean_database, strict_counted_tool, raw
):
    provisioned = await provision_agent_wallet(client)
    headers = {
        **provisioned["agent_headers"],
        **MCP_HEADERS,
        "Idempotency-Key": "strict-1",
    }
    wallet_url = f"/v1/billing/wallets/{provisioned['agent_wallet_id']}"
    before = (
        await client.get(wallet_url, headers=provisioned["agent_headers"])
    ).json()["balance"]

    resp = await client.post("/mcp", content=raw.encode("ascii"), headers=headers)

    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert error["code"] == -32600
    assert error["message"].startswith("Invalid Request")
    assert strict_counted_tool == [], (
        "nothing may execute for a body the reply cannot carry"
    )
    after = (await client.get(wallet_url, headers=provisioned["agent_headers"])).json()[
        "balance"
    ]
    assert after == before


@pytest.mark.anyio
async def test_non_utf8_body_is_a_parse_error_not_500(client, standard_mcp_enabled):
    resp = await client.post(
        "/mcp",
        content=b'{"jsonrpc":"2.0","id":1,"method":"ping","x":"\xff"}',
        headers=BOOTSTRAP_MCP_HEADERS,
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == -32700


@pytest.mark.anyio
async def test_malformed_json_is_still_the_sdks_parse_error(
    client, standard_mcp_enabled
):
    """The strict pre-check leaves syntax errors to the SDK's own -32700 path."""
    resp = await client.post(
        "/mcp", content=b"{not json", headers=BOOTSTRAP_MCP_HEADERS
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == -32700
