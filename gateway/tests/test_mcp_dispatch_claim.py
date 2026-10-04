from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSessionTransaction

from app.db.database import get_session_factory
from app.db.models import LedgerEntryModel, McpDispatchAttemptModel, PermitModel
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.agent_money import get_agent_money
from app.services.idempotency import get_idempotency_service
from app.services.mcp_dispatch_attempts import (
    DISPATCH_CLAIMED,
    MAX_UPSTREAM_CALL_TIMEOUT_SECONDS,
    MAX_UPSTREAM_CONNECT_TIMEOUT_SECONDS,
    DispatchAttemptConflictError,
    DispatchClaimUnavailableError,
    McpDispatchAttemptService,
    dispatch_reconciliation_idle_seconds,
    get_mcp_dispatch_attempt_service,
)
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as value:
        yield value


async def _prepare_attempt(
    client: AsyncClient,
    *,
    suffix: str,
    charge: bool,
) -> tuple[McpDispatchAttemptService, McpDispatchAttemptModel]:
    provisioned = await provision_agent_wallet(client)
    tool_name = f"partner-tool-{suffix}"
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name=tool_name,
        idem_key=f"permit-{suffix}",
    )
    begun = await get_idempotency_service().begin_with_record(
        wallet_id=provisioned["agent_wallet_id"],
        endpoint="/mcp/messages",
        idempotency_key=f"invoke-{suffix}",
        request_payload={"tool": tool_name, "arguments": {"value": suffix}},
    )
    service = get_mcp_dispatch_attempt_service()
    attempt = await service.prepare(
        idempotency_record_id=begun.record_id,
        wallet_id=provisioned["agent_wallet_id"],
        permit_id=permit["permit_id"],
        key_id=provisioned["key_id"],
        public_tool_id=tool_name,
        upstream_tool_name="remote_tool",
        upstream_origin="https://partner.example",
        request_hash=begun.request_hash,
        credits_authorized=Decimal("1.5"),
    )
    if not charge:
        return service, attempt

    debit = await get_agent_money().charge(
        wallet_id=provisioned["agent_wallet_id"],
        service_category=ServiceCategory.AGENT_COMMS,
        units=Decimal("1"),
        request_path="/mcp/messages",
        operation_key=begun.record_id,
    )
    attempt = await service.attach_charge(
        attempt_id=attempt.attempt_id,
        ledger_entry_id=debit.entry_id,
        credits_charged=Decimal("1.5"),
    )
    return service, attempt


async def _prepare_reserved_attempt(
    client: AsyncClient,
    *,
    suffix: str,
) -> tuple[McpDispatchAttemptService, McpDispatchAttemptModel, str]:
    provisioned = await provision_agent_wallet(client)
    tool_name = f"partner-reserved-{suffix}"
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name=tool_name,
        idem_key=f"permit-reserved-{suffix}",
    )
    begun = await get_idempotency_service().begin_with_record(
        wallet_id=provisioned["agent_wallet_id"],
        endpoint="/mcp/messages",
        idempotency_key=f"invoke-reserved-{suffix}",
        request_payload={"tool": tool_name, "arguments": {"value": suffix}},
        operation_kind="upstream_mcp",
    )
    service = get_mcp_dispatch_attempt_service()
    validation, attempt = await service.authorize_reserve_and_prepare(
        idempotency_record_id=begun.record_id,
        wallet_id=provisioned["agent_wallet_id"],
        permit_id=permit["permit_id"],
        key_id=provisioned["key_id"],
        public_tool_id=tool_name,
        upstream_tool_name="remote_tool",
        upstream_origin="https://partner.example",
        request_hash=begun.request_hash,
        credits_authorized=Decimal("1.5"),
        arguments={"value": suffix},
    )
    assert validation.allowed is True
    assert attempt is not None
    return service, attempt, permit["permit_id"]


