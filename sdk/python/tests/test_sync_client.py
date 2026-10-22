"""Sync wrapper tests run as plain blocking code: no event loop here."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from b2a_sdk import PermitRequest, SyncAgentMiddlewareClient


def _permit_payload() -> dict:
    now = datetime.now(timezone.utc)
    return {
        "permit_id": "permit-1",
        "issuer_wallet_id": "wallet-1",
        "subject_wallet_id": "wallet-1",
        "subject_key_id": "key-1",
        "scopes": ["tool:partner.search:invoke", "billing:charge"],
        "allowed_tools": ["partner.search"],
        "max_credits": "25",
        "spent_credits": "2",
        "expires_at": (now + timedelta(minutes=30)).isoformat(),
        "nonce": "nonce-1",
        "status": "active",
        "signature": "permit-signature",
        "key_id": "signing-key-1",
        "issued_at": now.isoformat(),
        "revoked_at": None,
    }


def _receipt_payload() -> dict:
    return {
        "receipt_id": "receipt-success",
        "permit_id": "permit-1",
        "wallet_id": "wallet-1",
        "key_id": "key-1",
        "tool": "partner.search",
        "request_hash": "request-hash",
        "response_hash": "response-hash",
        "ledger_entry_id": "ledger-1",
        "dispatch_attempt_id": "dispatch-1",
        "credits_authorized": "2",
        "credits_charged": "2",
        "outcome": "success",
        "audit_event_id": "audit-1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "signature": "receipt-signature",
        "signature_key_id": "signing-key-1",
    }


def test_sync_default_base_url_is_localhost() -> None:
    client = SyncAgentMiddlewareClient(api_key="test-key")
    try:
        assert client.base_url == "http://localhost:8000"
    finally:
        client.close()


def test_sync_discover_tools_needs_no_event_loop() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/mcp/tools.json"
        return httpx.Response(
            200,
            json={
                "tools": [
                    {
                        "name": "partner.search",
                        "description": "Search partner data",
                        "inputSchema": {"type": "object"},
                    }
                ]
            },
        )

    with SyncAgentMiddlewareClient(
        api_key="test-key",
        base_url="http://test",
        transport=httpx.MockTransport(handler),
    ) as client:
        tools = client.discover_tools()

    assert [tool.name for tool in tools] == ["partner.search"]


def test_sync_permit_and_invoke_round_trip() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/permits":
            assert request.headers["idempotency-key"] == "permit-key-1"
            return httpx.Response(201, json=_permit_payload())
        assert request.url.path == "/mcp/messages"
        body = json.loads(request.content)
        assert body["params"]["mcpContext"]["idempotency_key"] == "invoke-key-1"
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "invoke-key-1",
                "result": {
                    "content": [{"type": "text", "text": '{"ok": true}'}],
                    "isError": False,
                    "receipt": _receipt_payload(),
                },
            },
        )

    with SyncAgentMiddlewareClient(
        api_key="test-key",
        base_url="http://test",
        transport=httpx.MockTransport(handler),
    ) as client:
        permit = client.create_permit(
            PermitRequest(
                issuer_wallet_id="wallet-1",
                subject_wallet_id="wallet-1",
                subject_key_id="key-1",
                scopes=["tool:partner.search:invoke", "billing:charge"],
                allowed_tools=["partner.search"],
                max_credits=Decimal("25"),
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
            ),
            idempotency_key="permit-key-1",
        )
        result = client.invoke_tool(
            "partner.search",
            {"query": "risk"},
            wallet_id="wallet-1",
            permit_id=permit.permit_id,
            idempotency_key="invoke-key-1",
        )

    assert permit.permit_id == "permit-1"
    assert result.receipt.receipt_id == "receipt-success"

    assert client.api_key == "test-key"


def test_sync_calls_reuse_pooled_connections_across_calls() -> None:
    """Sequential calls over a real keep-alive socket must all succeed.

    ``asyncio.run`` per call closed the loop that owned the pooled
    connection, so the second call raised ``Event loop is closed``.
    ``MockTransport`` has no pool and cannot show this; a real server can.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            body = json.dumps({"tools": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = SyncAgentMiddlewareClient(
            api_key="test-key",
            base_url=f"http://127.0.0.1:{server.server_address[1]}",
        )
        assert client.discover_tools() == []
        assert client.discover_tools() == []
        assert client.discover_tools() == []
        client.close()
        client.close()  # idempotent
        with pytest.raises(RuntimeError, match="closed"):
            client.discover_tools()
    finally:
        server.shutdown()
        server.server_close()


def test_sync_client_refuses_to_run_inside_an_event_loop() -> None:
    import asyncio

    client = SyncAgentMiddlewareClient(api_key="test-key")

    async def inside() -> None:
        with pytest.raises(RuntimeError, match="inside an event loop"):
            client.discover_tools()

    try:
        asyncio.run(inside())
    finally:
        client.close()
