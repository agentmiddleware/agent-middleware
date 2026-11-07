"""PRD 2.0 T-02 acceptance: POST /mcp Streamable HTTP lifecycle.

Proves, end to end through the ASGI app, the sequence an MCP client runs:

    initialize -> notifications/initialized -> tools/list -> tools/call

and that a client Idempotency-Key reaches the governed call path: a retry with
the same key replays the stored receipt without a second dispatch or a second
ledger debit. The key is accepted from the HTTP header and from
``params._meta["io.agentmiddleware/idempotency_key"]``.

The legacy POST /mcp/messages endpoint stays mounted but is advertised as
deprecated; the discovery assertions below pin that.

Protocol-level edge cases (version negotiation, origin checks, unkeyed retry
windows, lock fencing) live in test_standard_mcp_endpoint.py.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.mcp_generator import MCP_SERVER_VERSION, McpGenerator
from app.services.service_registry import get_service_registry
from tests.test_standard_mcp_endpoint import (
    MCP_HEADERS,
    _initialize,
    _ledger_debits,
    _rpc,
)
from tests.test_trust_helpers import provision_agent_wallet

IDEMPOTENCY_META_KEY = "io.agentmiddleware/idempotency_key"


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


@pytest.fixture
def counted_tool():
    """Register a metered local tool that records every real dispatch."""
    service_id = f"t02-streamable-echo-{uuid4().hex[:8]}"
    dispatched: list[str] = []

    def echo(message: str = "ok") -> dict:
        dispatched.append(message)
        return {"message": message}

    registry = get_service_registry()
    registry.register_local(
        service_id=service_id,
        name="T-02 Streamable Echo",
        description="Streamable HTTP acceptance test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=echo,
        credits_per_unit=2.0,
        unit_name="call",
    )
    try:
        yield service_id, dispatched
    finally:
        registry.unregister_local(service_id)


def _tools_call(tool: str, message: str, meta: dict | None = None) -> dict:
    params: dict = {"name": tool, "arguments": {"message": message}}
    if meta is not None:
        params["_meta"] = meta
    return _rpc("tools/call", request_id=3, params=params)


@pytest.mark.anyio
async def test_streamable_http_lifecycle_with_header_idempotency_key(
    client, standard_mcp_enabled, clean_database, counted_tool
):
    tool, dispatched = counted_tool
    provisioned = await provision_agent_wallet(client)
    headers = {**provisioned["agent_headers"], **MCP_HEADERS}

    # 1. initialize
    init = await client.post("/mcp", json=_initialize(), headers=headers)
    assert init.status_code == 200, init.text
    result = init.json()["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"] == McpGenerator.MANIFEST_NAME
    assert result["serverInfo"]["version"] == MCP_SERVER_VERSION

    initialized = await client.post(
        "/mcp",
        json=_rpc("notifications/initialized", request_id=None),
        headers=headers,
    )
    assert initialized.status_code == 202

    # 2. tools/list
    listed = await client.post(
        "/mcp", json=_rpc("tools/list", request_id=2), headers=headers
    )
    assert listed.status_code == 200, listed.text
    tools = listed.json()["result"]["tools"]
    assert isinstance(tools, list)
    assert tool in {t["name"] for t in tools}

    # 3. tools/call with the key in the HTTP header only
    keyed = {**headers, "Idempotency-Key": f"t02-header-{uuid4().hex}"}
    call = _tools_call(tool, "hello")
    first = await client.post("/mcp", json=call, headers=keyed)
    assert first.status_code == 200, first.text
    first_result = first.json()["result"]
    assert first_result["isError"] is False
    receipt = first_result["receipt"]
    assert receipt["wallet_id"] == provisioned["agent_wallet_id"]
    assert first_result["_meta"]["io.agentmiddleware/receipt"] == receipt
    assert dispatched == ["hello"]
    assert len(await _ledger_debits(client, provisioned, tool)) == 1

    # Retry with the same key: same receipt, no second dispatch or debit.
    retry = await client.post("/mcp", json=call, headers=keyed)
    assert retry.status_code == 200, retry.text
    assert retry.json()["result"]["receipt"] == receipt
    assert dispatched == ["hello"]
    assert len(await _ledger_debits(client, provisioned, tool)) == 1

    # Identical arguments under a fresh header key are a new call. An unkeyed
    # retry of identical arguments also replays (see the retry window tests in
    # test_standard_mcp_endpoint.py), so this is what shows the header key, not
    # an argument fingerprint, selected the replay slot above.
    fresh = {**headers, "Idempotency-Key": f"t02-header-{uuid4().hex}"}
    second = await client.post("/mcp", json=call, headers=fresh)
    assert second.status_code == 200, second.text
    assert second.json()["result"]["receipt"]["permit_id"] != receipt["permit_id"]
    assert dispatched == ["hello", "hello"]
    assert len(await _ledger_debits(client, provisioned, tool)) == 2


@pytest.mark.anyio
async def test_meta_idempotency_key_replays_without_second_charge(
    client, standard_mcp_enabled, clean_database, counted_tool
):
    tool, dispatched = counted_tool
    provisioned = await provision_agent_wallet(client)
    headers = {**provisioned["agent_headers"], **MCP_HEADERS}
    call = _tools_call(
        tool, "meta", meta={IDEMPOTENCY_META_KEY: f"t02-meta-{uuid4().hex}"}
    )

    first = await client.post("/mcp", json=call, headers=headers)
    assert first.status_code == 200, first.text
    receipt = first.json()["result"]["receipt"]

    retry = await client.post("/mcp", json=call, headers=headers)
    assert retry.json()["result"]["receipt"] == receipt
    assert dispatched == ["meta"]
    assert len(await _ledger_debits(client, provisioned, tool)) == 1

    # A fresh _meta key with identical arguments dispatches again.
    fresh = _tools_call(
        tool, "meta", meta={IDEMPOTENCY_META_KEY: f"t02-meta-{uuid4().hex}"}
    )
    second = await client.post("/mcp", json=fresh, headers=headers)
    assert second.json()["result"]["isError"] is False
    assert dispatched == ["meta", "meta"]
    assert len(await _ledger_debits(client, provisioned, tool)) == 2


@pytest.mark.anyio
async def test_matching_header_and_meta_keys_are_one_idempotency_slot(
    client, standard_mcp_enabled, clean_database, counted_tool
):
    tool, dispatched = counted_tool
    provisioned = await provision_agent_wallet(client)
    key = f"t02-both-{uuid4().hex}"
    keyed = {**provisioned["agent_headers"], **MCP_HEADERS, "Idempotency-Key": key}

    first = await client.post(
        "/mcp",
        json=_tools_call(tool, "both", meta={IDEMPOTENCY_META_KEY: key}),
        headers=keyed,
    )
    assert first.status_code == 200, first.text
    receipt = first.json()["result"]["receipt"]

    # The same key sent through the header alone hits the same slot.
    header_only = await client.post(
        "/mcp", json=_tools_call(tool, "both"), headers=keyed
    )
    assert header_only.json()["result"]["receipt"] == receipt
    assert dispatched == ["both"]
    assert len(await _ledger_debits(client, provisioned, tool)) == 1


@pytest.mark.anyio
async def test_conflicting_header_and_meta_keys_are_refused_without_charge(
    client, standard_mcp_enabled, clean_database, counted_tool
):
    tool, dispatched = counted_tool
    provisioned = await provision_agent_wallet(client)
    keyed = {
        **provisioned["agent_headers"],
        **MCP_HEADERS,
        "Idempotency-Key": f"t02-header-{uuid4().hex}",
    }

    resp = await client.post(
        "/mcp",
        json=_tools_call(
            tool, "conflict", meta={IDEMPOTENCY_META_KEY: f"t02-meta-{uuid4().hex}"}
        ),
        headers=keyed,
    )
    body = resp.json()
    assert "error" in body, body
    assert "disagree" in body["error"]["message"]
    assert dispatched == []
    assert await _ledger_debits(client, provisioned, tool) == []


@pytest.mark.anyio
async def test_same_key_with_different_arguments_is_refused(
    client, standard_mcp_enabled, clean_database, counted_tool
):
    tool, dispatched = counted_tool
    provisioned = await provision_agent_wallet(client)
    keyed = {
        **provisioned["agent_headers"],
        **MCP_HEADERS,
        "Idempotency-Key": f"t02-reuse-{uuid4().hex}",
    }

    first = await client.post("/mcp", json=_tools_call(tool, "one"), headers=keyed)
    assert first.json()["result"]["isError"] is False

    reused = await client.post("/mcp", json=_tools_call(tool, "two"), headers=keyed)
    assert reused.json()["error"]["message"] == "idempotency_key_reused"
    assert dispatched == ["one"]
    assert len(await _ledger_debits(client, provisioned, tool)) == 1


def test_legacy_messages_endpoint_is_documented_as_deprecated():
    operation = app.openapi()["paths"]["/mcp/messages"]["post"]
    assert operation["deprecated"] is True
    assert "Deprecated" in operation["summary"]
    assert "POST /mcp" in operation["summary"]
    assert operation["description"].startswith(
        "Deprecated in favor of the standard MCP Streamable HTTP endpoint at POST /mcp."
    )


@pytest.mark.anyio
async def test_agent_json_points_agents_at_streamable_http(
    client, standard_mcp_enabled
):
    manifest = (await client.get("/.well-known/agent.json")).json()

    integrations = manifest["integrations"]
    assert integrations["preferred_integration"] == "standard_mcp_streamable_http"
    assert integrations["standard_mcp_streamable_http"] == "/mcp"
    # The legacy transport stays listed, marked deprecated.
    assert integrations["mcp_json_rpc"] == "/mcp/messages"
    assert integrations["mcp_json_rpc_status"] == "deprecated"
    assert "POST /mcp" in integrations["mcp_json_rpc_note"]

    assert manifest["endpoints"]["mcp"] == "/mcp"
    assert manifest["endpoints"]["mcp_json_rpc"] == "/mcp/messages"

    transport = manifest["agent_first"]["mcp_transport"]
    assert transport["preferred"] == "standard_mcp_streamable_http"
    assert transport["endpoint"] == "POST /mcp"
    assert transport["lifecycle"] == ["initialize", "tools/list", "tools/call"]
    assert "Idempotency-Key" in transport["idempotency_key"]
    assert IDEMPOTENCY_META_KEY in transport["idempotency_key"]
    assert transport["legacy_json_rpc_status"] == "deprecated"
