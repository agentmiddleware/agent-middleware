"""
Tests for B2A SDK.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from b2a_sdk import (
    AgentMiddlewareClient,
    B2AClient,
    IdempotencyConflictError,
    InsufficientFundsError,
    billable,
    combined,
    monitored,
)


def _recording_client(
    responses: dict[tuple[str, str], httpx.Response],
) -> tuple[AgentMiddlewareClient, list[httpx.Request]]:
    """A real client whose transport records each request it is sent.

    Asserting on the recorded request (method, path, query, headers, body) is
    what makes these tests check the SDK's request construction instead of
    echoing a mocked ``.json()`` back.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        key = (request.method, request.url.path)
        if key not in responses:
            raise AssertionError(f"unexpected request: {request.method} {request.url}")
        return responses[key]

    client = AgentMiddlewareClient(
        api_key="test-key",
        base_url="http://test",
        transport=httpx.MockTransport(handler),
    )
    return client, seen


_CHARGE_OK = {
    ("POST", "/v1/billing/charge"): httpx.Response(
        200,
        json={"action": "debit", "amount": -20.0, "balance_after": 4980.0},
    )
}


class TestB2AClient:
    """Tests for the legacy wallet and billing methods."""

    @pytest.mark.asyncio
    async def test_charge_success(self):
        """charge() posts the wallet, service and units as query params."""
        client, seen = _recording_client(_CHARGE_OK)
        async with client:
            await client.charge("wallet-123", "iot_bridge", units=10)

        assert len(seen) == 1
        request = seen[0]
        assert request.method == "POST"
        assert request.url.path == "/v1/billing/charge"
        assert dict(request.url.params) == {
            "wallet_id": "wallet-123",
            "service": "iot_bridge",
            "units": "10",
        }
        assert request.content == b""
        # Legacy calls send no key: they are not replay-safe, and must not
        # start sending one implicitly.
        assert "idempotency-key" not in request.headers

    @pytest.mark.asyncio
    async def test_charge_sends_optional_request_path_and_description(self):
        client, seen = _recording_client(_CHARGE_OK)
        async with client:
            await client.charge(
                "wallet-123",
                "iot_bridge",
                request_path="/tools/run",
                description="nightly batch",
            )

        params = dict(seen[0].url.params)
        assert params["request_path"] == "/tools/run"
        assert params["description"] == "nightly batch"
        assert params["units"] == "1.0"

    @pytest.mark.asyncio
    async def test_charge_forwards_idempotency_key(self):
        """A caller-owned key reaches the server, which replays retries."""
        client, seen = _recording_client(_CHARGE_OK)
        async with client:
            await client.charge(
                "wallet-123",
                "iot_bridge",
                units=10,
                idempotency_key="  charge-key-1  ",
            )

        assert seen[0].headers["idempotency-key"] == "charge-key-1"
        assert dict(seen[0].url.params)["units"] == "10"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("bad_key", "message"),
        [
            ("", "must not be blank"),
            ("   ", "must not be blank"),
            ("k" * 129, "at most 128 characters"),
        ],
    )
    async def test_charge_rejects_invalid_idempotency_key_before_sending(self, bad_key, message):
        """An invalid key fails closed locally: no request, so no charge."""
        client, seen = _recording_client(_CHARGE_OK)
        async with client:
            with pytest.raises(ValueError, match=message):
                await client.charge("wallet-123", "iot_bridge", idempotency_key=bad_key)

        assert seen == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reason", ["idempotency_key_reused", "idempotency_in_progress"])
    async def test_charge_idempotency_conflict_is_typed(self, reason):
        client, _ = _recording_client(
            {
                ("POST", "/v1/billing/charge"): httpx.Response(
                    409,
                    json={"detail": {"error": reason, "message": "conflict"}},
                )
            }
        )
        async with client:
            with pytest.raises(IdempotencyConflictError) as exc_info:
                await client.charge("wallet-123", "iot_bridge", idempotency_key="charge-key-1")

        assert exc_info.value.detail == reason
        assert exc_info.value.status_code == 409

    @pytest.mark.asyncio
    async def test_charge_insufficient_funds(self):
        """Test charge raises InsufficientFundsError on 402."""
        client, _ = _recording_client(
            {
                ("POST", "/v1/billing/charge"): httpx.Response(
                    402,
                    json={"detail": {"shortfall": "100.0"}},
                )
            }
        )
        async with client:
            with pytest.raises(InsufficientFundsError) as exc_info:
                await client.charge("wallet-123", "iot_bridge", units=100)

        assert exc_info.value.wallet_id == "wallet-123"
        assert exc_info.value.shortfall == 100.0

    @pytest.mark.asyncio
    async def test_charge_cross_wallet_denial_still_raises(self):
        """A 403 (wallet the key does not own) is not swallowed, with or without a key."""
        client, _ = _recording_client(
            {
                ("POST", "/v1/billing/charge"): httpx.Response(
                    403, json={"detail": "wallet_access_denied"}
                )
            }
        )
        async with client:
            with pytest.raises(httpx.HTTPStatusError):
                await client.charge("someone-elses-wallet", "iot_bridge")
            with pytest.raises(httpx.HTTPStatusError):
                await client.charge(
                    "someone-elses-wallet", "iot_bridge", idempotency_key="charge-key-1"
                )

    @pytest.mark.asyncio
    async def test_telemetry_non_blocking(self):
        """Test telemetry failures don't block execution."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Network error", request=request)

        async with AgentMiddlewareClient(
            api_key="test-key",
            base_url="http://test",
            transport=httpx.MockTransport(handler),
        ) as client:
            await client.telemetry(
                event_type="api_call",
                source="test_service",
                message="Test message",
            )

    @pytest.mark.asyncio
    async def test_create_sponsor_wallet(self):
        """create_sponsor_wallet() posts the sponsor fields as a JSON body."""
        client, seen = _recording_client(
            {
                ("POST", "/v1/billing/wallets/sponsor"): httpx.Response(
                    201, json={"wallet_id": "spn-123"}
                )
            }
        )
        async with client:
            await client.create_sponsor_wallet(
                sponsor_name="Test Corp",
                email="test@example.com",
                initial_credits=50000.0,
            )

        assert json.loads(seen[0].content) == {
            "sponsor_name": "Test Corp",
            "email": "test@example.com",
            "initial_credits": 50000.0,
        }

    @pytest.mark.asyncio
    async def test_create_agent_wallet_sends_zero_daily_limit(self):
        """daily_limit=0 means "spend nothing"; dropping it would mean "no cap"."""
        client, seen = _recording_client(
            {
                ("POST", "/v1/billing/wallets/agent"): httpx.Response(
                    201, json={"wallet_id": "agt-123"}
                )
            }
        )
        async with client:
            await client.create_agent_wallet(
                sponsor_wallet_id="spn-123",
                agent_id="agent-1",
                budget_credits=100.0,
                daily_limit=0,
            )

        assert json.loads(seen[0].content) == {
            "sponsor_wallet_id": "spn-123",
            "agent_id": "agent-1",
            "budget_credits": 100.0,
            "daily_limit": 0,
        }

    @pytest.mark.asyncio
    async def test_create_agent_wallet_omits_unset_daily_limit(self):
        client, seen = _recording_client(
            {
                ("POST", "/v1/billing/wallets/agent"): httpx.Response(
                    201, json={"wallet_id": "agt-123"}
                )
            }
        )
        async with client:
            await client.create_agent_wallet(
                sponsor_wallet_id="spn-123",
                agent_id="agent-1",
                budget_credits=100.0,
            )
            await client.create_agent_wallet(
                sponsor_wallet_id="spn-123",
                agent_id="agent-2",
                budget_credits=100.0,
                daily_limit=25.0,
            )

        assert "daily_limit" not in json.loads(seen[0].content)
        assert json.loads(seen[1].content)["daily_limit"] == 25.0

    @pytest.mark.asyncio
    async def test_get_wallet_and_balance(self):
        """get_wallet()/get_balance() read the wallet by its path id."""
        client, seen = _recording_client(
            {
                ("GET", "/v1/billing/wallets/agt-123"): httpx.Response(
                    200, json={"wallet_id": "agt-123", "balance": 5000.0}
                )
            }
        )
        async with client:
            wallet = await client.get_wallet("agt-123")
            balance = await client.get_balance("agt-123")

        assert [(r.method, r.url.path) for r in seen] == [
            ("GET", "/v1/billing/wallets/agt-123"),
            ("GET", "/v1/billing/wallets/agt-123"),
        ]
        assert wallet["wallet_id"] == "agt-123"
        assert balance == 5000.0

    @pytest.mark.asyncio
    async def test_prepare_top_up(self):
        """prepare_top_up() sends the wallet, amount and currency as query params."""
        client, seen = _recording_client(
            {
                ("POST", "/v1/billing/top-up/prepare"): httpx.Response(
                    200, json={"client_secret": "pi_xxx_secret"}
                )
            }
        )
        async with client:
            await client.prepare_top_up("wallet-123", 50.0)

        assert dict(seen[0].url.params) == {
            "wallet_id": "wallet-123",
            "amount_fiat": "50.0",
            "currency": "USD",
        }


class TestDecorators:
    """Tests for the @monitored and @billable decorators."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock(spec=B2AClient)
        client.telemetry = AsyncMock()
        client.charge = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_monitored_decorator_success(self, mock_client):
        """Test @monitored fires telemetry on success."""

        @monitored(mock_client, service_name="test_service")
        async def my_function():
            return "success"

        result = await my_function()

        assert result == "success"
        mock_client.telemetry.assert_called_once()
        call_kwargs = mock_client.telemetry.call_args.kwargs
        assert call_kwargs["source"] == "test_service"
        assert call_kwargs["severity"] == "info"

    @pytest.mark.asyncio
    async def test_monitored_decorator_error(self, mock_client):
        """Test @monitored fires error telemetry on exception."""

        @monitored(mock_client, service_name="test_service")
        async def my_function():
            raise ValueError("Test error")

        with pytest.raises(ValueError):
            await my_function()

        mock_client.telemetry.assert_called_once()
        call_kwargs = mock_client.telemetry.call_args.kwargs
        assert call_kwargs["event_type"] == "error"
        assert call_kwargs["severity"] == "high"
        assert call_kwargs["error_type"] == "ValueError"

    @pytest.mark.asyncio
    async def test_monitored_error_omits_exception_text_by_default(self, mock_client):
        """Exception messages and tracebacks can carry secrets; off by default."""

        @monitored(mock_client, service_name="test_service")
        async def my_function():
            raise ValueError("token=sk-live-secret")

        with pytest.raises(ValueError):
            await my_function()

        call_kwargs = mock_client.telemetry.call_args.kwargs
        assert "stack_trace" not in call_kwargs
        assert "sk-live-secret" not in repr(call_kwargs)
        assert call_kwargs["message"] == "Error in my_function"

    @pytest.mark.asyncio
    async def test_monitored_error_includes_traceback_when_opted_in(self, mock_client):
        @monitored(mock_client, service_name="test_service", capture_traceback=True)
        async def my_function():
            raise ValueError("Test error")

        with pytest.raises(ValueError):
            await my_function()

        call_kwargs = mock_client.telemetry.call_args.kwargs
        assert "ValueError: Test error" in call_kwargs["stack_trace"]
        assert call_kwargs["message"] == "Error in my_function: Test error"

    def test_monitored_sync_function_outside_event_loop_returns_value(self, mock_client):
        """A sync function must not fail after it ran just because no loop exists."""

        @monitored(mock_client, service_name="test_service")
        def add(a, b):
            return a + b

        assert add(2, 3) == 5

    def test_monitored_sync_function_outside_event_loop_reraises_original(self, mock_client):
        @monitored(mock_client, service_name="test_service")
        def explode():
            raise ValueError("original failure")

        with pytest.raises(ValueError, match="original failure"):
            explode()

    @pytest.mark.asyncio
    async def test_monitored_sync_function_inside_event_loop_emits_telemetry(self, mock_client):
        @monitored(mock_client, service_name="test_service")
        def add(a, b):
            return a + b

        assert add(2, 3) == 5
        await asyncio.sleep(0)
        mock_client.telemetry.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_billable_decorator_success(self, mock_client):
        """Test @billable charges before execution."""

        @billable(mock_client, wallet_id="wallet-123", service_category="iot_bridge", units=5.0)
        async def my_function():
            return "success"

        result = await my_function()

        assert result == "success"
        mock_client.charge.assert_called_once()
        call_kwargs = mock_client.charge.call_args.kwargs
        assert call_kwargs["wallet_id"] == "wallet-123"
        assert call_kwargs["service_category"] == "iot_bridge"
        assert call_kwargs["units"] == 5.0
        assert "my_function" in call_kwargs["request_path"]
        # Without a factory the charge call is unchanged, so clients whose
        # charge() predates the keyword keep working.
        assert "idempotency_key" not in call_kwargs

    @pytest.mark.asyncio
    async def test_billable_derives_idempotency_key_from_call_arguments(self, mock_client):
        @billable(
            mock_client,
            wallet_id="wallet-123",
            service_category="iot_bridge",
            idempotency_key_factory=lambda job_id, **_: f"job-{job_id}",
        )
        async def run_job(job_id, *, verbose=False):
            return job_id

        assert await run_job("42", verbose=True) == "42"
        assert mock_client.charge.call_args.kwargs["idempotency_key"] == "job-42"

    @pytest.mark.asyncio
    async def test_billable_forwards_factory_key_on_the_wire(self):
        client, seen = _recording_client(_CHARGE_OK)

        @billable(
            client,
            wallet_id="wallet-123",
            service_category="iot_bridge",
            idempotency_key_factory=lambda job_id: f"job-{job_id}",
        )
        async def run_job(job_id):
            return job_id

        async with client:
            await run_job("7")
            await run_job("7")

        assert [r.headers["idempotency-key"] for r in seen] == ["job-7", "job-7"]

    @pytest.mark.asyncio
    async def test_billable_blank_factory_key_fails_before_charge_or_execution(self):
        client, seen = _recording_client(_CHARGE_OK)
        ran = False

        @billable(
            client,
            wallet_id="wallet-123",
            service_category="iot_bridge",
            idempotency_key_factory=lambda: "  ",
        )
        async def my_function():
            nonlocal ran
            ran = True

        async with client:
            with pytest.raises(ValueError, match="must not be blank"):
                await my_function()

        assert seen == []
        assert ran is False

    @pytest.mark.asyncio
    async def test_combined_passes_idempotency_key_factory_through(self, mock_client):
        @combined(
            mock_client,
            wallet_id="wallet-123",
            service_category="iot_bridge",
            service_name="svc",
            idempotency_key_factory=lambda request_id: f"req-{request_id}",
        )
        async def handle(request_id):
            return request_id

        assert await handle("abc") == "abc"
        assert mock_client.charge.call_args.kwargs["idempotency_key"] == "req-abc"

    @pytest.mark.asyncio
    async def test_billable_decorator_insufficient_funds(self, mock_client):
        """Test @billable raises on insufficient funds."""
        mock_client.charge.side_effect = InsufficientFundsError(
            wallet_id="wallet-123",
            shortfall=100.0,
            top_up_url="http://test/top-up",
        )

        @billable(mock_client, wallet_id="wallet-123", service_category="iot_bridge", units=100.0)
        async def my_function():
            return "success"

        with pytest.raises(InsufficientFundsError):
            await my_function()


class TestInsufficientFundsError:
    """Tests for InsufficientFundsError."""

    def test_error_attributes(self):
        """Test error has correct attributes."""
        error = InsufficientFundsError(
            wallet_id="wallet-123",
            shortfall=100.0,
            top_up_url="http://test/top-up",
        )

        assert error.wallet_id == "wallet-123"
        assert error.shortfall == 100.0
        assert "wallet-123" in str(error)
        assert "100.0" in str(error)

    def test_error_unknown_shortfall(self):
        """Test error handles unknown shortfall."""
        error = InsufficientFundsError(
            wallet_id="wallet-123",
            shortfall="unknown",
            top_up_url="http://test/top-up",
        )

        assert error.shortfall is None
