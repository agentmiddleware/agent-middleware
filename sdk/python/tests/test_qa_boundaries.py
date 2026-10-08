"""Boundary tests for the Python SDK trust and billing client.

These cover header safety, 402 parsing, receipt binding, path segments,
ACP request checks, the offline key fetch, and the legacy MCP CLI.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from b2a_sdk.client import AgentMiddlewareClient
from b2a_sdk.edge_client import B2AEdgeClient
from b2a_sdk.errors import APIError, DeliveryUncertainError, InsufficientFundsError
from b2a_sdk.models import ACPCheckoutRequest, ACPLineItem
from b2a_sdk.receipt_verifier import VerificationError

TOOL = "partner.search"


def _receipt(**overrides: object) -> dict:
    payload: dict = {
        "receipt_id": "receipt-success",
        "permit_id": "permit-1",
        "wallet_id": "wallet-1",
        "key_id": "key-1",
        "tool": TOOL,
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
    payload.update(overrides)
    return payload


def _client(handler) -> tuple[AgentMiddlewareClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = AgentMiddlewareClient(
        api_key="test-key",
        base_url="http://test",
        transport=httpx.MockTransport(wrapped),
    )
    return client, seen


def _checkout(**overrides: object) -> ACPCheckoutRequest:
    values: dict = {
        "intent_id": "int-abc",
        "line_items": [
            ACPLineItem(
                name="Widget",
                sku="WDG-001",
                quantity=2,
                unit_amount=2125,
                currency="usd",
            )
        ],
        "spt_token": "spt_test_token",
        "merchant_domain": "example.com",
        "client_total": 4250,
    }
    values.update(overrides)
    return ACPCheckoutRequest(**values)


class TestHeaderSafety:
    @pytest.mark.asyncio
    async def test_idempotency_key_rejects_internal_control_characters(self):
        """A CR/LF inside the key must not be sent as a second header."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"sent {request.url}")

        client, seen = _client(handler)
        async with client:
            with pytest.raises(ValueError, match="control"):
                await client.charge(
                    "wallet-123",
                    "iot_bridge",
                    idempotency_key="charge-key\r\nX-Injected: 1",
                )
        assert seen == []

    def test_bearer_token_rejects_internal_control_characters(self):
        with pytest.raises(ValueError, match="control"):
            AgentMiddlewareClient(
                api_key="test-key",
                base_url="http://test",
                bearer_token="tok-abc\r\nX-Api-Key: stolen",
            )

    @pytest.mark.asyncio
    async def test_edge_execute_rejects_control_characters_in_headers(self):
        edge = B2AEdgeClient(api_url="http://test", api_key="test-key", wallet_id="wallet-1")

        async def boom(*_args, **_kwargs):
            raise AssertionError("request was sent")

        edge._client.post = boom
        try:
            with pytest.raises(ValueError, match="control"):
                await edge.execute_awi_action(
                    "session-1",
                    "click",
                    {},
                    permit_id="permit-1\r\nX-Injected: 1",
                    idempotency_key="action-1",
                )
            with pytest.raises(ValueError, match="control"):
                await edge.execute_awi_action(
                    "session-1",
                    "click",
                    {},
                    permit_id="permit-1",
                    idempotency_key="action-1\nX-Injected: 1",
                )
        finally:
            await edge.close()