def test_dispatch_idle_window_covers_full_transport_lifetime() -> None:
    default_window = dispatch_reconciliation_idle_seconds(
        connect_timeout_seconds=5,
        call_timeout_seconds=30,
    )
    maximum_window = dispatch_reconciliation_idle_seconds(
        connect_timeout_seconds=MAX_UPSTREAM_CONNECT_TIMEOUT_SECONDS,
        call_timeout_seconds=MAX_UPSTREAM_CALL_TIMEOUT_SECONDS,
    )

    # A rolling worker whose local settings shrink must preserve the same
    # globally conservative stale threshold as the worker that claimed.
    assert default_window == maximum_window
    assert maximum_window > (
        MAX_UPSTREAM_CONNECT_TIMEOUT_SECONDS + (3 * MAX_UPSTREAM_CALL_TIMEOUT_SECONDS)
    )
    with pytest.raises(ValueError, match="dispatch_timeout_invalid"):
        dispatch_reconciliation_idle_seconds(
            connect_timeout_seconds=float("nan"),
            call_timeout_seconds=30,
        )
    with pytest.raises(ValueError, match="dispatch_timeout_exceeds_supported_maximum"):
        dispatch_reconciliation_idle_seconds(
            connect_timeout_seconds=1e308,
            call_timeout_seconds=30,
        )
    with pytest.raises(ValueError, match="dispatch_timeout_exceeds_supported_maximum"):
        dispatch_reconciliation_idle_seconds(
            connect_timeout_seconds=MAX_UPSTREAM_CONNECT_TIMEOUT_SECONDS + 0.001,
            call_timeout_seconds=MAX_UPSTREAM_CALL_TIMEOUT_SECONDS,
        )
    with pytest.raises(ValueError, match="dispatch_timeout_exceeds_supported_maximum"):
        dispatch_reconciliation_idle_seconds(
            connect_timeout_seconds=5,
            call_timeout_seconds=1e308,
        )
    with pytest.raises(ValueError, match="dispatch_timeout_exceeds_supported_maximum"):
        dispatch_reconciliation_idle_seconds(
            connect_timeout_seconds=MAX_UPSTREAM_CONNECT_TIMEOUT_SECONDS,
            call_timeout_seconds=MAX_UPSTREAM_CALL_TIMEOUT_SECONDS + 0.001,
        )


@pytest.mark.anyio
async def test_dispatch_claim_is_durable_and_cannot_be_reacquired(
    client: AsyncClient,
    clean_database,
) -> None:
    service, attempt = await _prepare_attempt(
        client,
        suffix="one-shot",
        charge=True,
    )

    claimed = await service.claim_dispatch(attempt.attempt_id)

    assert claimed.state == DISPATCH_CLAIMED
    assert claimed.dispatch_claim_hash is not None
    assert len(claimed.dispatch_claim_hash) == 64
    with pytest.raises(
        DispatchClaimUnavailableError,
        match="dispatch_claim_unavailable",
    ):
        await service.claim_dispatch(attempt.attempt_id)

    completed = await service.complete(
        attempt_id=attempt.attempt_id,
        state="succeeded",
        result_payload={"ok": True},
        error_code=None,
        max_result_bytes=1024,
    )
    assert completed.state == "succeeded"


@pytest.mark.parametrize(
    "corruption",
    [
        "attempt_amount",
        "ledger_wallet",
        "ledger_action",
        "ledger_operation",
        "ledger_amount",
    ],
)
@pytest.mark.anyio
async def test_dispatch_claim_refuses_invalid_debit_linkage_before_send(
    client: AsyncClient,
    clean_database,
    corruption: str,
) -> None:
    service, attempt = await _prepare_attempt(
        client,
        suffix=f"invalid-debit-{corruption}",
        charge=True,
    )
    forged_wallet_id = None
    if corruption == "ledger_wallet":
        forged_wallet_id = (await provision_agent_wallet(client))["agent_wallet_id"]
    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            stored = await session.get(McpDispatchAttemptModel, attempt.attempt_id)
            assert stored is not None
            assert stored.ledger_entry_id is not None
            ledger = await session.get(LedgerEntryModel, stored.ledger_entry_id)
            assert ledger is not None
            if corruption == "attempt_amount":
                stored.credits_charged = Decimal("1.25")
                session.add(stored)
            elif corruption == "ledger_wallet":
                assert forged_wallet_id is not None
                ledger.wallet_id = forged_wallet_id
                session.add(ledger)
            elif corruption == "ledger_action":
                ledger.action = "refund"
                session.add(ledger)
            elif corruption == "ledger_operation":
                ledger.operation_key = "idm-forged-operation"
                session.add(ledger)
            else:
                ledger.amount = Decimal("-1.25")
                session.add(ledger)

    with pytest.raises(
        DispatchAttemptConflictError,
        match="dispatch_claim_evidence_invalid",
    ):
        await service.claim_dispatch(attempt.attempt_id)

    context = await service.get_context(attempt.attempt_id)
    assert context is not None
    assert context.attempt.state == "prepared"
    assert context.attempt.dispatch_claim_hash is None
    assert context.attempt.dispatched_at is None


