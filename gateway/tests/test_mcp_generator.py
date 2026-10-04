"""
Tests for MCP Server Generator
===============================

Tests for:
- Service registry registration and discovery
- MCP manifest generation
- Standalone server generation
- Tool invocation
"""

import asyncio
import pytest
from decimal import Decimal

from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.service_registry import (
    ServiceRegistry,
    get_service_registry,
    pydantic_to_mcp_schema,
    extract_schema_from_callable,
)
from app.services.mcp_generator import McpGenerator, get_mcp_generator
from app.schemas.billing import ServiceCategory
from pydantic import BaseModel


@pytest.mark.parametrize(
    "schema", [None, {}, {"type": "object", "properties": {"id": {"type": "integer"}}}]
)
def test_manifest_normalizes_only_missing_or_null_input_schema(schema):
    generator = McpGenerator()
    service = {"service_id": "schema-fixture", "input_schema": schema}
    tool = generator._service_to_mcp_tool(service)
    assert tool["inputSchema"] == (
        {"type": "object", "properties": {}} if schema is None else schema
    )
    service.pop("input_schema")
    assert generator._service_to_mcp_tool(service)["inputSchema"] == {
        "type": "object",
        "properties": {},
    }


class TestSchemaExtraction:
    """Test Pydantic to MCP schema conversion."""

    def test_simple_model(self):
        class InputModel(BaseModel):
            name: str
            count: int
            enabled: bool = False

        schema = pydantic_to_mcp_schema(InputModel)

        assert schema["type"] == "object"
        assert "name" in schema["properties"]
        assert "count" in schema["properties"]
        assert "enabled" in schema["properties"]
        assert "required" in schema
        assert "name" in schema["required"]
        assert "count" in schema["required"]
        assert "enabled" not in schema["required"]

    def test_nested_model(self):
        class Inner(BaseModel):
            value: str

        class Outer(BaseModel):
            inner: Inner
            count: int

        schema = pydantic_to_mcp_schema(Outer)

        assert schema["type"] == "object"
        assert "inner" in schema["properties"]
        assert "count" in schema["properties"]
        assert "inner" in schema["required"]
        assert schema["properties"]["inner"]["$ref"] == "#/$defs/Inner"
        assert "Inner" in schema["$defs"]

    def test_none_model(self):
        assert pydantic_to_mcp_schema(None) is None

    def test_extract_from_callable_sync(self):
        def simple_func(name: str, count: int) -> dict:
            return {"name": name, "count": count}

        input_schema, output_schema = extract_schema_from_callable(simple_func)

        assert input_schema is not None
        assert input_schema["type"] == "object"
        assert "name" in input_schema["properties"]
        assert "count" in input_schema["properties"]
        assert "name" in input_schema["required"]
        assert "count" in input_schema["required"]
        assert input_schema["properties"]["count"] == {"type": "integer"}
        assert output_schema == {"type": "object"}

    def test_extract_from_callable_async(self):
        async def async_func(url: str, style: str = "default") -> str:
            return f"generated: {url}"

        input_schema, output_schema = extract_schema_from_callable(async_func)

        assert input_schema is not None
        assert "url" in input_schema["required"]
        assert "style" not in input_schema["required"]
        assert output_schema == {"type": "string"}

    def test_extract_typed_container_output(self):
        def typed_func() -> dict[str, list[int]]:
            return {"values": [1]}

        _, output_schema = extract_schema_from_callable(typed_func)

        assert output_schema == {
            "type": "object",
            "additionalProperties": {
                "type": "array",
                "items": {"type": "integer"},
            },
        }


