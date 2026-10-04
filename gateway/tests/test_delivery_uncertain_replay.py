"""Test delivery_uncertain replay protection and reconciliation.

This test verifies the core economic-loop requirement from WEDGE.md:
"a genuinely ambiguous post-dispatch outcome becomes a distinct receipted state
rather than a silent redispatch", and that "replay of the same idempotency key
must not cause a second gateway dispatch or a second ledger debit."
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update as sa_update

from app.core.config import get_settings
from app.db.database import get_session_factory
from app.db.models import IdempotencyRecordModel
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.agent_money import get_agent_money
from app.services.idempotency import get_idempotency_service
from app.services.mcp_dispatch_attempts import (
    DISPATCH_CLAIMED,
    dispatch_reconciliation_idle_seconds,
    get_mcp_dispatch_attempt_service,
)
from app.services.service_registry import get_service_registry
from app.services.upstream_mcp import (
    UpstreamMcpDeliveryUncertainError,
    UpstreamMcpResult,
)
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet


def _upstream_result(payload: dict[str, Any]) -> UpstreamMcpResult:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return UpstreamMcpResult(
        payload=payload,
        canonical_json=canonical,
        response_hash=hashlib.sha256(canonical.encode()).hexdigest(),
        size_bytes=len(canonical.encode()),
        is_error=bool(payload.get("isError")),
    )


@dataclass
class AmbiguousExecutor:
    """Executor that always raises delivery_uncertain after the dispatch checkpoint."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    dispatch_count: int = 0

    async def call_tool(
        self,
        arguments: dict[str, Any],
        *,
        invocation_id: str,
        idempotency_key: str,
        before_dispatch: Callable[[], Awaitable[None]],
    ) -> UpstreamMcpResult:
        self.calls.append({"arguments": arguments, "idempotency_key": idempotency_key})
        await before_dispatch()
        self.dispatch_count += 1
        raise UpstreamMcpDeliveryUncertainError()


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as value:
        yield value


@pytest.mark.anyio
async def test_replay_after_dispatched_triggers_reconciliation(
    client: AsyncClient,
    clean_database,
) -> None:
    """Replay of a dispatched-but-incomplete attempt reconciles immediately.

    This tests the fix for the scenario where:
    1. First call marks attempt as dispatched
    2. Crash before idempotency.complete()
    3. Reconciler hasn't run yet
    4. Retry with same idempotency key should:
       - NOT raise IdempotencyInProgressError indefinitely
       - NOT redispatch
       - Trigger immediate reconciliation
       - Return the delivery_uncertain receipt
    """
    tool_name = "uncertain-replay-test"
    executor = AmbiguousExecutor()
    registry = get_service_registry()
    registry.register_upstream(
        service_id=tool_name,
        name="Uncertain Replay Test Tool",
        description="Test tool",
        category=ServiceCategory.AGENT_COMMS,
        executor=executor,
        input_schema={"type": "object", "properties": {"test": {"type": "string"}}},
        output_schema=None,
        credits_per_unit=2.0,
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
            idem_key="uncertain-replay-permit",
        )

        # Prepare the idempotency record and dispatch attempt as if a
        # crash happened right after claim_dispatch()
        idem_key = "uncertain-replay-invoke"
        request_payload = {
            "tool": tool_name,
            "arguments": {"test": "value"},
            "wallet_id": provisioned["agent_wallet_id"],
            "permit_id": permit["permit_id"],
        }

        # Create the idempotency record
        idem = get_idempotency_service()
        begun = await idem.begin_with_record(
            wallet_id=provisioned["agent_wallet_id"],
            endpoint="/mcp/invoke",
            idempotency_key=idem_key,
            request_payload=request_payload,
            operation_kind="upstream_mcp",
        )
        assert begun.replay is None

        # Create a dispatched attempt (simulating the crash scenario)
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
            credits_authorized=Decimal("2"),
        )
        assert validation.allowed
        assert attempt is not None

        # Attach charge (simulating successful charge before crash)
        # Default AGENT_COMMS pricing is 1.5 credits/unit, so 4/3 units = 2 credits
        money = get_agent_money()
        charge = await money.charge(
            wallet_id=provisioned["agent_wallet_id"],
            service_category=ServiceCategory.AGENT_COMMS,
            units=Decimal("4") / Decimal("3"),
            request_path="/test",
            operation_key=begun.record_id,
        )
        await dispatch.attach_charge(
            attempt_id=attempt.attempt_id,
            ledger_entry_id=charge.entry_id,
            credits_charged=Decimal("2"),
        )

        # Claim the one-shot send right (this is where the "crash" happens).
        attempt = await dispatch.claim_dispatch(attempt.attempt_id)
        assert attempt.state == DISPATCH_CLAIMED

        # Backdate beyond the configured live-call window so on-demand
        # reconciliation treats this attempt as abandoned rather than live.
        from datetime import timedelta

        from app.core.time import utc_now
        from app.db.database import get_session_factory as get_db_factory
        from app.db.models import McpDispatchAttemptModel

        settings = get_settings()
        idle_seconds = dispatch_reconciliation_idle_seconds(
            connect_timeout_seconds=settings.MCP_UPSTREAM_CONNECT_TIMEOUT_SECONDS,
            call_timeout_seconds=settings.MCP_UPSTREAM_CALL_TIMEOUT_SECONDS,
        )
        stale_timestamp = utc_now() - timedelta(seconds=idle_seconds + 1)
        db_factory = get_db_factory()
        async with db_factory() as session:
            await session.execute(
                sa_update(McpDispatchAttemptModel)
                .where(McpDispatchAttemptModel.attempt_id == attempt.attempt_id)
                .values(updated_at=stale_timestamp, dispatched_at=stale_timestamp)
            )
            await session.commit()

        # Verify the idempotency record is incomplete
        factory = get_session_factory()
        async with factory() as session:
            record = await session.get(IdempotencyRecordModel, begun.record_id)
            assert record is not None
            assert record.response_json is None

        # Now replay the same idempotency key - this should trigger on-demand
        # reconciliation rather than returning idempotency_in_progress.
        replayed = await idem.begin_with_record(
            wallet_id=provisioned["agent_wallet_id"],
            endpoint="/mcp/invoke",
            idempotency_key=idem_key,
            request_payload=request_payload,
            operation_kind="upstream_mcp",
        )

        # Should have replay data now (reconciliation completed)
        assert replayed.replay is not None
        assert replayed.replay.response_json is not None
        response = replayed.replay.response_json

        # Verify it's a delivery_uncertain response
        assert "error" in response
        assert response["error"] == "delivery_uncertain"
        assert response.get("receipt", {}).get("outcome") == "delivery_uncertain"
        assert Decimal(str(response["receipt"]["credits_charged"])) == Decimal("2")

        # Verify NO redispatch occurred (executor was never called in this test)
        assert executor.dispatch_count == 0, "Executor should not have been called"

        # Verify only one debit
        wallet = await money.get_wallet(provisioned["agent_wallet_id"])
        # Started with 1000, charged 2, so should be 998
        assert wallet.balance == Decimal("998")

    finally:
        registry.unregister_local(tool_name)