@pytest.mark.parametrize(
    ("field_name", "corrupt_value"),
    [
        ("dispatch_claim_hash", None),
        ("dispatch_claim_hash", "g" * 64),
        ("dispatched_at", None),
        ("ledger_entry_id", None),
        ("credits_charged", Decimal("0")),
        ("credits_charged", Decimal("-1")),
    ],
    ids=[
        "missing-claim-hash",
        "malformed-claim-hash",
        "missing-dispatched-at",
        "missing-ledger-entry",
        "zero-charge",
        "negative-charge",
    ],
)
@pytest.mark.anyio
async def test_dispatch_claimed_corruption_cannot_be_terminalized(
    client: AsyncClient,
    clean_database,
    field_name: str,
    corrupt_value: object,
) -> None:
    service, attempt = await _prepare_attempt(
        client,
        suffix=f"corrupt-{field_name}",
        charge=True,
    )
    await service.claim_dispatch(attempt.attempt_id)

    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            stored = await session.get(McpDispatchAttemptModel, attempt.attempt_id)
            assert stored is not None
            setattr(stored, field_name, corrupt_value)
            session.add(stored)

    with pytest.raises(
        DispatchAttemptConflictError,
        match="dispatch_claim_evidence_invalid",
    ):
        await service.complete(
            attempt_id=attempt.attempt_id,
            state="succeeded",
            result_payload={"ok": True},
            error_code=None,
            max_result_bytes=1024,
        )

    context = await service.get_context(attempt.attempt_id)
    assert context is not None
    assert context.attempt.state == DISPATCH_CLAIMED
    assert context.attempt.completed_at is None


@pytest.mark.parametrize(
    ("field_name", "corrupt_value"),
    [
        ("action", "refund"),
        ("amount", Decimal("-1.25")),
    ],
)
@pytest.mark.anyio
async def test_dispatch_claimed_ledger_tampering_cannot_be_terminalized(
    client: AsyncClient,
    clean_database,
    field_name: str,
    corrupt_value: object,
) -> None:
    service, attempt = await _prepare_attempt(
        client,
        suffix=f"post-claim-ledger-{field_name}",
        charge=True,
    )
    await service.claim_dispatch(attempt.attempt_id)

    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            stored = await session.get(McpDispatchAttemptModel, attempt.attempt_id)
            assert stored is not None and stored.ledger_entry_id is not None
            ledger = await session.get(LedgerEntryModel, stored.ledger_entry_id)
            assert ledger is not None
            setattr(ledger, field_name, corrupt_value)
            session.add(ledger)

    with pytest.raises(
        DispatchAttemptConflictError,
        match="dispatch_claim_evidence_invalid",
    ):
        await service.complete(
            attempt_id=attempt.attempt_id,
            state="succeeded",
            result_payload={"ok": True},
            error_code=None,
            max_result_bytes=1024,
        )

    context = await service.get_context(attempt.attempt_id)
    assert context is not None
    assert context.attempt.state == DISPATCH_CLAIMED
    assert context.attempt.completed_at is None


@pytest.mark.anyio
async def test_pre_dispatch_failure_cannot_overwrite_a_claim(
    client: AsyncClient,
    clean_database,
) -> None:
    service, attempt = await _prepare_attempt(
        client,
        suffix="claim-beats-cleanup",
        charge=True,
    )
    expected_updated_at = attempt.updated_at
    claimed = await service.claim_dispatch(attempt.attempt_id)

    with pytest.raises(
        DispatchClaimUnavailableError,
        match="dispatch_claim_unavailable",
    ):
        await service.complete_pre_dispatch_failure(
            attempt_id=attempt.attempt_id,
            expected_updated_at=expected_updated_at,
            ledger_entry_id=attempt.ledger_entry_id,
            credits_charged=attempt.credits_charged,
            result_payload={"error": "connect_failed"},
            error_code="connect_failed",
            max_result_bytes=1024,
        )

    context = await service.get_context(attempt.attempt_id)
    assert context is not None
    assert context.attempt.state == DISPATCH_CLAIMED
    assert context.attempt.dispatch_claim_hash == claimed.dispatch_claim_hash
    assert context.attempt.completed_at is None