class TestServiceRegistry:
    """Test service registry operations."""

    @pytest.fixture
    def registry(self):
        return ServiceRegistry()

    def test_register_local_service(self, registry):
        async def dummy_service(url: str, style: str = "default") -> dict:
            return {"result": "ok"}

        result = registry.register_local(
            service_id="test-service",
            name="Test Service",
            description="A test service",
            category=ServiceCategory.CONTENT_FACTORY,
            func=dummy_service,
            credits_per_unit=10.0,
            unit_name="call",
        )

        assert result["service_id"] == "test-service"
        assert result["name"] == "Test Service"
        assert result["credits_per_unit"] == 10.0
        assert result["is_local"] is True
        assert result["input_schema"] is not None

    def test_get_local_service(self, registry):
        async def dummy_service(name: str) -> str:
            return name

        registry.register_local(
            service_id="get-test",
            name="Get Test",
            description="Test get",
            category=ServiceCategory.AGENT_COMMS,
            func=dummy_service,
        )

        service = registry.get_local("get-test")
        assert service is not None
        assert service["service_id"] == "get-test"
        assert service["name"] == "Get Test"

    def test_get_local_func(self, registry):
        async def my_service(x: int) -> int:
            return x * 2

        registry.register_local(
            service_id="func-test",
            name="Func Test",
            description="Test func",
            category=ServiceCategory.IOT_BRIDGE,
            func=my_service,
        )

        func = registry.get_local_func("func-test")
        assert func is my_service

    def test_get_nonexistent_service(self, registry):
        assert registry.get_local("nonexistent") is None
        assert registry.get_local_func("nonexistent") is None

    def test_unregister_local(self, registry):
        async def service1():
            pass

        registry.register_local(
            service_id="unreg-test",
            name="Unreg Test",
            description="Test unreg",
            category=ServiceCategory.PLATFORM_FEE,
            func=service1,
        )

        assert registry.unregister_local("unreg-test") is True
        assert registry.get_local("unreg-test") is None
        assert registry.unregister_local("nonexistent") is False

    def test_list_local_services(self, registry):
        async def service_a():
            pass

        async def service_b():
            pass

        registry.register_local(
            service_id="list-a",
            name="List A",
            description="A",
            category=ServiceCategory.IOT_BRIDGE,
            func=service_a,
        )
        registry.register_local(
            service_id="list-b",
            name="List B",
            description="B",
            category=ServiceCategory.TELEMETRY_PM,
            func=service_b,
        )

        services = asyncio.run(registry.list_local())
        assert len(services) == 2
        assert all(s["is_local"] for s in services)