@pytest.mark.anyio
async def test_delivery_uncertain_replay_never_redispatches(
    client: AsyncClient,
    clean_database,
) -> None:
    """Replay of a completed delivery_uncertain never redispatches.

    This is the core economic invariant: once a dispatch is ambiguous
    and has been receipted, replay returns the same receipt without
    re-executing the tool.
    """
    tool_name = "no-redispatch-test"
    executor = AmbiguousExecutor()
    registry = get_service_registry()
    registry.register_upstream(
        service_id=tool_name,
        name="No Redispatch Test Tool",
        description="Test tool that always returns delivery_uncertain",
        category=ServiceCategory.AGENT_COMMS,
        executor=executor,
        input_schema={"type": "object", "properties": {"test": {"type": "string"}}},
        output_schema=None,
        credits_per_unit=2.0,
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
            idem_key="no-redispatch-permit",
        )

        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": {"test": "value"},
                "mcpContext": {
                    "wallet_id": provisioned["agent_wallet_id"],
                    "permit_id": permit["permit_id"],
                    "idempotency_key": "no-redispatch-invoke",
                },
            },
        }

        # First call - should result in delivery_uncertain
        first = await client.post(
            "/mcp/messages", json=body, headers=provisioned["agent_headers"]
        )
        assert first.status_code == 200
        first_data = first.json()
        assert "error" in first_data
        assert first_data["error"]["message"] == "delivery_uncertain"
        first_receipt = first_data["error"]["data"]["receipt"]
        assert first_receipt["outcome"] == "delivery_uncertain"

        # Verify one dispatch occurred
        assert executor.dispatch_count == 1

        # Replay - should return identical receipt without redispatching
        replay = await client.post(
            "/mcp/messages", json=body, headers=provisioned["agent_headers"]
        )
        assert replay.status_code == 200
        replay_data = replay.json()

        # Should be identical
        assert replay_data == first_data

        # Verify NO additional dispatch
        assert executor.dispatch_count == 1, "Should not redispatch on replay"

        # Verify only one debit
        ledger_response = await client.get(
            f"/v1/billing/ledger/{provisioned['agent_wallet_id']}",
            headers=provisioned["agent_headers"],
        )
        assert ledger_response.status_code == 200
        entries = ledger_response.json()["entries"]
        debits = [e for e in entries if e["action"] == "debit"]
        assert len(debits) == 1, "Should have exactly one debit"

    finally:
        registry.unregister_local(tool_name)
