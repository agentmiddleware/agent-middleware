"""Unclassified failures on the MCP surface must not echo the exception.

The catch-all branches on ``POST /mcp/messages``, ``POST /mcp`` and
``POST /mcp/tools/{service_id}/invoke`` used to return ``str(exc)`` as the
error message; for a database driver failure that is the SQL statement
itself. Each route now answers a fixed ``internal_error`` with a correlation
id, and the real exception is logged server-side under that id.

The failure is injected at the governed adapter, below every route-level
handler, so the same fault reaches all three catch-alls. ``ValueError`` is
exercised separately because the routes classify some ``ValueError`` texts as
contract and must still refuse to echo the rest.
"""

from __future__ import annotations

import logging
import re

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import app
from app.routers import mcp as mcp_router
from app.routers import mcp_standard as standard_mcp_router
from app.schemas.billing import ServiceCategory
from app.services.service_registry import get_service_registry
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
TOOL = "leak-probe"
SECRET = "SELECT secret FROM wallets"
CORRELATION_ID = re.compile(r"^[0-9a-f]{32}$")
LOGGER = "app.routers.mcp"

UNCLASSIFIED = [
    pytest.param(RuntimeError, id="exception"),
    pytest.param(ValueError, id="value-error"),
]


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
def registered_tool():
    registry = get_service_registry()
    registry.register_local(
        service_id=TOOL,
        name="Leak Probe",
        description="Never runs; the adapter is failed before dispatch",
        category=ServiceCategory.AGENT_COMMS,
        func=lambda: {"ok": True},
        credits_per_unit=1.0,
        unit_name="call",
    )
    yield
    registry.unregister_local(TOOL)


def _break_invoke(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    """Fail the governed adapter with text the routes must never echo."""

    async def explode(_request):
        raise exc

    monkeypatch.setattr(mcp_router._mcp_adapter, "invoke", explode)


def _assert_internal_error(error: dict, *, wire: str) -> str:
    assert error["code"] == -32603
    assert error["message"] == "internal_error"
    correlation_id = error["data"]["correlation_id"]
    assert CORRELATION_ID.match(correlation_id)
    assert set(error) == {"code", "message", "data"}
    assert set(error["data"]) == {"correlation_id"}
    assert "SELECT" not in wire
    return correlation_id


def _assert_logged_under(caplog, correlation_id: str) -> None:
    """The operator finds the real exception by the id the client was given."""
    record = next(r for r in caplog.records if correlation_id in r.getMessage())
    assert record.name == LOGGER
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert SECRET in str(record.exc_info[1])


@pytest.mark.anyio
@pytest.mark.parametrize("exc_type", UNCLASSIFIED)
async def test_legacy_jsonrpc_route_hides_unclassified_failure(
    client, clean_database, monkeypatch, caplog, exc_type
):
    provisioned = await provision_agent_wallet(client)
    _break_invoke(monkeypatch, exc_type(SECRET))

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        resp = await client.post(
            "/mcp/messages",
            json={
                "jsonrpc": "2.0",
                "id": "leak-1",
                "method": "tools/call",
                "params": {
                    "name": TOOL,
                    "arguments": {},
                    "mcpContext": {
                        "wallet_id": provisioned["agent_wallet_id"],
                        "idempotency_key": "leak-jsonrpc-1",
                    },
                },
            },
            headers=provisioned["agent_headers"],
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["id"] == "leak-1"
    correlation_id = _assert_internal_error(payload["error"], wire=resp.text)
    _assert_logged_under(caplog, correlation_id)


@pytest.mark.anyio
@pytest.mark.parametrize("exc_type", UNCLASSIFIED)
async def test_standard_route_hides_unclassified_failure(
    client,
    standard_mcp_enabled,
    clean_database,
    registered_tool,
    monkeypatch,
    caplog,
    exc_type,
):
    provisioned = await provision_agent_wallet(client)
    _break_invoke(monkeypatch, exc_type(SECRET))

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        resp = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": TOOL, "arguments": {}},
            },
            headers={
                **provisioned["agent_headers"],
                **MCP_HEADERS,
                "Idempotency-Key": "leak-standard-1",
            },
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["id"] == 7
    correlation_id = _assert_internal_error(payload["error"], wire=resp.text)
    _assert_logged_under(caplog, correlation_id)


@pytest.mark.anyio
async def test_standard_route_hides_failure_before_the_governed_call(
    client,
    standard_mcp_enabled,
    clean_database,
    registered_tool,
    monkeypatch,
    caplog,
):
    """The auto-permit mint runs before the governed call and must be covered too.

    Anything that escapes the SDK's tool handler is reported by the SDK as
    ``str(exc)``; the handler is the last boundary that can sanitize it.
    """
    provisioned = await provision_agent_wallet(client)

    async def explode(**_kwargs):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(standard_mcp_router, "_mint_auto_permit", explode)

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        resp = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {"name": TOOL, "arguments": {}},
            },
            headers={
                **provisioned["agent_headers"],
                **MCP_HEADERS,
                "Idempotency-Key": "leak-standard-mint-1",
            },
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["id"] == 8
    correlation_id = _assert_internal_error(payload["error"], wire=resp.text)
    _assert_logged_under(caplog, correlation_id)