class TestMcpGenerator:
    """Test MCP manifest generation."""

    @pytest.fixture
    def generator(self):
        registry = ServiceRegistry()
        return McpGenerator(registry), registry

    def test_generate_tools_json_empty(self, generator):
        gen, registry = generator
        manifest = gen.generate_tools_json()

        assert manifest["version"] == "1.0"
        assert manifest["positioning"]["schema_version"] == "1.0"
        assert manifest["positioning"]["id"] == (
            "transaction_integrity_for_consequential_autonomous_actions"
        )
        assert (
            manifest["name"]
            == manifest["positioning"]["legacy_protocol_identifiers"]["mcp_server_name"]
        )
        assert "tools" in manifest
        assert len(manifest["tools"]) == 0

    def test_generate_tools_json_with_services(self, generator):
        gen, registry = generator

        async def video_service(url: str, style: str = "cinematic") -> dict:
            return {"video_url": f"{url}.mp4"}

        async def data_service(query: str) -> list:
            return [query]

        registry.register_local(
            service_id="video-generator",
            name="Video Generator",
            description="Generate videos from URLs",
            category=ServiceCategory.CONTENT_FACTORY,
            func=video_service,
            credits_per_unit=50.0,
            unit_name="video",
        )

        registry.register_local(
            service_id="data-indexer",
            name="Data Indexer",
            description="Index data for search",
            category=ServiceCategory.ORACLE,
            func=data_service,
            credits_per_unit=5.0,
            unit_name="query",
        )

        manifest = gen.generate_tools_json()

        assert len(manifest["tools"]) == 2

        video_tool = next(
            t for t in manifest["tools"] if t["name"] == "video-generator"
        )
        assert video_tool["description"] == "Generate videos from URLs"
        assert video_tool["annotations"]["creditsPerCall"] == 50.0
        assert video_tool["annotations"]["unitName"] == "video"

        data_tool = next(t for t in manifest["tools"] if t["name"] == "data-indexer")
        assert data_tool["annotations"]["creditsPerCall"] == 5.0

    def test_generate_tools_json_category_filter(self, generator):
        gen, registry = generator

        async def service1():
            pass

        registry.register_local(
            service_id="iot-service",
            name="IoT Service",
            description="IoT",
            category=ServiceCategory.IOT_BRIDGE,
            func=service1,
        )

        registry.register_local(
            service_id="media-service",
            name="Media Service",
            description="Media",
            category=ServiceCategory.MEDIA_ENGINE,
            func=service1,
        )

        manifest = gen.generate_tools_json(category=ServiceCategory.IOT_BRIDGE)

        assert len(manifest["tools"]) == 1
        assert manifest["tools"][0]["name"] == "iot-service"

    def test_service_to_mcp_tool(self, generator):
        gen, registry = generator

        service = {
            "service_id": "test-tool",
            "name": "Test Tool",
            "description": "A test tool",
            "category": "content_factory",
            "credits_per_unit": 25.0,
            "unit_name": "call",
            "input_schema": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
            "output_schema": {
                "type": "object",
                "properties": {"result": {"type": "string"}},
                "required": ["result"],
            },
            "owner_wallet_id": "wallet-123",
        }

        tool = gen._service_to_mcp_tool(service)

        assert tool["name"] == "test-tool"
        assert tool["description"] == "A test tool"
        assert tool["inputSchema"]["properties"]["url"]
        assert "outputSchema" not in tool
        assert tool["annotations"]["hasOutputSchema"] is True
        assert tool["annotations"]["creditsPerCall"] == 25.0
        assert tool["annotations"]["providerWallet"] == "wallet-123"

    @pytest.mark.parametrize("existing", [False, True])
    def test_generate_standalone_server_refuses_without_effects(
        self, generator, tmp_path, existing
    ):
        gen, registry = generator

        async def service1(url: str):
            return {"url": url}

        registry.register_local(
            service_id="partner.search",
            name="Partner Search",
            description="Synthetic legacy service",
            category=ServiceCategory.CONTENT_FACTORY,
            func=service1,
        )
        output = tmp_path / "server.py"
        if existing:
            output.write_text("keep existing file")
        with pytest.raises(RuntimeError, match="retired.*governed.*POST /mcp/messages"):
            gen.generate_standalone_server(
                output_path=str(output), title="Test MCP Server", transport="sse"
            )
        assert (
            output.read_text() == "keep existing file"
            if existing
            else not output.exists()
        )


class TestMcpGeneratorAsync:
    """Test async MCP generator methods."""

    @pytest.fixture
    def generator(self):
        registry = ServiceRegistry()
        return McpGenerator(registry)

    @pytest.mark.asyncio
    async def test_generate_tools_json_async(self, generator):
        manifest = await generator.generate_tools_json_async()
        assert "tools" in manifest
        assert "generated_at" in manifest

    @pytest.mark.asyncio
    async def test_executable_discovery_omits_metadata_only_persistent_services(
        self, generator, monkeypatch
    ):
        async def fail_if_loaded(*, category=None):
            raise AssertionError("metadata-only registry should not be queried")

        monkeypatch.setattr(generator.registry, "list_persistent", fail_if_loaded)

        manifest = await generator.generate_tools_json_async()

        assert manifest["tools"] == []


class TestGlobalSingletons:
    """Test global singleton getters."""

    def test_get_service_registry(self):
        registry = get_service_registry()
        assert registry is not None
        assert isinstance(registry, ServiceRegistry)

    def test_get_mcp_generator(self):
        generator = get_mcp_generator()
        assert generator is not None
        assert isinstance(generator, McpGenerator)


async def _create_funded_agent_wallet(client: AsyncClient, agent_id: str) -> str:
    headers = {"X-API-Key": "test-key"}
    sponsor_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": f"{agent_id} sponsor",
            "email": f"{agent_id}@example.com",
            "initial_credits": 10000,
            "require_kyc": False,
        },
        headers=headers,
    )
    assert sponsor_resp.status_code == 201
    sponsor_wallet_id = sponsor_resp.json()["wallet_id"]

    agent_resp = await client.post(
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": sponsor_wallet_id,
            "agent_id": agent_id,
            "budget_credits": 1000,
            "daily_limit": 250,
        },
        headers=headers,
    )
    assert agent_resp.status_code == 201
    return agent_resp.json()["wallet_id"]


