"""Lost refund response recovery demo.

At 75278a8 there is no API or CLI that calls append_resolution, and the
reconciler never revisits a signed delivery_uncertain receipt; this test
exercises the service primitive directly.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.db.database import get_session_factory
from app.db.models import LedgerEntryModel, ReceiptModel, ReceiptResolutionModel
from app.services.receipt_resolutions import append_resolution
from app.services.receipts import get_receipt_service
from b2a_sdk.receipt_verifier import key_set_from_document, verify_bundle
from b2a_sdk.resolution_verifier import verify_resolution
from failure_lab.configurations import Configuration, LabEnvironment, configured_target
from failure_lab.faults import FaultMode, FaultPlan
from failure_lab.identity import OperationIdentity, new_business_operation_id
from failure_lab.refund_tool import RefundRequest
from tests.conftest import running_on_sqlite


pytestmark = pytest.mark.skipif(
    not running_on_sqlite(),
    reason="SQLite only; another job owns the shared Postgres instance",
)


@pytest.fixture
def restore_service_registry():
    from app.services import service_registry

    registry = service_registry._service_registry
    yield
    service_registry._service_registry = registry


def _reset_in_process_singletons() -> None:
    """Drop process-local service objects so the next call reloads from SQLite."""
    from app.core.config import get_settings
    from app.services import (
        agent_money,
        api_key_service,
        idempotency,
        mcp_dispatch_attempts,
        mcp_dispatch_reconciliation,
        permits,
        receipts,
        service_registry,
        signing_keys,
    )

    agent_money._agent_money = None
    api_key_service._api_key_service = None
    api_key_service.clear_verified_key_cache()
    idempotency._service = None
    mcp_dispatch_attempts._service = None
    mcp_dispatch_reconciliation._service = None
    permits._service = None
    receipts._service = None
    service_registry._service_registry = None
    signing_keys._signing_key_service = None
    get_settings.cache_clear()


async def _simulate_gateway_restart(target) -> None:
    """Rebuild in-process gateway state the way T11 recovery and T06 do.

    T11's restarted-gateway sweep is ``reconcile()``. T06 rebuilds the caller
    so nothing in-process carries over. Durable SQLite rows stay put.
    """
    gateway, _tenant, permit = target.require_gateway()
    await gateway.__aexit__(None, None, None)
    _reset_in_process_singletons()
    await gateway.__aenter__()
    await gateway.reconcile(idle_seconds=0)
    target.agent = target.gateway_agent(permit["permit_id"])


async def _portable_and_keys(gateway, tenant, receipt_id: str) -> tuple[dict, dict]:
    portable = await gateway.client.get(
        f"/v1/receipts/{receipt_id}/portable", headers=tenant.headers
    )
    assert portable.status_code == 200, portable.text
    keys = await gateway.client.get("/.well-known/trust-keys.json")
    assert keys.status_code == 200, keys.text
    return portable.json(), keys.json()


async def _receipt_fingerprint(receipt_id: str) -> tuple[str, str, Decimal, str]:
    async with get_session_factory()() as session:
        row = await session.get(ReceiptModel, receipt_id)
        assert row is not None
        return row.signature, row.outcome, row.credits_charged, row.signature_key_id


async def _wallet_refund_rows(wallet_id: str) -> list[LedgerEntryModel]:
    async with get_session_factory()() as session:
        rows = (
            (
                await session.execute(
                    select(LedgerEntryModel).where(
                        LedgerEntryModel.wallet_id == wallet_id
                    )
                )
            )
            .scalars()
            .all()
        )
    return [
        row
        for row in rows
        if str(row.action).lower() == "refund"
        or str(row.entry_id).startswith("refund-")
    ]


@pytest.mark.anyio
async def test_lost_refund_response_recovery_demo(
    tmp_path, clean_database, restore_service_registry
):
    """Upstream refund commits, response is lost, retry after restart is one effect."""
    from app.main import app

    env = LabEnvironment(run_dir=tmp_path, app=app, admin_api_key="test-key")
    async with configured_target(env, Configuration.GATEWAY_NAIVE) as target:
        gateway, tenant, permit = target.require_gateway()
        operation_id = new_business_operation_id("recovery-demo")
        identity = OperationIdentity.first_attempt(operation_id)
        refund = RefundRequest(
            operation_id=operation_id,
            customer_id="cus_recovery_demo",
            payment_id="pay_recovery_demo",
            amount=5000,
        )
        target.injector.arm(
            FaultPlan(
                mode=FaultMode.RESPONSE_LOST_AFTER_EXECUTION,
                operation_id=operation_id,
                hold_seconds=20,
            )
        )

        first = await target.agent.submit(identity, refund, timeout_seconds=8.0)
        assert first.status == "delivery_uncertain", first
        assert first.client_visible_state == "explicit_uncertain"
        assert first.receipt_id
        assert first.receipt is not None
        assert first.receipt["outcome"] == "delivery_uncertain"
        charged = Decimal(str(first.receipt["credits_charged"]))
        assert charged > 0
        valid, reason, _ = await get_receipt_service().verify_receipt(first.receipt_id)
        assert (valid, reason) == (True, None)
        snapshot = await gateway.snapshot(tenant)
        assert snapshot.sent_attempt_count == 1
        assert snapshot.debit_count == 1
        assert snapshot.refund_count == 0
        assert snapshot.receipt_outcomes() == ["delivery_uncertain"]
        assert target.injector.dispatch_count(operation_id) == 1
        portable_before, keys_before = await _portable_and_keys(
            gateway, tenant, first.receipt_id
        )
        assert verify_bundle(portable_before, key_set_from_document(keys_before)).ok
        fingerprint_before = await _receipt_fingerprint(first.receipt_id)

        await _simulate_gateway_restart(target)
        gateway, tenant, permit = target.require_gateway()
        assert permit["permit_id"]

        retry = await target.agent.submit(identity.retry(), refund, timeout_seconds=8.0)
        assert retry.status == "delivery_uncertain", retry
        assert retry.receipt_id == first.receipt_id
        assert target.injector.dispatch_count(operation_id) == 1
        snapshot = await gateway.snapshot(tenant)
        assert snapshot.debit_count == 1
        assert snapshot.refund_count == 0
        assert snapshot.receipt_outcomes() == ["delivery_uncertain"]

        crossings = target.injector.crossings(operation_id=operation_id)
        assert len(crossings) == 1
        forwarded_key = crossings[0].idempotency_key
        invocation_id = crossings[0].invocation_id
        assert forwarded_key == identity.idempotency_key
        effects = [
            record
            for record in target.ledger.effects()
            if record.idempotency_key == forwarded_key
            or (invocation_id is not None and record.request_id == invocation_id)
        ]
        assert len(effects) == 1
        assert target.ledger.execution_count(operation_id) == 1

        first_resolution = await append_resolution(
            first.receipt_id, "confirmed_success"
        )
        replayed_resolution = await append_resolution(
            first.receipt_id, "confirmed_success"
        )
        assert first_resolution.resolution_id == replayed_resolution.resolution_id
        assert first_resolution.signature == replayed_resolution.signature
        assert first_resolution.resolution_hash == replayed_resolution.resolution_hash
        assert first_resolution.sequence == 1

        fingerprint_after = await _receipt_fingerprint(first.receipt_id)
        assert fingerprint_after == fingerprint_before
        portable_after, keys_after = await _portable_and_keys(
            gateway, tenant, first.receipt_id
        )
        assert portable_after["signing_input"] == portable_before["signing_input"]
        assert portable_after["signature"] == portable_before["signature"]
        assert verify_bundle(portable_after, key_set_from_document(keys_after)).ok
        valid, reason, _ = await get_receipt_service().verify_receipt(first.receipt_id)
        assert (valid, reason) == (True, None)

        record_response = await gateway.client.get(
            f"/v1/receipts/{first.receipt_id}/resolution",
            headers=tenant.headers,
        )
        assert record_response.status_code == 200, record_response.text
        verdict = verify_resolution(
            record_response.json(),
            portable_after,
            key_set_from_document(keys_after),
        )
        assert verdict.ok, verdict.reason
        assert await _wallet_refund_rows(tenant.wallet_id) == []
        snapshot = await gateway.snapshot(tenant)
        assert snapshot.refund_count == 0
        async with get_session_factory()() as session:
            resolution_count = await session.scalar(
                select(func.count()).select_from(ReceiptResolutionModel)
            )
        assert int(resolution_count or 0) == 1
