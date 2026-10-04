"""Test that live prepared/claimed attempts with running owners wait, not steal.

Attempts inside the globally conservative reconciliation window are assumed to
have live owners and must not be stolen by concurrent requests. Only attempts
idle beyond that fixed window are reconciled.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update as sa_update

from app.core.config import get_settings
from app.core.time import utc_now
from app.db.database import get_session_factory
from app.db.models import McpDispatchAttemptModel
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.agent_money import get_agent_money
from app.services.idempotency import IdempotencyInProgressError, get_idempotency_service
from app.services.mcp_dispatch_attempts import (
    DISPATCH_CLAIMED,
    DISPATCH_PREPARED,
    dispatch_reconciliation_idle_seconds,
    get_mcp_dispatch_attempt_service,
)
from app.services.mcp_dispatch_reconciliation import (
    get_mcp_dispatch_reconciliation_service,
)
from app.services.service_registry import get_service_registry
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as value:
        yield value


class PausedUpstreamExecutor:
    """Test executor that pauses before actually executing, for concurrency tests."""

    def __init__(self) -> None:
        self.dispatched = asyncio.Event()
        self.release = asyncio.Event()
        self.dispatch_count = 0
        self.calls: list[dict] = []

    async def execute(self, invocation_id: str, arguments: dict) -> dict:
        self.dispatch_count += 1
        self.calls.append({"invocation_id": invocation_id, "arguments": arguments})
        self.dispatched.set()
        await self.release.wait()
        return {"result": "ok"}


@pytest.mark.anyio
async def test_live_prepared_attempt_waits_not_steal(
    client: AsyncClient,
    clean_database,
) -> None:
    """A live PREPARED attempt with a running owner must wait, not be stolen.

    Scenario: First request creates PREPARED attempt and is processing.
    Second concurrent request with same idempotency key arrives while first
    is still inside the global idle window. It must wait for the
    first request instead of stealing or reconciling the attempt.
    """
    tool_name = f"live-prepared-{uuid.uuid4().hex[:8]}"
    executor = PausedUpstreamExecutor()
    registry = get_service_registry()
    registry.register_upstream(
        service_id=tool_name,
        name="Live Prepared Test",
        description="Test tool for live owner wait",
        category=ServiceCategory.AGENT_COMMS,
        executor=executor,
        input_schema={"type": "object", "properties": {"test": {"type": "string"}}},
        output_schema=None,
        credits_per_unit=1.0,
        upstream_tool_name=tool_name,
        upstream_origin="https://test.example.com",
    )

    try:
        provisioned = await provision_agent_wallet(client)
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key=f"live-prepared-permit-{uuid.uuid4().hex[:8]}",
        )

        idem_key = f"live-prepared-{uuid.uuid4().hex[:8]}"
        request_payload = {
            "tool": tool_name,
            "arguments": {"test": "value"},
            "wallet_id": provisioned["agent_wallet_id"],
            "permit_id": permit["permit_id"],
        }

        # Create PREPARED attempt (simulating first request in progress)
        idem = get_idempotency_service()
        begun = await idem.begin_with_record(
            wallet_id=provisioned["agent_wallet_id"],
            endpoint="/mcp/invoke",
            idempotency_key=idem_key,
            request_payload=request_payload,
            operation_kind="upstream_mcp",
        )
        assert begun.replay is None

        dispatch = get_mcp_dispatch_attempt_service()
        validation, attempt = await dispatch.authorize_reserve_and_prepare(
            idempotency_record_id=begun.record_id,
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            key_id=provisioned["key_id"],
            public_tool_id=tool_name,
            upstream_tool_name=tool_name,
            upstream_origin="https://test.example.com",
            request_hash=begun.request_hash,
            credits_authorized=Decimal("1"),
        )
        assert validation.allowed
        assert attempt is not None
        assert attempt.state == DISPATCH_PREPARED

        # Second concurrent request arrives while first is still PREPARED (<300s old)
        # Should raise IdempotencyInProgressError (wait_timeout=0 default), not steal
        with pytest.raises(IdempotencyInProgressError) as exc_info:
            await idem.begin_with_record(
                wallet_id=provisioned["agent_wallet_id"],
                endpoint="/mcp/invoke",
                idempotency_key=idem_key,
                request_payload=request_payload,
                operation_kind="upstream_mcp",
            )
        assert "idempotency_in_progress" in str(exc_info.value)

        # Verify attempt is still PREPARED (not stolen/reconciled)
        factory = get_session_factory()
        async with factory() as session:
            fresh_attempt = await session.get(
                McpDispatchAttemptModel, attempt.attempt_id
            )
            assert fresh_attempt is not None
            assert fresh_attempt.state == DISPATCH_PREPARED

    finally:
        registry.unregister_local(tool_name)


@pytest.mark.parametrize(
    ("attempt_age_seconds", "call_timeout_seconds"),
    [(2, 30), (301, 600)],
    ids=["default-window", "long-timeout-window"],
)
@pytest.mark.anyio
async def test_live_claimed_attempt_inside_global_idle_window_waits_not_steal(
    client: AsyncClient,
    clean_database,
    monkeypatch: pytest.MonkeyPatch,
    attempt_age_seconds: int,
    call_timeout_seconds: float,
) -> None:
    """A claimed attempt inside the fixed maximum-lifetime window stays live."""
    settings = get_settings()
    monkeypatch.setattr(settings, "MCP_UPSTREAM_CONNECT_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(
        settings,
        "MCP_UPSTREAM_CALL_TIMEOUT_SECONDS",
        call_timeout_seconds,
    )
    idle_seconds = dispatch_reconciliation_idle_seconds(
        connect_timeout_seconds=settings.MCP_UPSTREAM_CONNECT_TIMEOUT_SECONDS,
        call_timeout_seconds=settings.MCP_UPSTREAM_CALL_TIMEOUT_SECONDS,
    )
    assert attempt_age_seconds < idle_seconds
    tool_name = f"live-dispatched-{uuid.uuid4().hex[:8]}"
    executor = PausedUpstreamExecutor()
    registry = get_service_registry()
    registry.register_upstream(
        service_id=tool_name,
        name="Live Dispatched Test",
        description="Test tool for live owner wait",
        category=ServiceCategory.AGENT_COMMS,
        executor=executor,
        input_schema={"type": "object", "properties": {"test": {"type": "string"}}},
        output_schema=None,
        credits_per_unit=1.0,
        upstream_tool_name=tool_name,
        upstream_origin="https://test.example.com",
    )

    try:
        provisioned = await provision_agent_wallet(client)
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key=f"live-dispatched-permit-{uuid.uuid4().hex[:8]}",
        )

        idem_key = f"live-dispatched-{uuid.uuid4().hex[:8]}"
        request_payload = {
            "tool": tool_name,
            "arguments": {"test": "value"},
            "wallet_id": provisioned["agent_wallet_id"],
            "permit_id": permit["permit_id"],
        }

        # Create DISPATCHED attempt (simulating first request dispatched and processing)
        idem = get_idempotency_service()
        begun = await idem.begin_with_record(
            wallet_id=provisioned["agent_wallet_id"],
            endpoint="/mcp/invoke",
            idempotency_key=idem_key,
            request_payload=request_payload,
            operation_kind="upstream_mcp",
        )
        assert begun.replay is None

        dispatch = get_mcp_dispatch_attempt_service()
        money = get_agent_money()

        validation, attempt = await dispatch.authorize_reserve_and_prepare(
            idempotency_record_id=begun.record_id,
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            key_id=provisioned["key_id"],
            public_tool_id=tool_name,
            upstream_tool_name=tool_name,
            upstream_origin="https://test.example.com",
            request_hash=begun.request_hash,
            credits_authorized=Decimal("1"),
        )
        assert validation.allowed
        assert attempt is not None

        # For this test, we don't need to charge/dispatch in the full real-world flow.
        # We just need a DISPATCHED attempt to exist so we can test staleness logic.
        # However, mark_dispatched requires ledger_entry_id to be set. So do a minimal charge.
        charge = await money.charge(
            wallet_id=provisioned["agent_wallet_id"],
            service_category=ServiceCategory.AGENT_COMMS,
            units=Decimal("1")
            / Decimal("1.5"),  # AGENT_COMMS pricing is 1.5 credits/unit
            request_path="/test",
            operation_key=begun.record_id,
        )
        await dispatch.attach_charge(
            attempt_id=attempt.attempt_id,
            ledger_entry_id=charge.entry_id,
            credits_charged=Decimal("1"),
        )

        # Claim the one-shot send right.
        attempt = await dispatch.claim_dispatch(attempt.attempt_id)
        assert attempt.state == DISPATCH_CLAIMED

        backdated = utc_now() - timedelta(seconds=attempt_age_seconds)
        factory = get_session_factory()
        async with factory() as session:
            await session.execute(
                sa_update(McpDispatchAttemptModel)
                .where(McpDispatchAttemptModel.attempt_id == attempt.attempt_id)
                .values(updated_at=backdated)
            )
            await session.commit()

        # Second concurrent request arrives while first is still processing
        # and the global idle window still considers the owner live.
        # Should raise IdempotencyInProgressError (wait_timeout=0), not steal
        with pytest.raises(IdempotencyInProgressError) as exc_info:
            await idem.begin_with_record(
                wallet_id=provisioned["agent_wallet_id"],
                endpoint="/mcp/invoke",
                idempotency_key=idem_key,
                request_payload=request_payload,
                operation_kind="upstream_mcp",
            )
        assert "idempotency_in_progress" in str(exc_info.value)

        await get_mcp_dispatch_reconciliation_service().reconcile_attempt(
            attempt.attempt_id,
            idle_seconds=idle_seconds,
        )

        # Verify the live claim was not stolen or reconciled to uncertainty.
        async with factory() as session:
            fresh_attempt = await session.get(
                McpDispatchAttemptModel, attempt.attempt_id
            )
            assert fresh_attempt is not None
            assert fresh_attempt.state == DISPATCH_CLAIMED

    finally:
        registry.unregister_local(tool_name)
