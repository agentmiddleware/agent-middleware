"""Standard MCP endpoint (POST /mcp): SDK-owned lifecycle, gating, auto-permits.

The protocol surface (initialize, version negotiation, notifications, ping,
JSON-RPC framing and errors) is owned by the official MCP SDK; these tests
pin the trust-plane contract layered on top of it — auth gating, origin
validation, server-minted permits, exactly-once replay, and signed receipts.
"""

from __future__ import annotations

import hashlib

from decimal import Decimal

import pytest

from tests.conftest import running_on_sqlite
from uuid import uuid4
from httpx import ASGITransport, AsyncClient
from mcp.types import LATEST_PROTOCOL_VERSION
from b2a_sdk.receipt_verifier import key_set_from_document, verify_bundle

from app.core.config import get_settings
from app.main import app
from app.routers import mcp_standard as standard_mcp_router
from app.schemas.billing import ServiceCategory
from app.services.idempotency import (
    IdempotencyInProgressError,
    get_idempotency_service,
)
from app.services.mcp_generator import MCP_SERVER_VERSION, McpGenerator
from app.services.service_registry import get_service_registry
from tests.test_trust_helpers import (
    BOOTSTRAP_HEADERS,
    create_tool_permit,
    provision_agent_wallet,
)

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


def test_openapi_documents_distinct_jsonrpc_post_bodies():
    schema = app.openapi()["paths"]["/mcp"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert schema["required"] == ["jsonrpc"]
    variants = {variant["title"]: variant for variant in schema["oneOf"]}
    assert set(variants) == {
        "JSON-RPC request",
        "JSON-RPC notification",
        "JSON-RPC response",
    }
    request = variants["JSON-RPC request"]
    assert request["required"] == ["method", "id"]
    assert request["not"] == {
        "anyOf": [{"required": ["result"]}, {"required": ["error"]}]
    }
    assert variants["JSON-RPC notification"]["required"] == ["method"]
    assert variants["JSON-RPC notification"]["not"] == {
        "anyOf": [
            {"required": ["id"]},
            {"required": ["result"]},
            {"required": ["error"]},
        ]
    }
    response = variants["JSON-RPC response"]
    assert response["required"] == ["id"]
    assert response["not"] == {"required": ["method"]}
    assert response["oneOf"] == [
        {"required": ["result"]},
        {"required": ["error"]},
    ]
    assert "result" in response["properties"]
    assert {item["type"] for item in request["properties"]["id"]["anyOf"]} == {
        "integer",
        "string",
    }
    assert {item["type"] for item in response["properties"]["id"]["anyOf"]} == {
        "integer",
        "string",
        "null",
    }


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
async def test_endpoint_enabled_by_default_and_requires_auth(client, monkeypatch):
    monkeypatch.delenv("ENABLE_STANDARD_MCP_ENDPOINT", raising=False)
    get_settings.cache_clear()
    try:
        assert get_settings().ENABLE_STANDARD_MCP_ENDPOINT is True
        resp = await client.post("/mcp", json=_initialize(), headers=MCP_HEADERS)
        assert resp.status_code == 401
        resp = await client.post(
            "/mcp", json=_initialize(), headers=BOOTSTRAP_MCP_HEADERS
        )
        assert resp.status_code == 200
    finally:
        get_settings.cache_clear()


@pytest.mark.anyio
async def test_endpoint_returns_404_when_disabled(client, monkeypatch):
    monkeypatch.setenv("ENABLE_STANDARD_MCP_ENDPOINT", "false")
    get_settings.cache_clear()
    try:
        unauthenticated = await client.post(
            "/mcp", json=_initialize(), headers=MCP_HEADERS
        )
        assert unauthenticated.status_code == 401
        resp = await client.post(
            "/mcp", json=_initialize(), headers=BOOTSTRAP_MCP_HEADERS
        )
        assert resp.status_code == 404
    finally:
        get_settings.cache_clear()


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
    assert result["serverInfo"]["version"] == MCP_SERVER_VERSION
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
async def test_standard_mcp_rejects_rebound_host_without_public_url(
    client, standard_mcp_enabled
):
    assert not get_settings().PUBLIC_URL
    resp = await client.post(
        "/mcp",
        json=_rpc("ping"),
        headers={
            **BOOTSTRAP_MCP_HEADERS,
            "Host": "rebind.evil",
            "Origin": "http://rebind.evil",
        },
    )
    assert resp.status_code == 421


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
    assert error["data"]["code"] == "wallet_scoped_key_required"
    assert error["data"]["docs_url"].endswith("#wallet_scoped_key_required")


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
async def test_tools_call_dogfood_tool_works_without_prior_tools_list(
    client, standard_mcp_enabled, clean_database, monkeypatch, tmp_path
):
    """A fresh gateway must resolve flag-gated tools on tools/call alone."""
    from app.services import dogfood_tool

    monkeypatch.setenv("ENABLE_DOGFOOD_TOOL", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(dogfood_tool, "DOGFOOD_NOTES_PATH", tmp_path / "notes.jsonl")
    # Simulate a freshly started gateway: nothing has run tools/list yet.
    dogfood_tool.unregister_dogfood_tool()
    get_service_registry().unregister_local(dogfood_tool.DOGFOOD_TOOL_ID)

    provisioned = await provision_agent_wallet(client)
    try:
        resp = await client.post(
            "/mcp",
            json=_rpc(
                "tools/call",
                params={
                    "name": dogfood_tool.DOGFOOD_TOOL_ID,
                    "arguments": {"text": "fresh"},
                },
            ),
            headers={**provisioned["agent_headers"], **MCP_HEADERS},
        )
        body = resp.json()
        assert "error" not in body, body
        assert body["result"]["isError"] is False
        assert body["result"]["receipt"]["wallet_id"] == provisioned["agent_wallet_id"]
    finally:
        dogfood_tool.unregister_dogfood_tool()
        monkeypatch.setenv("ENABLE_DOGFOOD_TOOL", "false")
        get_settings.cache_clear()


@pytest.mark.anyio
async def test_tools_call_auto_mints_bounded_permit_and_reuses_replay(
    client, standard_mcp_enabled, clean_database
):
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    registry = get_service_registry()
    dispatched = []

    def echo(message="ok"):
        dispatched.append(message)
        return {"message": message}

    registry.register_local(
        service_id="standard-echo",
        name="Standard Echo",
        description="Standard MCP endpoint test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=echo,
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

        # A retry with the same idempotency key must return the original
        # receipt instead of dispatching again.
        replay = await client.post("/mcp", json=call, headers=headers)
        assert replay.status_code == 200
        assert replay.json()["result"]["receipt"] == receipt
        assert dispatched == ["hi"]

        next_call = _rpc(
            "tools/call",
            params={"name": "standard-echo", "arguments": {"message": "next"}},
        )
        conflict = await client.post("/mcp", json=next_call, headers=headers)
        assert conflict.status_code == 200
        assert conflict.json()["error"]["message"] == "idempotency_key_reused"
        assert dispatched == ["hi"]

        next_headers = {**headers, "Idempotency-Key": "standard-mcp-call-2"}
        next_response = await client.post("/mcp", json=next_call, headers=next_headers)
        assert next_response.status_code == 200
        assert next_response.json()["result"]["receipt"]["permit_id"] != permit_id
        assert dispatched == ["hi", "next"]

        ledger = await client.get(
            f"/v1/billing/ledger/{wallet_id}", headers=provisioned["agent_headers"]
        )
        debits = [
            entry
            for entry in ledger.json()["entries"]
            if "standard-echo" in entry["description"]
        ]
        assert len(debits) == 2
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
    error = response.json()["error"]
    assert error["code"] == -32005
    assert error["message"] == "idempotency_in_progress"
    assert error["data"]["code"] == "idempotency_in_progress"
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
    error = response.json()["error"]
    assert error["code"] == -32005
    assert error["message"] == "idempotency_in_progress"
    assert error["data"]["code"] == "idempotency_in_progress"


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


@pytest.mark.anyio
async def test_unkeyed_tools_call_retry_replays_without_second_dispatch(
    client, standard_mcp_enabled, clean_database
):
    """SEL-484: an unkeyed retry (fresh JSON-RPC id) must not dispatch twice."""
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    invocations: list[str] = []

    def _echo(message="ok", n=0):
        invocations.append(message)
        return {"message": message}

    registry = get_service_registry()
    registry.register_local(
        service_id="standard-echo-retry",
        name="Standard Echo Retry",
        description="Unkeyed retry test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=_echo,
        credits_per_unit=2.0,
        unit_name="call",
    )
    headers = {**provisioned["agent_headers"], **MCP_HEADERS}
    try:
        first = await client.post(
            "/mcp",
            json=_rpc(
                "tools/call",
                request_id=101,
                params={
                    "name": "standard-echo-retry",
                    "arguments": {"message": "hi", "n": 1},
                },
            ),
            headers=headers,
        )
        assert first.status_code == 200
        receipt = first.json()["result"]["receipt"]

        # Retry: new JSON-RPC id, same arguments in a different key order.
        retry = await client.post(
            "/mcp",
            json=_rpc(
                "tools/call",
                request_id=102,
                params={
                    "name": "standard-echo-retry",
                    "arguments": {"n": 1, "message": "hi"},
                },
            ),
            headers=headers,
        )
        assert retry.status_code == 200
        assert retry.json()["result"]["receipt"]["receipt_id"] == receipt["receipt_id"]
        assert invocations == ["hi"]

        # Different arguments are a distinct call and dispatch.
        other = await client.post(
            "/mcp",
            json=_rpc(
                "tools/call",
                request_id=103,
                params={
                    "name": "standard-echo-retry",
                    "arguments": {"message": "bye", "n": 1},
                },
            ),
            headers=headers,
        )
        assert other.status_code == 200
        assert other.json()["result"]["receipt"]["receipt_id"] != receipt["receipt_id"]
        assert invocations == ["hi", "bye"]

        ledger = await client.get(
            f"/v1/billing/ledger/{wallet_id}", headers=provisioned["agent_headers"]
        )
        debits = [
            e
            for e in ledger.json()["entries"]
            if "standard-echo-retry" in e["description"]
        ]
        assert len(debits) == 2
    finally:
        registry.unregister_local("standard-echo-retry")


async def _ledger_debits(client, provisioned, tool):
    ledger = await client.get(
        f"/v1/billing/ledger/{provisioned['agent_wallet_id']}",
        headers=provisioned["agent_headers"],
    )
    assert ledger.status_code == 200, ledger.text
    return [e for e in ledger.json()["entries"] if tool in e["description"]]


def _assert_idempotency_key_required(resp):
    assert resp.status_code == 200, resp.text
    error = resp.json()["error"]
    assert error["code"] == -32003
    assert error["message"] == "idempotency_key_required"
    assert error["data"]["reason_code"] == "idempotency_key_required"
    assert error["data"]["remediation"]["type"] == "retry_with_idempotency_key"


@pytest.mark.anyio
async def test_unkeyed_call_with_window_zero_is_refused_without_dispatch(
    client, standard_mcp_enabled, clean_database, monkeypatch
):
    """SEL-498: no retry slot and no key is refused, never a random key."""
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "MCP_UNKEYED_RETRY_WINDOW_SECONDS", 0)
    provisioned = await provision_agent_wallet(client)
    invocations: list[str] = []
    _register_counting_tool("standard-echo-legacy", invocations)
    headers = {**provisioned["agent_headers"], **MCP_HEADERS}
    try:
        for rid in (201, 202):
            resp = await _call(client, headers, "standard-echo-legacy", rid)
            _assert_idempotency_key_required(resp)
        assert invocations == []
        assert await _ledger_debits(client, provisioned, "standard-echo-legacy") == []

        # The remediation works: a keyed call and its keyed retry dispatch
        # and bill exactly once.
        keyed = {**headers, "Idempotency-Key": "sel-498-window-zero"}
        first = await _call(client, keyed, "standard-echo-legacy", 203)
        retry = await _call(client, keyed, "standard-echo-legacy", 204)
        assert first.status_code == retry.status_code == 200
        assert (
            retry.json()["result"]["receipt"]["receipt_id"]
            == first.json()["result"]["receipt"]["receipt_id"]
        )
        assert invocations == ["hi"]
        assert (
            len(await _ledger_debits(client, provisioned, "standard-echo-legacy")) == 1
        )
    finally:
        get_service_registry().unregister_local("standard-echo-legacy")


def _register_counting_tool(service_id: str, invocations: list, **extra):
    def _echo(message="ok", n=0):
        invocations.append(message)
        return {"message": message}

    get_service_registry().register_local(
        service_id=service_id,
        name=service_id,
        description="SEL-484 unkeyed retry test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=_echo,
        credits_per_unit=2.0,
        unit_name="call",
        **extra,
    )


async def _call(client, headers, tool, request_id, arguments=None):
    return await client.post(
        "/mcp",
        json=_rpc(
            "tools/call",
            request_id=request_id,
            params={"name": tool, "arguments": arguments or {"message": "hi"}},
        ),
        headers=headers,
    )


@pytest.mark.anyio
async def test_unkeyed_retry_straddling_window_boundary_dispatches_once(
    client, standard_mcp_enabled, clean_database, monkeypatch
):
    """Codex blocking 1: a retry across a fixed bucket edge must still replay."""
    import time as _time

    from app.routers import mcp_standard

    window = mcp_standard._effective_unkeyed_retry_window()
    edge = (int(_time.time() // window) + 1) * window
    provisioned = await provision_agent_wallet(client)
    headers = {**provisioned["agent_headers"], **MCP_HEADERS}
    invocations: list[str] = []
    _register_counting_tool("echo-boundary", invocations)
    try:
        monkeypatch.setattr(mcp_standard, "_unkeyed_clock", lambda: edge - 0.001)
        first = await _call(client, headers, "echo-boundary", 1)
        monkeypatch.setattr(mcp_standard, "_unkeyed_clock", lambda: edge + 0.001)
        retry = await _call(client, headers, "echo-boundary", 2)
        assert first.status_code == retry.status_code == 200
        assert (
            retry.json()["result"]["receipt"]["receipt_id"]
            == first.json()["result"]["receipt"]["receipt_id"]
        )
        assert invocations == ["hi"]
    finally:
        get_service_registry().unregister_local("echo-boundary")


@pytest.mark.anyio
async def test_unkeyed_read_only_tool_is_refused_without_dispatch(
    client, standard_mcp_enabled, clean_database
):
    """Read-only tools get no deterministic slot, so unkeyed calls are refused."""
    provisioned = await provision_agent_wallet(client)
    headers = {**provisioned["agent_headers"], **MCP_HEADERS}
    invocations: list[str] = []
    _register_counting_tool("echo-read-only", invocations, read_only=True)
    try:
        for rid in (1, 2):
            resp = await _call(client, headers, "echo-read-only", rid)
            _assert_idempotency_key_required(resp)
        assert invocations == []
        assert await _ledger_debits(client, provisioned, "echo-read-only") == []

        # Distinct keys are distinct reads; each dispatches (no stale replay).
        for rid, key in ((3, "sel-498-read-a"), (4, "sel-498-read-b")):
            resp = await _call(
                client, {**headers, "Idempotency-Key": key}, "echo-read-only", rid
            )
            assert resp.status_code == 200
            assert "result" in resp.json(), resp.text
        assert invocations == ["hi", "hi"]
        # Lead's exemption test (#233): read-only calls ARE metered. Each keyed
        # read debits the wallet at the tool's per-call price, so an unkeyed
        # read could bill twice on retry and must stay refused.
        debits = await _ledger_debits(client, provisioned, "echo-read-only")
        assert len(debits) == 2
        assert all(Decimal(str(e["amount"])) != 0 for e in debits)
    finally:
        get_service_registry().unregister_local("echo-read-only")


@pytest.mark.anyio
async def test_demo_permit_mint_refuses_unkeyed_call_without_slot(monkeypatch):
    """mcp.py demo path: no slot and no key raises before any permit is minted."""
    from types import SimpleNamespace

    from app.core.config import get_settings
    from app.routers import mcp as mcp_router
    from app.routers import mcp_standard

    minted: list[str] = []

    async def _no_slot(**_kwargs):
        return None

    async def _mint(**_kwargs):
        minted.append("permit")
        return "permit-should-not-exist"

    async def _no_approval(_wallet_id):
        return False

    monkeypatch.setattr(mcp_standard, "_resolve_unkeyed_retry", _no_slot)
    monkeypatch.setattr(mcp_standard, "_mint_auto_permit", _mint)
    monkeypatch.setattr(mcp_router, "wallet_human_approval_required", _no_approval)
    monkeypatch.setattr(get_settings(), "DEMO_ALLOWED_TOOLS", "partner.echo")
    monkeypatch.setattr(mcp_router, "_ensure_local_mcp_tools_registered", lambda: None)
    registry = get_service_registry()
    existed = registry.get_local("partner.echo") is not None
    if not existed:
        _register_counting_tool("partner.echo", [])
    request = SimpleNamespace(
        auth=SimpleNamespace(tenant="demo", wallet_id="w-demo", allowed_tools=None),
        wallet_id="w-demo",
        permit_id=None,
        tool_name="partner.echo",
        arguments={"message": "hi"},
        idempotency_key=None,
    )
    try:
        with pytest.raises(ValueError, match="^idempotency_key_required$"):
            await mcp_router._maybe_mint_demo_permit(request)
        assert minted == []
        assert request.idempotency_key is None
    finally:
        if not existed:
            registry.unregister_local("partner.echo")


def test_dogfood_notes_count_is_declared_read_only(monkeypatch):
    from app.services import dogfood_tool

    monkeypatch.setattr(dogfood_tool, "_second_registered", False)
    registry = get_service_registry()
    existed = registry.get_local(dogfood_tool.DOGFOOD_SECOND_TOOL_ID) is not None
    dogfood_tool.register_dogfood_second_tool()
    try:
        record = registry.get_local(dogfood_tool.DOGFOOD_SECOND_TOOL_ID)
        assert record is not None and record["read_only"] is True
    finally:
        if not existed:
            registry.unregister_local(dogfood_tool.DOGFOOD_SECOND_TOOL_ID)


@pytest.mark.anyio
async def test_unkeyed_retry_after_no_effect_failure_runs_again(
    client, standard_mcp_enabled, clean_database
):
    """Codex: a real refunded pre-dispatch failure is retried, not replayed.

    The upstream executor genuinely fails before dispatch (nothing reaches
    the tool, the charge is refunded); the identical unkeyed retry then runs
    once and a further retry replays that success.
    """
    from tests.test_mcp_upstream_governed import (
        FakeUpstreamExecutor,
        _register_upstream,
    )

    tool = f"echo-no-effect-{uuid4().hex[:8]}"
    provisioned = await provision_agent_wallet(client)
    headers = {**provisioned["agent_headers"], **MCP_HEADERS}
    executor = FakeUpstreamExecutor("pre_dispatch_failure")
    _register_upstream(tool, executor)

    async def _entries():
        ledger = await client.get(
            f"/v1/billing/ledger/{provisioned['agent_wallet_id']}",
            headers=provisioned["agent_headers"],
        )
        assert ledger.status_code == 200, ledger.text
        return {e["entry_id"]: e for e in ledger.json()["entries"]}

    def _net(entries):
        return sum((Decimal(str(e["amount"])) for e in entries), Decimal("0"))

    try:
        before = await _entries()
        first = await _call(client, headers, tool, 1)
        assert first.status_code == 200, first.text
        assert executor.dispatch_count == 0
        assert len(executor.calls) == 1
        after_failure = await _entries()
        failure_entries = [e for k, e in after_failure.items() if k not in before]
        # Charged then refunded: no net charge for the pre-dispatch failure.
        assert failure_entries, "expected charge+refund ledger rows"
        assert _net(failure_entries) == 0, failure_entries

        executor.mode = "success"
        retry = await _call(client, headers, tool, 2)
        assert retry.status_code == 200, retry.text
        after_success = await _entries()
        success_entries = [
            e for k, e in after_success.items() if k not in after_failure
        ]
        debits = [e for e in success_entries if Decimal(str(e["amount"])) < 0]
        assert len(debits) == 1, success_entries
        assert _net(success_entries) == Decimal(str(debits[0]["amount"])) < 0
        assert "result" in retry.json(), retry.text
        assert executor.dispatch_count == 1
        again = await _call(client, headers, tool, 3)
        assert (
            again.json()["result"]["receipt"]["receipt_id"]
            == retry.json()["result"]["receipt"]["receipt_id"]
        )
        assert executor.dispatch_count == 1
        # The replay adds no ledger entry.
        assert await _entries() == after_success
    finally:
        get_service_registry().unregister_local(tool)


@pytest.mark.anyio
async def test_simultaneous_identical_unkeyed_calls_dispatch_once(
    client, standard_mcp_enabled, clean_database
):
    """Codex non-blocking 2: two concurrent identical calls are one operation."""
    import asyncio

    provisioned = await provision_agent_wallet(client)
    headers = {**provisioned["agent_headers"], **MCP_HEADERS}
    invocations: list[str] = []
    _register_counting_tool("echo-concurrent", invocations)
    try:
        a, b = await asyncio.gather(
            _call(client, headers, "echo-concurrent", 1),
            _call(client, headers, "echo-concurrent", 2),
        )
        assert invocations == ["hi"]
        receipts = set()
        for resp in (a, b):
            body = resp.json()
            if "result" in body:
                receipts.add(body["result"]["receipt"]["receipt_id"])
            else:
                assert body["error"]["code"] == -32005, resp.text
        assert len(receipts) == 1
    finally:
        get_service_registry().unregister_local("echo-concurrent")


async def _backdate_lock(wallet_id: str, lock_key: str) -> None:
    """Age a held lock row past the stale threshold via its real created_at."""
    from datetime import timedelta

    from sqlalchemy import update

    from app.db.database import get_session_factory
    from app.db.models import IdempotencyRecordModel
    from app.routers import mcp_standard
    from app.core.time import utc_now

    factory = get_session_factory()
    async with factory() as session:
        await session.execute(
            update(IdempotencyRecordModel)
            .where(
                IdempotencyRecordModel.wallet_id == wallet_id,
                IdempotencyRecordModel.endpoint
                == mcp_standard._UNKEYED_RETRY_LOCK_ENDPOINT,
                IdempotencyRecordModel.idempotency_key == lock_key,
            )
            .values(
                created_at=utc_now()
                - timedelta(seconds=mcp_standard._UNKEYED_RETRY_LOCK_STALE_SECONDS + 5)
            )
        )
        await session.commit()


def _stall_after_acquire(monkeypatch, on_stall):
    """Holder A acquires the lock for real, then stalls (runs ``on_stall``)
    before minting. Only the first acquire stalls; later ones are untouched.
    Neither the ownership check nor the mint is patched."""
    from app.routers import mcp_standard

    real_acquire = mcp_standard._acquire_unkeyed_retry_lock
    state = {"stalled": False}

    async def _acquire(wallet_id, lock_key):
        record_id = await real_acquire(wallet_id, lock_key)
        if not state["stalled"]:
            state["stalled"] = True
            await on_stall(wallet_id, lock_key, record_id)
        return record_id

    monkeypatch.setattr(mcp_standard, "_acquire_unkeyed_retry_lock", _acquire)


@pytest.mark.anyio
async def test_stale_takeover_via_created_at_fences_stalled_holder_mint(
    client, standard_mcp_enabled, clean_database, monkeypatch
):
    """Codex blocker: no window between the ownership check and the mint.
    A stalls holding the lock; its row ages past the stale threshold (real
    created_at); worker B reclaims it through the real acquire path and runs
    the call end-to-end. A then resumes: its fenced mint-row insert must fail
    atomically or replay B's outcome, so the tool runs exactly once."""
    from app.routers import mcp_standard
    from app.services.idempotency import get_idempotency_service

    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    headers = {**provisioned["agent_headers"], **MCP_HEADERS}
    invocations: list[str] = []
    _register_counting_tool("echo-stale", invocations)
    idem = get_idempotency_service()
    seen: dict[str, object] = {}

    async def _while_a_stalls(wid, lock_key, record_id):
        await _backdate_lock(wid, lock_key)
        seen["b"] = await _call(client, headers, "echo-stale", 2)
        seen["key"] = lock_key

    _stall_after_acquire(monkeypatch, _while_a_stalls)
    try:
        stalled = await _call(client, headers, "echo-stale", 1)
        b = seen["b"]
        assert "result" in b.json(), b.text
        assert invocations == ["hi"]
        # A resumed after B finished: B's mint row already exists, so A's
        # mint replays it (not fenced) and A returns B's receipt — never a
        # second dispatch. The fence-lost path is the next test.
        receipt_key = "io.agentmiddleware/receipt"
        assert (
            stalled.json()["result"]["_meta"][receipt_key]["receipt_id"]
            == b.json()["result"]["_meta"][receipt_key]["receipt_id"]
        ), stalled.text
        # B released its own lock; A's fenced release deleted nothing extra.
        assert (
            await idem.get_record(
                wallet_id=wallet_id,
                endpoint=mcp_standard._UNKEYED_RETRY_LOCK_ENDPOINT,
                idempotency_key=seen["key"],
            )
        ) is None
    finally:
        get_service_registry().unregister_local("echo-stale")


@pytest.mark.anyio
async def test_stale_takeover_via_created_at_keeps_new_holders_lock(
    client, standard_mcp_enabled, clean_database, monkeypatch
):
    """A stalled holder reclaimed via the real stale path neither mints nor
    dispatches, and its release leaves the new holder's lock row intact."""
    from app.routers import mcp_standard
    from app.services.idempotency import get_idempotency_service

    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    headers = {**provisioned["agent_headers"], **MCP_HEADERS}
    invocations: list[str] = []
    _register_counting_tool("echo-takeover", invocations)
    idem = get_idempotency_service()
    taken: dict[str, str] = {}
    real_acquire = mcp_standard._acquire_unkeyed_retry_lock

    async def _while_a_stalls(wid, lock_key, record_id):
        await _backdate_lock(wid, lock_key)
        # B goes through the unpatched acquire: stale created_at -> reclaim.
        taken["id"] = await real_acquire(wid, lock_key)
        taken["key"] = lock_key
        assert taken["id"] != record_id

    _stall_after_acquire(monkeypatch, _while_a_stalls)
    try:
        stalled = await _call(client, headers, "echo-takeover", 1)
        assert stalled.json()["error"]["code"] == -32005, stalled.text
        assert invocations == []
        held = await idem.get_record(
            wallet_id=wallet_id,
            endpoint=mcp_standard._UNKEYED_RETRY_LOCK_ENDPOINT,
            idempotency_key=taken["key"],
        )
        assert held is not None and held.record_id == taken["id"]

        await idem.abandon(
            wallet_id=wallet_id,
            endpoint=mcp_standard._UNKEYED_RETRY_LOCK_ENDPOINT,
            idempotency_key=taken["key"],
            expected_record_id=taken["id"],
        )
        retry = await _call(client, headers, "echo-takeover", 2)
        assert "result" in retry.json(), retry.text
        assert invocations == ["hi"]
    finally:
        get_service_registry().unregister_local("echo-takeover")


@pytest.mark.anyio
async def test_fenced_begin_writes_nothing_when_fence_row_reissued(
    client, clean_database
):
    """The mint-row insert and the lock ownership check are one transaction:
    a fence naming a lost (missing or re-issued) lock row writes no record."""
    from app.services.idempotency import (
        IdempotencyFenceLostError,
        get_idempotency_service,
    )

    wallet = (await provision_agent_wallet(client))["agent_wallet_id"]
    idem = get_idempotency_service()
    lock = await idem.begin_with_record(
        wallet_id=wallet,
        endpoint="/mcp#unkeyed-retry-lock",
        idempotency_key="ulock-x",
        request_payload={"kind": "unkeyed-retry-lock"},
    )
    for fence in (
        ("/mcp#unkeyed-retry-lock", "ulock-x", "idm-not-the-holder"),
        ("/mcp#unkeyed-retry-lock", "ulock-gone", lock.record_id),
    ):
        with pytest.raises(IdempotencyFenceLostError):
            await idem.begin_with_record(
                wallet_id=wallet,
                endpoint="/mcp#auto-permit",
                idempotency_key="smcp-x",
                request_payload={"k": 1},
                fence=fence,
            )
    assert (
        await idem.get_record(
            wallet_id=wallet, endpoint="/mcp#auto-permit", idempotency_key="smcp-x"
        )
    ) is None
    owned = await idem.begin_with_record(
        wallet_id=wallet,
        endpoint="/mcp#auto-permit",
        idempotency_key="smcp-x",
        request_payload={"k": 1},
        fence=("/mcp#unkeyed-retry-lock", "ulock-x", lock.record_id),
    )
    assert owned.replay is None


def _sqlite_db_path() -> str:
    from app.db.database import get_engine

    engine = get_engine()
    assert engine is not None
    return engine.sync_engine.url.database


def _external_takeover(lock_record_id: str) -> int:
    """Delete the holder's lock row from a separate SQLite connection, the
    way a stale-lock takeover would. Short timeout: if the holder has the
    write lock this raises ``database is locked``; it is never swallowed."""
    import sqlite3

    conn = sqlite3.connect(_sqlite_db_path(), timeout=0.2)
    try:
        cur = conn.execute(
            "DELETE FROM idempotency_records WHERE record_id = ?",
            (lock_record_id,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


async def _fresh_lock(idem, wallet):
    lock_key = f"ulock-{uuid4()}"
    lock = await idem.begin_with_record(
        wallet_id=wallet,
        endpoint="/mcp#unkeyed-retry-lock",
        idempotency_key=lock_key,
        request_payload={"kind": "unkeyed-retry-lock"},
    )
    return lock_key, lock


@pytest.mark.anyio
@pytest.mark.skipif(not running_on_sqlite(), reason="SQLite lock semantics")
async def test_fence_claim_blocks_takeover_until_mint_commits(client, clean_database):
    """Codex blocker 1 (deterministic): a takeover forced after the holder's
    fence step and before its mint insert must lose, and the loser writes
    nothing. Under the old SELECT ... FOR UPDATE (ignored by SQLite) the
    takeover deletes the lock row and the mint still commits: both win."""
    import sqlite3

    from sqlalchemy import event

    from app.db.database import get_engine
    from app.services.idempotency import get_idempotency_service

    wallet = (await provision_agent_wallet(client))["agent_wallet_id"]
    idem = get_idempotency_service()
    lock_key, lock = await _fresh_lock(idem, wallet)
    mint_key = f"smcp-{uuid4()}"
    takeover: dict = {}

    def _before_insert(conn, cursor, statement, parameters, context, many):
        if (
            statement.lstrip().upper().startswith("INSERT INTO IDEMPOTENCY_RECORDS")
            and mint_key in str(parameters)
            and not takeover
        ):
            try:
                takeover["deleted"] = _external_takeover(lock.record_id)
            except sqlite3.OperationalError as exc:
                takeover["error"] = str(exc)

    sync_engine = get_engine().sync_engine
    event.listen(sync_engine, "before_cursor_execute", _before_insert)
    try:
        minted = await idem.begin_with_record(
            wallet_id=wallet,
            endpoint="/mcp#auto-permit",
            idempotency_key=mint_key,
            request_payload={"k": 1},
            fence=("/mcp#unkeyed-retry-lock", lock_key, lock.record_id),
        )
    finally:
        event.remove(sync_engine, "before_cursor_execute", _before_insert)
    assert minted.replay is None
    # The takeover ran and lost: it hit the holder's write lock, deleted
    # nothing, and the holder's lock row survives.
    assert takeover == {"error": "database is locked"}, takeover
    held = await idem.get_record(
        wallet_id=wallet,
        endpoint="/mcp#unkeyed-retry-lock",
        idempotency_key=lock_key,
    )
    assert held is not None and held.record_id == lock.record_id


@pytest.mark.anyio
@pytest.mark.skipif(not running_on_sqlite(), reason="SQLite lock semantics")
async def test_fence_lost_before_claim_writes_nothing(client, clean_database):
    """Takeover forced between the holder's read and its fence claim: the
    claim matches no row, the holder raises fence-lost and writes nothing."""
    from sqlalchemy import event

    from app.db.database import get_engine
    from app.services.idempotency import (
        IdempotencyFenceLostError,
        get_idempotency_service,
    )

    wallet = (await provision_agent_wallet(client))["agent_wallet_id"]
    idem = get_idempotency_service()
    lock_key, lock = await _fresh_lock(idem, wallet)
    mint_key = f"smcp-{uuid4()}"
    takeover: dict = {}

    def _before_claim(conn, cursor, statement, parameters, context, many):
        upper = statement.lstrip().upper()
        if (
            upper.startswith(("UPDATE IDEMPOTENCY_RECORDS", "SELECT"))
            and lock_key in str(parameters)
            and not takeover
        ):
            takeover["deleted"] = _external_takeover(lock.record_id)

    sync_engine = get_engine().sync_engine
    event.listen(sync_engine, "before_cursor_execute", _before_claim)
    try:
        with pytest.raises(IdempotencyFenceLostError):
            await idem.begin_with_record(
                wallet_id=wallet,
                endpoint="/mcp#auto-permit",
                idempotency_key=mint_key,
                request_payload={"k": 1},
                fence=("/mcp#unkeyed-retry-lock", lock_key, lock.record_id),
            )
    finally:
        event.remove(sync_engine, "before_cursor_execute", _before_claim)
    assert takeover == {"deleted": 1}
    assert (
        await idem.get_record(
            wallet_id=wallet, endpoint="/mcp#auto-permit", idempotency_key=mint_key
        )
    ) is None


@pytest.mark.anyio
@pytest.mark.skipif(not running_on_sqlite(), reason="SQLite lock semantics")
async def test_fence_claim_lock_timeout_is_retryable_in_progress(
    client, clean_database
):
    """Codex: a SQLite lock timeout on the fence claim maps to the retryable
    idempotency_in_progress, not an internal error, and writes nothing."""
    import sqlite3

    from sqlalchemy import event

    from app.db.database import get_engine
    from app.services.idempotency import (
        IdempotencyFenceLostError,
        IdempotencyInProgressError,
        get_idempotency_service,
    )

    wallet = (await provision_agent_wallet(client))["agent_wallet_id"]
    idem = get_idempotency_service()
    lock_key, lock = await _fresh_lock(idem, wallet)
    mint_key = f"smcp-{uuid4()}"
    blocker = sqlite3.connect(_sqlite_db_path(), timeout=5)
    blocker.isolation_level = None

    def _short_busy(conn, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("UPDATE IDEMPOTENCY_RECORDS"):
            cursor.execute("PRAGMA busy_timeout=100")

    engine = get_engine()
    event.listen(engine.sync_engine, "before_cursor_execute", _short_busy)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(IdempotencyInProgressError) as caught:
            await idem.begin_with_record(
                wallet_id=wallet,
                endpoint="/mcp#auto-permit",
                idempotency_key=mint_key,
                request_payload={"k": 1},
                fence=("/mcp#unkeyed-retry-lock", lock_key, lock.record_id),
            )
        assert not isinstance(caught.value, IdempotencyFenceLostError)
        assert str(caught.value) == "idempotency_in_progress"
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
        event.remove(engine.sync_engine, "before_cursor_execute", _short_busy)
        await engine.dispose()  # drop the connection left at busy_timeout=100
    assert (
        await idem.get_record(
            wallet_id=wallet, endpoint="/mcp#auto-permit", idempotency_key=mint_key
        )
    ) is None


def test_unkeyed_retry_window_rejects_negative_and_keeps_zero():
    """CodeRabbit: negative windows are invalid; 0 stays the opt-out."""
    from pydantic import ValidationError

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(MCP_UNKEYED_RETRY_WINDOW_SECONDS=-1)
    assert (
        Settings(MCP_UNKEYED_RETRY_WINDOW_SECONDS=0).MCP_UNKEYED_RETRY_WINDOW_SECONDS
        == 0
    )
    assert Settings().MCP_UNKEYED_RETRY_WINDOW_SECONDS == 300


def test_unregistered_tool_replay_runs_iga_check():
    """Codex should-fix: the replay served after a tool is removed must run
    the same IGA gate as the registered-tool replay path.

    #232 moved durable replay for unregistered tools into the early
    ``(permit_id or not service)`` idempotency begin; #233's IGA gate must
    still run there before ``_raise_replayed_error``.
    """
    import inspect

    from app.routers import mcp

    src = inspect.getsource(mcp)
    # The early begin that covers unregistered tools.
    marker = "and (permit_id or not service)"
    start = src.index(marker)
    # Bound the search to this begin block: stop before the later
    # permit-outside / Tool-not-found fallthrough.
    end = src.index(
        "if governed_call and permit_id and action_identity is None:", start
    )
    branch = src[start:end]
    assert "_assert_replay_iga_access(auth=auth, tool_name=tool_name)" in branch
    assert branch.index("_assert_replay_iga_access") < branch.index(
        "_raise_replayed_error"
    )


def test_default_unkeyed_retry_window_is_permit_ttl_and_documented():
    """Codex blocker 2: with defaults the window is the 120 s permit TTL,
    not the 300 s setting, and the docs say so (a later retry bills again)."""
    from pathlib import Path

    from app.core.config import Settings, get_settings
    from app.routers import mcp_standard

    defaults = Settings()
    settings = get_settings()
    assert defaults.MCP_UNKEYED_RETRY_WINDOW_SECONDS == 300
    assert defaults.STANDARD_MCP_PERMIT_TTL_SECONDS == 120
    assert settings.MCP_UNKEYED_RETRY_WINDOW_SECONDS == 300
    assert mcp_standard._effective_unkeyed_retry_window() == min(
        300, settings.STANDARD_MCP_PERMIT_TTL_SECONDS
    )
    docs = Path(__file__).resolve().parents[1] / "docs"
    for name in ("concepts/request-keys.md", "failure-semantics.md"):
        text = " ".join((docs / name).read_text().split())
        assert "STANDARD_MCP_PERMIT_TTL_SECONDS" in text or "auto-permit TTL" in text
        assert "120" in text
        assert "bills again" in text


def test_effective_unkeyed_retry_window_capped_at_permit_ttl(monkeypatch):
    """Bugbot: a joined slot must never outlive the permit it reuses."""
    from app.core.config import get_settings
    from app.routers import mcp_standard

    settings = get_settings()
    monkeypatch.setattr(settings, "STANDARD_MCP_PERMIT_TTL_SECONDS", 120)
    monkeypatch.setattr(settings, "MCP_UNKEYED_RETRY_WINDOW_SECONDS", 300)
    assert mcp_standard._effective_unkeyed_retry_window() == 120
    monkeypatch.setattr(settings, "MCP_UNKEYED_RETRY_WINDOW_SECONDS", 60)
    assert mcp_standard._effective_unkeyed_retry_window() == 60
    monkeypatch.setattr(settings, "MCP_UNKEYED_RETRY_WINDOW_SECONDS", 0)
    assert mcp_standard._effective_unkeyed_retry_window() == 0


@pytest.mark.anyio
async def test_unkeyed_mint_failure_releases_row_for_retry(
    client, standard_mcp_enabled, clean_database, monkeypatch
):
    """CodeRabbit: a non-PermitError mint failure must not strand the mint row."""
    from app.services import permits as permits_module

    provisioned = await provision_agent_wallet(client)
    headers = {**provisioned["agent_headers"], **MCP_HEADERS}
    invocations: list[str] = []
    _register_counting_tool("echo-mint-fail", invocations)
    service = permits_module.get_permit_service()
    real_create = service.create_permit
    calls = {"n": 0}

    async def _flaky_create(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient permit store failure")
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(service, "create_permit", _flaky_create)
    try:
        # The failure surfaces either as a raised error or an error
        # response depending on the transport; either way nothing dispatched.
        raised: BaseException | None = None
        try:
            first = await _call(client, headers, "echo-mint-fail", 1)
            assert "result" not in first.json() or first.json()["result"].get(
                "isError"
            ), first.text
        except BaseException as exc:  # noqa: BLE001 - transport-dependent
            raised = exc
        if raised is not None:
            assert "transient permit store failure" in repr(raised)
        assert invocations == []
        retry = await _call(client, headers, "echo-mint-fail", 2)
        assert retry.status_code == 200, retry.text
        assert "result" in retry.json(), retry.text
        assert invocations == ["hi"]
    finally:
        get_service_registry().unregister_local("echo-mint-fail")


def test_effective_unkeyed_retry_window_capped_at_demo_permit_ttl(monkeypatch):
    """Bugbot: demo auto permits are shorter-lived; the window must follow."""
    from app.core.config import get_settings
    from app.routers import mcp_standard

    settings = get_settings()
    monkeypatch.setattr(settings, "STANDARD_MCP_PERMIT_TTL_SECONDS", 120)
    monkeypatch.setattr(settings, "MCP_UNKEYED_RETRY_WINDOW_SECONDS", 300)
    monkeypatch.setattr(settings, "DEMO_AUTO_PERMIT_TTL_SECONDS", 45)
    monkeypatch.setattr(settings, "DEMO_MAX_PERMIT_TTL_MINUTES", None)
    assert mcp_standard._effective_unkeyed_retry_window(tenant="demo") == 45
    assert mcp_standard._effective_unkeyed_retry_window(tenant="acme") == 120
    monkeypatch.setattr(settings, "DEMO_AUTO_PERMIT_TTL_SECONDS", 200)
    monkeypatch.setattr(settings, "DEMO_MAX_PERMIT_TTL_MINUTES", 1)
    assert mcp_standard._effective_unkeyed_retry_window(tenant="demo") == 60
    # The window always equals the TTL the mint itself uses.
    assert mcp_standard._effective_unkeyed_retry_window(
        tenant="demo"
    ) == mcp_standard._auto_permit_ttl_seconds(
        tenant="demo", requires_human_approval=False
    )


@pytest.mark.anyio
async def test_tools_call_explicit_permit_out_of_scope_is_refused_without_charge(
    client, standard_mcp_enabled, clean_database
):
    """An explicit permit that does not name the tool is a permit refusal.

    The caller gets -32003 with the stable reason code as the message and a
    signed denial receipt; the tool never runs and nothing is charged.
    """
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    calls: list[str] = []
    registry = get_service_registry()
    registry.register_local(
        service_id="standard-oos-target",
        name="Standard OOS Target",
        description="Standard MCP out-of-scope permit test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=lambda message="ok": calls.append(message) or {"message": message},
        credits_per_unit=2.0,
        unit_name="call",
    )
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=wallet_id,
            key_id=provisioned["key_id"],
            tool_name="standard-oos-other",
            idem_key="standard-oos-permit-1",
        )
        ledger_before = (
            await client.get(
                f"/v1/billing/ledger/{wallet_id}", headers=provisioned["agent_headers"]
            )
        ).json()["entries"]
        call = _rpc(
            "tools/call",
            params={
                "name": "standard-oos-target",
                "arguments": {"message": "hi"},
                "_meta": {"io.agentmiddleware/permit_id": permit["permit_id"]},
            },
        )
        headers = {
            **provisioned["agent_headers"],
            **MCP_HEADERS,
            "Idempotency-Key": "standard-oos-call-1",
        }
        resp = await client.post("/mcp", json=call, headers=headers)
        assert resp.status_code == 200
        error = resp.json()["error"]
        assert error["code"] == -32003
        assert error["message"] == "permit_tool_not_allowed"
        receipt = error["data"]["receipt"]
        bundle_resp = await client.get(
            f"/v1/receipts/{receipt['receipt_id']}/portable",
            headers=provisioned["agent_headers"],
        )
        keys_resp = await client.get("/.well-known/trust-keys.json")
        assert bundle_resp.status_code == 200, bundle_resp.text
        assert keys_resp.status_code == 200, keys_resp.text
        verified = verify_bundle(
            bundle_resp.json(), key_set_from_document(keys_resp.json())
        )
        assert verified.ok, verified.reason
        assert verified.receipt_id == receipt["receipt_id"]
        assert verified.claims["outcome"] == receipt["outcome"] == "denied"
        assert (
            verified.claims["reason_code"]
            == receipt["reason_code"]
            == "permit_tool_not_allowed"
        )
        assert (
            verified.claims["permit_id"] == receipt["permit_id"] == permit["permit_id"]
        )
        assert receipt["ledger_entry_id"] is None
        assert receipt["dispatch_attempt_id"] is None

        assert calls == []
        ledger_after = (
            await client.get(
                f"/v1/billing/ledger/{wallet_id}", headers=provisioned["agent_headers"]
            )
        ).json()["entries"]
        # No debit, hold, or any other ledger movement for the refused call.
        assert ledger_after == ledger_before
    finally:
        registry.unregister_local("standard-oos-target")
