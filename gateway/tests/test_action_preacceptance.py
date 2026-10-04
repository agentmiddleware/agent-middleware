"""Preacceptance refusals must not spend single-action execution authority."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.db.database import get_session_factory
from app.db.models import McpDispatchAttemptModel, PermitModel, ReceiptModel
from tests.test_action_invocation import (
    action_runtime as _action_runtime,
    invoke_action,
    action_rows,
    assert_action_accounting,
)


action_runtime = _action_runtime


@pytest.fixture(autouse=True)
def isolated_services(monkeypatch):
    # Instance method patches must not shadow later class-level fault injection
    # after monkeypatch restores a bound method on a shared singleton.
    from app.services import agent_money, idempotency, mcp_dispatch_attempts, permits

    monkeypatch.setattr(agent_money, "_agent_money", agent_money.AgentMoney())
    monkeypatch.setattr(idempotency, "_service", idempotency.IdempotencyService())
    monkeypatch.setattr(
        mcp_dispatch_attempts,
        "_service",
        mcp_dispatch_attempts.McpDispatchAttemptService(),
    )
    monkeypatch.setattr(permits, "_service", permits.PermitService())
    yield
    from app.core import oidc_iga

    oidc_iga.reset_iga_counters()


async def assert_detached_refusal(runtime, response, reason):
    assert reason in response.text, response.text
    assert await action_rows() == []
    assert runtime[3].dispatch_count == 0
    async with get_session_factory()() as session:
        assert (
            not (await session.execute(select(McpDispatchAttemptModel))).scalars().all()
        )
        permit = await session.get(PermitModel, runtime[2])
        assert permit.spent_credits == 0
        assert not json.loads(permit.tool_call_counts_json or "{}")
        receipts = (
            (
                await session.execute(
                    select(ReceiptModel).where(ReceiptModel.permit_id == runtime[2])
                )
            )
            .scalars()
            .all()
        )
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.receipt_id in response.text
    assert receipt.idempotency_record_id is None
    assert receipt.dispatch_attempt_id is None
    assert receipt.ledger_entry_id is None
    assert receipt.credits_charged == 0
    assert receipt.audit_event_id
    from app.services.receipts import get_receipt_service

    assert (await get_receipt_service().verify_receipt(receipt.receipt_id))[0]


@pytest.mark.anyio
@pytest.mark.parametrize("surface", ["standard", "legacy", "rest"])
@pytest.mark.parametrize("exhaust_when", ["before", "after_precheck"])
async def test_exhausted_iga_can_renew_without_spending_action(
    action_runtime, monkeypatch, surface, exhaust_when
):
    from app.core import oidc_iga as iga
    from app.routers import mcp
    from app.services import policies

    iga.reset_iga_counters()
    principal = iga.EnterprisePrincipal(
        "fixture", "okta", "https://fixture.invalid", groups=("payer",)
    )
    grant = iga.IGAGrant("payer", "fixture-policy", max_uses=1)
    monkeypatch.setattr(iga, "resolve_policy_grants", lambda _: [grant])
    monkeypatch.setattr(
        policies,
        "get_policy_bundle",
        AsyncMock(
            return_value=SimpleNamespace(is_active=True, allowed_tools=["partner.pay"])
        ),
    )
    monkeypatch.setattr(mcp, "_verified_enterprise_principal", lambda _: principal)
    if exhaust_when == "after_precheck":
        exhausted = False

        async def exhaust_after_precheck(*args, **kwargs):
            nonlocal exhausted
            result = await iga.enforce_tool_call(*args, **kwargs)
            if kwargs.get("consume") is False and not exhausted:
                assert result.allowed
                assert (await iga.enforce_tool_call(principal, "partner.pay")).allowed
                exhausted = True
            return result

        monkeypatch.setattr(mcp, "enforce_tool_call", exhaust_after_precheck)
    try:
        if exhaust_when == "before":
            assert (await iga.enforce_tool_call(principal, "partner.pay")).allowed
        response = await invoke_action(action_runtime, surface, "exhausted")
        await assert_detached_refusal(action_runtime, response, "iga_max_uses_exceeded")
        iga.reset_iga_counters()
        response = await invoke_action(action_runtime, surface, "renewed")
        assert "error" not in response.json(), response.text
        # Completed replay must remain non-consuming after this last use.
        replay = await invoke_action(action_runtime, surface, "completed")
        assert "error" not in replay.json(), replay.text
        await assert_action_accounting(action_runtime)
    finally:
        iga.reset_iga_counters()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "gate",
    [
        "rollback",
        "permit",
        "permit_missing",
        "policy",
        "atomic_denial",
        "atomic_missing",
    ],
)
@pytest.mark.parametrize("real_iga", [False, True])
async def test_preacceptance_gate_can_recover(
    action_runtime, monkeypatch, gate, real_iga
):
    from app.routers import mcp
    from app.services.permits import get_permit_service, PermitValidation
    from app.services.mcp_dispatch_attempts import (
        get_mcp_dispatch_attempt_service,
        DispatchPrepareRolledBackError,
    )

    if real_iga:
        from app.core import oidc_iga as iga
        from app.services import policies

        iga.reset_iga_counters()
        principal = iga.EnterprisePrincipal(
            "fixture", "okta", "https://fixture.invalid", groups=("payer",)
        )
        grant = iga.IGAGrant("payer", "fixture-policy", max_uses=1)
        monkeypatch.setattr(iga, "resolve_policy_grants", lambda _: [grant])
        monkeypatch.setattr(
            policies,
            "get_policy_bundle",
            AsyncMock(
                return_value=SimpleNamespace(
                    is_active=True, allowed_tools=["partner.pay"]
                )
            ),
        )
        monkeypatch.setattr(mcp, "_verified_enterprise_principal", lambda _: principal)

    service = get_mcp_dispatch_attempt_service()
    calls = 0
    if gate in {"rollback", "atomic_denial", "atomic_missing"}:
        target, method = service, "authorize_reserve_and_prepare"
        reason = (
            "upstream_prepare_failed"
            if gate == "rollback"
            else "permit_budget_exceeded"
        )
    elif gate in {"permit", "permit_missing"}:
        target, method = get_permit_service(), "validate_for_action"
        reason = "permit_budget_exceeded"
    else:
        target, method = mcp, "evaluate_wallet_policy"
        reason = "policy_tool_not_allowed"
    original = getattr(target, method)

    async def deny(**kwargs):
        nonlocal calls
        calls += 1
        if gate == "rollback":
            raise DispatchPrepareRolledBackError("dispatch_prepare_rolled_back")
        if gate in {"atomic_denial", "atomic_missing"}:
            async with get_session_factory()() as session:
                permit = await session.get(PermitModel, action_runtime[2])
            return PermitValidation(
                allowed=False,
                reason=reason,
                permit=permit if gate == "atomic_denial" else None,
            ), None
        result = await original(**kwargs)
        if calls == 2:
            if gate in {"permit", "permit_missing"}:
                return PermitValidation(
                    allowed=False,
                    reason=reason,
                    permit=result.permit if gate == "permit" else None,
                )
            return SimpleNamespace(
                allowed=False,
                reason=reason,
                policy_id="fixture",
                evaluated_constraints={},
            )
        return result

    monkeypatch.setattr(target, method, deny)
    response = await invoke_action(action_runtime, "rest", "refused")
    if gate.endswith("missing"):
        assert reason in response.text
        assert await action_rows() == []
        assert action_runtime[3].dispatch_count == 0
    else:
        await assert_detached_refusal(action_runtime, response, reason)
    monkeypatch.setattr(target, method, original)
    response = await invoke_action(action_runtime, "rest", "restored")
    assert "error" not in response.json(), response.text
    await assert_action_accounting(action_runtime)


@pytest.mark.anyio
@pytest.mark.parametrize("uncertain", [False, True])
async def test_unknown_prepare_outcome_retains_owner(
    action_runtime, monkeypatch, uncertain
):
    from app.routers import mcp
    from app.core import oidc_iga as iga
    from app.services import policies

    iga.reset_iga_counters()
    principal = iga.EnterprisePrincipal(
        "fixture", "okta", "https://fixture.invalid", groups=("payer",)
    )
    grant = iga.IGAGrant("payer", "fixture-policy", max_uses=1)
    monkeypatch.setattr(iga, "resolve_policy_grants", lambda _: [grant])
    monkeypatch.setattr(
        policies,
        "get_policy_bundle",
        AsyncMock(
            return_value=SimpleNamespace(is_active=True, allowed_tools=["partner.pay"])
        ),
    )
    monkeypatch.setattr(mcp, "_verified_enterprise_principal", lambda _: principal)

    from app.services.mcp_dispatch_attempts import (
        get_mcp_dispatch_attempt_service,
        DispatchPrepareCommitUncertainError,
    )

    error = DispatchPrepareCommitUncertainError if uncertain else RuntimeError
    monkeypatch.setattr(
        get_mcp_dispatch_attempt_service(),
        "authorize_reserve_and_prepare",
        AsyncMock(side_effect=error("unknown")),
    )
    response = await invoke_action(action_runtime, "rest", "unknown")
    assert "idempotency_in_progress" in response.text
    owner = (await action_rows())[0]
    assert (
        await iga.enforce_tool_call(principal, "partner.pay")
    ).reason == "iga_max_uses_exceeded"
    assert owner.response_json is None
    async with get_session_factory()() as session:
        assert not (await session.execute(select(ReceiptModel))).scalars().all()
    assert action_runtime[3].dispatch_count == 0


@pytest.mark.anyio
async def test_funding_refusal_after_acceptance_keeps_owner(
    action_runtime, monkeypatch
):
    from app.services.agent_money import get_agent_money
    from app.schemas.billing import InsufficientFundsResponse

    money = get_agent_money()
    original = money.charge
    monkeypatch.setattr(
        money,
        "charge",
        AsyncMock(
            return_value=InsufficientFundsResponse(
                wallet_id=action_runtime[1]["agent_wallet_id"],
                current_balance=0,
                required_amount=2,
                shortfall=2,
                top_up_url="https://fixture.invalid",
            )
        ),
    )
    denied = await invoke_action(action_runtime, "rest", "unfunded")
    assert "insufficient_funds" in denied.text, denied.text
    owner = (await action_rows())[0]
    assert owner.response_json is not None
    async with get_session_factory()() as session:
        attempt = (await session.execute(select(McpDispatchAttemptModel))).scalar_one()
        permit = await session.get(PermitModel, action_runtime[2])
        assert attempt.idempotency_record_id == owner.record_id
        assert permit.spent_credits == 0
    monkeypatch.setattr(money, "charge", original)
    retry = await invoke_action(action_runtime, "rest", "funded")
    assert "insufficient_funds" in retry.text
    assert (await action_rows())[0].model_dump() == owner.model_dump()
    assert action_runtime[3].dispatch_count == 0


@pytest.mark.anyio
@pytest.mark.parametrize("race", ["prepared", "replaced"])
async def test_preacceptance_cleanup_cannot_release_changed_owner(
    action_runtime, monkeypatch, race
):
    from app.services.idempotency import get_idempotency_service
    from app.services.mcp_dispatch_attempts import (
        get_mcp_dispatch_attempt_service,
        DispatchPrepareRolledBackError,
    )
    from tests.test_action_invocation import prepare_action
    from app.db.models import IdempotencyRecordModel

    from app.routers import mcp
    from app.core import oidc_iga as iga
    from app.services import policies

    iga.reset_iga_counters()
    principal = iga.EnterprisePrincipal(
        "fixture", "okta", "https://fixture.invalid", groups=("payer",)
    )
    grant = iga.IGAGrant("payer", "fixture-policy", max_uses=1)
    monkeypatch.setattr(iga, "resolve_policy_grants", lambda _: [grant])
    monkeypatch.setattr(
        policies,
        "get_policy_bundle",
        AsyncMock(
            return_value=SimpleNamespace(is_active=True, allowed_tools=["partner.pay"])
        ),
    )
    monkeypatch.setattr(mcp, "_verified_enterprise_principal", lambda _: principal)

    service = get_mcp_dispatch_attempt_service()
    original_prepare = service.authorize_reserve_and_prepare
    monkeypatch.setattr(
        service,
        "authorize_reserve_and_prepare",
        AsyncMock(side_effect=DispatchPrepareRolledBackError("rollback")),
    )
    idem = get_idempotency_service()
    original_abandon = idem.abandon

    async def race_abandon(**kwargs):
        if race == "prepared":
            monkeypatch.setattr(
                service, "authorize_reserve_and_prepare", original_prepare
            )
            accepted, _ = await prepare_action(
                action_runtime, {"amount_minor": 1, "recipient": "alice"}
            )
            assert accepted.allowed
        else:
            async with get_session_factory()() as session:
                owner = await session.get(
                    IdempotencyRecordModel, kwargs["expected_record_id"]
                )
                owner.record_id = "replacement-owner"
                await session.commit()
        return await original_abandon(**kwargs)

    monkeypatch.setattr(idem, "abandon", race_abandon)
    response = await invoke_action(action_runtime, "rest", "raced")
    assert "idempotency_in_progress" in response.text, response.text
    assert (
        await iga.enforce_tool_call(principal, "partner.pay")
    ).reason == "iga_max_uses_exceeded"
    assert len(await action_rows()) == 1
    async with get_session_factory()() as session:
        assert not (await session.execute(select(ReceiptModel))).scalars().all()
    assert action_runtime[3].dispatch_count == 0


@pytest.mark.anyio
@pytest.mark.parametrize("marker", ["atomic", "missing_slot", "missing_counter"])
async def test_action_recovery_requires_atomic_call_reservation(
    action_runtime, monkeypatch, marker
):
    from tests.test_action_invocation import prepare_action
    from app.services.mcp_dispatch_attempts import (
        get_mcp_dispatch_attempt_service,
        DispatchPrepareCommitUncertainError,
    )

    args = {"amount_minor": 1, "recipient": "alice"}
    accepted, prepared = await prepare_action(action_runtime, args)
    assert accepted.allowed and prepared.call_slot_reserved
    async with get_session_factory()() as session:
        permit = await session.get(PermitModel, action_runtime[2])
        assert json.loads(permit.tool_call_counts_json) == {"partner.pay": 1}
        if marker == "missing_slot":
            attempt = await session.get(McpDispatchAttemptModel, prepared.attempt_id)
            attempt.call_slot_reserved = False
        elif marker == "missing_counter":
            permit.tool_call_counts_json = "{}"
        await session.commit()
    service = get_mcp_dispatch_attempt_service()
    original = service._get_by_idempotency_record
    calls = 0

    async def fail_first(session, record_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("synthetic ACK loss")
        return await original(session, record_id)

    monkeypatch.setattr(service, "_get_by_idempotency_record", fail_first)
    if marker == "atomic":
        validation, adopted = await prepare_action(action_runtime, args)
        assert validation.allowed and adopted.attempt_id == prepared.attempt_id
    else:
        with pytest.raises(
            DispatchPrepareCommitUncertainError,
            match="dispatch_prepare_constraint_unsupported",
        ):
            await prepare_action(action_runtime, args)
    assert action_runtime[3].dispatch_count == 0


@pytest.fixture
def limited_iga(monkeypatch):
    from app.core import oidc_iga as iga
    from app.routers import mcp
    from app.services import policies

    iga.reset_iga_counters()
    principal = iga.EnterprisePrincipal(
        "fixture", "okta", "https://fixture.invalid", groups=("payer",)
    )
    grant = iga.IGAGrant("payer", "fixture-policy", max_uses=1)
    monkeypatch.setattr(iga, "resolve_policy_grants", lambda _: [grant])
    monkeypatch.setattr(
        policies,
        "get_policy_bundle",
        AsyncMock(
            return_value=SimpleNamespace(is_active=True, allowed_tools=["partner.pay"])
        ),
    )
    monkeypatch.setattr(mcp, "_verified_enterprise_principal", lambda _: principal)
    release = AsyncMock(wraps=mcp.release_tool_use)
    monkeypatch.setattr(mcp, "release_tool_use", release)
    return principal, release


async def invoke_with_optional_key(runtime, client_key):
    if client_key:
        return await invoke_action(runtime, "rest", client_key)
    client, wallets, permit_id, _ = runtime
    return await client.post(
        "/mcp/tools/partner.pay/invoke",
        json={
            "name": "partner.pay",
            "arguments": {"amount_minor": 1, "recipient": "alice"},
            "mcp_context": {
                "wallet_id": wallets["agent_wallet_id"],
                "permit_id": permit_id,
            },
        },
        headers=wallets["agent_headers"],
    )


def inject_preacceptance_contention(monkeypatch, contention):
    from app.routers import mcp
    from app.services.audit_chain import AuditChainContendedError
    from app.services.mcp_dispatch_attempts import get_mcp_dispatch_attempt_service
    from app.services.permits import PermitWriteContendedError
    from app.services.receipts import get_receipt_service, ReceiptWriteContendedError

    if contention == "permit":
        monkeypatch.setattr(
            get_mcp_dispatch_attempt_service(),
            "authorize_reserve_and_prepare",
            AsyncMock(side_effect=PermitWriteContendedError()),
        )
        return
    original_policy = mcp.evaluate_wallet_policy
    calls = 0

    async def deny_after_precheck(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            return SimpleNamespace(
                allowed=False,
                reason="policy_tool_not_allowed",
                policy_id="fixture-policy",
                evaluated_constraints={},
            )
        return await original_policy(**kwargs)

    monkeypatch.setattr(mcp, "evaluate_wallet_policy", deny_after_precheck)
    if contention == "audit":
        monkeypatch.setattr(
            mcp,
            "_audit_mcp_invocation",
            AsyncMock(side_effect=AuditChainContendedError()),
        )
    else:
        # The denial releases the owner and IGA use before writing its receipt.
        monkeypatch.setattr(
            type(get_receipt_service()),
            "create_receipt",
            AsyncMock(side_effect=ReceiptWriteContendedError()),
        )


@pytest.mark.anyio
@pytest.mark.parametrize("client_key", [None, "client-key"])
@pytest.mark.parametrize("contention", ["audit", "permit", "receipt"])
async def test_action_contention_returns_only_unaccepted_iga_use(
    action_runtime, monkeypatch, limited_iga, client_key, contention
):
    from app.db.models import LedgerEntryModel

    principal, release = limited_iga
    with monkeypatch.context() as patch:
        inject_preacceptance_contention(patch, contention)
        response = await invoke_with_optional_key(action_runtime, client_key)
    assert response.status_code == 409, response.text
    assert await action_rows() == []
    assert action_runtime[3].dispatch_count == 0
    async with get_session_factory()() as session:
        assert (
            not (await session.execute(select(McpDispatchAttemptModel))).scalars().all()
        )
        assert not (await session.execute(select(ReceiptModel))).scalars().all()
        assert (
            not (
                await session.execute(
                    select(LedgerEntryModel).where(
                        LedgerEntryModel.operation_key.is_not(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        permit = await session.get(PermitModel, action_runtime[2])
        assert permit.spent_credits == 0
        assert not json.loads(permit.tool_call_counts_json or "{}")
    reservation = release.await_args.kwargs["reservation"]
    assert reservation.released is True
    assert reservation.counter_key == (
        principal.issuer,
        principal.subject,
        "partner.pay",
        "payer",
        "fixture-policy",
    )
    release.assert_awaited_once_with(
        principal,
        "partner.pay",
        group="payer",
        policy_id="fixture-policy",
        reservation=reservation,
    )
    retry = await invoke_with_optional_key(action_runtime, client_key)
    assert retry.status_code == 200, retry.text
    replay = await invoke_with_optional_key(action_runtime, client_key)
    assert replay.status_code == 200, replay.text
    assert release.await_count == 1
    await assert_action_accounting(action_runtime)


@pytest.mark.anyio
@pytest.mark.parametrize("client_key", [None, "client-key"])
@pytest.mark.parametrize(
    "cleanup", ["prepared", "replaced", "uncertain", "failed", "ack_lost"]
)
async def test_action_contention_keeps_iga_without_confirmed_owner_deletion(
    action_runtime, monkeypatch, limited_iga, client_key, cleanup
):
    from app.core import oidc_iga as iga
    from app.db.models import IdempotencyRecordModel
    from app.services.idempotency import get_idempotency_service
    from tests.test_action_invocation import prepare_action

    principal, release = limited_iga
    idem = get_idempotency_service()
    original_abandon = idem.abandon

    async def cannot_confirm_abandon(**kwargs):
        if cleanup == "failed":
            raise RuntimeError("synthetic cleanup failure")
        if cleanup == "ack_lost":
            assert await original_abandon(**kwargs)
            raise RuntimeError("synthetic commit acknowledgement loss")
        if cleanup == "prepared":
            accepted, _ = await prepare_action(
                action_runtime, {"amount_minor": 1, "recipient": "alice"}
            )
            assert accepted.allowed
        else:
            async with get_session_factory()() as session:
                owner = await session.get(
                    IdempotencyRecordModel, kwargs["expected_record_id"]
                )
                if cleanup == "replaced":
                    owner.record_id = "replacement-owner"
                else:
                    # A mismatched owner hash cannot prove non-acceptance.
                    owner.request_hash = "uncertain"
                await session.commit()
        return await original_abandon(**kwargs)

    inject_preacceptance_contention(monkeypatch, "audit")
    abandon = AsyncMock(wraps=cannot_confirm_abandon)
    monkeypatch.setattr(idem, "abandon", abandon)
    response = await invoke_with_optional_key(action_runtime, client_key)
    assert response.status_code == 409, response.text
    abandon.assert_awaited_once()
    release.assert_not_awaited()
    assert (
        await iga.enforce_tool_call(principal, "partner.pay")
    ).reason == "iga_max_uses_exceeded"
    owners = await action_rows()
    assert len(owners) == (0 if cleanup == "ack_lost" else 1)
    if cleanup == "replaced":
        assert owners[0].record_id == "replacement-owner"
    assert action_runtime[3].dispatch_count == 0
    async with get_session_factory()() as session:
        assert not (await session.execute(select(ReceiptModel))).scalars().all()
