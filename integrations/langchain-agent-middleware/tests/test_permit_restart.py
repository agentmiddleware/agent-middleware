"""A restarted process sends the stored permit body again (#363).

These tests cover the client side only: which ``POST /v1/permits`` body each
process puts on the wire, and what is on disk before it leaves. The transport
below drops every request, so no permit, receipt, ledger entry or signature is
written here. The replay itself (same permit, same receipt, one debit) is
proven against the real application in
``gateway/tests/test_wrapper_permit_restart.py``.

A "restart" is a fresh tool instance on the same store file.
"""

import json
from decimal import Decimal

import httpx
import pytest
from b2a_sdk import TransportError

from langchain_b2a import B2AClient, JsonFilePermitStore
from langchain_b2a._tools import create_mcp_tool

PERMIT_KEY = "permit-key-1"


class DroppedConnection:
    """Records each permit body, then drops the connection."""

    def __init__(self) -> None:
        self.permit_bodies: list[dict] = []
        self.on_permit = None

    async def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/permits", "nothing follows a lost permit"
        assert request.headers["idempotency-key"] == PERMIT_KEY
        self.permit_bodies.append(json.loads(request.content))
        if self.on_permit is not None:
            self.on_permit(self.permit_bodies[-1])
        raise httpx.ConnectError("connection dropped", request=request)


def _tool(wire: DroppedConnection, **kwargs):
    client = B2AClient(api_key="test-key", transport=httpx.MockTransport(wire.handler))
    terms = {"wallet_id": "wallet-1", **kwargs}
    return client, create_mcp_tool(client, **terms)


async def _call(pair) -> str:
    client, tool = pair
    try:
        return await tool.ainvoke(
            {
                "tool_name": "partner.search",
                "idempotency_key": "call-key-1",
                "permit_idempotency_key": PERMIT_KEY,
                "arguments": {"q": "x"},
            }
        )
    finally:
        await client.close()


async def _attempt(pair) -> None:
    """One process whose permit request is lost in transit."""
    with pytest.raises(TransportError):
        await _call(pair)


@pytest.mark.asyncio
async def test_restart_with_store_sends_the_same_permit_body(tmp_path):
    wire = DroppedConnection()
    path = tmp_path / "permits.json"

    await _attempt(_tool(wire, permit_store=JsonFilePermitStore(path)))
    await _attempt(_tool(wire, permit_store=JsonFilePermitStore(path)))

    assert len(wire.permit_bodies) == 2
    assert wire.permit_bodies[0] == wire.permit_bodies[1]


@pytest.mark.asyncio
async def test_restart_without_store_sends_a_new_expiry_under_the_same_key():
    wire = DroppedConnection()

    await _attempt(_tool(wire))
    await _attempt(_tool(wire))

    first, second = wire.permit_bodies
    assert first["expires_at"] != second["expires_at"]
    first.pop("expires_at")
    second.pop("expires_at")
    assert first == second


@pytest.mark.asyncio
async def test_body_is_stored_before_the_request_leaves(tmp_path):
    wire = DroppedConnection()
    path = tmp_path / "permits.json"
    stored_at_send: list[dict] = []

    def read_store(_body: dict) -> None:
        record = JsonFilePermitStore(path).get(PERMIT_KEY)
        stored_at_send.append(record["request"])

    wire.on_permit = read_store
    await _attempt(_tool(wire, permit_store=JsonFilePermitStore(path)))

    assert stored_at_send == wire.permit_bodies


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "value"),
    [("permit_budget", Decimal(50)), ("wallet_id", "wallet-2")],
)
async def test_restart_with_changed_terms_is_refused_before_the_network(tmp_path, change, value):
    wire = DroppedConnection()
    path = tmp_path / "permits.json"
    await _attempt(_tool(wire, permit_store=JsonFilePermitStore(path)))

    changed = _tool(wire, permit_store=JsonFilePermitStore(path), **{change: value})
    with pytest.raises(ValueError, match="different permit terms"):
        await _call(changed)
    assert len(wire.permit_bodies) == 1
