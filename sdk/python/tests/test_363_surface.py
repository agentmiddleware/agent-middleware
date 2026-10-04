"""
Tests for the 363 surface additions:
- ACP checkout
- IGA bearer token support
- x402 parse endpoint
- x402 402 vs InsufficientFundsError distinction
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from b2a_sdk import AgentMiddlewareClient, InsufficientFundsError
from b2a_sdk.errors import APIError, AuthenticationError
from b2a_sdk.models import ACPCheckoutRequest, ACPCheckoutResponse, ACPLineItem
from b2a_sdk.x402 import X402Client, parse_402_response


class TestACPCheckout:
    """Tests for ACP checkout surface."""

    @pytest.fixture
    def client(self):
        return AgentMiddlewareClient(api_key="test-key", base_url="http://test")

    @pytest.mark.asyncio
    async def test_acp_checkout_success(self, client):
        """Test successful ACP checkout."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.is_error = False
        mock_response.json.return_value = {
            "order_id": "ord-xyz",
            "intent_id": "int-abc",
            "permit_id": "pmt-123",
            "receipt_id": "rct-456",
            "audit_event_id": "aud-789",
            "derived_total": "42.50",
            "status": "settled",
        }

        request = ACPCheckoutRequest(
            intent_id="int-abc",
            line_items=[
                ACPLineItem(
                    name="Widget",
                    sku="WDG-001",
                    quantity=2,
                    unit_amount=2125,
                    currency="usd",
                )
            ],
            spt_token="spt_test_token",
            merchant_domain="example.com",
            client_total=4250,
        )

        with patch.object(client._client, "request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response
            result = await client.acp_checkout(
                request,
                sponsor_wallet_id="spn-wallet",
                agent_wallet_id="agt-wallet",
                idempotency_key="acp-checkout-001",
            )

            assert isinstance(result, ACPCheckoutResponse)
            assert result.order_id == "ord-xyz"
            assert result.permit_id == "pmt-123"
            assert result.receipt_id == "rct-456"
            assert result.derived_total == "42.50"
            assert result.status == "settled"

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "POST"
            assert call_args[0][1] == "/v1/billing/acp/checkout"
            assert call_args[1]["headers"]["Idempotency-Key"] == "acp-checkout-001"
            # The server requires both wallet ids as query params.
            assert call_args[1]["params"] == {
                "sponsor_wallet_id": "spn-wallet",
                "agent_wallet_id": "agt-wallet",
            }

    @staticmethod
    def _request() -> ACPCheckoutRequest:
        """Build a representative ACP checkout request for the tests below."""
        return ACPCheckoutRequest(
            intent_id="int-abc",
            line_items=[
                ACPLineItem(
                    name="Widget",
                    sku="WDG-001",
                    quantity=2,
                    unit_amount=2125,
                    currency="usd",
                )
            ],
            spt_token="spt_test_token",
            merchant_domain="example.com",
            client_total=4250,
        )

    @pytest.mark.asyncio
    async def test_acp_checkout_blank_idempotency_key_rejected(self, client):
        """A blank idempotency key is refused before any request is sent."""
        with patch.object(client._client, "request", new_callable=AsyncMock) as mock_request:
            with pytest.raises(ValueError):
                await client.acp_checkout(
                    self._request(),
                    sponsor_wallet_id="spn-wallet",
                    agent_wallet_id="agt-wallet",
                    idempotency_key="   ",
                )
            mock_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_acp_checkout_unauthorized_raises(self, client):
        """A 401 surfaces as AuthenticationError, not a generic APIError."""
        bearer_client = AgentMiddlewareClient(
            api_key="test-key", base_url="http://test", bearer_token="tok-abc"
        )
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.is_error = True
        mock_response.json.return_value = {"detail": "invalid_api_key"}

        with patch.object(bearer_client._client, "request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response
            with pytest.raises(AuthenticationError):
                await bearer_client.acp_checkout(
                    self._request(),
                    sponsor_wallet_id="spn-wallet",
                    agent_wallet_id="agt-wallet",
                    idempotency_key="acp-checkout-401",
                )

    @pytest.mark.asyncio
    async def test_acp_checkout_missing_field_raises_api_error(self, client):
        """A 200 body missing a required ACP field is an APIError, not a crash."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.is_error = False
        mock_response.json.return_value = {
            "order_id": "ord-xyz",
            "intent_id": "int-abc",
            "permit_id": "pmt-123",
            # receipt_id omitted: the trust-plane evidence is incomplete.
            "audit_event_id": "aud-789",
            "derived_total": "42.50",
            "status": "settled",
        }

        with patch.object(client._client, "request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response
            with pytest.raises(APIError):
                await client.acp_checkout(
                    self._request(),
                    sponsor_wallet_id="spn-wallet",
                    agent_wallet_id="agt-wallet",
                    idempotency_key="acp-checkout-bad",
                )

    def test_acp_request_repr_redacts_spt_token(self):
        """The delegated payment credential never reaches a repr or traceback."""
        request = self._request()
        assert "spt_test_token" not in repr(request)
        # ...but it is still sent on the wire.
        assert request.to_payload()["spt_token"] == "spt_test_token"

    @pytest.mark.asyncio
    async def test_acp_line_item_to_payload(self):
        """Test ACPLineItem.to_payload()."""
        item = ACPLineItem(
            name="Widget",
            sku="WDG-001",
            quantity=2,
            unit_amount=2125,
            currency="usd",
        )
        payload = item.to_payload()
        assert payload["name"] == "Widget"
        assert payload["sku"] == "WDG-001"
        assert payload["quantity"] == 2
        assert payload["unit_amount"] == 2125
        assert payload["currency"] == "usd"

    @pytest.mark.asyncio
    async def test_acp_line_item_to_payload_no_sku(self):
        """A line item with no SKU is built by omitting it entirely."""
        item = ACPLineItem(
            name="Widget",
            quantity=2,
            unit_amount=2125,
            currency="usd",
        )
        assert item.sku is None
        payload = item.to_payload()
        assert "sku" not in payload

        # Passing None explicitly stays equivalent.
        explicit = ACPLineItem(
            name="Widget",
            quantity=2,
            unit_amount=2125,
            currency="usd",
            sku=None,
        )
        assert explicit.to_payload() == payload


class TestIGABearerToken:
    """Tests for IGA bearer token support."""

    def test_client_with_bearer_token(self):
        """Test that bearer_token is added to Authorization header."""
        client = AgentMiddlewareClient(
            api_key="test-key",
            base_url="http://test",
            bearer_token="eyJhbGc...",
        )
        assert "Authorization" in client._client.headers
        assert client._client.headers["Authorization"] == "Bearer eyJhbGc..."

    def test_client_without_bearer_token(self):
        """Test that Authorization header is not set when bearer_token is None."""
        client = AgentMiddlewareClient(
            api_key="test-key",
            base_url="http://test",
        )
        assert "Authorization" not in client._client.headers


class TestX402Parse:
    """Tests for x402 parse endpoint and field names."""

    @pytest.fixture
    def x402_client(self):
        return X402Client(api_key="test-key", base_url="http://test")

    @pytest.mark.asyncio
    async def test_parse_402_success(self, x402_client):
        """Test successful parse_402 call."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.is_error = False
        mock_response.json.return_value = {
            "amount_usd": "10.50",
            "pay_to": "0x1234567890abcdef",
            "network": "ethereum",
            "asset": "USDC",
        }

        with patch.object(x402_client._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            result = await x402_client.parse_402(
                status_code=402,
                headers={
                    "X-402-Amount": "10.50",
                    "X-402-Payto": "0x1234567890abcdef",
                    "X-402-Network": "ethereum",
                    "X-402-Asset": "USDC",
                },
            )

            assert result["amount_usd"] == "10.50"
            assert result["pay_to"] == "0x1234567890abcdef"
            assert result["network"] == "ethereum"
            assert result["asset"] == "USDC"

            mock_post.assert_called_once()
            call_args = mock_post.call_args
            assert call_args[0][0] == "/v1/x402/parse"
            assert call_args[1]["json"]["status_code"] == 402

    def test_parse_402_response_with_amount_usd(self):
        """Test parse_402_response returns amount_usd field."""
        mock_response = MagicMock()
        mock_response.status_code = 402
        mock_response.headers = {
            "X-402-Amount": "10.50",
            "X-402-Payto": "0x1234567890abcdef",
            "X-402-Network": "ethereum",
            "X-402-Asset": "USDC",
        }

        result = parse_402_response(mock_response)
        assert result is not None
        assert result["amount_usd"] == "10.50"
        # Legacy alias preserved for existing callers.
        assert result["amount"] == "10.50"
        assert result["pay_to"] == "0x1234567890abcdef"
        assert result["network"] == "ethereum"
        assert result["asset"] == "USDC"

    def test_parse_402_response_no_asset(self):
        """Test parse_402_response when asset header is absent."""
        mock_response = MagicMock()
        mock_response.status_code = 402
        mock_response.headers = {
            "X-402-Amount": "10.50",
            "X-402-Payto": "0x1234567890abcdef",
            "X-402-Network": "ethereum",
        }

        result = parse_402_response(mock_response)
        assert result is not None
        assert result["amount_usd"] == "10.50"
        assert "asset" not in result

    def test_parse_402_response_non_402(self):
        """Test parse_402_response returns None for non-402 status."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {
            "X-402-Amount": "10.50",
            "X-402-Payto": "0x1234567890abcdef",
            "X-402-Network": "ethereum",
        }

        result = parse_402_response(mock_response)
        assert result is None


class TestX402VsInsufficientFunds:
    """Tests that HTTP 402 is not collapsed into InsufficientFundsError."""

    @pytest.fixture
    def x402_client(self):
        return X402Client(api_key="test-key", base_url="http://test")

    @pytest.mark.asyncio
    async def test_parse_402_http_402_not_insufficient_funds(self, x402_client):
        """Test that parse_402 does not raise InsufficientFundsError on HTTP 402."""
        mock_response = MagicMock()
        mock_response.status_code = 402
        mock_response.is_error = True
        mock_response.json.return_value = {
            "detail": "x402_invalid_headers",
        }

        with patch.object(x402_client._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            with pytest.raises(APIError) as exc_info:
                await x402_client.parse_402(
                    status_code=402,
                    headers={},
                )

            # Should raise APIError, not InsufficientFundsError
            assert not isinstance(exc_info.value, InsufficientFundsError)
            assert isinstance(exc_info.value, APIError)

    @pytest.mark.asyncio
    async def test_settle_402_http_402_not_insufficient_funds(self, x402_client):
        """Test that settle_402 does not raise InsufficientFundsError on HTTP 402."""
        mock_response = MagicMock()
        mock_response.status_code = 402
        mock_response.is_error = True
        mock_response.json.return_value = {
            "detail": "x402_invalid_requirement",
        }

        with patch.object(x402_client._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            with pytest.raises(APIError) as exc_info:
                await x402_client.settle_402(
                    permit_id="pmt-123",
                    wallet_id="agt-wallet",
                    requirement={
                        "amount_usd": "10.50",
                        "pay_to": "0x1234",
                        "network": "ethereum",
                        "asset": "USDC",
                    },
                    idempotency_key="settle-001",
                )

            # HTTP 402 in x402 context should raise APIError, not InsufficientFundsError
            assert not isinstance(exc_info.value, InsufficientFundsError)
            assert isinstance(exc_info.value, APIError)


class TestSettleAmountPrecedence:
    """settle_402() must settle the canonical amount, not the legacy alias."""

    @pytest.fixture
    def x402_client(self):
        """An X402Client pointed at a stub base URL."""
        return X402Client(api_key="test-key", base_url="http://test")

    @staticmethod
    def _settled_response():
        """A minimal successful /v1/x402/settle response."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.is_error = False
        mock_response.json.return_value = {
            "settlement_id": "stl-1",
            "receipt_id": "rct-1",
        }
        return mock_response

    @pytest.mark.asyncio
    async def test_amount_usd_wins_over_legacy_amount(self, x402_client):
        """When both keys are present and differ, amount_usd is settled."""
        with patch.object(x402_client._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = self._settled_response()
            await x402_client.settle_402(
                permit_id="pmt-1",
                wallet_id="agt-1",
                requirement={
                    # A stale/mutated legacy alias must not decide the amount.
                    "amount": "999.00",
                    "amount_usd": "1.50",
                    "pay_to": "0x1111111111111111111111111111111111111111",
                    "network": "base",
                },
                idempotency_key="settle-precedence-1",
            )
            assert mock_post.call_args[1]["json"]["amount"] == "1.50"

    @pytest.mark.asyncio
    async def test_legacy_amount_used_when_amount_usd_absent(self, x402_client):
        """An older requirement carrying only `amount` still settles."""
        with patch.object(x402_client._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = self._settled_response()
            await x402_client.settle_402(
                permit_id="pmt-1",
                wallet_id="agt-1",
                requirement={
                    "amount": "2.25",
                    "pay_to": "0x1111111111111111111111111111111111111111",
                    "network": "base",
                },
                idempotency_key="settle-precedence-2",
            )
            assert mock_post.call_args[1]["json"]["amount"] == "2.25"


class TestParseResponseValidation:
    """parse_402() must not pass a malformed 2xx body through to callers."""

    @pytest.fixture
    def x402_client(self):
        """An X402Client pointed at a stub base URL."""
        return X402Client(api_key="test-key", base_url="http://test")

    @staticmethod
    def _response(body):
        """A 200 response carrying the given JSON body."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.is_error = False
        mock_response.json.return_value = body
        return mock_response

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "missing",
        ["amount_usd", "pay_to", "network"],
    )
    async def test_missing_required_field_raises(self, x402_client, missing):
        """A gap in a required field is an APIError."""
        body = {
            "amount_usd": "1.50",
            "pay_to": "0x1111111111111111111111111111111111111111",
            "network": "base",
            "asset": "usdc",
        }
        del body[missing]
        with patch.object(x402_client._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = self._response(body)
            with pytest.raises(APIError, match=missing):
                await x402_client.parse_402(402, {"X-402-Amount": "1.50"})

    @pytest.mark.asyncio
    async def test_asset_less_response_is_accepted(self, x402_client):
        """`asset` is optional, as it is in parse_402_response and settle_402."""
        body = {
            "amount_usd": "1.50",
            "pay_to": "0x1111111111111111111111111111111111111111",
            "network": "base",
        }
        with patch.object(x402_client._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = self._response(body)
            assert await x402_client.parse_402(402, {"X-402-Amount": "1.50"}) == body

    @pytest.mark.asyncio
    async def test_malformed_asset_still_rejected(self, x402_client):
        """Optional does not mean unchecked: a present `asset` must be usable."""
        with patch.object(x402_client._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = self._response(
                {
                    "amount_usd": "1.50",
                    "pay_to": "0x1111111111111111111111111111111111111111",
                    "network": "base",
                    "asset": "",
                }
            )
            with pytest.raises(APIError, match="asset"):
                await x402_client.parse_402(402, {"X-402-Amount": "1.50"})

    @pytest.mark.asyncio
    async def test_wrong_type_field_raises(self, x402_client):
        """A non-string amount must not reach settlement."""
        with patch.object(x402_client._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = self._response(
                {
                    "amount_usd": 1.50,  # number, not the documented string
                    "pay_to": "0x1111111111111111111111111111111111111111",
                    "network": "base",
                    "asset": "usdc",
                }
            )
            with pytest.raises(APIError, match="amount_usd"):
                await x402_client.parse_402(402, {"X-402-Amount": "1.50"})

    @pytest.mark.asyncio
    async def test_wellformed_response_returned(self, x402_client):
        """A complete requirement still passes through unchanged."""
        body = {
            "amount_usd": "1.50",
            "pay_to": "0x1111111111111111111111111111111111111111",
            "network": "base",
            "asset": "usdc",
        }
        with patch.object(x402_client._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = self._response(body)
            assert await x402_client.parse_402(402, {"X-402-Amount": "1.50"}) == body


class TestACPLineItemBounds:
    """ACPLineItem rejects what the server would 422, before the SPT is sent."""

    @pytest.mark.parametrize("quantity", [0, -1, 10_001])
    def test_invalid_quantity_rejected(self, quantity):
        """Quantity must be in 1..10000, mirroring app/schemas/acp.py."""
        with pytest.raises(ValueError, match="quantity"):
            ACPLineItem(
                name="Widget",
                quantity=quantity,
                unit_amount=2125,
                currency="usd",
            )

    @pytest.mark.parametrize("unit_amount", [-1, 10_000_001])
    def test_invalid_unit_amount_rejected(self, unit_amount):
        """unit_amount must be in 0..10000000 minor units."""
        with pytest.raises(ValueError, match="unit_amount"):
            ACPLineItem(
                name="Widget",
                quantity=1,
                unit_amount=unit_amount,
                currency="usd",
            )

    @pytest.mark.parametrize("unit_amount", [0, 10_000_000])
    def test_boundary_unit_amounts_accepted(self, unit_amount):
        """A zero-cost or max-cost line is legal; the bounds are inclusive."""
        item = ACPLineItem(name="Widget", quantity=1, unit_amount=unit_amount, currency="usd")
        assert item.to_payload()["unit_amount"] == unit_amount


class TestBearerTokenValidation:
    """A blank bearer must not become a malformed `Authorization: Bearer `."""

    @pytest.mark.parametrize("blank", ["", "   ", "\t"])
    def test_blank_bearer_token_rejected(self, blank):
        """An unset env var fails here, not silently at the server."""
        with pytest.raises(ValueError, match="bearer_token"):
            AgentMiddlewareClient(api_key="test-key", base_url="http://test", bearer_token=blank)

    def test_bearer_token_is_stripped(self):
        """Surrounding whitespace never reaches the header value."""
        client = AgentMiddlewareClient(
            api_key="test-key", base_url="http://test", bearer_token="  tok-abc  "
        )
        assert client._client.headers["Authorization"] == "Bearer tok-abc"

    def test_omitted_bearer_token_sends_no_header(self):
        """Omitting it stays the way to send no Authorization header."""
        client = AgentMiddlewareClient(api_key="test-key", base_url="http://test")
        assert "Authorization" not in client._client.headers