class TestInsufficientFundsBody:
    @pytest.mark.asyncio
    async def test_string_detail_is_still_insufficient_funds(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(402, json={"detail": "insufficient_funds"})

        client, _seen = _client(handler)
        async with client:
            with pytest.raises(InsufficientFundsError) as exc_info:
                await client.charge("wallet-123", "iot_bridge")
        assert exc_info.value.wallet_id == "wallet-123"
        assert exc_info.value.shortfall is None

    @pytest.mark.asyncio
    async def test_non_json_402_is_still_insufficient_funds(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(402, content=b"upstream down")

        client, _seen = _client(handler)
        async with client:
            with pytest.raises(InsufficientFundsError):
                await client.charge("wallet-123", "iot_bridge")

    @pytest.mark.asyncio
    async def test_charge_prefers_exact_shortfall_and_server_top_up(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                402,
                json={
                    "detail": {
                        "wallet_id": "wallet-123",
                        "shortfall": 0.30000000000000004,
                        "shortfall_exact": "0.30",
                        "top_up_url": "/v1/billing/top-up/prepare",
                    }
                },
            )

        client, _seen = _client(handler)
        async with client:
            with pytest.raises(InsufficientFundsError) as exc_info:
                await client.charge("wallet-123", "iot_bridge")
        error = exc_info.value
        assert error.shortfall_exact == "0.30"
        assert error.shortfall == 0.3
        assert error.top_up_url == "http://test/v1/billing/top-up/prepare"
        assert "dashboard/top-up" not in str(error)
        assert "0.30000000000000004" not in str(error)

    @pytest.mark.asyncio
    async def test_governed_402_uses_exact_shortfall_and_server_top_up(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                402,
                json={
                    "detail": {
                        "wallet_id": "wallet-123",
                        "shortfall": 0.30000000000000004,
                        "shortfall_exact": "0.30",
                        "top_up_url": "https://pay.example/top",
                    }
                },
            )

        client, _seen = _client(handler)
        async with client:
            with pytest.raises(InsufficientFundsError) as exc_info:
                await client.discover_tools()
        error = exc_info.value
        assert error.wallet_id == "wallet-123"
        assert error.shortfall_exact == "0.30"
        assert error.top_up_url == "https://pay.example/top"


class TestReceiptBindingAndPaths:
    @pytest.mark.asyncio
    async def test_invoke_rejects_a_receipt_for_another_tool(self):
        body = _receipt(tool="other.tool")

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": "key-1",
                    "result": {
                        "content": [{"type": "text", "text": "ok"}],
                        "isError": False,
                        "receipt": body,
                    },
                },
            )

        client, _seen = _client(handler)
        async with client:
            with pytest.raises(APIError, match="mcp_receipt_does_not_match_call") as exc_info:
                await client.invoke_tool(
                    TOOL,
                    {"query": "risk"},
                    wallet_id="wallet-1",
                    permit_id="permit-1",
                    idempotency_key="invoke-1",
                )
        assert type(exc_info.value) is APIError
        assert "idempotency key" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_mismatched_uncertain_receipt_is_not_delivery_proof(self):
        body = _receipt(outcome="delivery_uncertain", wallet_id="other-wallet")

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": "key-1",
                    "error": {
                        "code": -32000,
                        "message": "delivery_uncertain",
                        "data": {"receipt": body},
                    },
                },
            )

        client, _seen = _client(handler)
        async with client:
            with pytest.raises(APIError, match="mcp_receipt_does_not_match_call") as exc_info:
                await client.invoke_tool(
                    TOOL,
                    {},
                    wallet_id="wallet-1",
                    permit_id="permit-1",
                    idempotency_key="invoke-uncertain",
                )
        assert not isinstance(exc_info.value, DeliveryUncertainError)

    @pytest.mark.asyncio
    async def test_get_receipt_refuses_a_path_escape(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"sent {request.url.path}")

        client, seen = _client(handler)
        async with client:
            with pytest.raises(ValueError, match="receipt_id"):
                await client.get_receipt("../admin")
        assert seen == []

    @pytest.mark.asyncio
    async def test_get_wallet_refuses_a_path_escape(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"sent {request.url.path}")

        client, seen = _client(handler)
        async with client:
            with pytest.raises(ValueError, match="wallet_id"):
                await client.get_wallet("wallet/../admin")
        assert seen == []

    @pytest.mark.asyncio
    async def test_returned_receipt_id_must_match_the_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/receipts/receipt-success":
                return httpx.Response(200, json=_receipt(receipt_id="receipt-other"))
            if request.url.path == "/v1/receipts/verify":
                return httpx.Response(
                    200,
                    json={
                        "valid": True,
                        "reason": None,
                        "receipt": _receipt(receipt_id="receipt-other"),
                    },
                )
            if request.url.path == "/v1/evidence/receipt-success":
                return httpx.Response(
                    200,
                    json={
                        "receipt_id": "receipt-other",
                        "valid": True,
                        "receipt": _receipt(),
                        "verification": {"receipt_signature": "ok"},
                    },
                )
            raise AssertionError(request.url.path)

        client, _seen = _client(handler)
        async with client:
            with pytest.raises(APIError, match="receipt_id_mismatch"):
                await client.get_receipt("receipt-success")
            with pytest.raises(APIError, match="receipt_id_mismatch"):
                await client.verify_receipt("receipt-success")
            with pytest.raises(APIError, match="receipt_id_mismatch"):
                await client.get_evidence("receipt-success")

    @pytest.mark.asyncio
    async def test_blank_tool_is_not_sent(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"sent {request.url.path}")

        client, seen = _client(handler)
        async with client:
            with pytest.raises(ValueError, match="tool"):
                await client.invoke_tool(
                    "  ",
                    {},
                    wallet_id="wallet-1",
                    permit_id="permit-1",
                    idempotency_key="invoke-blank",
                )
        assert seen == []

    @pytest.mark.asyncio
    async def test_dry_run_skips_delete_for_an_unsafe_session_id(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path == "/v1/billing/dry-run/session":
                return httpx.Response(200, json={"session_id": "../admin"})
            raise AssertionError(f"sent {request.method} {request.url.path}")

        client, seen = _client(handler)
        async with client:
            async with client.simulate_session("wallet-1"):
                pass
        assert [request.url.path for request in seen] == ["/v1/billing/dry-run/session"]

    @pytest.mark.asyncio
    async def test_edge_balance_refuses_a_path_escape(self):
        edge = B2AEdgeClient(
            api_url="http://test",
            api_key="test-key",
            wallet_id="wallet/../admin",
        )

        async def boom(*_args, **_kwargs):
            raise AssertionError("request was sent")

        edge._client.get = boom
        try:
            with pytest.raises(ValueError, match="wallet_id"):
                await edge.get_balance()
        finally:
            await edge.close()


class TestAcpClientBounds:
    def test_valid_checkout_still_builds_the_server_payload(self):
        payload = _checkout().to_payload()
        assert payload["client_total"] == 4250
        assert payload["line_items"][0]["currency"] == "usd"
        assert payload["spt_token"] == "spt_test_token"

    def test_empty_sku_is_allowed(self):
        item = ACPLineItem(name="Widget", sku="", quantity=1, unit_amount=0, currency="usd")
        assert item.to_payload()["sku"] == ""

    @pytest.mark.parametrize(
        ("field", "value", "match"),
        [
            ("currency", "USD", "acp_unsupported_currency"),
            ("quantity", True, "quantity"),
            ("quantity", 1.5, "quantity"),
            ("name", "", "name"),
            ("unit_amount", True, "unit_amount"),
        ],
    )
    def test_line_item_rejects_values_the_server_rejects(self, field, value, match):
        kwargs = {
            "name": "Widget",
            "quantity": 1,
            "unit_amount": 100,
            "currency": "usd",
            field: value,
        }
        with pytest.raises(ValueError, match=match):
            ACPLineItem(**kwargs)

    def test_checkout_rejects_total_mismatch_currency_and_domain(self):
        with pytest.raises(ValueError, match="acp_total_mismatch"):
            _checkout(client_total=1)
        with pytest.raises(ValueError, match="acp_merchant_domain_invalid"):
            _checkout(merchant_domain="localhost")
        with pytest.raises(ValueError, match="acp_spt_token_invalid"):
            _checkout(spt_token="")
        with pytest.raises(ValueError, match="line_items"):
            _checkout(line_items=[], client_total=0)
        with pytest.raises(ValueError, match="acp_intent_id_invalid"):
            _checkout(intent_id="has space")
        with pytest.raises(ValueError, match="client_total"):
            _checkout(
                line_items=[
                    ACPLineItem(name="Widget", quantity=1, unit_amount=1, currency="usd")
                ],
                client_total=True,
            )

    @pytest.mark.asyncio
    async def test_mutated_total_is_not_sent(self):
        request = _checkout()
        request.client_total = 1

        def handler(request_sent: httpx.Request) -> httpx.Response:
            raise AssertionError(f"sent {request_sent.url.path}")

        client, seen = _client(handler)
        async with client:
            with pytest.raises(ValueError, match="acp_total_mismatch"):
                await client.acp_checkout(
                    request,
                    sponsor_wallet_id="spn-wallet",
                    agent_wallet_id="agt-wallet",
                    idempotency_key="acp-1",
                )
        assert seen == []


class TestTrustKeyFetch:
    def test_issuer_fetch_uses_the_origin_and_does_not_follow_redirects(self, monkeypatch):
        captured: dict = {}

        class Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"keys": []}

        def fake_get(url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return Response()

        import httpx as httpx_module

        monkeypatch.setattr(httpx_module, "get", fake_get)
        from b2a_sdk import verify_cli

        document = verify_cli._fetch_key_document("https://api.example.com/ignored", timeout=5)
        assert document == {"keys": []}
        assert captured["url"] == "https://api.example.com/.well-known/trust-keys.json"
        assert captured["kwargs"]["follow_redirects"] is False

    def test_issuer_userinfo_is_not_fetched(self, monkeypatch):
        def fake_get(*_args, **_kwargs):
            raise AssertionError("request was sent")

        import httpx as httpx_module

        monkeypatch.setattr(httpx_module, "get", fake_get)
        from b2a_sdk import verify_cli

        with pytest.raises(VerificationError, match="userinfo"):
            verify_cli._fetch_key_document("https://api.example.com@evil.com", timeout=5)


class TestLegacyMcpCli:
    def test_list_tools_prints_real_line_breaks(self, monkeypatch, capsys):
        from b2a_sdk import mcp

        monkeypatch.setattr(
            mcp,
            "generate_manifest",
            lambda **_kwargs: {
                "tools": [
                    {
                        "name": "partner.search",
                        "description": "Find a partner",
                        "annotations": {
                            "creditsPerCall": 1,
                            "unitName": "call",
                            "category": "search",
                        },
                    }
                ]
            },
        )
        mcp.list_tools()
        output = capsys.readouterr().out
        assert "\\n" not in output
        assert "Available MCP Tools (1)" in output.splitlines()

    def test_category_filter_is_encoded_and_redirects_are_off(self, monkeypatch):
        from b2a_sdk import mcp

        captured: dict = {}

        class Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"tools": []}

        def fake_get(url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return Response()

        assert mcp.httpx is not None
        monkeypatch.setattr(mcp.httpx, "get", fake_get)
        manifest = mcp.generate_manifest(api_url="http://gateway.example", category="a&b=c")
        assert manifest == {"tools": []}
        assert captured["url"] == "http://gateway.example/mcp/tools.json?category=a%26b%3Dc"
        assert captured["kwargs"]["follow_redirects"] is False