@pytest.mark.anyio
@pytest.mark.parametrize("exc_type", UNCLASSIFIED)
async def test_http_invoke_route_hides_unclassified_failure(
    client, clean_database, monkeypatch, caplog, exc_type
):
    provisioned = await provision_agent_wallet(client)
    _break_invoke(monkeypatch, exc_type(SECRET))

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        resp = await client.post(
            f"/mcp/tools/{TOOL}/invoke",
            json={
                "name": TOOL,
                "arguments": {},
                "mcp_context": {
                    "wallet_id": provisioned["agent_wallet_id"],
                    "idempotency_key": "leak-http-1",
                },
            },
            headers=provisioned["agent_headers"],
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["isError"] is True
    assert body["content"] == [{"type": "text", "text": "Error: internal_error"}]
    assert body["structuredContent"]["error"] == "internal_error"
    correlation_id = body["structuredContent"]["correlation_id"]
    assert CORRELATION_ID.match(correlation_id)
    assert "SELECT" not in resp.text
    _assert_logged_under(caplog, correlation_id)


@pytest.mark.anyio
async def test_governed_refund_failure_keeps_refund_store_text_server_side(
    client, clean_database, monkeypatch
):
    """A failed refund is a classified, receipted outcome; its message still
    must not carry the refund store's own text, only the tool's error."""
    provisioned = await provision_agent_wallet(client)
    registry = get_service_registry()

    def exploding_tool() -> dict:
        raise RuntimeError("tool exploded")

    async def failing_refund(self, **_kwargs):
        raise RuntimeError(SECRET)

    registry.register_local(
        service_id="leak-refund-probe",
        name="Leak Refund Probe",
        description="Fails, then its refund fails with SQL-shaped text",
        category=ServiceCategory.AGENT_COMMS,
        func=exploding_tool,
        credits_per_unit=2.0,
        unit_name="call",
    )
    monkeypatch.setattr(
        "app.services.agent_money.AgentMoney.refund_charge", failing_refund
    )
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name="leak-refund-probe",
            idem_key="leak-refund-permit-1",
        )
        resp = await client.post(
            "/mcp/messages",
            json={
                "jsonrpc": "2.0",
                "id": "leak-refund-1",
                "method": "tools/call",
                "params": {
                    "name": "leak-refund-probe",
                    "arguments": {},
                    "mcpContext": {
                        "wallet_id": provisioned["agent_wallet_id"],
                        "permit_id": permit["permit_id"],
                        "idempotency_key": "leak-refund-invoke-1",
                    },
                },
            },
            headers=provisioned["agent_headers"],
        )
    finally:
        registry.unregister_local("leak-refund-probe")

    assert resp.status_code == 200
    error = resp.json()["error"]
    assert error["code"] == -32603
    assert error["message"] == "refund_failed; tool_error:tool exploded"
    assert error["data"]["receipt"]["outcome"] == "failed_unrefunded"
    assert "SELECT" not in resp.text


@pytest.mark.anyio
async def test_classified_value_error_text_still_reaches_the_client(
    client, clean_database, monkeypatch
):
    """Only unknown text is replaced; the strings clients match stay on the wire.

    ``idempotency_key_reused`` is the one contract message that shares the
    -32603 code with the internal-error fallback, so it is the case most
    likely to be swept up by mistake.
    """
    provisioned = await provision_agent_wallet(client)
    _break_invoke(monkeypatch, ValueError("idempotency_key_reused"))

    resp = await client.post(
        "/mcp/messages",
        json={
            "jsonrpc": "2.0",
            "id": "reused-1",
            "method": "tools/call",
            "params": {
                "name": TOOL,
                "arguments": {},
                "mcpContext": {
                    "wallet_id": provisioned["agent_wallet_id"],
                    "idempotency_key": "leak-reused-1",
                },
            },
        },
        headers=provisioned["agent_headers"],
    )

    assert resp.status_code == 200
    assert resp.json()["error"] == {
        "code": -32603,
        "message": "idempotency_key_reused",
    }