class TestMcpInvokeRoute:
    """Test the HTTP MCP invoke route."""

    @pytest.mark.anyio
    async def test_invoke_tool_accepts_api_key_header(self, clean_database):
        registry = get_service_registry()

        def echo_tool(value: str = "ok") -> dict:
            return {"value": value}

        registry.register_local(
            service_id="header-auth-echo",
            name="Header Auth Echo",
            description="Echo for auth route testing",
            category=ServiceCategory.AGENT_COMMS,
            func=echo_tool,
            credits_per_unit=2.0,
            unit_name="call",
        )

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                wallet_id = await _create_funded_agent_wallet(
                    client,
                    "header-auth-agent",
                )
                response = await client.post(
                    "/mcp/tools/header-auth-echo/invoke",
                    json={
                        "name": "header-auth-echo",
                        "arguments": {"value": "hello"},
                        "mcp_context": {"wallet_id": wallet_id},
                    },
                    headers={"X-API-Key": "test-key"},
                )

            assert response.status_code == 200
            assert response.json()["isError"] is False
        finally:
            registry.unregister_local("header-auth-echo")

    @pytest.mark.anyio
    async def test_messages_tools_call_requires_api_key_header(self):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/mcp/messages",
                json={
                    "jsonrpc": "2.0",
                    "id": "call-1",
                    "method": "tools/call",
                    "params": {
                        "name": "anything",
                        "arguments": {},
                        "mcpContext": {"wallet_id": "wallet-test"},
                    },
                },
            )

        assert response.status_code == 401

    @pytest.mark.anyio
    async def test_messages_tools_call_maps_missing_wallet_to_invalid_params(self):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/mcp/messages",
                json={
                    "jsonrpc": "2.0",
                    "id": "missing-wallet-1",
                    "method": "tools/call",
                    "params": {
                        "name": "anything",
                        "arguments": {},
                        "mcpContext": {},
                    },
                },
                headers={"X-API-Key": "test-key"},
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["error"]["code"] == -32602
        assert payload["error"]["message"] == "Missing wallet_id in mcpContext"

    @pytest.mark.anyio
    async def test_messages_tools_call_maps_tool_not_found(self, clean_database):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            wallet_id = await _create_funded_agent_wallet(
                client,
                "missing-tool-agent",
            )
            response = await client.post(
                "/mcp/messages",
                json={
                    "jsonrpc": "2.0",
                    "id": "missing-tool-1",
                    "method": "tools/call",
                    "params": {
                        "name": "missing-jsonrpc-tool",
                        "arguments": {},
                        "mcpContext": {"wallet_id": wallet_id},
                    },
                },
                headers={"X-API-Key": "test-key"},
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["error"]["code"] == -32001
        assert payload["error"]["message"] == "Tool not found: missing-jsonrpc-tool"

    @pytest.mark.anyio
    async def test_messages_tools_list_requires_api_key_header(self):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            unauthenticated_resp = await client.post(
                "/mcp/messages",
                json={
                    "jsonrpc": "2.0",
                    "id": "list-unauth",
                    "method": "tools/list",
                    "params": {},
                },
            )
            authenticated_resp = await client.post(
                "/mcp/messages",
                json={
                    "jsonrpc": "2.0",
                    "id": "list-auth",
                    "method": "tools/list",
                    "params": {},
                },
                headers={"X-API-Key": "test-key"},
            )

        assert unauthenticated_resp.status_code == 401
        assert authenticated_resp.status_code == 200
        payload = authenticated_resp.json()
        assert payload["id"] == "list-auth"
        assert "tools" in payload["result"]

    @pytest.mark.anyio
    async def test_messages_tools_call_rejects_cross_wallet_db_key(
        self, clean_database
    ):
        registry = get_service_registry()

        def echo_tool(value: str = "ok") -> dict:
            return {"value": value}

        registry.register_local(
            service_id="cross-wallet-echo",
            name="Cross Wallet Echo",
            description="Echo for cross-wallet auth testing",
            category=ServiceCategory.AGENT_COMMS,
            func=echo_tool,
            credits_per_unit=1.0,
            unit_name="call",
        )

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                owned_wallet_id = await _create_funded_agent_wallet(
                    client,
                    "owned-runtime-agent",
                )
                other_wallet_id = await _create_funded_agent_wallet(
                    client,
                    "other-runtime-agent",
                )
                key_resp = await client.post(
                    "/v1/api-keys",
                    json={
                        "wallet_id": owned_wallet_id,
                        "key_name": "runtime",
                        "expires_in_days": 30,
                    },
                    headers={"X-API-Key": "test-key"},
                )
                assert key_resp.status_code == 201

                response = await client.post(
                    "/mcp/messages",
                    json={
                        "jsonrpc": "2.0",
                        "id": "call-1",
                        "method": "tools/call",
                        "params": {
                            "name": "cross-wallet-echo",
                            "arguments": {"value": "hello"},
                            "mcpContext": {"wallet_id": other_wallet_id},
                        },
                    },
                    headers={"X-API-Key": key_resp.json()["api_key"]},
                )
                audit_resp = await client.get(
                    (
                        f"/v1/audit/events?wallet_id={other_wallet_id}"
                        "&tool=cross-wallet-echo"
                    ),
                    headers={"X-API-Key": "test-key"},
                )

            assert response.status_code == 200
            payload = response.json()
            assert payload["error"]["code"] == -32003
            assert "wallet_access_denied" in payload["error"]["message"]
            assert audit_resp.status_code == 200
            audit_events = audit_resp.json()["events"]
            assert len(audit_events) == 1
            assert audit_events[0]["ok"] is False
            assert audit_events[0]["error"] == "wallet_access_denied"
            assert audit_events[0]["policy_decision_id"].startswith("pol-")
        finally:
            registry.unregister_local("cross-wallet-echo")

    @pytest.mark.anyio
    async def test_messages_tools_call_charges_wallet_and_records_audit(
        self, clean_database
    ):
        registry = get_service_registry()

        def paid_echo(value: str = "ok") -> dict:
            return {"value": value}

        registry.register_local(
            service_id="jsonrpc-paid-echo",
            name="JSON-RPC Paid Echo",
            description="Echo for JSON-RPC billing and audit testing",
            category=ServiceCategory.AGENT_COMMS,
            func=paid_echo,
            credits_per_unit=2.0,
            unit_name="call",
        )

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                wallet_id = await _create_funded_agent_wallet(
                    client,
                    "jsonrpc-paid-agent",
                )
                response = await client.post(
                    "/mcp/messages",
                    json={
                        "jsonrpc": "2.0",
                        "id": "paid-call-1",
                        "method": "tools/call",
                        "params": {
                            "name": "jsonrpc-paid-echo",
                            "arguments": {"value": "hello"},
                            "mcpContext": {"wallet_id": wallet_id},
                        },
                    },
                    headers={"X-API-Key": "test-key"},
                )
                assert response.status_code == 200
                assert response.json()["result"]["isError"] is False

                ledger_resp = await client.get(
                    f"/v1/billing/ledger/{wallet_id}",
                    headers={"X-API-Key": "test-key"},
                )
                assert ledger_resp.status_code == 200
                paid_entries = [
                    entry
                    for entry in ledger_resp.json()["entries"]
                    if "jsonrpc-paid-echo" in entry.get("description", "")
                ]
                assert len(paid_entries) == 1
                debit_amount = Decimal(
                    paid_entries[0].get("amount_exact")
                    or str(paid_entries[0]["amount"])
                )
                assert debit_amount == Decimal("-2.0")

                audit_resp = await client.get(
                    f"/v1/audit/events?wallet_id={wallet_id}&tool=jsonrpc-paid-echo",
                    headers={"X-API-Key": "test-key"},
                )
                assert audit_resp.status_code == 200
                audit_events = audit_resp.json()["events"]
                assert len(audit_events) == 1
                assert audit_events[0]["metadata"]["transport"] == "jsonrpc"
        finally:
            registry.unregister_local("jsonrpc-paid-echo")

    @pytest.mark.anyio
    async def test_messages_tools_call_does_not_charge_when_tool_fails(
        self, clean_database
    ):
        registry = get_service_registry()

        def failing_tool() -> dict:
            raise RuntimeError("boom")

        registry.register_local(
            service_id="jsonrpc-failing-echo",
            name="JSON-RPC Failing Echo",
            description="Failing tool for JSON-RPC billing testing",
            category=ServiceCategory.AGENT_COMMS,
            func=failing_tool,
            credits_per_unit=2.0,
            unit_name="call",
        )

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                wallet_id = await _create_funded_agent_wallet(
                    client,
                    "jsonrpc-failing-agent",
                )
                response = await client.post(
                    "/mcp/messages",
                    json={
                        "jsonrpc": "2.0",
                        "id": "failing-call-1",
                        "method": "tools/call",
                        "params": {
                            "name": "jsonrpc-failing-echo",
                            "arguments": {},
                            "mcpContext": {"wallet_id": wallet_id},
                        },
                    },
                    headers={"X-API-Key": "test-key"},
                )
                ledger_resp = await client.get(
                    f"/v1/billing/ledger/{wallet_id}",
                    headers={"X-API-Key": "test-key"},
                )

            assert response.status_code == 200
            payload = response.json()
            assert payload["error"]["code"] == -32603
            assert "boom" in payload["error"]["message"]
            assert ledger_resp.status_code == 200
            tool_entries = [
                entry
                for entry in ledger_resp.json()["entries"]
                if "jsonrpc-failing-echo" in entry.get("description", "")
            ]
            assert len(tool_entries) == 2
            debit_entries = [
                entry for entry in tool_entries if entry["action"] == "debit"
            ]
            refund_entries = [
                entry for entry in tool_entries if entry["action"] == "refund"
            ]
            assert len(debit_entries) == 1
            assert len(refund_entries) == 1
            net_amount = sum(
                Decimal(entry.get("amount_exact") or str(entry["amount"]))
                for entry in tool_entries
            )
            assert net_amount == Decimal("0.0")
        finally:
            registry.unregister_local("jsonrpc-failing-echo")

    @pytest.mark.anyio
    async def test_messages_tools_call_tool_value_error_is_execution_failure(
        self, clean_database
    ):
        registry = get_service_registry()

        def failing_tool() -> dict:
            raise ValueError("insufficient_funds")

        registry.register_local(
            service_id="jsonrpc-value-error-echo",
            name="JSON-RPC ValueError Echo",
            description="Tool ValueError billing classification test",
            category=ServiceCategory.AGENT_COMMS,
            func=failing_tool,
            credits_per_unit=2.0,
            unit_name="call",
        )

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                wallet_id = await _create_funded_agent_wallet(
                    client,
                    "jsonrpc-value-error-agent",
                )
                response = await client.post(
                    "/mcp/messages",
                    json={
                        "jsonrpc": "2.0",
                        "id": "value-error-call-1",
                        "method": "tools/call",
                        "params": {
                            "name": "jsonrpc-value-error-echo",
                            "arguments": {},
                            "mcpContext": {"wallet_id": wallet_id},
                        },
                    },
                    headers={"X-API-Key": "test-key"},
                )
                ledger_resp = await client.get(
                    f"/v1/billing/ledger/{wallet_id}",
                    headers={"X-API-Key": "test-key"},
                )

            assert response.status_code == 200
            payload = response.json()
            assert payload["error"]["code"] == -32603
            assert "insufficient_funds" in payload["error"]["message"]
            assert ledger_resp.status_code == 200
            tool_entries = [
                entry
                for entry in ledger_resp.json()["entries"]
                if "jsonrpc-value-error-echo" in entry.get("description", "")
            ]
            debit_entries = [
                entry for entry in tool_entries if entry["action"] == "debit"
            ]
            refund_entries = [
                entry for entry in tool_entries if entry["action"] == "refund"
            ]
            assert len(debit_entries) == 1
            assert len(refund_entries) == 1
            net_amount = sum(
                Decimal(entry.get("amount_exact") or str(entry["amount"]))
                for entry in tool_entries
            )
            assert net_amount == Decimal("0.0")
        finally:
            registry.unregister_local("jsonrpc-value-error-echo")

    @pytest.mark.anyio
    async def test_http_invoke_tool_value_error_returns_tool_error(
        self, clean_database
    ):
        registry = get_service_registry()

        def failing_tool() -> dict:
            raise ValueError("insufficient_funds")

        registry.register_local(
            service_id="http-value-error-echo",
            name="HTTP ValueError Echo",
            description="HTTP tool ValueError classification test",
            category=ServiceCategory.AGENT_COMMS,
            func=failing_tool,
            credits_per_unit=2.0,
            unit_name="call",
        )

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                wallet_id = await _create_funded_agent_wallet(
                    client,
                    "http-value-error-agent",
                )
                response = await client.post(
                    "/mcp/tools/http-value-error-echo/invoke",
                    json={
                        "name": "http-value-error-echo",
                        "arguments": {},
                        "mcp_context": {"wallet_id": wallet_id},
                    },
                    headers={"X-API-Key": "test-key"},
                )

            assert response.status_code == 200
            payload = response.json()
            assert payload["isError"] is True
            assert "insufficient_funds" in payload["content"][0]["text"]
        finally:
            registry.unregister_local("http-value-error-echo")

    @pytest.mark.anyio
    async def test_messages_tools_call_reports_refund_failure(
        self, clean_database, monkeypatch
    ):
        registry = get_service_registry()

        def failing_tool() -> dict:
            raise RuntimeError("tool exploded")

        async def failing_refund(self, **_kwargs):
            raise RuntimeError("refund down")

        monkeypatch.setattr(
            "app.services.agent_money.AgentMoney.refund_charge",
            failing_refund,
        )
        registry.register_local(
            service_id="jsonrpc-refund-fails",
            name="JSON-RPC Refund Fails",
            description="Failing refund tool for JSON-RPC billing testing",
            category=ServiceCategory.AGENT_COMMS,
            func=failing_tool,
            credits_per_unit=2.0,
            unit_name="call",
        )

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                wallet_id = await _create_funded_agent_wallet(
                    client,
                    "jsonrpc-refund-fails-agent",
                )
                response = await client.post(
                    "/mcp/messages",
                    json={
                        "jsonrpc": "2.0",
                        "id": "refund-fails-call-1",
                        "method": "tools/call",
                        "params": {
                            "name": "jsonrpc-refund-fails",
                            "arguments": {},
                            "mcpContext": {"wallet_id": wallet_id},
                        },
                    },
                    headers={"X-API-Key": "test-key"},
                )
                audit_resp = await client.get(
                    (
                        f"/v1/audit/events?wallet_id={wallet_id}"
                        "&tool=jsonrpc-refund-fails"
                    ),
                    headers={"X-API-Key": "test-key"},
                )
                ledger_resp = await client.get(
                    f"/v1/billing/ledger/{wallet_id}",
                    headers={"X-API-Key": "test-key"},
                )

            assert response.status_code == 200
            payload = response.json()
            # A failed refund is an infrastructure failure, not a tool
            # outcome: the client gets the fixed internal_error and a
            # correlation id; the composed reason is kept in the audit log.
            assert payload["error"]["code"] == -32603
            assert payload["error"]["message"] == "internal_error"
            assert payload["error"]["data"]["correlation_id"]
            assert "refund down" not in response.text
            assert "tool exploded" not in response.text
            assert audit_resp.status_code == 200
            audit_events = audit_resp.json()["events"]
            assert len(audit_events) == 1
            assert audit_events[0]["ok"] is False
            assert "refund_failed:refund down" in audit_events[0]["error"]
            assert "tool_error:tool exploded" in audit_events[0]["error"]

            assert ledger_resp.status_code == 200
            tool_entries = [
                entry
                for entry in ledger_resp.json()["entries"]
                if "jsonrpc-refund-fails" in entry.get("description", "")
            ]
            debit_entries = [
                entry for entry in tool_entries if entry["action"] == "debit"
            ]
            assert len(debit_entries) == 1
            assert not [entry for entry in tool_entries if entry["action"] == "refund"]
        finally:
            registry.unregister_local("jsonrpc-refund-fails")

    @pytest.mark.anyio
    async def test_messages_tools_call_reports_refund_and_audit_failure(
        self, clean_database, monkeypatch
    ):
        registry = get_service_registry()

        def failing_tool() -> dict:
            raise RuntimeError("tool exploded")

        async def failing_refund(self, **_kwargs):
            raise RuntimeError("refund down")

        async def failing_audit(**_kwargs):
            raise RuntimeError("audit down")

        monkeypatch.setattr(
            "app.services.agent_money.AgentMoney.refund_charge",
            failing_refund,
        )
        monkeypatch.setattr("app.routers.mcp._audit_mcp_invocation", failing_audit)
        registry.register_local(
            service_id="jsonrpc-refund-audit-fails",
            name="JSON-RPC Refund Audit Fails",
            description="Failing refund and audit tool for JSON-RPC billing testing",
            category=ServiceCategory.AGENT_COMMS,
            func=failing_tool,
            credits_per_unit=2.0,
            unit_name="call",
        )

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                wallet_id = await _create_funded_agent_wallet(
                    client,
                    "jsonrpc-refund-audit-fails-agent",
                )
                response = await client.post(
                    "/mcp/messages",
                    json={
                        "jsonrpc": "2.0",
                        "id": "refund-audit-fails-call-1",
                        "method": "tools/call",
                        "params": {
                            "name": "jsonrpc-refund-audit-fails",
                            "arguments": {},
                            "mcpContext": {"wallet_id": wallet_id},
                        },
                    },
                    headers={"X-API-Key": "test-key"},
                )

            assert response.status_code == 200
            payload = response.json()
            assert payload["error"]["code"] == -32603
            assert payload["error"]["message"] == "internal_error"
            assert payload["error"]["data"]["correlation_id"]
            for leaked in ("refund down", "tool exploded", "audit down"):
                assert leaked not in response.text
        finally:
            registry.unregister_local("jsonrpc-refund-audit-fails")

    @pytest.mark.anyio
    async def test_messages_tools_call_insufficient_funds_uses_jsonrpc_error(
        self, clean_database
    ):
        registry = get_service_registry()
        called = False

        def expensive_tool() -> dict:
            nonlocal called
            called = True
            return {"value": "should not run"}

        registry.register_local(
            service_id="jsonrpc-expensive-echo",
            name="JSON-RPC Expensive Echo",
            description="Expensive tool for JSON-RPC billing testing",
            category=ServiceCategory.AGENT_COMMS,
            func=expensive_tool,
            credits_per_unit=10000.0,
            unit_name="call",
        )

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                wallet_id = await _create_funded_agent_wallet(
                    client,
                    "jsonrpc-expensive-agent",
                )
                response = await client.post(
                    "/mcp/messages",
                    json={
                        "jsonrpc": "2.0",
                        "id": "expensive-call-1",
                        "method": "tools/call",
                        "params": {
                            "name": "jsonrpc-expensive-echo",
                            "arguments": {},
                            "mcpContext": {"wallet_id": wallet_id},
                        },
                    },
                    headers={"X-API-Key": "test-key"},
                )

            assert response.status_code == 200
            payload = response.json()
            assert payload["error"]["code"] == -32004
            assert payload["error"]["message"] == "insufficient_funds"
            assert called is False
        finally:
            registry.unregister_local("jsonrpc-expensive-echo")

    @pytest.mark.anyio
    async def test_invoke_tool_requires_api_key_header(self):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/mcp/tools/anything/invoke",
                json={
                    "name": "anything",
                    "arguments": {},
                    "mcp_context": {"wallet_id": "wallet-test"},
                },
            )

        assert response.status_code == 401
