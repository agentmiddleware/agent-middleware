"""Focused tests for b2a_sdk.client.AgentMiddlewareClient.

The billing/wallet surface is covered by test_sdk.py, the permit and invoke
loop by test_trust_client.py, and ACP/bearer/x402 by test_363_surface.py.
These tests cover the client module itself: construction headers, HTTP error
mapping, receipt read edges, dry-run simulation, and the simple GET surfaces.
All transport is faked with httpx.MockTransport; no network is used.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from b2a_sdk import (
    AgentMiddlewareClient,
    AuthenticationError,
    AuthorizationError,
    IdempotencyConflictError,
    PermitDeniedError,
)
from b2a_sdk.decorators import _dry_run_context
from b2a_sdk.errors import APIError, TransportError


def _client(handler) -> AgentMiddlewareClient:
    return AgentMiddlewareClient(
        api_key="test-key",
        base_url="http://test",
        transport=httpx.MockTransport(handler),
    )


def _receipt_payload() -> dict:
    return {
        "receipt_id": "receipt-1",
        "permit_id": "permit-1",
        "wallet_id": "wallet-1",
        "tool": "partner.search",
        "request_hash": "request-hash",
        "credits_authorized": "2",
        "credits_charged": "2",
        "outcome": "success",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "signature": "receipt-signature",
        "signature_key_id": "signing-key-1",
    }


class TestConstruction:
    @pytest.mark.asyncio
    async def test_base_url_trailing_slashes_stripped(self) -> None:
        client = AgentMiddlewareClient(
            api_key="k",
            base_url="https://api.example.com///",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        )
        try:
            assert client.base_url == "https://api.example.com"
            assert client.api_key == "k"
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_default_headers_carry_key_without_authorization(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"status": "ok"})

        async with _client(handler) as client:
            assert "authorization" not in client._client.headers
            await client.health_check()

        assert seen[0].headers["x-api-key"] == "test-key"
        assert seen[0].headers["user-agent"] == "b2a-sdk/0.5.0"


class TestIdempotencyKeys:
    @pytest.mark.asyncio
    async def test_too_long_key_rejected_before_sending(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be sent")

        async with _client(handler) as client:
            with pytest.raises(ValueError, match="at most 128"):
                await client.invoke_tool(
                    "partner.search",
                    {},
                    wallet_id="wallet-1",
                    permit_id="permit-1",
                    idempotency_key="k" * 129,
                )

    @pytest.mark.asyncio
    async def test_128_char_key_is_forwarded(self) -> None:
        key = "k" * 128

        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["idempotency-key"] == key
            return httpx.Response(200, json={"action": "debit"})

        async with _client(handler) as client:
            result = await client.charge("wallet-1", "content_factory", idempotency_key=key)

        assert result == {"action": "debit"}


class TestHttpErrorMapping:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status", "payload", "error"),
        [
            (401, {"detail": "invalid_api_key"}, AuthenticationError),
            (403, {"detail": "permit_expired"}, PermitDeniedError),
            (403, {"detail": "forbidden"}, AuthorizationError),
            (409, {"detail": "key in use"}, IdempotencyConflictError),
            (500, {"detail": "boom"}, APIError),
        ],
    )
    async def test_error_statuses_map_to_typed_errors(
        self, status: int, payload: dict, error: type[APIError]
    ) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json=payload)

        async with _client(handler) as client:
            with pytest.raises(error):
                await client.discover_tools()

    @pytest.mark.asyncio
    async def test_non_json_body_raises_api_error(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not json")

        async with _client(handler) as client:
            with pytest.raises(APIError, match="invalid_json_response"):
                await client.discover_tools()

    @pytest.mark.asyncio
    async def test_non_dict_json_raises_api_error(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[1, 2])

        async with _client(handler) as client:
            with pytest.raises(APIError, match="invalid_object_response"):
                await client.discover_tools()

    @pytest.mark.asyncio
    async def test_transport_failure_raises_transport_error(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        async with _client(handler) as client:
            with pytest.raises(TransportError):
                await client.discover_tools()

    @pytest.mark.asyncio
    async def test_simple_reads_propagate_transport_errors_unwrapped(self) -> None:
        """get_pricing uses the raw client, so httpx errors pass through as-is."""

        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        async with _client(handler) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get_pricing()

    @pytest.mark.asyncio
    async def test_unkeyed_charge_409_keeps_legacy_http_error(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(409, json={"detail": "conflict"})

        async with _client(handler) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await client.charge("wallet-1", "content_factory")


class TestReceiptReads:
    @pytest.mark.asyncio
    async def test_get_receipt_malformed_payload_raises(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/receipts/receipt-1"
            return httpx.Response(200, json={})

        async with _client(handler) as client:
            with pytest.raises(APIError, match="invalid_get_receipt"):
                await client.get_receipt("receipt-1")

    @pytest.mark.asyncio
    async def test_verify_receipt_without_receipt_returns_none(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"valid": True})

        async with _client(handler) as client:
            verification = await client.verify_receipt("receipt-1")

        assert verification.valid is True
        assert verification.receipt is None

    @pytest.mark.asyncio
    async def test_verify_receipt_non_bool_valid_raises(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"valid": "yes", "receipt": _receipt_payload()})

        async with _client(handler) as client:
            with pytest.raises(APIError, match="invalid_receipt_verification"):
                await client.verify_receipt("receipt-1")


class TestInvokeEdges:
    @pytest.mark.asyncio
    async def test_invoke_tool_error_result_raises_api_error(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "result": {
                        "receipt": _receipt_payload(),
                        "isError": True,
                        "error": "tool blew up",
                        "content": [{"type": "text", "text": "x"}],
                    }
                },
            )

        async with _client(handler) as client:
            with pytest.raises(APIError, match="tool blew up"):
                await client.invoke_tool(
                    "partner.search",
                    {"query": "x"},
                    wallet_id="wallet-1",
                    permit_id="permit-1",
                    idempotency_key="invoke-key-1",
                )

    @pytest.mark.asyncio
    async def test_invoke_tool_invalid_content_raises(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"result": {"receipt": _receipt_payload(), "content": "oops"}},
            )

        async with _client(handler) as client:
            with pytest.raises(APIError, match="invalid_mcp_content"):
                await client.invoke_tool(
                    "partner.search",
                    {},
                    wallet_id="wallet-1",
                    permit_id="permit-1",
                    idempotency_key="invoke-key-2",
                )


class TestSimpleReads:
    @pytest.mark.asyncio
    async def test_health_check_and_pricing(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok"})
            if request.url.path == "/v1/billing/pricing":
                return httpx.Response(200, json={"content_factory": 2.0})
            raise AssertionError(f"unexpected path: {request.url.path}")

        async with _client(handler) as client:
            assert await client.health_check() == {"status": "ok"}
            assert await client.get_pricing() == {"content_factory": 2.0}

    @pytest.mark.asyncio
    async def test_list_wallets_filter_and_balance(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/billing/wallets":
                assert request.url.params["wallet_type"] == "agent"
                return httpx.Response(200, json={"wallets": [{"wallet_id": "w-1"}]})
            if request.url.path == "/v1/billing/wallets/w-1":
                return httpx.Response(200, json={"wallet_id": "w-1", "balance": 7.5})
            raise AssertionError(f"unexpected path: {request.url.path}")

        async with _client(handler) as client:
            assert await client.list_wallets(wallet_type="agent") == [{"wallet_id": "w-1"}]
            assert await client.get_balance("w-1") == 7.5


class TestDryRun:
    def test_simulation_tracks_cost_and_failure(self) -> None:
        from b2a_sdk.client import DryRunSimulation

        client = AgentMiddlewareClient(api_key="k", base_url="http://test")
        simulation = DryRunSimulation(client=client, wallet_id="wallet-1")
        simulation.add_charge_result({"would_succeed": True, "credits_would_charge": 5.0})
        simulation.add_charge_result({"would_succeed": True, "credits_would_charge": 2.5})
        assert simulation.total_cost == 7.5
        assert simulation.charge_count == 2
        assert simulation.would_succeed is True

        charges = simulation.charges
        charges.clear()
        assert simulation.charge_count == 2

        simulation.add_charge_result({"would_succeed": False})
        assert simulation.would_succeed is False
        assert simulation.charge_count == 2

    @pytest.mark.asyncio
    async def test_simulate_session_sets_and_clears_context(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                assert request.url.path == "/v1/billing/dry-run/session"
                return httpx.Response(200, json={"session_id": "sess-1"})
            assert request.url.path == "/v1/billing/dry-run/session/sess-1"
            return httpx.Response(200, json={"ended": True})

        async with _client(handler) as client:
            assert _dry_run_context.get() is None
            async with client.simulate_session("wallet-1") as sim:
                assert _dry_run_context.get() is sim
                assert sim.session_id == "sess-1"
                assert sim.total_cost == 0
            assert _dry_run_context.get() is None

    @pytest.mark.asyncio
    async def test_get_dry_run_estimate_omits_session(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/billing/dry-run/charge"
            body = json.loads(request.content)
            assert body["wallet_id"] == "wallet-1"
            assert "dry_run_session_id" not in body
            return httpx.Response(200, json={"credits_would_charge": 3, "would_succeed": True})

        async with _client(handler) as client:
            estimate = await client.get_dry_run_estimate("wallet-1", "content_factory", units=2.0)

        assert estimate["would_succeed"] is True