@pytest.mark.anyio
async def test_dispatch_claim_recovers_lost_commit_acknowledgement(
    client: AsyncClient,
    clean_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, attempt = await _prepare_attempt(
        client,
        suffix="claim-lost-ack",
        charge=True,
    )
    original_exit = AsyncSessionTransaction.__aexit__
    acknowledgement_lost = False

    async def lose_commit_ack(self, exc_type, exc, tb):  # noqa: ANN001
        nonlocal acknowledgement_lost
        result = await original_exit(self, exc_type, exc, tb)
        if exc_type is None and not acknowledgement_lost:
            acknowledgement_lost = True
            raise RuntimeError("simulated_claim_commit_ack_loss")
        return result

    monkeypatch.setattr(AsyncSessionTransaction, "__aexit__", lose_commit_ack)

    claimed = await service.claim_dispatch(attempt.attempt_id)

    assert acknowledgement_lost is True
    assert claimed.state == DISPATCH_CLAIMED
    assert claimed.dispatch_claim_hash is not None


@pytest.mark.anyio
async def test_effect_free_abandon_recovers_lost_commit_acknowledgement(
    client: AsyncClient,
    clean_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, attempt, permit_id = await _prepare_reserved_attempt(
        client,
        suffix="abandon-lost-ack",
    )
    original_exit = AsyncSessionTransaction.__aexit__
    acknowledgement_lost = False

    async def lose_commit_ack(self, exc_type, exc, tb):  # noqa: ANN001
        nonlocal acknowledgement_lost
        result = await original_exit(self, exc_type, exc, tb)
        if exc_type is None and not acknowledgement_lost:
            acknowledgement_lost = True
            raise RuntimeError("simulated_abandon_commit_ack_loss")
        return result

    monkeypatch.setattr(AsyncSessionTransaction, "__aexit__", lose_commit_ack)

    await service.abandon_effect_free_prepared_attempt(
        attempt_id=attempt.attempt_id,
        expected_updated_at=attempt.updated_at,
    )

    assert acknowledgement_lost is True
    factory = get_session_factory()
    async with factory() as session:
        stored_attempt = await session.get(
            McpDispatchAttemptModel,
            attempt.attempt_id,
        )
        stored_permit = await session.get(PermitModel, permit_id)
    assert stored_attempt is None
    assert stored_permit is not None
    assert stored_permit.spent_credits == Decimal("0")


@pytest.mark.anyio
async def test_effect_free_abandon_refuses_an_operation_with_a_debit(
    client: AsyncClient,
    clean_database,
) -> None:
    service, attempt, permit_id = await _prepare_reserved_attempt(
        client,
        suffix="abandon-after-debit",
    )
    debit = await get_agent_money().charge(
        wallet_id=attempt.wallet_id,
        service_category=ServiceCategory.AGENT_COMMS,
        units=Decimal("1"),
        request_path="/mcp/messages",
        operation_key=attempt.idempotency_record_id,
    )
    assert debit.entry_id

    with pytest.raises(
        DispatchClaimUnavailableError,
        match="dispatch_attempt_advanced",
    ):
        await service.abandon_effect_free_prepared_attempt(
            attempt_id=attempt.attempt_id,
            expected_updated_at=attempt.updated_at,
        )

    factory = get_session_factory()
    async with factory() as session:
        stored_attempt = await session.get(
            McpDispatchAttemptModel,
            attempt.attempt_id,
        )
        stored_permit = await session.get(PermitModel, permit_id)
    assert stored_attempt is not None
    assert stored_attempt.state == "prepared"
    assert stored_permit is not None
    assert stored_permit.spent_credits == Decimal("1.5")


@pytest.mark.anyio
async def test_legacy_dispatched_attempt_only_allows_uncertain_completion(
    client: AsyncClient,
    clean_database,
) -> None:
    service, attempt = await _prepare_attempt(
        client,
        suffix="legacy-dispatched",
        charge=True,
    )
    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            stored = await session.get(McpDispatchAttemptModel, attempt.attempt_id)
            assert stored is not None
            stored.state = "dispatched"
            stored.dispatched_at = stored.updated_at
            session.add(stored)

    with pytest.raises(
        DispatchAttemptConflictError,
        match="dispatch_terminal_transition_invalid",
    ):
        await service.complete(
            attempt_id=attempt.attempt_id,
            state="succeeded",
            result_payload={"ok": True},
            error_code=None,
            max_result_bytes=1024,
        )

    terminal = await service.complete(
        attempt_id=attempt.attempt_id,
        state="delivery_uncertain",
        result_payload=None,
        error_code="legacy_dispatch_without_claim",
        max_result_bytes=1024,
    )
    assert terminal.state == "delivery_uncertain"
