"""Focused tests for the public behavior of b2a_sdk.decorators."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from b2a_sdk import B2AClient, InsufficientFundsError
from b2a_sdk.decorators import (
    _extract_schema_from_func,
    _registration_callbacks,
    billable,
    combined,
    mcp_tool,
    monitored,
    register_mcp_tool_callback,
)


@pytest.fixture
def mock_client():
    client = MagicMock(spec=B2AClient)
    client.telemetry = AsyncMock()
    client.charge = AsyncMock()
    return client


@pytest.fixture
def clean_callbacks():
    saved = list(_registration_callbacks)
    _registration_callbacks.clear()
    yield _registration_callbacks
    _registration_callbacks.clear()
    _registration_callbacks.extend(saved)


async def _flush_telemetry():
    await asyncio.sleep(0)


class TestMonitored:
    @pytest.mark.asyncio
    async def test_async_success_returns_value_and_reports_telemetry(self, mock_client):
        @monitored(mock_client, service_name="svc")
        async def run():
            return "ok"

        assert await run() == "ok"
        await _flush_telemetry()
        mock_client.telemetry.assert_awaited_once()
        call_kwargs = mock_client.telemetry.call_args.kwargs
        assert call_kwargs["source"] == "svc"
        assert call_kwargs["severity"] == "info"
        assert call_kwargs["status"] == "success"
        assert call_kwargs["function"] == "run"

    @pytest.mark.asyncio
    async def test_async_error_reraises_and_omits_message_by_default(self, mock_client):
        @monitored(mock_client, service_name="svc")
        async def run():
            raise ValueError("secret-token-abc")

        with pytest.raises(ValueError, match="secret-token-abc"):
            await run()
        await _flush_telemetry()
        call_kwargs = mock_client.telemetry.call_args.kwargs
        assert call_kwargs["event_type"] == "error"
        assert call_kwargs["error_type"] == "ValueError"
        assert call_kwargs["message"] == "Error in run"
        assert "secret-token-abc" not in repr(call_kwargs)

    @pytest.mark.asyncio
    async def test_capture_args_records_arguments(self, mock_client):
        @monitored(mock_client, service_name="svc", capture_args=True)
        async def run(job_id, *, verbose=False):
            return job_id

        assert await run("42", verbose=True) == "42"
        await _flush_telemetry()
        call_kwargs = mock_client.telemetry.call_args.kwargs
        assert "42" in call_kwargs["args"]
        assert call_kwargs["kwargs"] == {"verbose": "True"}

    def test_sync_without_running_loop_still_returns_value(self, mock_client):
        @monitored(mock_client, service_name="svc")
        def add(a, b):
            return a + b

        assert add(2, 3) == 5

    def test_sync_error_reraises_original_exception(self, mock_client):
        @monitored(mock_client, service_name="svc")
        def explode():
            raise ValueError("original failure")

        with pytest.raises(ValueError, match="original failure"):
            explode()

    def test_preserves_function_name_and_doc(self, mock_client):
        @monitored(mock_client, service_name="svc")
        async def run():
            """Does the thing."""
            return 1

        assert run.__name__ == "run"
        assert run.__doc__ == "Does the thing."


class TestBillable:
    @pytest.mark.asyncio
    async def test_charges_before_running(self, mock_client):
        @billable(mock_client, wallet_id="w-1", service_category="cat", units=5.0)
        async def run():
            return "ok"

        assert await run() == "ok"
        mock_client.charge.assert_awaited_once()
        call_kwargs = mock_client.charge.call_args.kwargs
        assert call_kwargs["wallet_id"] == "w-1"
        assert call_kwargs["service_category"] == "cat"
        assert call_kwargs["units"] == 5.0

    @pytest.mark.asyncio
    async def test_charge_failure_skips_function(self, mock_client):
        mock_client.charge.side_effect = InsufficientFundsError(
            wallet_id="w-1",
            shortfall=10.0,
            top_up_url="http://test/top-up",
        )
        ran = False

        @billable(mock_client, wallet_id="w-1", service_category="cat")
        async def run():
            nonlocal ran
            ran = True

        with pytest.raises(InsufficientFundsError):
            await run()
        assert ran is False

    def test_sync_function_is_rejected(self, mock_client):
        @billable(mock_client, wallet_id="w-1", service_category="cat")
        def run():
            return "ok"

        with pytest.raises(RuntimeError, match="requires an async function"):
            run()


class TestCombined:
    @pytest.mark.asyncio
    async def test_charges_and_reports_telemetry(self, mock_client):
        @combined(
            mock_client,
            wallet_id="w-1",
            service_category="cat",
            service_name="svc",
        )
        async def run():
            return "ok"

        assert await run() == "ok"
        mock_client.charge.assert_awaited_once()
        await _flush_telemetry()
        mock_client.telemetry.assert_awaited_once()


class TestMcpTool:
    @pytest.mark.asyncio
    async def test_metadata_attached_and_result_preserved(self, clean_callbacks):
        @mcp_tool(
            service_id="gen",
            name="Generator",
            description="Makes things",
            category="cat",
            credits_per_unit=2.0,
        )
        async def generate(url: str, style: str = "cinematic"):
            """Docstring description."""
            return {"url": url, "style": style}

        assert await generate("x", style="noir") == {"url": "x", "style": "noir"}
        metadata = generate._b2a_mcp_metadata
        assert metadata["service_id"] == "gen"
        assert metadata["name"] == "Generator"
        assert metadata["description"] == "Makes things"
        assert metadata["credits_per_unit"] == 2.0
        assert metadata["input_schema"]["properties"].keys() == {"url", "style"}
        assert metadata["input_schema"]["required"] == ["url"]

    @pytest.mark.asyncio
    async def test_notifies_registered_callback(self, clean_callbacks):
        seen = []
        register_mcp_tool_callback(
            lambda service_id, func, input_schema, output_schema: seen.append(
                (service_id, func.__name__)
            )
        )

        @mcp_tool(service_id="gen")
        async def generate(url: str):
            return url

        assert await generate("x") == "x"
        assert seen == [("gen", "generate")]

    def test_failing_callback_does_not_break_registration(self, clean_callbacks):
        def bad_callback(*args, **kwargs):
            raise RuntimeError("callback blew up")

        seen = []
        register_mcp_tool_callback(bad_callback)
        register_mcp_tool_callback(
            lambda service_id, func, input_schema, output_schema: seen.append(service_id)
        )

        @mcp_tool(service_id="gen")
        async def generate(url: str):
            return url

        assert generate._b2a_mcp_metadata["service_id"] == "gen"
        assert seen == ["gen"]

    def test_schema_extraction_skips_self_and_empty_params(self):
        class Tool:
            def method(self, needle: int, haystack: str = "h"):
                return needle

        input_schema, _ = _extract_schema_from_func(Tool.method)
        assert set(input_schema["properties"]) == {"needle", "haystack"}
        assert input_schema["required"] == ["needle"]

        assert _extract_schema_from_func(lambda: None) == (None, None)
