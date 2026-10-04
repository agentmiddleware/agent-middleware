from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any, Literal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.routers import mcp as mcp_router
from app.core.time import utc_now
from app.db.database import get_session_factory
from app.db.models import (
    ControlPlaneAuditEventModel,
    HumanApprovalModel,
    IdempotencyRecordModel,
    LedgerEntryModel,
    McpDispatchAttemptModel,
    PermitModel,
    ReceiptModel,
    WalletModel,
)
from app.main import app
from app.routers.mcp import ToolCallRequest
from app.schemas.billing import ServiceCategory
from app.services.agent_money import get_agent_money
from app.services.idempotency import (
    GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
    get_idempotency_service,
)
from app.services.mcp_dispatch_attempts import (
    DispatchPrepareCommitUncertainError,
    DispatchPrepareRolledBackError,
    McpDispatchAttemptService,
)
from app.services.mcp_dispatch_reconciliation import (
    McpDispatchReconciliationService,
    get_mcp_dispatch_reconciliation_service,
)
from app.services.permits import get_permit_service
from app.services.quotes import get_quote_service
from app.services.receipts import ReceiptService, get_receipt_service
from app.services.service_registry import get_service_registry
from app.services.signing_keys import sha256_hex
from app.services.upstream_mcp import (
    UpstreamMcpDeliveryUncertainError,
    UpstreamMcpPreDispatchError,
    UpstreamMcpResponseRejectedError,
    UpstreamMcpResult,
    UpstreamMcpReturnedError,
)
from tests.test_trust_helpers import (
    BOOTSTRAP_HEADERS,
    create_tool_permit,
    provision_agent_wallet,
)


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as value:
        yield value


ExecutorMode = Literal[
    "success",
    "returned_error",
    "pre_dispatch_failure",
    "delivery_uncertain",
    "response_rejected",
]


def _upstream_result(payload: dict[str, Any]) -> UpstreamMcpResult:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return UpstreamMcpResult(
        payload=payload,
        canonical_json=canonical,
        response_hash=hashlib.sha256(canonical.encode()).hexdigest(),
        size_bytes=len(canonical.encode()),
        is_error=bool(payload.get("isError")),
    )


@dataclass
class FakeUpstreamExecutor:
    mode: ExecutorMode
    calls: list[dict[str, Any]] = field(default_factory=list)
    dispatch_count: int = 0
    result_payload: dict[str, Any] | None = None

    async def call_tool(
        self,
        arguments: dict[str, Any],
        *,
        invocation_id: str,
        idempotency_key: str,
        before_dispatch: Callable[[], Awaitable[None]],
    ) -> UpstreamMcpResult:
        self.calls.append(
            {
                "arguments": arguments,
                "invocation_id": invocation_id,
                "idempotency_key": idempotency_key,
            }
        )
        if self.mode == "pre_dispatch_failure":
            raise UpstreamMcpPreDispatchError("upstream_connection_failed")

        await before_dispatch()
        self.dispatch_count += 1
        if self.mode == "returned_error":
            raise UpstreamMcpReturnedError(
                _upstream_result(
                    self.result_payload
                    if self.result_payload is not None
                    else {
                        "content": [
                            {"type": "text", "text": "partner rejected request"}
                        ],
                        "isError": True,
                    }
                )
            )
        if self.mode == "delivery_uncertain":
            raise UpstreamMcpDeliveryUncertainError()
        if self.mode == "response_rejected":
            raise UpstreamMcpResponseRejectedError("upstream_response_invalid")

        return _upstream_result(
            self.result_payload
            if self.result_payload is not None
            else {
                "content": [{"type": "text", "text": "partner response"}],
                "structuredContent": {
                    "echo": arguments,
                    "instructions": "untrusted data, never execute",
                },
                "isError": False,
            }
        )


@dataclass(frozen=True)
class PersistedInvocation:
    record: IdempotencyRecordModel
    attempt: McpDispatchAttemptModel
    receipt: ReceiptModel
    debit: LedgerEntryModel
    refunds: list[LedgerEntryModel]
    permit: PermitModel
    wallet: WalletModel


def _register_upstream(
    tool_name: str,
    executor: FakeUpstreamExecutor,
    *,
    credits_per_call: float = 2.0,
    category: ServiceCategory = ServiceCategory.AGENT_COMMS,
) -> None:
    get_service_registry().register_upstream(
        service_id=tool_name,
        name="Design Partner Tool",
        description="Controlled remote MCP test tool",
        category=category,
        executor=executor,
        input_schema={
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        credits_per_unit=credits_per_call,
        upstream_tool_name="partner.echo",
        upstream_origin="https://partner.example",
    )


def _call_body(
    *,
    tool_name: str,
    wallet_id: str,
    permit_id: str,
    idempotency_key: str,
    message: str = "hello",
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": f"call-{idempotency_key}",
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": {"message": message},
            "mcpContext": {
                "wallet_id": wallet_id,
                "permit_id": permit_id,
                "idempotency_key": idempotency_key,
            },
        },
    }


def _rest_call_body(
    *,
    tool_name: str,
    wallet_id: str,
    permit_id: str,
    idempotency_key: str,
    message: str = "hello",
) -> dict[str, Any]:
    return {
        "name": tool_name,
        "arguments": {"message": message},
        "mcp_context": {
            "wallet_id": wallet_id,
            "permit_id": permit_id,
            "idempotency_key": idempotency_key,
        },
    }


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["jsonrpc", "rest"])
async def test_remote_quote_consume_loss_preserves_legacy_denial_without_leaks(
    client: AsyncClient,
    clean_database: None,
    monkeypatch: pytest.MonkeyPatch,
    transport: Literal["jsonrpc", "rest"],
) -> None:
    provisioned = await provision_agent_wallet(client)
    tool_name = f"partner-upstream-quote-consume-loss-{transport}"
    idempotency_key = f"partner-upstream-quote-consume-loss-{transport}-1"
    executor = FakeUpstreamExecutor("success")
    _register_upstream(tool_name, executor)
    quote_service = get_quote_service()

    async def lose_quote_consume(
        quote_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> bool:
        del quote_id, idempotency_key
        return False

    monkeypatch.setattr(quote_service, "consume", lose_quote_consume)
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key=f"partner-upstream-quote-consume-loss-permit-{transport}",
        )
        quote = await quote_service.create_quote(
            wallet_id=provisioned["agent_wallet_id"],
            tool=tool_name,
            quoted_credits=Decimal("2"),
            category=ServiceCategory.AGENT_COMMS.value,
        )
        if transport == "jsonrpc":
            body = _call_body(
                tool_name=tool_name,
                wallet_id=provisioned["agent_wallet_id"],
                permit_id=permit["permit_id"],
                idempotency_key=idempotency_key,
            )
            body["params"]["mcpContext"]["quote_id"] = quote.quote_id
            denied = await client.post(
                "/mcp/messages",
                json=body,
                headers=provisioned["agent_headers"],
            )
            assert denied.status_code == 200
            assert denied.json()["error"] == {
                "code": -32003,
                "message": "quote_already_consumed",
            }
        else:
            body = _rest_call_body(
                tool_name=tool_name,
                wallet_id=provisioned["agent_wallet_id"],
                permit_id=permit["permit_id"],
                idempotency_key=idempotency_key,
            )
            body["mcp_context"]["quote_id"] = quote.quote_id
            denied = await client.post(
                f"/mcp/tools/{tool_name}/invoke",
                json=body,
                headers=provisioned["agent_headers"],
            )
            assert denied.status_code == 403
            assert denied.json() == {"detail": "quote_already_consumed"}
        assert executor.calls == []

        factory = get_session_factory()
        async with factory() as session:
            record = (
                await session.execute(
                    select(IdempotencyRecordModel).where(
                        IdempotencyRecordModel.idempotency_key == idempotency_key
                    )
                )
            ).scalar_one()
            stored_response = record.response_json
            attempt_count = await session.scalar(
                select(func.count())
                .select_from(McpDispatchAttemptModel)
                .where(
                    McpDispatchAttemptModel.idempotency_record_id == record.record_id
                )
            )
            ledger_count = await session.scalar(
                select(func.count())
                .select_from(LedgerEntryModel)
                .where(LedgerEntryModel.operation_key == record.record_id)
            )
            receipt_count = await session.scalar(
                select(func.count())
                .select_from(ReceiptModel)
                .where(ReceiptModel.idempotency_record_id == record.record_id)
            )
            stored_permit = await session.get(PermitModel, permit["permit_id"])
        assert int(attempt_count or 0) == 0
        assert int(ledger_count or 0) == 0
        assert int(receipt_count or 0) == 0
        assert stored_permit is not None
        assert stored_permit.spent_credits == Decimal("0")

        # A later, unrelated reservation must survive repeated sweeps. The
        # quote-loser left no attempt for reconciliation to refund twice or to
        # convert into a new receipt-bearing public response.
        await get_permit_service().reserve_budget(permit["permit_id"], Decimal("2"))
        first = await get_mcp_dispatch_reconciliation_service().reconcile(
            idle_seconds=0,
            terminal_idle_seconds=0,
        )
        second = await get_mcp_dispatch_reconciliation_service().reconcile(
            idle_seconds=0,
            terminal_idle_seconds=0,
        )
        assert first.repaired == 0
        assert second.repaired == 0

        async with factory() as session:
            record = await session.get(IdempotencyRecordModel, record.record_id)
            stored_permit = await session.get(PermitModel, permit["permit_id"])
            receipt_count = await session.scalar(
                select(func.count())
                .select_from(ReceiptModel)
                .where(ReceiptModel.idempotency_record_id == record.record_id)
            )
        assert record is not None and record.response_json == stored_response
        assert stored_permit is not None
        assert stored_permit.spent_credits == Decimal("2")
        assert int(receipt_count or 0) == 0
    finally:
        get_service_registry().unregister_local(tool_name)


def _legacy_transport_identity(
    *,
    transport: Literal["jsonrpc", "rest"],
    body: dict[str, Any],
    tool_name: str,
) -> tuple[str, dict[str, Any]]:
    if transport == "jsonrpc":
        return "/mcp/messages", body
    return (
        f"/mcp/tools/{tool_name}/invoke",
        ToolCallRequest.model_validate(body).model_dump(mode="json"),
    )


async def _seed_completed_legacy_idempotency(
    *,
    wallet_id: str,
    endpoint: str,
    idempotency_key: str,
    request_payload: dict[str, Any],
    response_payload: dict[str, Any],
) -> None:
    record_id = (
        "idm-legacy-"
        + hashlib.sha256(
            f"{wallet_id}:{endpoint}:{idempotency_key}".encode()
        ).hexdigest()[:16]
    )
    factory = get_session_factory()
    async with factory() as session:
        session.add(
            IdempotencyRecordModel(
                record_id=record_id,
                wallet_id=wallet_id,
                endpoint=endpoint,
                idempotency_key=idempotency_key,
                request_hash=sha256_hex(request_payload),
                response_json=json.dumps(response_payload),
                response_reference=response_payload.get("receipt", {}).get(
                    "receipt_id"
                ),
                status_code=200,
            )
        )
        await session.commit()


