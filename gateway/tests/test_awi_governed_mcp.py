"""Always-governed AWI MCP tools must require permits even in legacy mode."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services import awi_rag_engine as awi_rag_engine_module
from app.services import mcp_phase9_tools
from app.services.awi_rag_engine import AWIRAGEngine
from app.services.mcp_phase9_tools import (
    ALWAYS_GOVERNED_AWI_TOOLS,
    MCP_PHASE9_TOOLS,
)
from app.services.service_registry import get_service_registry
from tests.test_trust_helpers import (
    create_tool_permit,
    provision_agent_wallet,
)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _reregister_phase9():
    registry = get_service_registry()
    for tool in MCP_PHASE9_TOOLS:
        registry.unregister_local(tool["service_id"])
    mcp_phase9_tools._registered = False
    mcp_phase9_tools.ensure_phase9_registered()


@pytest.fixture(autouse=True)
def _fresh_phase9_tools():
    _reregister_phase9()
    yield


def test_always_governed_tools_are_registered_with_require_permit():
    registry = get_service_registry()
    for tool_id in ALWAYS_GOVERNED_AWI_TOOLS:
        record = registry.get_local(tool_id)
        assert record is not None, tool_id
        assert record.get("require_permit") is True


@pytest.mark.anyio
async def test_manifest_annotates_require_permit(client):
    resp = await client.get("/mcp/tools.json")
    assert resp.status_code == 200
    by_name = {t["name"]: t for t in resp.json()["tools"]}
    for tool_id in ALWAYS_GOVERNED_AWI_TOOLS:
        assert by_name[tool_id]["annotations"]["requirePermit"] is True


@pytest.mark.anyio
async def test_awi_passkey_challenge_denied_without_permit_in_legacy_mode(
    client, clean_database
):
    """Even with ALLOW_LEGACY_UNPERMITTED_MCP, this tool requires a permit."""
    provisioned = await provision_agent_wallet(client)
    resp = await client.post(
        "/mcp/messages",
        json={
            "jsonrpc": "2.0",
            "id": "no-permit",
            "method": "tools/call",
            "params": {
                "name": "awi_passkey_challenge",
                "arguments": {
                    "session_id": "sess-1",
                    "action": "checkout",
                },
                "mcpContext": {
                    "wallet_id": provisioned["agent_wallet_id"],
                },
            },
        },
        headers=provisioned["agent_headers"],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["error"]["message"] == "permit_required"


@pytest.mark.anyio
async def test_awi_rag_query_succeeds_with_permit_and_receipt(client, clean_database):
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="awi_rag_query",
        max_credits=50,
        idem_key="permit-awi-rag-1",
    )
    resp = await client.post(
        "/mcp/messages",
        json={
            "jsonrpc": "2.0",
            "id": "rag-governed",
            "method": "tools/call",
            "params": {
                "name": "awi_rag_query",
                "arguments": {
                    "query": "laptops",
                    "top_k": 3,
                },
                "mcpContext": {
                    "wallet_id": provisioned["agent_wallet_id"],
                    "permit_id": permit["permit_id"],
                    "idempotency_key": "awi-rag-governed-1",
                },
            },
        },
        headers=provisioned["agent_headers"],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "error" not in body, body
    result = body["result"]
    receipt = result["receipt"]
    assert receipt["permit_id"] == permit["permit_id"]
    assert receipt["outcome"] == "success"
    assert receipt["ledger_entry_id"]


@pytest.mark.proof
@pytest.mark.anyio
async def test_awi_rag_query_never_returns_another_tenants_memory(
    client, clean_database, monkeypatch
):
    """The MCP dispatch carries no caller wallet into the tool, so the tool
    must not search the shared memory store: it points at the wallet-scoped
    HTTP route instead, like its sibling AWI wrappers."""
    engine = AWIRAGEngine(embedding_model="mock-embedding")
    monkeypatch.setattr(awi_rag_engine_module, "_rag_engine", engine)

    caller = await provision_agent_wallet(client)
    victim = await provision_agent_wallet(client)
    victim_session = await client.post(
        "/v1/awi/sessions",
        json={
            "target_url": "https://example.com",
            "wallet_id": victim["agent_wallet_id"],
        },
        headers=victim["agent_headers"],
    )
    assert victim_session.status_code == 201, victim_session.text
    victim_session_id = victim_session.json()["session_id"]
    victim_memory = await engine.index_session(
        session_id=victim_session_id,
        session_type="shopping",
        action_history=[
            {"action": "add_to_cart", "parameters": {"product": "victim-widget"}}
        ],
        state_snapshots=[],
    )
    stored = engine._memories[victim_memory]
    # A query identical to the memory's embedding text scores 1.0, so an
    # unscoped search could not miss it.
    query = engine._prepare_embedding_text(
        stored.session_type,
        stored.action_sequence,
        stored.page_summaries,
        stored.key_entities,
        stored.user_intent,
    )

    permit = await create_tool_permit(
        client,
        wallet_id=caller["agent_wallet_id"],
        key_id=caller["key_id"],
        tool_name="awi_rag_query",
        max_credits=50,
        idem_key="permit-awi-rag-isolation",
    )
    resp = await client.post(
        "/mcp/messages",
        json={
            "jsonrpc": "2.0",
            "id": "rag-isolation",
            "method": "tools/call",
            "params": {
                "name": "awi_rag_query",
                "arguments": {"query": query, "top_k": 5},
                "mcpContext": {
                    "wallet_id": caller["agent_wallet_id"],
                    "permit_id": permit["permit_id"],
                    "idempotency_key": "awi-rag-isolation-1",
                },
            },
        },
        headers=caller["agent_headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "error" not in body, body
    assert body["result"]["receipt"]["outcome"] == "success"
    assert victim_memory not in resp.text
    assert victim_session_id not in resp.text
    assert victim["agent_wallet_id"] not in resp.text
    assert "/v1/awi/rag/query" in resp.text
    assert stored.access_count == 0