async def _wallet_debit_and_canonical_counts(
    *,
    wallet_id: str,
    idempotency_key: str,
) -> tuple[int, int]:
    factory = get_session_factory()
    async with factory() as session:
        debit_count = await session.scalar(
            select(func.count())
            .select_from(LedgerEntryModel)
            .where(
                LedgerEntryModel.wallet_id == wallet_id,
                LedgerEntryModel.action == "debit",
            )
        )
        canonical_count = await session.scalar(
            select(func.count())
            .select_from(IdempotencyRecordModel)
            .where(
                IdempotencyRecordModel.wallet_id == wallet_id,
                IdempotencyRecordModel.endpoint == GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
                IdempotencyRecordModel.idempotency_key == idempotency_key,
            )
        )
    return int(debit_count or 0), int(canonical_count or 0)


async def _load_persisted_invocation(
    *,
    wallet_id: str,
    permit_id: str,
    idempotency_key: str,
) -> PersistedInvocation:
    factory = get_session_factory()
    async with factory() as session:
        record = (
            await session.execute(
                select(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.wallet_id == wallet_id,
                    IdempotencyRecordModel.endpoint
                    == GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
                    IdempotencyRecordModel.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one()
        attempt = (
            await session.execute(
                select(McpDispatchAttemptModel).where(
                    McpDispatchAttemptModel.idempotency_record_id == record.record_id
                )
            )
        ).scalar_one()
        receipt = (
            await session.execute(
                select(ReceiptModel).where(
                    ReceiptModel.idempotency_record_id == record.record_id
                )
            )
        ).scalar_one()
        debit = (
            await session.execute(
                select(LedgerEntryModel).where(
                    LedgerEntryModel.wallet_id == wallet_id,
                    LedgerEntryModel.operation_key == record.record_id,
                )
            )
        ).scalar_one()
        refunds = list(
            (
                await session.execute(
                    select(LedgerEntryModel).where(
                        LedgerEntryModel.wallet_id == wallet_id,
                        LedgerEntryModel.action == "refund",
                        LedgerEntryModel.correlation_id == debit.entry_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        permit = await session.get(PermitModel, permit_id)
        wallet = await session.get(WalletModel, wallet_id)

    assert permit is not None
    assert wallet is not None
    return PersistedInvocation(
        record=record,
        attempt=attempt,
        receipt=receipt,
        debit=debit,
        refunds=refunds,
        permit=permit,
        wallet=wallet,
    )


def _assert_linked_terminal_state(
    persisted: PersistedInvocation,
    *,
    state: str,
    outcome: str,
    charged: Decimal,
    refunded: bool,
) -> None:
    assert persisted.attempt.state == state
    assert persisted.attempt.ledger_entry_id == persisted.debit.entry_id
    assert persisted.debit.operation_key == persisted.record.record_id
    assert persisted.debit.action == "debit"
    assert persisted.receipt.idempotency_record_id == persisted.record.record_id
    assert persisted.receipt.dispatch_attempt_id == persisted.attempt.attempt_id
    assert persisted.receipt.ledger_entry_id == persisted.debit.entry_id
    assert persisted.receipt.outcome == outcome
    assert persisted.receipt.credits_charged == charged
    assert persisted.receipt.response_hash == persisted.attempt.response_hash
    assert len(persisted.refunds) == int(refunded)
    assert persisted.permit.spent_credits == charged
    assert persisted.wallet.balance == Decimal("1000") - charged


@pytest.mark.anyio
async def test_governed_upstream_success_replays_without_second_debit_or_dispatch(
    client: AsyncClient,
    clean_database: None,
) -> None:
    provisioned = await provision_agent_wallet(client)
    tool_name = "partner-upstream-success"
    idempotency_key = "partner-upstream-success-1"
    executor = FakeUpstreamExecutor("success")
    _register_upstream(tool_name, executor)
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key="partner-upstream-success-permit",
        )
        body = _call_body(
            tool_name=tool_name,
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
        )

        first = await client.post(
            "/mcp/messages", json=body, headers=provisioned["agent_headers"]
        )
        replay = await client.post(
            "/mcp/messages", json=body, headers=provisioned["agent_headers"]
        )

        assert first.status_code == 200
        assert replay.status_code == 200
        assert replay.json()["result"] == first.json()["result"]
        result = first.json()["result"]
        assert result["isError"] is False
        assert result["content"] == [{"type": "text", "text": "partner response"}]
        assert result["structuredContent"]["echo"] == {"message": "hello"}
        assert executor.dispatch_count == 1
        assert len(executor.calls) == 1

        persisted = await _load_persisted_invocation(
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
        )
        _assert_linked_terminal_state(
            persisted,
            state="succeeded",
            outcome="success",
            charged=Decimal("2"),
            refunded=False,
        )
        assert executor.calls[0]["invocation_id"] == persisted.record.record_id
        assert executor.calls[0]["idempotency_key"] == idempotency_key
        assert result["receipt"]["receipt_id"] == persisted.receipt.receipt_id
        assert result["receipt"]["dispatch_attempt_id"] == (
            persisted.attempt.attempt_id
        )
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_approval_required_upstream_crash_reconciliation_keeps_approval(
    client: AsyncClient,
    clean_database: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mcp_router.settings,
        "SIMULATION_MODE_HUMAN_APPROVAL",
        True,
    )
    provisioned = await provision_agent_wallet(client)
    tool_name = "partner-upstream-approved-crash"
    idempotency_key = "partner-upstream-approved-crash-1"
    executor = FakeUpstreamExecutor("success")
    original_create_receipt = ReceiptService.create_receipt
    receipt_crashed = False

    async def crash_first_success_receipt(
        self: ReceiptService,
        *args: Any,
        **kwargs: Any,
    ):
        nonlocal receipt_crashed
        if (
            not receipt_crashed
            and kwargs.get("dispatch_attempt_id") is not None
            and kwargs.get("outcome") == "success"
        ):
            receipt_crashed = True
            raise RuntimeError("simulated_receipt_crash")
        return await original_create_receipt(self, *args, **kwargs)

    monkeypatch.setattr(ReceiptService, "create_receipt", crash_first_success_receipt)
    _register_upstream(tool_name, executor)
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key="partner-upstream-approved-crash-permit",
            requires_human_approval=True,
        )
        body = _call_body(
            tool_name=tool_name,
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
        )

        failed = await client.post(
            "/mcp/messages",
            json=body,
            headers=provisioned["agent_headers"],
        )
        # A receipt-store crash is an unclassified failure: the caller gets the
        # fixed internal_error and a correlation id, never the exception text.
        failed_error = failed.json()["error"]
        assert failed_error["code"] == -32603
        assert failed_error["message"] == "internal_error"
        assert failed_error["data"]["correlation_id"]
        assert "simulated_receipt_crash" not in failed.text
        assert receipt_crashed is True
        assert executor.dispatch_count == 1

        factory = get_session_factory()
        async with factory() as session:
            record = (
                await session.execute(
                    select(IdempotencyRecordModel).where(
                        IdempotencyRecordModel.wallet_id
                        == provisioned["agent_wallet_id"],
                        IdempotencyRecordModel.idempotency_key == idempotency_key,
                    )
                )
            ).scalar_one()
            attempt = (
                await session.execute(
                    select(McpDispatchAttemptModel).where(
                        McpDispatchAttemptModel.idempotency_record_id
                        == record.record_id
                    )
                )
            ).scalar_one()
            approval = await session.get(HumanApprovalModel, attempt.approval_id)
            receipt_count = await session.scalar(
                select(func.count())
                .select_from(ReceiptModel)
                .where(ReceiptModel.idempotency_record_id == record.record_id)
            )
        assert attempt.state == "succeeded"
        assert attempt.approval_id is not None
        assert approval is not None and approval.status == "consumed"
        assert int(receipt_count or 0) == 0

        await get_mcp_dispatch_reconciliation_service().reconcile_attempt(
            attempt.attempt_id
        )
        replay = await client.post(
            "/mcp/messages",
            json=body,
            headers=provisioned["agent_headers"],
        )

        assert replay.status_code == 200
        assert replay.json()["result"]["receipt"]["approval_id"] == (
            attempt.approval_id
        )
        assert executor.dispatch_count == 1
        persisted = await _load_persisted_invocation(
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
        )
        assert persisted.attempt.approval_id == attempt.approval_id
        assert persisted.receipt.approval_id == attempt.approval_id
        valid, reason, _ = await get_receipt_service().verify_receipt(
            persisted.receipt.receipt_id
        )
        assert (valid, reason) == (True, None)
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_direct_finalizer_and_reconciler_share_one_dispatch_audit(
    client: AsyncClient,
    clean_database: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provisioned = await provision_agent_wallet(client)
    tool_name = "partner-upstream-audit-race"
    idempotency_key = "partner-upstream-audit-race-1"
    executor = FakeUpstreamExecutor("success")
    _register_upstream(tool_name, executor)
    request_task: asyncio.Task[Any] | None = None
    both_observed_missing = asyncio.Event()
    direct_waiting = asyncio.Event()
    missing_observations = 0
    observed_attempt_id: str | None = None
    original_find = McpDispatchReconciliationService._find_existing_audit

    async def pause_after_missing_audit(self, context):  # noqa: ANN001, ANN202
        nonlocal missing_observations, observed_attempt_id
        existing = await original_find(self, context)
        if existing is None:
            missing_observations += 1
            observed_attempt_id = context.attempt.attempt_id
            if missing_observations == 1:
                direct_waiting.set()
            if missing_observations == 2:
                both_observed_missing.set()
            await asyncio.wait_for(both_observed_missing.wait(), timeout=5)
        return existing

    monkeypatch.setattr(
        McpDispatchReconciliationService,
        "_find_existing_audit",
        pause_after_missing_audit,
    )
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key="partner-upstream-audit-race-permit",
        )
        body = _call_body(
            tool_name=tool_name,
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
        )
        request_task = asyncio.create_task(
            client.post(
                "/mcp/messages",
                json=body,
                headers=provisioned["agent_headers"],
            )
        )
        await asyncio.wait_for(direct_waiting.wait(), timeout=5)
        assert observed_attempt_id is not None

        response, _ = await asyncio.wait_for(
            asyncio.gather(
                request_task,
                get_mcp_dispatch_reconciliation_service().reconcile_attempt(
                    observed_attempt_id
                ),
            ),
            timeout=10,
        )

        assert response.status_code == 200
        persisted = await _load_persisted_invocation(
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
        )
        factory = get_session_factory()
        async with factory() as session:
            audits = (
                (
                    await session.execute(
                        select(ControlPlaneAuditEventModel).where(
                            ControlPlaneAuditEventModel.request_id
                            == persisted.attempt.attempt_id,
                            ControlPlaneAuditEventModel.event == "mcp.invoke",
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert missing_observations == 2
        assert len(audits) == 1
        assert persisted.receipt.audit_event_id == audits[0].event_id
        assert audits[0].event_id == (
            f"audit-dsp-{sha256_hex(persisted.attempt.attempt_id)[:32]}"
        )
        assert audits[0].created_at == persisted.attempt.completed_at
        assert audits[0].auth_source == "governed_dispatch"
    finally:
        both_observed_missing.set()
        if request_task is not None and not request_task.done():
            await asyncio.gather(request_task, return_exceptions=True)
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_unicode_response_uses_the_adapter_byte_limit_when_persisted(
    client: AsyncClient,
    clean_database: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provisioned = await provision_agent_wallet(client)
    tool_name = "partner-upstream-unicode-result"
    idempotency_key = "partner-upstream-unicode-result-1"
    payload = {
        "content": [{"type": "text", "text": "伙伴响应 👋"}],
        "structuredContent": {"city": "München", "status": "成功"},
        "isError": False,
    }
    upstream_result = _upstream_result(payload)
    ascii_escaped_size = len(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    assert ascii_escaped_size > upstream_result.size_bytes
    monkeypatch.setattr(
        mcp_router.settings,
        "MCP_UPSTREAM_MAX_RESPONSE_BYTES",
        upstream_result.size_bytes,
    )
    executor = FakeUpstreamExecutor("success", result_payload=payload)
    _register_upstream(tool_name, executor)
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key="partner-upstream-unicode-result-permit",
        )
        body = _call_body(
            tool_name=tool_name,
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
        )

        first = await client.post(
            "/mcp/messages", json=body, headers=provisioned["agent_headers"]
        )
        replay = await client.post(
            "/mcp/messages", json=body, headers=provisioned["agent_headers"]
        )

        assert first.status_code == 200
        assert first.json()["result"]["content"] == payload["content"]
        assert replay.json()["result"] == first.json()["result"]
        assert executor.dispatch_count == 1
        persisted = await _load_persisted_invocation(
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
        )
        _assert_linked_terminal_state(
            persisted,
            state="succeeded",
            outcome="success",
            charged=Decimal("2"),
            refunded=False,
        )
        assert persisted.attempt.result_json == upstream_result.canonical_json
        assert persisted.attempt.result_size_bytes == upstream_result.size_bytes
        assert persisted.attempt.response_hash == upstream_result.response_hash
        await get_mcp_dispatch_reconciliation_service().reconcile_attempt(
            persisted.attempt.attempt_id
        )
        valid, reason, _ = await get_receipt_service().verify_receipt(
            persisted.receipt.receipt_id
        )
        assert (valid, reason) == (True, None)
        evidence = await client.get(
            f"/v1/receipts/{persisted.receipt.receipt_id}/evidence",
            headers=provisioned["agent_headers"],
        )
        assert evidence.status_code == 200
        assert evidence.json()["valid"] is True
        checks = {check["name"]: check for check in evidence.json()["checks"]}
        assert checks["dispatch_linkage"]["status"] == "passed"
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_confirmed_result_rejected_by_persistence_is_terminal_and_replayable(
    client: AsyncClient,
    clean_database: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provisioned = await provision_agent_wallet(client)
    tool_name = "partner-upstream-persistence-rejected"
    idempotency_key = "partner-upstream-persistence-rejected-1"
    monkeypatch.setattr(
        mcp_router.settings,
        "MCP_UPSTREAM_MAX_RESPONSE_BYTES",
        128,
    )
    executor = FakeUpstreamExecutor(
        "success",
        result_payload={
            "content": [{"type": "text", "text": "x" * 1024}],
            "isError": False,
        },
    )
    _register_upstream(tool_name, executor)
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key="partner-upstream-persistence-rejected-permit",
        )
        body = _call_body(
            tool_name=tool_name,
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
        )

        first = await client.post(
            "/mcp/messages", json=body, headers=provisioned["agent_headers"]
        )
        replay = await client.post(
            "/mcp/messages", json=body, headers=provisioned["agent_headers"]
        )

        first_error = first.json()["error"]
        assert first_error["code"] == -32006
        assert first_error["message"] == "response_rejected"
        assert replay.json()["error"] == first_error
        assert executor.dispatch_count == 1
        persisted = await _load_persisted_invocation(
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
        )
        _assert_linked_terminal_state(
            persisted,
            state="response_rejected",
            outcome="response_rejected",
            charged=Decimal("2"),
            refunded=False,
        )
        assert persisted.attempt.error_code == "upstream_response_too_large"
        assert persisted.attempt.result_json is None
        assert persisted.attempt.response_hash is None
        assert persisted.receipt.response_hash is None
        valid, reason, _ = await get_receipt_service().verify_receipt(
            persisted.receipt.receipt_id
        )
        assert (valid, reason) == (True, None)
        evidence = await client.get(
            f"/v1/receipts/{persisted.receipt.receipt_id}/evidence",
            headers=provisioned["agent_headers"],
        )
        assert evidence.status_code == 200
        assert evidence.json()["valid"] is True
        checks = {check["name"]: check for check in evidence.json()["checks"]}
        assert checks["dispatch_linkage"]["status"] == "passed"
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_frozen_wallet_is_denied_before_upstream_dispatch_and_replays(
    client: AsyncClient,
    clean_database: None,
) -> None:
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    tool_name = "partner-upstream-frozen-wallet"
    idempotency_key = "partner-upstream-frozen-wallet-1"
    executor = FakeUpstreamExecutor("success")
    _register_upstream(tool_name, executor)
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=wallet_id,
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key="partner-upstream-frozen-wallet-permit",
        )
        factory = get_session_factory()
        async with factory() as session:
            wallet = await session.get(WalletModel, wallet_id)
            assert wallet is not None
            balance_before = wallet.balance
            wallet.status = "frozen"
            await session.commit()

        body = _call_body(
            tool_name=tool_name,
            wallet_id=wallet_id,
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
        )
        first = await client.post(
            "/mcp/messages", json=body, headers=provisioned["agent_headers"]
        )
        replay = await client.post(
            "/mcp/messages", json=body, headers=provisioned["agent_headers"]
        )

        first_error = first.json()["error"]
        assert first_error["code"] == -32003
        assert first_error["message"] == "wallet_frozen"
        assert replay.json()["error"] == first_error
        receipt_payload = first_error["data"]["receipt"]
        assert receipt_payload["outcome"] == "failed_refunded"
        assert receipt_payload["ledger_entry_id"] is None
        assert receipt_payload["credits_charged"] == "0"
        assert executor.calls == []
        assert executor.dispatch_count == 0

        async with factory() as session:
            record = (
                await session.execute(
                    select(IdempotencyRecordModel).where(
                        IdempotencyRecordModel.wallet_id == wallet_id,
                        IdempotencyRecordModel.endpoint
                        == GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
                        IdempotencyRecordModel.idempotency_key == idempotency_key,
                    )
                )
            ).scalar_one()
            attempt = (
                await session.execute(
                    select(McpDispatchAttemptModel).where(
                        McpDispatchAttemptModel.idempotency_record_id
                        == record.record_id
                    )
                )
            ).scalar_one()
            wallet = await session.get(WalletModel, wallet_id)
            assert wallet is not None
            assert wallet.balance == balance_before

        assert attempt.state == "returned_error"
        assert attempt.error_code == "wallet_frozen"
        assert attempt.dispatched_at is None
        assert attempt.ledger_entry_id is None
        assert attempt.credits_charged == Decimal("0")

        stored_permit = await get_permit_service().get_permit(permit["permit_id"])
        assert stored_permit is not None
        assert stored_permit.spent_credits == Decimal("0")

        await get_mcp_dispatch_reconciliation_service().reconcile_attempt(
            attempt.attempt_id
        )
        evidence = await client.get(
            f"/v1/receipts/{receipt_payload['receipt_id']}/evidence",
            headers=provisioned["agent_headers"],
        )
        assert evidence.status_code == 200
        assert evidence.json()["valid"] is True
        checks = {check["name"]: check for check in evidence.json()["checks"]}
        assert checks["dispatch_linkage"]["status"] == "passed"
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_losing_dispatch_claim_does_not_compensate_or_send(
    client: AsyncClient,
    clean_database: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provisioned = await provision_agent_wallet(client)
    tool_name = "partner-upstream-dispatch-claim-loser"
    idempotency_key = "partner-upstream-dispatch-claim-loser-1"
    executor = FakeUpstreamExecutor("success")
    _register_upstream(tool_name, executor)
    original_claim = McpDispatchAttemptService.claim_dispatch
    winner_claimed = False

    async def force_losing_claim(
        self: McpDispatchAttemptService,
        attempt_id: str,
    ) -> McpDispatchAttemptModel:
        nonlocal winner_claimed
        if not winner_claimed:
            winner_claimed = True
            await original_claim(self, attempt_id)
        return await original_claim(self, attempt_id)

    monkeypatch.setattr(
        McpDispatchAttemptService,
        "claim_dispatch",
        force_losing_claim,
    )
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key="partner-upstream-dispatch-claim-loser-permit",
        )
        body = _call_body(
            tool_name=tool_name,
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
        )

        loser = await client.post(
            "/mcp/messages",
            json=body,
            headers=provisioned["agent_headers"],
        )

        assert loser.status_code == 200
        assert loser.json()["error"] == {
            "code": -32005,
            "message": "idempotency_in_progress",
        }
        assert winner_claimed is True
        assert executor.dispatch_count == 0
        assert len(executor.calls) == 1

        factory = get_session_factory()
        async with factory() as session:
            record = (
                await session.execute(
                    select(IdempotencyRecordModel).where(
                        IdempotencyRecordModel.wallet_id
                        == provisioned["agent_wallet_id"],
                        IdempotencyRecordModel.idempotency_key == idempotency_key,
                    )
                )
            ).scalar_one()
            attempt = (
                await session.execute(
                    select(McpDispatchAttemptModel).where(
                        McpDispatchAttemptModel.idempotency_record_id
                        == record.record_id
                    )
                )
            ).scalar_one()
            ledger_rows = list(
                (
                    await session.execute(
                        select(LedgerEntryModel).where(
                            LedgerEntryModel.wallet_id
                            == provisioned["agent_wallet_id"],
                            LedgerEntryModel.operation_key == record.record_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            receipt_count = await session.scalar(
                select(func.count())
                .select_from(ReceiptModel)
                .where(ReceiptModel.idempotency_record_id == record.record_id)
            )
            stored_permit = await session.get(PermitModel, permit["permit_id"])
            wallet = await session.get(
                WalletModel,
                provisioned["agent_wallet_id"],
            )

        assert record.response_json is None
        assert record.response_reference is None
        assert attempt.state == "dispatch_claimed"
        assert attempt.dispatch_claim_hash is not None
        assert attempt.completed_at is None
        assert attempt.debit_refunded_at is None
        assert attempt.budget_released_at is None
        assert [row.action for row in ledger_rows] == ["debit"]
        assert int(receipt_count or 0) == 0
        assert stored_permit is not None
        assert stored_permit.spent_credits == Decimal("2")
        assert wallet is not None
        assert wallet.balance == Decimal("998")
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["jsonrpc", "rest"])
async def test_prepare_commit_uncertain_preserves_owner_state_and_public_contract(
    client: AsyncClient,
    clean_database: None,
    monkeypatch: pytest.MonkeyPatch,
    transport: Literal["jsonrpc", "rest"],
) -> None:
    provisioned = await provision_agent_wallet(client)
    tool_name = "partner-upstream-prepare-uncertain"
    idempotency_key = "partner-upstream-prepare-uncertain-1"
    executor = FakeUpstreamExecutor("success")
    original_prepare = McpDispatchAttemptService.authorize_reserve_and_prepare
    committed_attempt_id: str | None = None

    async def commit_then_report_uncertain(
        self: McpDispatchAttemptService,
        **kwargs: Any,
    ) -> None:
        nonlocal committed_attempt_id
        _validation, attempt = await original_prepare(self, **kwargs)
        assert attempt is not None
        committed_attempt_id = attempt.attempt_id
        raise DispatchPrepareCommitUncertainError("dispatch_prepare_commit_uncertain")

    monkeypatch.setattr(
        McpDispatchAttemptService,
        "authorize_reserve_and_prepare",
        commit_then_report_uncertain,
    )
    _register_upstream(tool_name, executor)
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key="partner-upstream-prepare-uncertain-permit",
        )
        body = _call_body(
            tool_name=tool_name,
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
        )

        if transport == "jsonrpc":
            uncertain = await client.post(
                "/mcp/messages",
                json=body,
                headers=provisioned["agent_headers"],
            )
            assert uncertain.status_code == 200
            assert uncertain.json()["error"] == {
                "code": -32005,
                "message": "idempotency_in_progress",
            }
        else:
            uncertain = await client.post(
                f"/mcp/tools/{tool_name}/invoke",
                json=_rest_call_body(
                    tool_name=tool_name,
                    wallet_id=provisioned["agent_wallet_id"],
                    permit_id=permit["permit_id"],
                    idempotency_key=idempotency_key,
                ),
                headers=provisioned["agent_headers"],
            )
            assert uncertain.status_code == 409
            assert uncertain.json()["detail"] == {"error": "idempotency_in_progress"}
        assert committed_attempt_id is not None
        assert executor.calls == []

        factory = get_session_factory()
        async with factory() as session:
            attempt = await session.get(
                McpDispatchAttemptModel,
                committed_attempt_id,
            )
            assert attempt is not None
            record = await session.get(
                IdempotencyRecordModel,
                attempt.idempotency_record_id,
            )
            stored_permit = await session.get(PermitModel, permit["permit_id"])
            receipt_count = await session.scalar(
                select(func.count())
                .select_from(ReceiptModel)
                .where(
                    ReceiptModel.idempotency_record_id == attempt.idempotency_record_id
                )
            )
            ledger_count = await session.scalar(
                select(func.count())
                .select_from(LedgerEntryModel)
                .where(LedgerEntryModel.operation_key == attempt.idempotency_record_id)
            )

        assert attempt.state == "prepared"
        assert attempt.dispatch_claim_hash is None
        assert attempt.completed_at is None
        assert record is not None and record.response_json is None
        assert stored_permit is not None
        assert stored_permit.spent_credits == Decimal("2")
        assert int(receipt_count or 0) == 0
        assert int(ledger_count or 0) == 0
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_prepare_rollback_keeps_existing_terminal_error_contract(
    client: AsyncClient,
    clean_database: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provisioned = await provision_agent_wallet(client)
    tool_name = "partner-upstream-prepare-rollback"
    idempotency_key = "partner-upstream-prepare-rollback-1"
    executor = FakeUpstreamExecutor("success")

    async def report_definite_rollback(
        _self: McpDispatchAttemptService,
        **_kwargs: Any,
    ) -> None:
        raise DispatchPrepareRolledBackError("dispatch_prepare_rolled_back")

    monkeypatch.setattr(
        McpDispatchAttemptService,
        "authorize_reserve_and_prepare",
        report_definite_rollback,
    )
    _register_upstream(tool_name, executor)
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key="partner-upstream-prepare-rollback-permit",
        )
        body = _call_body(
            tool_name=tool_name,
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
        )

        rolled_back = await client.post(
            "/mcp/messages",
            json=body,
            headers=provisioned["agent_headers"],
        )

        assert rolled_back.status_code == 200
        error = rolled_back.json()["error"]
        assert error["code"] == -32006
        assert error["message"] == "upstream_prepare_failed"
        assert error["data"]["receipt"]["outcome"] == "failed_refunded"
        assert executor.calls == []

        factory = get_session_factory()
        async with factory() as session:
            record = (
                await session.execute(
                    select(IdempotencyRecordModel).where(
                        IdempotencyRecordModel.wallet_id
                        == provisioned["agent_wallet_id"],
                        IdempotencyRecordModel.idempotency_key == idempotency_key,
                    )
                )
            ).scalar_one()
            attempt_count = await session.scalar(
                select(func.count()).select_from(McpDispatchAttemptModel)
            )
            debit_count = await session.scalar(
                select(func.count())
                .select_from(LedgerEntryModel)
                .where(
                    LedgerEntryModel.wallet_id == provisioned["agent_wallet_id"],
                    LedgerEntryModel.action == "debit",
                )
            )
            receipt_count = await session.scalar(
                select(func.count())
                .select_from(ReceiptModel)
                .where(ReceiptModel.idempotency_record_id == record.record_id)
            )
            stored_permit = await session.get(PermitModel, permit["permit_id"])

        assert record.response_json is not None
        assert int(attempt_count or 0) == 0
        assert int(debit_count or 0) == 0
        assert int(receipt_count or 0) == 1
        assert stored_permit is not None
        assert stored_permit.spent_credits == Decimal("0")
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_unicode_returned_error_keeps_linked_evidence_if_refund_fails(
    client: AsyncClient,
    clean_database: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mcp_router.settings,
        "SIMULATION_MODE_HUMAN_APPROVAL",
        True,
    )
    provisioned = await provision_agent_wallet(client)
    tool_name = "partner-upstream-unicode-unrefunded"
    idempotency_key = "partner-upstream-unicode-unrefunded-1"
    executor = FakeUpstreamExecutor(
        "returned_error",
        result_payload={
            "content": [{"type": "text", "text": "伙伴拒绝 👎"}],
            "structuredContent": {"reason": "超出范围"},
            "isError": True,
        },
    )

    async def failing_refund(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("refund unavailable")

    monkeypatch.setattr(
        "app.services.agent_money.AgentMoney.refund_charge",
        failing_refund,
    )
    _register_upstream(tool_name, executor)
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key="partner-upstream-unicode-unrefunded-permit",
            requires_human_approval=True,
        )
        response = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=provisioned["agent_wallet_id"],
                permit_id=permit["permit_id"],
                idempotency_key=idempotency_key,
            ),
            headers=provisioned["agent_headers"],
        )

        assert response.status_code == 200
        assert response.json()["error"]["message"].startswith("refund_failed")
        approval_id = response.json()["error"]["data"]["receipt"]["approval_id"]
        assert approval_id
        persisted = await _load_persisted_invocation(
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
        )
        _assert_linked_terminal_state(
            persisted,
            state="returned_error",
            outcome="failed_unrefunded",
            charged=Decimal("2"),
            refunded=False,
        )
        assert persisted.attempt.approval_id == approval_id
        assert persisted.receipt.approval_id == approval_id
        assert persisted.attempt.response_hash is not None
        valid, reason, _ = await get_receipt_service().verify_receipt(
            persisted.receipt.receipt_id
        )
        assert (valid, reason) == (True, None)
        evidence = await client.get(
            f"/v1/receipts/{persisted.receipt.receipt_id}/evidence",
            headers=provisioned["agent_headers"],
        )
        assert evidence.status_code == 200
        assert evidence.json()["valid"] is True
        checks = {check["name"]: check for check in evidence.json()["checks"]}
        assert checks["dispatch_linkage"]["status"] == "passed"
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_removed_upstream_replay_denies_cross_wallet_key_for_unbound_permit(
    client: AsyncClient,
    clean_database: None,
) -> None:
    authorized = await provision_agent_wallet(client)
    unauthorized = await provision_agent_wallet(client)
    tool_name = "partner-upstream-removed-cross-wallet-replay"
    idempotency_key = "partner-upstream-removed-cross-wallet-replay-1"
    executor = FakeUpstreamExecutor("success")
    _register_upstream(tool_name, executor)
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=authorized["agent_wallet_id"],
            key_id=None,
            tool_name=tool_name,
            idem_key="partner-upstream-removed-cross-wallet-replay-permit",
        )
        body = _call_body(
            tool_name=tool_name,
            wallet_id=authorized["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
        )
        first = await client.post(
            "/mcp/messages",
            json=body,
            headers=authorized["agent_headers"],
        )
        assert first.status_code == 200
        assert "result" in first.json()
        assert executor.dispatch_count == 1

        get_service_registry().unregister_local(tool_name)
        replay = await client.post(
            "/mcp/messages",
            json=body,
            headers=unauthorized["agent_headers"],
        )

        assert replay.status_code == 200
        assert "result" not in replay.json()
        assert replay.json()["error"]["message"] == "wallet_access_denied"
        assert executor.dispatch_count == 1
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["jsonrpc", "rest"])
async def test_completed_precanonical_same_transport_replays_without_new_identity(
    client: AsyncClient,
    clean_database: None,
    transport: Literal["jsonrpc", "rest"],
) -> None:
    provisioned = await provision_agent_wallet(client)
    tool_name = f"partner-upstream-legacy-{transport}"
    idempotency_key = f"partner-upstream-legacy-{transport}-1"
    executor = FakeUpstreamExecutor("success")
    _register_upstream(tool_name, executor)
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key=f"partner-upstream-legacy-{transport}-permit",
        )
        if transport == "jsonrpc":
            body = _call_body(
                tool_name=tool_name,
                wallet_id=provisioned["agent_wallet_id"],
                permit_id=permit["permit_id"],
                idempotency_key=idempotency_key,
            )
        else:
            body = _rest_call_body(
                tool_name=tool_name,
                wallet_id=provisioned["agent_wallet_id"],
                permit_id=permit["permit_id"],
                idempotency_key=idempotency_key,
            )
        endpoint, historical_payload = _legacy_transport_identity(
            transport=transport,
            body=body,
            tool_name=tool_name,
        )
        historical_response = {
            "content": [{"type": "text", "text": "historical response"}],
            "structuredContent": {"source": f"legacy-{transport}"},
            "isError": False,
            "receipt": {
                "receipt_id": f"rcpt-legacy-{transport}",
                "outcome": "success",
            },
        }
        await _seed_completed_legacy_idempotency(
            wallet_id=provisioned["agent_wallet_id"],
            endpoint=endpoint,
            idempotency_key=idempotency_key,
            request_payload=historical_payload,
            response_payload=historical_response,
        )
        before_counts = await _wallet_debit_and_canonical_counts(
            wallet_id=provisioned["agent_wallet_id"],
            idempotency_key=idempotency_key,
        )

        if transport == "jsonrpc":
            response = await client.post(
                "/mcp/messages",
                json=body,
                headers=provisioned["agent_headers"],
            )
            replayed = response.json()["result"]
        else:
            response = await client.post(
                f"/mcp/tools/{tool_name}/invoke",
                json=body,
                headers=provisioned["agent_headers"],
            )
            replayed = response.json()

        assert response.status_code == 200
        assert replayed == historical_response
        assert executor.dispatch_count == 0
        assert (
            await _wallet_debit_and_canonical_counts(
                wallet_id=provisioned["agent_wallet_id"],
                idempotency_key=idempotency_key,
            )
            == before_counts
            == (0, 0)
        )
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("legacy_endpoint", "expect_replay"),
    [
        ("/mcp/messages", True),
        ("/mcp/tools/different-partner-tool/invoke", False),
    ],
)
async def test_legacy_worker_winning_after_probe_is_re_resolved_without_dispatch(
    client: AsyncClient,
    clean_database: None,
    monkeypatch: pytest.MonkeyPatch,
    legacy_endpoint: str,
    expect_replay: bool,
) -> None:
    """The old/new worker probe-to-insert race replays or conflicts safely."""
    provisioned = await provision_agent_wallet(client)
    tool_name = "partner-upstream-legacy-insert-race"
    idempotency_key = "partner-upstream-legacy-insert-race-1"
    executor = FakeUpstreamExecutor("success")
    _register_upstream(tool_name, executor)
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key="partner-upstream-legacy-insert-race-permit",
        )
        body = _call_body(
            tool_name=tool_name,
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
        )
        historical_response = {
            "content": [{"type": "text", "text": "legacy worker won"}],
            "isError": False,
            "receipt": {"receipt_id": "rcpt-legacy-insert-race"},
        }
        idem = get_idempotency_service()
        original_begin = idem.begin_with_record
        injected = False

        async def insert_legacy_before_canonical(**kwargs: Any):
            nonlocal injected
            if kwargs["endpoint"] == GOVERNED_MCP_IDEMPOTENCY_ENDPOINT and not injected:
                injected = True
                await original_begin(
                    wallet_id=provisioned["agent_wallet_id"],
                    endpoint=legacy_endpoint,
                    idempotency_key=idempotency_key,
                    request_payload=body,
                )
                await idem.complete(
                    wallet_id=provisioned["agent_wallet_id"],
                    endpoint=legacy_endpoint,
                    idempotency_key=idempotency_key,
                    response_reference="rcpt-legacy-insert-race",
                    response_json=historical_response,
                    status_code=200,
                )
            return await original_begin(**kwargs)

        monkeypatch.setattr(idem, "begin_with_record", insert_legacy_before_canonical)

        response = await client.post(
            "/mcp/messages",
            json=body,
            headers=provisioned["agent_headers"],
        )

        assert response.status_code == 200
        if expect_replay:
            assert response.json()["result"] == historical_response
        else:
            assert response.json()["error"]["message"] == "idempotency_key_reused"
        assert injected is True
        assert executor.dispatch_count == 0
        assert await _wallet_debit_and_canonical_counts(
            wallet_id=provisioned["agent_wallet_id"],
            idempotency_key=idempotency_key,
        ) == (0, 0)
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("historical_transport", "retry_transport"),
    [("jsonrpc", "rest"), ("rest", "jsonrpc")],
)
async def test_precanonical_alternate_transport_identity_fails_closed(
    client: AsyncClient,
    clean_database: None,
    historical_transport: Literal["jsonrpc", "rest"],
    retry_transport: Literal["jsonrpc", "rest"],
) -> None:
    provisioned = await provision_agent_wallet(client)
    tool_name = f"partner-upstream-legacy-alternate-{historical_transport}"
    idempotency_key = f"partner-upstream-legacy-alternate-{historical_transport}-1"
    executor = FakeUpstreamExecutor("success")
    _register_upstream(tool_name, executor)
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key=f"partner-upstream-legacy-alternate-{historical_transport}-permit",
        )
        body_builders = {
            "jsonrpc": _call_body,
            "rest": _rest_call_body,
        }
        historical_body = body_builders[historical_transport](
            tool_name=tool_name,
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
        )
        endpoint, historical_payload = _legacy_transport_identity(
            transport=historical_transport,
            body=historical_body,
            tool_name=tool_name,
        )
        await _seed_completed_legacy_idempotency(
            wallet_id=provisioned["agent_wallet_id"],
            endpoint=endpoint,
            idempotency_key=idempotency_key,
            request_payload=historical_payload,
            response_payload={
                "content": [{"type": "text", "text": "historical response"}],
                "isError": False,
                "receipt": {"receipt_id": "rcpt-legacy-alternate"},
            },
        )
        retry_body = body_builders[retry_transport](
            tool_name=tool_name,
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
        )

        if retry_transport == "jsonrpc":
            response = await client.post(
                "/mcp/messages",
                json=retry_body,
                headers=provisioned["agent_headers"],
            )
            assert response.status_code == 200
            assert response.json()["error"]["message"] == "idempotency_key_reused"
        else:
            response = await client.post(
                f"/mcp/tools/{tool_name}/invoke",
                json=retry_body,
                headers=provisioned["agent_headers"],
            )
            assert response.status_code == 400
            assert response.json()["detail"] == "idempotency_key_reused"

        assert executor.dispatch_count == 0
        assert await _wallet_debit_and_canonical_counts(
            wallet_id=provisioned["agent_wallet_id"],
            idempotency_key=idempotency_key,
        ) == (0, 0)
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_precanonical_same_transport_payload_conflict_never_dispatches(
    client: AsyncClient,
    clean_database: None,
) -> None:
    provisioned = await provision_agent_wallet(client)
    tool_name = "partner-upstream-legacy-payload-conflict"
    idempotency_key = "partner-upstream-legacy-payload-conflict-1"
    executor = FakeUpstreamExecutor("success")
    _register_upstream(tool_name, executor)
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key="partner-upstream-legacy-payload-conflict-permit",
        )
        historical_body = _call_body(
            tool_name=tool_name,
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
            message="historical",
        )
        await _seed_completed_legacy_idempotency(
            wallet_id=provisioned["agent_wallet_id"],
            endpoint="/mcp/messages",
            idempotency_key=idempotency_key,
            request_payload=historical_body,
            response_payload={
                "content": [{"type": "text", "text": "historical response"}],
                "isError": False,
                "receipt": {"receipt_id": "rcpt-legacy-payload-conflict"},
            },
        )
        conflicting_body = _call_body(
            tool_name=tool_name,
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
            message="different",
        )

        response = await client.post(
            "/mcp/messages",
            json=conflicting_body,
            headers=provisioned["agent_headers"],
        )

        assert response.status_code == 200
        assert response.json()["error"]["message"] == "idempotency_key_reused"
        assert executor.dispatch_count == 0
        assert await _wallet_debit_and_canonical_counts(
            wallet_id=provisioned["agent_wallet_id"],
            idempotency_key=idempotency_key,
        ) == (0, 0)
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_removed_tool_completed_precanonical_replay_remains_available(
    client: AsyncClient,
    clean_database: None,
) -> None:
    provisioned = await provision_agent_wallet(client)
    tool_name = "partner-upstream-legacy-removed"
    idempotency_key = "partner-upstream-legacy-removed-1"
    executor = FakeUpstreamExecutor("success")
    _register_upstream(tool_name, executor)
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key="partner-upstream-legacy-removed-permit",
        )
        body = _call_body(
            tool_name=tool_name,
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
        )
        historical_response = {
            "content": [{"type": "text", "text": "removed historical response"}],
            "isError": False,
            "receipt": {"receipt_id": "rcpt-legacy-removed"},
        }
        await _seed_completed_legacy_idempotency(
            wallet_id=provisioned["agent_wallet_id"],
            endpoint="/mcp/messages",
            idempotency_key=idempotency_key,
            request_payload=body,
            response_payload=historical_response,
        )
        get_service_registry().unregister_local(tool_name)

        response = await client.post(
            "/mcp/messages",
            json=body,
            headers=provisioned["agent_headers"],
        )

        assert response.status_code == 200
        assert response.json()["result"] == historical_response
        assert executor.dispatch_count == 0
        assert await _wallet_debit_and_canonical_counts(
            wallet_id=provisioned["agent_wallet_id"],
            idempotency_key=idempotency_key,
        ) == (0, 0)
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_governed_upstream_replays_across_jsonrpc_and_rest_transport(
    client: AsyncClient,
    clean_database: None,
) -> None:
    provisioned = await provision_agent_wallet(client)
    tool_name = "partner-upstream-cross-transport"
    idempotency_key = "partner-upstream-cross-transport-1"
    executor = FakeUpstreamExecutor("success")
    _register_upstream(tool_name, executor)
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key="partner-upstream-cross-transport-permit",
        )
        first = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=provisioned["agent_wallet_id"],
                permit_id=permit["permit_id"],
                idempotency_key=idempotency_key,
            ),
            headers=provisioned["agent_headers"],
        )
        replay = await client.post(
            f"/mcp/tools/{tool_name}/invoke",
            json=_rest_call_body(
                tool_name=tool_name,
                wallet_id=provisioned["agent_wallet_id"],
                permit_id=permit["permit_id"],
                idempotency_key=idempotency_key,
            ),
            headers=provisioned["agent_headers"],
        )

        assert first.status_code == 200
        assert replay.status_code == 200
        assert replay.json() == first.json()["result"]
        receipt_id = first.json()["result"]["receipt"]["receipt_id"]
        assert replay.json()["receipt"]["receipt_id"] == receipt_id
        assert executor.dispatch_count == 1
        assert len(executor.calls) == 1

        persisted = await _load_persisted_invocation(
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
        )
        _assert_linked_terminal_state(
            persisted,
            state="succeeded",
            outcome="success",
            charged=Decimal("2"),
            refunded=False,
        )
        assert persisted.receipt.receipt_id == receipt_id
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_governed_upstream_replay_ignores_jsonrpc_correlation_id(
    client: AsyncClient,
    clean_database: None,
) -> None:
    provisioned = await provision_agent_wallet(client)
    tool_name = "partner-upstream-jsonrpc-correlation"
    idempotency_key = "partner-upstream-jsonrpc-correlation-1"
    executor = FakeUpstreamExecutor("success")
    _register_upstream(tool_name, executor)
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key="partner-upstream-jsonrpc-correlation-permit",
        )
        first_body = _call_body(
            tool_name=tool_name,
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
        )
        replay_body = dict(first_body)
        replay_body["id"] = "different-jsonrpc-correlation-id"

        first = await client.post(
            "/mcp/messages",
            json=first_body,
            headers=provisioned["agent_headers"],
        )
        replay = await client.post(
            "/mcp/messages",
            json=replay_body,
            headers=provisioned["agent_headers"],
        )

        assert first.status_code == 200
        assert replay.status_code == 200
        assert replay.json()["id"] == "different-jsonrpc-correlation-id"
        assert replay.json()["result"] == first.json()["result"]
        assert (
            replay.json()["result"]["receipt"]["receipt_id"]
            == first.json()["result"]["receipt"]["receipt_id"]
        )
        assert executor.dispatch_count == 1
        assert len(executor.calls) == 1

        persisted = await _load_persisted_invocation(
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
        )
        _assert_linked_terminal_state(
            persisted,
            state="succeeded",
            outcome="success",
            charged=Decimal("2"),
            refunded=False,
        )
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_governed_upstream_replay_remains_bound_to_permit_api_key(
    client: AsyncClient,
    clean_database: None,
) -> None:
    provisioned = await provision_agent_wallet(client)
    tool_name = "partner-upstream-replay-key-binding"
    idempotency_key = "partner-upstream-replay-key-binding-1"
    executor = FakeUpstreamExecutor("success")
    _register_upstream(tool_name, executor)
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key="partner-upstream-replay-key-binding-permit",
        )
        body = _call_body(
            tool_name=tool_name,
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
        )
        first = await client.post(
            "/mcp/messages",
            json=body,
            headers=provisioned["agent_headers"],
        )
        second_key_response = await client.post(
            "/v1/api-keys",
            json={
                "wallet_id": provisioned["agent_wallet_id"],
                "key_name": "partner-upstream-replay-second-key",
                "expires_in_days": 30,
            },
            # The agent's own key expires, and a bounded key cannot mint
            # siblings, so the second same-wallet key comes from bootstrap.
            headers=BOOTSTRAP_HEADERS,
        )
        assert second_key_response.status_code == 201

        denied_replay = await client.post(
            "/mcp/messages",
            json=body,
            headers={"X-API-Key": second_key_response.json()["api_key"]},
        )

        assert first.status_code == 200
        assert denied_replay.status_code == 200
        assert denied_replay.json()["error"] == {
            "code": -32003,
            "message": "permit_key_mismatch",
        }
        assert "result" not in denied_replay.json()
        assert executor.dispatch_count == 1
        assert len(executor.calls) == 1

        persisted = await _load_persisted_invocation(
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
        )
        _assert_linked_terminal_state(
            persisted,
            state="succeeded",
            outcome="success",
            charged=Decimal("2"),
            refunded=False,
        )
        assert (
            persisted.receipt.receipt_id
            == first.json()["result"]["receipt"]["receipt_id"]
        )
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_governed_upstream_conflicting_payload_reuse_never_redispatches(
    client: AsyncClient,
    clean_database: None,
) -> None:
    provisioned = await provision_agent_wallet(client)
    tool_name = "partner-upstream-conflict"
    idempotency_key = "partner-upstream-conflict-1"
    executor = FakeUpstreamExecutor("success")
    _register_upstream(tool_name, executor)
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key="partner-upstream-conflict-permit",
        )
        first_body = _call_body(
            tool_name=tool_name,
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
            message="first",
        )
        conflicting_body = _call_body(
            tool_name=tool_name,
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
            message="changed",
        )

        first = await client.post(
            "/mcp/messages",
            json=first_body,
            headers=provisioned["agent_headers"],
        )
        conflict = await client.post(
            "/mcp/messages",
            json=conflicting_body,
            headers=provisioned["agent_headers"],
        )

        assert "result" in first.json()
        assert conflict.json()["error"]["message"] == "idempotency_key_reused"
        assert executor.dispatch_count == 1
        assert len(executor.calls) == 1
        persisted = await _load_persisted_invocation(
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
        )
        _assert_linked_terminal_state(
            persisted,
            state="succeeded",
            outcome="success",
            charged=Decimal("2"),
            refunded=False,
        )
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("mode", "expected_reason", "expected_dispatch_count"),
    [
        ("returned_error", "upstream_returned_error", 1),
        ("pre_dispatch_failure", "upstream_pre_dispatch_failed", 0),
    ],
)
async def test_confirmed_upstream_failures_refund_and_replay_without_redispatch(
    client: AsyncClient,
    clean_database: None,
    mode: ExecutorMode,
    expected_reason: str,
    expected_dispatch_count: int,
) -> None:
    provisioned = await provision_agent_wallet(client)
    tool_name = f"partner-upstream-{mode}"
    idempotency_key = f"partner-upstream-{mode}-1"
    executor = FakeUpstreamExecutor(mode)
    _register_upstream(tool_name, executor)
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key=f"partner-upstream-{mode}-permit",
        )
        body = _call_body(
            tool_name=tool_name,
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
        )

        first = await client.post(
            "/mcp/messages", json=body, headers=provisioned["agent_headers"]
        )
        replay = await client.post(
            "/mcp/messages", json=body, headers=provisioned["agent_headers"]
        )

        assert first.json()["error"]["message"] == expected_reason
        assert replay.json()["error"] == first.json()["error"]
        receipt = first.json()["error"]["data"]["receipt"]
        assert receipt["outcome"] == "failed_refunded"
        assert receipt["credits_charged"] == "0"
        assert executor.dispatch_count == expected_dispatch_count
        assert len(executor.calls) == 1

        persisted = await _load_persisted_invocation(
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
        )
        _assert_linked_terminal_state(
            persisted,
            state="returned_error",
            outcome="failed_refunded",
            charged=Decimal("0"),
            refunded=True,
        )
        assert persisted.attempt.credits_charged == Decimal("2")
        if expected_dispatch_count:
            assert persisted.attempt.dispatched_at is not None
        else:
            assert persisted.attempt.dispatched_at is None
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_charge_failure_is_immediately_signed_and_releases_remote_budget(
    client: AsyncClient,
    clean_database: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provisioned = await provision_agent_wallet(client)
    tool_name = "partner-upstream-charge-failure"
    idempotency_key = "partner-upstream-charge-failure-1"
    executor = FakeUpstreamExecutor("success")
    _register_upstream(tool_name, executor)
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key="partner-upstream-charge-failure-permit",
        )

        async def fail_charge(**_kwargs):
            raise RuntimeError("simulated_charge_failure")

        monkeypatch.setattr(get_agent_money(), "charge", fail_charge)
        body = _call_body(
            tool_name=tool_name,
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
        )

        response = await client.post(
            "/mcp/messages",
            json=body,
            headers=provisioned["agent_headers"],
        )

        error = response.json()["error"]
        assert error["message"] == "upstream_pre_dispatch_failed"
        receipt_payload = error["data"]["receipt"]
        assert receipt_payload["outcome"] == "failed_refunded"
        assert receipt_payload["ledger_entry_id"] is None
        assert receipt_payload["credits_charged"] == "0"
        assert executor.calls == []
        assert executor.dispatch_count == 0
        permit_after = await get_permit_service().get_permit(permit["permit_id"])
        assert permit_after is not None
        assert permit_after.spent_credits == Decimal("0")
        valid, reason, _ = await get_receipt_service().verify_receipt(
            receipt_payload["receipt_id"]
        )
        assert (valid, reason) == (True, None)
        factory = get_session_factory()
        async with factory() as session:
            record = (
                await session.execute(
                    select(IdempotencyRecordModel).where(
                        IdempotencyRecordModel.wallet_id
                        == provisioned["agent_wallet_id"],
                        IdempotencyRecordModel.endpoint
                        == GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
                        IdempotencyRecordModel.idempotency_key == idempotency_key,
                    )
                )
            ).scalar_one()
            attempt = (
                await session.execute(
                    select(McpDispatchAttemptModel).where(
                        McpDispatchAttemptModel.idempotency_record_id
                        == record.record_id
                    )
                )
            ).scalar_one()
            debit_count = await session.scalar(
                select(func.count())
                .select_from(LedgerEntryModel)
                .where(LedgerEntryModel.operation_key == record.record_id)
            )
        assert attempt.state == "returned_error"
        assert attempt.budget_released_at is not None
        assert int(debit_count or 0) == 0
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_expired_child_wallet_denial_is_signed_released_and_replayable(
    client: AsyncClient,
    clean_database: None,
) -> None:
    provisioned = await provision_agent_wallet(client)
    tool_name = "partner-upstream-expired-child-wallet"
    idempotency_key = "partner-upstream-expired-child-wallet-1"
    executor = FakeUpstreamExecutor("success")
    _register_upstream(tool_name, executor)
    try:
        child_response = await client.post(
            "/v1/billing/wallets/child",
            json={
                "parent_wallet_id": provisioned["agent_wallet_id"],
                "child_agent_id": "expired-governed-child",
                "budget_credits": 100,
                "max_spend": 100,
                "ttl_seconds": 60,
            },
            headers=provisioned["agent_headers"],
        )
        assert child_response.status_code == 201
        child_wallet_id = child_response.json()["wallet_id"]
        key_response = await client.post(
            "/v1/api-keys",
            json={
                "wallet_id": child_wallet_id,
                "key_name": "expired-child-runtime",
                "expires_in_days": 30,
            },
            headers=BOOTSTRAP_HEADERS,
        )
        assert key_response.status_code == 201
        key_payload = key_response.json()
        child_headers = {"X-API-Key": key_payload["api_key"]}
        permit = await create_tool_permit(
            client,
            wallet_id=child_wallet_id,
            key_id=key_payload["key_id"],
            tool_name=tool_name,
            idem_key="partner-upstream-expired-child-wallet-permit",
        )

        factory = get_session_factory()
        async with factory() as session:
            wallet = await session.get(WalletModel, child_wallet_id)
            assert wallet is not None
            wallet.created_at = utc_now() - timedelta(seconds=61)
            session.add(wallet)
            await session.commit()

        body = _call_body(
            tool_name=tool_name,
            wallet_id=child_wallet_id,
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
        )
        first = await client.post("/mcp/messages", json=body, headers=child_headers)
        replay = await client.post("/mcp/messages", json=body, headers=child_headers)

        assert first.status_code == 200
        assert replay.status_code == 200
        assert replay.json()["error"] == first.json()["error"]
        error = first.json()["error"]
        assert error["code"] == -32003
        assert error["message"] == "wallet_expired"
        receipt_payload = error["data"]["receipt"]
        assert receipt_payload["outcome"] == "failed_refunded"
        assert receipt_payload["ledger_entry_id"] is None
        assert receipt_payload["credits_charged"] == "0"
        assert executor.calls == []
        assert executor.dispatch_count == 0
        valid, reason, _ = await get_receipt_service().verify_receipt(
            receipt_payload["receipt_id"]
        )
        assert (valid, reason) == (True, None)

        permit_after = await get_permit_service().get_permit(permit["permit_id"])
        assert permit_after is not None
        assert permit_after.spent_credits == Decimal("0")
        async with factory() as session:
            record = (
                await session.execute(
                    select(IdempotencyRecordModel).where(
                        IdempotencyRecordModel.wallet_id == child_wallet_id,
                        IdempotencyRecordModel.endpoint
                        == GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
                        IdempotencyRecordModel.idempotency_key == idempotency_key,
                    )
                )
            ).scalar_one()
            attempt = (
                await session.execute(
                    select(McpDispatchAttemptModel).where(
                        McpDispatchAttemptModel.idempotency_record_id
                        == record.record_id
                    )
                )
            ).scalar_one()
            debit_count = await session.scalar(
                select(func.count())
                .select_from(LedgerEntryModel)
                .where(LedgerEntryModel.operation_key == record.record_id)
            )
            wallet = await session.get(WalletModel, child_wallet_id)
        assert attempt.state == "returned_error"
        assert attempt.error_code == "wallet_expired"
        assert attempt.budget_released_at is not None
        assert int(debit_count or 0) == 0
        assert wallet is not None
        assert wallet.balance == Decimal("100")
        await get_mcp_dispatch_reconciliation_service().reconcile_attempt(
            attempt.attempt_id
        )
        reconciled_replay = await client.post(
            "/mcp/messages",
            json=body,
            headers=child_headers,
        )
        assert reconciled_replay.status_code == 200
        assert reconciled_replay.json()["error"]["code"] == -32003
        assert reconciled_replay.json()["error"]["message"] == "wallet_expired"
        assert (
            reconciled_replay.json()["error"]["data"]["receipt"]["receipt_id"]
            == receipt_payload["receipt_id"]
        )
        evidence = await client.get(
            f"/v1/receipts/{receipt_payload['receipt_id']}/evidence",
            headers=child_headers,
        )
        assert evidence.status_code == 200
        assert evidence.json()["valid"] is True
        checks = {check["name"]: check for check in evidence.json()["checks"]}
        assert checks["dispatch_linkage"]["status"] == "passed"
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("mode", "expected_reason", "expected_state", "expected_code"),
    [
        ("delivery_uncertain", "delivery_uncertain", "delivery_uncertain", -32005),
        ("response_rejected", "response_rejected", "response_rejected", -32006),
    ],
)
async def test_ambiguous_or_rejected_upstream_response_stays_charged_and_replays(
    client: AsyncClient,
    clean_database: None,
    mode: ExecutorMode,
    expected_reason: str,
    expected_state: str,
    expected_code: int,
) -> None:
    provisioned = await provision_agent_wallet(client)
    tool_name = f"partner-upstream-{mode}"
    idempotency_key = f"partner-upstream-{mode}-1"
    executor = FakeUpstreamExecutor(mode)
    _register_upstream(tool_name, executor)
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key=f"partner-upstream-{mode}-permit",
        )
        body = _call_body(
            tool_name=tool_name,
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
        )

        first = await client.post(
            "/mcp/messages", json=body, headers=provisioned["agent_headers"]
        )
        replay = await client.post(
            "/mcp/messages", json=body, headers=provisioned["agent_headers"]
        )

        first_error = first.json()["error"]
        assert first_error["code"] == expected_code
        assert first_error["message"] == expected_reason
        assert replay.json()["error"] == first_error
        receipt = first_error["data"]["receipt"]
        assert receipt["outcome"] == expected_state
        assert Decimal(receipt["credits_charged"]) == Decimal("2")
        assert executor.dispatch_count == 1
        assert len(executor.calls) == 1

        persisted = await _load_persisted_invocation(
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key=idempotency_key,
        )
        _assert_linked_terminal_state(
            persisted,
            state=expected_state,
            outcome=expected_state,
            charged=Decimal("2"),
            refunded=False,
        )
        assert persisted.attempt.dispatched_at is not None
        assert first_error["data"]["dispatch"] == {
            "attempt_id": persisted.attempt.attempt_id,
            "state": expected_state,
        }
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("denial", "expected_reason"),
    [
        ("expired", "permit_expired"),
        ("revoked", "permit_revoked"),
        ("out_of_scope", "permit_tool_not_allowed"),
        ("over_budget", "permit_budget_exceeded"),
    ],
)
async def test_upstream_permit_denials_never_charge_or_dispatch(
    client: AsyncClient,
    clean_database: None,
    denial: str,
    expected_reason: str,
) -> None:
    provisioned = await provision_agent_wallet(client)
    tool_name = f"partner-upstream-denied-{denial}"
    idempotency_key = f"partner-upstream-denied-{denial}-1"
    executor = FakeUpstreamExecutor("success")
    _register_upstream(tool_name, executor)
    permitted_tool = f"{tool_name}-different" if denial == "out_of_scope" else tool_name
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=permitted_tool,
            max_credits=1 if denial == "over_budget" else 50,
            idem_key=f"partner-upstream-denied-{denial}-permit",
        )
        if denial == "revoked":
            await get_permit_service().revoke_permit(permit["permit_id"])
        elif denial == "expired":
            factory = get_session_factory()
            async with factory() as session:
                model = await session.get(PermitModel, permit["permit_id"])
                assert model is not None
                model.expires_at = utc_now() - timedelta(seconds=1)
                session.add(model)
                await session.commit()

        response = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=provisioned["agent_wallet_id"],
                permit_id=permit["permit_id"],
                idempotency_key=idempotency_key,
            ),
            headers=provisioned["agent_headers"],
        )

        assert response.json()["error"]["message"] == expected_reason
        assert executor.calls == []
        assert executor.dispatch_count == 0
        receipt = response.json()["error"]["data"]["receipt"]
        assert receipt["outcome"] == "denied"
        assert receipt["ledger_entry_id"] is None
        assert receipt["dispatch_attempt_id"] is None

        factory = get_session_factory()
        async with factory() as session:
            record = (
                await session.execute(
                    select(IdempotencyRecordModel).where(
                        IdempotencyRecordModel.wallet_id
                        == provisioned["agent_wallet_id"],
                        IdempotencyRecordModel.idempotency_key == idempotency_key,
                    )
                )
            ).scalar_one()
            attempts = (
                (
                    await session.execute(
                        select(McpDispatchAttemptModel).where(
                            McpDispatchAttemptModel.idempotency_record_id
                            == record.record_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            debits = (
                (
                    await session.execute(
                        select(LedgerEntryModel).where(
                            LedgerEntryModel.wallet_id
                            == provisioned["agent_wallet_id"],
                            LedgerEntryModel.operation_key == record.record_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert attempts == []
        assert debits == []
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "constraint_name",
    ["max_calls_per_tool", "aggregate_value_cap"],
)
async def test_upstream_usage_constraints_fail_closed_before_reservation(
    client: AsyncClient,
    clean_database: None,
    constraint_name: str,
) -> None:
    provisioned = await provision_agent_wallet(client)
    tool_name = f"partner-upstream-unsupported-{constraint_name}"
    idempotency_key = f"partner-upstream-unsupported-{constraint_name}-1"
    executor = FakeUpstreamExecutor("success")
    _register_upstream(tool_name, executor)
    constraint_value: object = (
        {tool_name: 1} if constraint_name == "max_calls_per_tool" else 2
    )
    try:
        permit_response = await client.post(
            "/v1/permits",
            json={
                "issuer_wallet_id": provisioned["agent_wallet_id"],
                "subject_wallet_id": provisioned["agent_wallet_id"],
                "subject_key_id": provisioned["key_id"],
                "allowed_tools": [tool_name],
                "scopes": [f"tool:{tool_name}:invoke", "billing:charge"],
                "max_credits": 50,
                constraint_name: constraint_value,
                "expires_at": (utc_now() + timedelta(minutes=30)).isoformat(),
            },
            headers={
                **BOOTSTRAP_HEADERS,
                "Idempotency-Key": f"permit-unsupported-{constraint_name}",
            },
        )
        assert permit_response.status_code == 201
        permit_id = permit_response.json()["permit_id"]
        body = _call_body(
            tool_name=tool_name,
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit_id,
            idempotency_key=idempotency_key,
        )

        first = await client.post(
            "/mcp/messages",
            json=body,
            headers=provisioned["agent_headers"],
        )
        replay = await client.post(
            "/mcp/messages",
            json=body,
            headers=provisioned["agent_headers"],
        )

        if constraint_name == "max_calls_per_tool":
            # max_calls_per_tool is now SUPPORTED on upstream tools
            first_result = first.json()["result"]
            replay_result = replay.json()["result"]
            assert first_result["receipt"]["outcome"] == "success"
            assert (
                replay_result["receipt"]["receipt_id"]
                == first_result["receipt"]["receipt_id"]
            )
            assert executor.dispatch_count == 1  # Only dispatched once

            factory = get_session_factory()
            async with factory() as session:
                permit = await session.get(PermitModel, permit_id)
                record = (
                    await session.execute(
                        select(IdempotencyRecordModel).where(
                            IdempotencyRecordModel.wallet_id
                            == provisioned["agent_wallet_id"],
                            IdempotencyRecordModel.idempotency_key == idempotency_key,
                        )
                    )
                ).scalar_one()
                attempt_count = await session.scalar(
                    select(func.count())
                    .select_from(McpDispatchAttemptModel)
                    .where(
                        McpDispatchAttemptModel.idempotency_record_id
                        == record.record_id
                    )
                )
            assert permit is not None and permit.spent_credits > Decimal("0")
            assert int(attempt_count or 0) == 1
        else:
            # aggregate_value_cap remains unsupported
            first_error = first.json()["error"]
            replay_error = replay.json()["error"]
            assert first_error["code"] == -32003
            assert first_error["message"] == (
                "permit_constraint_unsupported_for_upstream"
            )
            assert first_error["data"]["details"] == {
                "execution_backend": "upstream_mcp",
                "unsupported_constraints": [constraint_name],
            }
            assert replay_error["code"] == first_error["code"]
            assert replay_error["message"] == first_error["message"]
            assert replay_error["data"]["receipt"] == first_error["data"]["receipt"]
            receipt = first_error["data"]["receipt"]
            assert receipt["outcome"] == "denied"
            assert receipt["reason_code"] == (
                "permit_constraint_unsupported_for_upstream"
            )
            assert receipt["ledger_entry_id"] is None
            assert receipt["dispatch_attempt_id"] is None
            assert executor.calls == []
            assert executor.dispatch_count == 0

            factory = get_session_factory()
            async with factory() as session:
                permit = await session.get(PermitModel, permit_id)
                record = (
                    await session.execute(
                        select(IdempotencyRecordModel).where(
                            IdempotencyRecordModel.wallet_id
                            == provisioned["agent_wallet_id"],
                            IdempotencyRecordModel.idempotency_key == idempotency_key,
                        )
                    )
                ).scalar_one()
                attempt_count = await session.scalar(
                    select(func.count())
                    .select_from(McpDispatchAttemptModel)
                    .where(
                        McpDispatchAttemptModel.idempotency_record_id
                        == record.record_id
                    )
                )
                debit_count = await session.scalar(
                    select(func.count())
                    .select_from(LedgerEntryModel)
                    .where(LedgerEntryModel.operation_key == record.record_id)
                )
                receipt_count = await session.scalar(
                    select(func.count())
                    .select_from(ReceiptModel)
                    .where(ReceiptModel.idempotency_record_id == record.record_id)
                )
            assert permit is not None and permit.spent_credits == Decimal("0")
            assert int(attempt_count or 0) == 0
            assert int(debit_count or 0) == 0
            assert int(receipt_count or 0) == 1
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_upstream_usage_constraint_rest_denial_keeps_call_slot_unreserved(
    client: AsyncClient,
    clean_database: None,
) -> None:
    """max_calls_per_tool is now supported on upstream, so this test now expects success."""
    provisioned = await provision_agent_wallet(client)
    tool_name = "partner-upstream-supported-rest-call-slot"
    idempotency_key = "partner-upstream-supported-rest-call-slot-1"
    executor = FakeUpstreamExecutor("success")
    _register_upstream(tool_name, executor)
    try:
        permit_response = await client.post(
            "/v1/permits",
            json={
                "issuer_wallet_id": provisioned["agent_wallet_id"],
                "subject_wallet_id": provisioned["agent_wallet_id"],
                "subject_key_id": provisioned["key_id"],
                "allowed_tools": [tool_name],
                "scopes": [f"tool:{tool_name}:invoke", "billing:charge"],
                "max_credits": 50,
                "max_calls_per_tool": {tool_name: 1},
                "expires_at": (utc_now() + timedelta(minutes=30)).isoformat(),
            },
            headers={
                **BOOTSTRAP_HEADERS,
                "Idempotency-Key": "permit-supported-rest-call-slot",
            },
        )
        assert permit_response.status_code == 201
        permit_id = permit_response.json()["permit_id"]

        # First call should succeed
        first = await client.post(
            f"/mcp/tools/{tool_name}/invoke",
            json=_rest_call_body(
                tool_name=tool_name,
                wallet_id=provisioned["agent_wallet_id"],
                permit_id=permit_id,
                idempotency_key=idempotency_key,
            ),
            headers=provisioned["agent_headers"],
        )

        assert first.status_code == 200
        first_result = first.json()
        assert first_result["receipt"]["outcome"] == "success"
        assert first_result["receipt"]["dispatch_attempt_id"] is not None
        assert executor.dispatch_count == 1

        factory = get_session_factory()
        async with factory() as session:
            permit = await session.get(PermitModel, permit_id)
            record = (
                await session.execute(
                    select(IdempotencyRecordModel).where(
                        IdempotencyRecordModel.wallet_id
                        == provisioned["agent_wallet_id"],
                        IdempotencyRecordModel.idempotency_key == idempotency_key,
                    )
                )
            ).scalar_one()
            attempt_count = await session.scalar(
                select(func.count())
                .select_from(McpDispatchAttemptModel)
                .where(
                    McpDispatchAttemptModel.idempotency_record_id == record.record_id
                )
            )
            debit_count = await session.scalar(
                select(func.count())
                .select_from(LedgerEntryModel)
                .where(LedgerEntryModel.operation_key == record.record_id)
            )
        assert permit is not None
        assert permit.spent_credits > Decimal("0")
        # Call slot was reserved
        import json as json_lib

        call_counts = json_lib.loads(permit.tool_call_counts_json or "{}")
        assert call_counts.get(tool_name, 0) == 1
        assert int(attempt_count or 0) == 1
        assert int(debit_count or 0) == 1
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_unauthorized_wallet_cannot_reach_upstream_dispatch(
    client: AsyncClient,
    clean_database: None,
) -> None:
    authorized = await provision_agent_wallet(client)
    unauthorized = await provision_agent_wallet(client)
    tool_name = "partner-upstream-unauthorized-wallet"
    executor = FakeUpstreamExecutor("success")
    _register_upstream(tool_name, executor)
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=authorized["agent_wallet_id"],
            key_id=authorized["key_id"],
            tool_name=tool_name,
            idem_key="partner-upstream-unauthorized-wallet-permit",
        )
        response = await client.post(
            "/mcp/messages",
            json=_call_body(
                tool_name=tool_name,
                wallet_id=unauthorized["agent_wallet_id"],
                permit_id=permit["permit_id"],
                idempotency_key="partner-upstream-unauthorized-wallet-1",
            ),
            headers=authorized["agent_headers"],
        )

        assert response.json()["error"]["message"] == "wallet_access_denied"
        assert executor.calls == []
        factory = get_session_factory()
        async with factory() as session:
            attempts = (
                (await session.execute(select(McpDispatchAttemptModel))).scalars().all()
            )
            operation_debits = (
                (
                    await session.execute(
                        select(LedgerEntryModel).where(
                            LedgerEntryModel.operation_key.is_not(None)
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert attempts == []
        assert operation_debits == []
    finally:
        get_service_registry().unregister_local(tool_name)


async def _invoke_once(
    client: AsyncClient,
    *,
    tool_name: str,
    idempotency_key: str,
) -> dict[str, Any]:
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name=tool_name,
        idem_key=f"{idempotency_key}-permit",
    )
    body = _call_body(
        tool_name=tool_name,
        wallet_id=provisioned["agent_wallet_id"],
        permit_id=permit["permit_id"],
        idempotency_key=idempotency_key,
    )
    response = await client.post(
        "/mcp/messages", json=body, headers=provisioned["agent_headers"]
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


def _record_simulation(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Capture the ``simulation`` flag the invoke path hands to policy evaluation.

    ``simulation`` has no other observable effect on the response, the debit, or
    the receipt, so recording it here is the only way to pin the fail-open
    default the runtime-mode gate is required to preserve.
    """
    seen: list[bool] = []
    original = mcp_router.evaluate_wallet_policy

    async def _spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs["simulation"])
        return await original(*args, **kwargs)

    monkeypatch.setattr(mcp_router, "evaluate_wallet_policy", _spy)
    return seen


@pytest.mark.anyio
async def test_non_runtime_category_does_not_warn_runtime_mode_check_failed(
    client: AsyncClient,
    clean_database: None,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured upstream tool is registered under PLATFORM_FEE, which has no
    simulation flag. That is expected, not a runtime-mode failure."""
    tool_name = "partner-upstream-platform-fee"
    executor = FakeUpstreamExecutor("success")
    _register_upstream(tool_name, executor, category=ServiceCategory.PLATFORM_FEE)
    seen = _record_simulation(monkeypatch)
    try:
        with caplog.at_level(logging.DEBUG, logger="app.routers.mcp"):
            result = await _invoke_once(
                client, tool_name=tool_name, idempotency_key="platform-fee-1"
            )
        assert result["isError"] is False
        assert executor.dispatch_count == 1
        assert not [
            r for r in caplog.records if r.getMessage() == "runtime_mode_check_failed"
        ]
        # Fail-open default preserved: a flagless category is never simulated.
        assert seen == [False]
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_runtime_mode_failure_still_warns_and_fails_open(
    client: AsyncClient,
    clean_database: None,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real runtime-mode failure for a simulated service category still logs
    the warning and defaults to simulation=False (real effects)."""
    from app.core import runtime_mode

    def _broken(service: str) -> bool:
        raise AttributeError(f"Settings has no simulation flag for {service}")

    monkeypatch.setattr(runtime_mode, "is_simulation", _broken)

    tool_name = "partner-upstream-runtime-broken"
    executor = FakeUpstreamExecutor("success")
    _register_upstream(tool_name, executor, category=ServiceCategory.AGENT_COMMS)
    seen = _record_simulation(monkeypatch)
    try:
        with caplog.at_level(logging.WARNING, logger="app.routers.mcp"):
            result = await _invoke_once(
                client, tool_name=tool_name, idempotency_key="runtime-broken-1"
            )
        assert result["isError"] is False
        assert executor.dispatch_count == 1
        warnings = [
            r
            for r in caplog.records
            if r.getMessage() == "runtime_mode_check_failed"
            and r.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        assert warnings[0].category == "agent_comms"
        # Fail-open default preserved: a failed check means real effects.
        assert seen == [False]
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_simulated_runtime_category_still_uses_the_runtime_mode_result(
    client: AsyncClient,
    clean_database: None,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control for the gate's positive branch: a category that IS a runtime
    service still has its simulation flag read and passed through, silently."""
    from app.core.runtime_mode import SERVICE_NAMES, is_simulation

    assert ServiceCategory.AGENT_COMMS.value in SERVICE_NAMES
    assert is_simulation("agent_comms") is True, "agent_comms defaults to simulation"

    tool_name = "partner-upstream-simulated-pillar"
    executor = FakeUpstreamExecutor("success")
    _register_upstream(tool_name, executor, category=ServiceCategory.AGENT_COMMS)
    seen = _record_simulation(monkeypatch)
    try:
        with caplog.at_level(logging.DEBUG, logger="app.routers.mcp"):
            result = await _invoke_once(
                client, tool_name=tool_name, idempotency_key="simulated-pillar-1"
            )
        assert result["isError"] is False
        assert executor.dispatch_count == 1
        assert seen == [True]
        assert not [
            r for r in caplog.records if r.getMessage() == "runtime_mode_check_failed"
        ]
    finally:
        get_service_registry().unregister_local(tool_name)
