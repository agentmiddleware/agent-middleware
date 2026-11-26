"""Offline action recovery, retention, and signed evidence contracts."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import OperationalError

from app.core.time import utc_now
from app.db.database import get_session_factory
from app.db.models import PermitModel, ReceiptModel
from app.services.action_permits import action_execution_identity
from app.services.agent_money import get_agent_money
from app.services.idempotency import (
    get_idempotency_service,
    IdempotencyConflictError,
    IdempotencyInProgressError,
)
from app.services.mcp_dispatch_attempts import (
    DispatchClaimUnavailableError,
    DispatchPrepareCommitUncertainError,
    get_mcp_dispatch_attempt_service,
)
from tests.test_action_invocation import (
    action_runtime as _action_runtime,
    action_rows,
    action_permit,
    invoke_action,
    prepare_action,
)
from tests.test_action_permits import BINDING

action_runtime = _action_runtime

ARGS = {"amount_minor": 1, "recipient": "alice"}


async def unstarted(runtime):
    async with get_session_factory()() as session:
        permit = await session.get(PermitModel, runtime[2])
    identity = action_execution_identity(permit, BINDING)
    await get_idempotency_service().begin_action_with_record(
        wallet_id=permit.subject_wallet_id, identity=identity
    )
    return (await action_rows())[0]


@pytest.mark.anyio
async def test_idle_unstarted_owner_can_clear(action_runtime):
    owner = await unstarted(action_runtime)
    async with get_session_factory()() as session:
        stored = await session.get(type(owner), owner.record_id)
        stored.created_at = utc_now() - timedelta(hours=1)
        await session.commit()
    assert (await get_idempotency_service().reconcile_stuck_records(idle_seconds=1))[
        0
    ] == 1
    assert await action_rows() == []


@pytest.mark.anyio
async def test_abandon_requires_exact_unstarted_owner(action_runtime):
    owner = await unstarted(action_runtime)
    idem = get_idempotency_service()
    kwargs = dict(
        wallet_id=owner.wallet_id,
        endpoint=owner.endpoint,
        idempotency_key=owner.idempotency_key,
    )
    await idem.abandon(**kwargs)
    assert len(await action_rows()) == 1
    await idem.abandon(**kwargs, expected_record_id="other")
    assert len(await action_rows()) == 1
    await idem.abandon(**kwargs, expected_record_id=owner.record_id)
    assert await action_rows() == []


@pytest.mark.anyio
async def test_prepared_owner_cannot_be_unwound(action_runtime):
    validation, attempt = await prepare_action(action_runtime, ARGS)
    assert validation.allowed
    with pytest.raises(DispatchClaimUnavailableError):
        await get_mcp_dispatch_attempt_service().abandon_effect_free_prepared_attempt(
            attempt_id=attempt.attempt_id, expected_updated_at=attempt.updated_at
        )
    owner = (await action_rows())[0]
    await get_idempotency_service().abandon(
        wallet_id=owner.wallet_id,
        endpoint=owner.endpoint,
        idempotency_key=owner.idempotency_key,
        expected_record_id=owner.record_id,
    )
    assert len(await action_rows()) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("changed", [False, True])
async def test_prepare_commit_ack_loss_revalidates(
    action_runtime, monkeypatch, changed
):
    # First establish a prepared row. A failed owner-lock read follows the same
    # recovery path as a commit ACK loss, without relying on a driver internals.
    accepted, original = await prepare_action(action_runtime, ARGS)
    assert accepted.allowed
    execute = AsyncSession.execute
    injected = False

    async def fail_once(self, statement, *args, **kwargs):
        nonlocal injected
        if not injected and getattr(statement, "_for_update_arg", None) is not None:
            injected = True
            raise OperationalError("synthetic ACK loss", {}, RuntimeError("offline"))
        return await execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", fail_once)
    if changed:
        with pytest.raises(DispatchPrepareCommitUncertainError):
            await prepare_action(action_runtime, {**ARGS, "amount_minor": 2})
    else:
        validation, recovered = await prepare_action(action_runtime, ARGS)
        assert validation.allowed
        assert recovered.attempt_id == original.attempt_id
    assert injected
    assert action_runtime[3].dispatch_count == 0


@pytest.mark.anyio
async def test_action_receipt_binding_signed_and_native_key_stable(action_runtime):
    response = await invoke_action(action_runtime, "standard", "first")
    assert "error" not in response.json(), response.text
    owner = (await action_rows())[0]
    receipt = json.loads(owner.response_json)["receipt"]
    async with get_session_factory()() as session:
        permit = await session.get(PermitModel, action_runtime[2])
        model = await session.get(ReceiptModel, receipt["receipt_id"])
    for field in (
        "action_contract_version",
        "action_payload_hash",
        "action_schema_id",
        "action_schema_version",
        "action_public_tool_id",
        "action_upstream_binding_hash",
    ):
        assert receipt[field] == getattr(permit, field)
    from app.services.receipts import get_receipt_service

    service = get_receipt_service()
    assert (await service.verify_receipt(model.receipt_id))[0]
    payload = json.loads(await service.signing_input(model.receipt_id))
    assert payload["action_payload_hash"] == permit.action_payload_hash
    identity = action_execution_identity(permit, BINDING)
    assert (
        action_runtime[3].calls[0]["idempotency_key"] == identity.native_idempotency_key
    )
    replay = await invoke_action(action_runtime, "legacy", "fresh")
    assert "error" not in replay.json(), replay.text
    assert action_runtime[3].dispatch_count == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "evidence", ["reservation", "released_reservation", "orphan_debit", "receipt"]
)
async def test_unstarted_cleanup_rejects_accounting_evidence(action_runtime, evidence):
    from decimal import Decimal
    from app.db.models import LedgerEntryModel
    from app.services.receipts import get_receipt_service
    from app.services.idempotency import may_abandon_action_owner

    owner = await unstarted(action_runtime)
    if evidence == "receipt":
        await get_receipt_service().create_receipt(
            permit_id=action_runtime[2],
            wallet_id=owner.wallet_id,
            key_id=action_runtime[1]["key_id"],
            tool="partner.pay",
            request_payload=None,
            request_hash=owner.request_hash,
            response_payload=None,
            ledger_entry_id=None,
            credits_authorized=Decimal("2"),
            credits_charged=Decimal("0"),
            outcome="denied",
            audit_event_id=None,
            idempotency_record_id=owner.record_id,
        )
    async with get_session_factory()() as session:
        if evidence in {"reservation", "released_reservation"}:
            permit = await session.get(PermitModel, action_runtime[2])
            permit.spent_credits = (
                Decimal("2") if evidence == "reservation" else Decimal("0")
            )
            permit.updated_at = utc_now()
        if evidence == "orphan_debit":
            session.add(
                LedgerEntryModel(
                    entry_id="orphan",
                    wallet_id=owner.wallet_id,
                    action="debit",
                    amount=Decimal("-2"),
                    balance_after=Decimal("0"),
                    operation_key=owner.record_id,
                )
            )
        await session.commit()
        assert not await may_abandon_action_owner(owner, session)
    await get_idempotency_service().abandon(
        wallet_id=owner.wallet_id,
        endpoint=owner.endpoint,
        idempotency_key=owner.idempotency_key,
        expected_record_id=owner.record_id,
    )
    assert len(await action_rows()) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "mode", ["success", "pre_dispatch_failure", "delivery_uncertain", "returned_error"]
)
async def test_terminal_and_uncertain_owners_never_reopen(action_runtime, mode):
    from app.db.models import McpDispatchAttemptModel
    from sqlalchemy import select
    from app.services.mcp_dispatch_reconciliation import (
        get_mcp_dispatch_reconciliation_service,
    )

    action_runtime[3].mode = mode
    await invoke_action(action_runtime, "standard", "first")
    owner = (await action_rows())[0]
    if mode in {"pre_dispatch_failure", "delivery_uncertain", "returned_error"}:
        async with get_session_factory()() as session:
            receipt = await session.get(ReceiptModel, owner.response_reference)
            assert receipt is not None
            assert json.loads(receipt.constraints_evaluated_json or "{}") == {
                "max_calls_per_tool": {"partner.pay": 1}
            }
    before = owner.model_dump()
    calls = len(action_runtime[3].calls)
    effects = action_runtime[3].dispatch_count
    async with get_session_factory()() as session:
        attempt = (await session.execute(select(McpDispatchAttemptModel))).scalar_one()
        stored = await session.get(type(owner), owner.record_id)
        stored.created_at = utc_now() - timedelta(days=90)
        stored.expires_at = utc_now() - timedelta(days=1)
        await session.commit()
    for _ in range(2):
        await get_mcp_dispatch_reconciliation_service().reconcile_attempt(
            attempt.attempt_id
        )
        await get_idempotency_service().reconcile_stuck_records(idle_seconds=0)
    for surface in ("standard", "legacy", "rest"):
        await invoke_action(action_runtime, surface, "retry-" + surface)
    after = (await action_rows())[0]
    assert after.record_id == before["record_id"]
    assert after.response_reference == before["response_reference"]
    assert len(action_runtime[3].calls) == calls
    assert action_runtime[3].dispatch_count == effects
    async with get_session_factory()() as session:
        permit = await session.get(PermitModel, action_runtime[2])
        if mode == "delivery_uncertain":
            assert permit.spent_credits == 2
        if mode == "pre_dispatch_failure":
            assert permit.spent_credits == 0


@pytest.mark.anyio
async def test_real_preparation_commit_ack_loss_adopts_once(
    action_runtime, monkeypatch
):
    from sqlalchemy.ext.asyncio import AsyncSessionTransaction
    from app.db.models import McpDispatchAttemptModel
    from sqlalchemy import select

    owner = await unstarted(action_runtime)
    original_exit = AsyncSessionTransaction.__aexit__
    injected = False

    async def lose_ack(self, exc_type, exc, tb):
        nonlocal injected
        result = await original_exit(self, exc_type, exc, tb)
        if not injected and exc_type is None:
            injected = True
            raise OperationalError(
                "synthetic committed ACK loss", {}, RuntimeError("offline")
            )
        return result

    monkeypatch.setattr(AsyncSessionTransaction, "__aexit__", lose_ack)
    accepted, attempt = await prepare_action(action_runtime, ARGS)
    assert injected and accepted.allowed
    async with get_session_factory()() as session:
        assert (
            len(
                (await session.execute(select(McpDispatchAttemptModel))).scalars().all()
            )
            == 1
        )
        permit = await session.get(PermitModel, action_runtime[2])
        assert permit.spent_credits == 2
        assert json.loads(permit.tool_call_counts_json) == {"partner.pay": 1}
    assert attempt.idempotency_record_id == owner.record_id
    assert action_runtime[3].dispatch_count == 0


@pytest.mark.anyio
async def test_action_bundle_offline_tamper_and_unknown_key(
    action_runtime, monkeypatch
):
    import socket
    from copy import deepcopy
    from b2a_sdk.receipt_verifier import (
        verify_bundle,
        key_set_from_document,
        VerificationStatus,
    )
    from tests.test_receipt_portability import _portable_bundle_and_keys

    await invoke_action(action_runtime, "standard", "receipt")
    receipt_id = (await action_rows())[0].response_reference
    bundle, keys = await _portable_bundle_and_keys(
        action_runtime[0], action_runtime[1], receipt_id
    )

    def denied(*args, **kwargs):
        raise AssertionError("offline verification attempted network")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    key_set = key_set_from_document(keys)
    result = verify_bundle(bundle, key_set)
    assert result.ok
    assert result.claims["action_contract_version"] == 1
    assert result.claims["idempotency_record_id"] == (await action_rows())[0].record_id
    tampered = deepcopy(bundle)
    tampered["signing_input"] = bundle["signing_input"].replace(
        '"action_contract_version":1', '"action_contract_version":2'
    )
    assert tampered["signing_input"] != bundle["signing_input"]
    assert not verify_bundle(tampered, key_set).ok
    assert verify_bundle(bundle, {}).status is VerificationStatus.UNKNOWN_KEY


@pytest.mark.anyio
@pytest.mark.parametrize("expired", [False, True])
async def test_current_authority_required_but_historical_receipt_readable(
    action_runtime, monkeypatch, expired
):
    from app.core import time
    from tests.test_trust_helpers import BOOTSTRAP_HEADERS
    from app.db.models import APIKeyModel

    await invoke_action(action_runtime, "standard", "complete")
    owner = (await action_rows())[0]
    if expired:
        async with get_session_factory()() as session:
            permit = await session.get(PermitModel, action_runtime[2])
        monkeypatch.setattr(
            time, "utc_now", lambda: permit.expires_at + timedelta(seconds=1)
        )
    else:
        async with get_session_factory()() as session:
            key = await session.get(APIKeyModel, action_runtime[1]["key_id"])
            key.status = "revoked"
            await session.commit()
    response = await invoke_action(action_runtime, "standard", "denied")
    assert response.status_code >= 400 or "error" in response.json(), response.text
    # A separately authenticated administrator retains historical evidence access.
    client, wallets, _, _ = action_runtime
    response = await client.get(
        f"/v1/receipts/{owner.response_reference}", headers=BOOTSTRAP_HEADERS
    )
    assert response.status_code == 200, response.text
    assert (await action_rows())[0].model_dump() == owner.model_dump()
    assert action_runtime[3].dispatch_count == 1


@pytest.mark.anyio
async def test_action_owner_excludes_legacy_and_other_wallets(clean_database):
    wallet = await get_agent_money().create_sponsor_wallet(
        sponsor_name="Action Owner", email="action@example.invalid"
    )
    service = get_idempotency_service()
    identity = action_execution_identity(
        action_permit(subject_wallet_id=wallet.wallet_id), BINDING
    )
    for endpoint in ["/mcp/invoke", "/mcp/messages", "/mcp/tools/partner.pay/invoke"]:
        # The canonical governed index forbids these three together. Distinct
        # caller keys prove lookup never searches any of these namespaces.
        key = identity.idempotency_key if endpoint == "/mcp/invoke" else endpoint
        await service.begin_with_record(
            wallet_id=wallet.wallet_id,
            endpoint=endpoint,
            idempotency_key=key,
            request_payload=identity.request_payload,
        )
    assert (
        await service.get_action_record(
            wallet_id=wallet.wallet_id, internal_key=identity.idempotency_key
        )
        is None
    )
    begun = await service.begin_action_with_record(
        wallet_id=wallet.wallet_id, identity=identity
    )
    record = await service.get_action_record(
        wallet_id=wallet.wallet_id, internal_key=identity.idempotency_key
    )
    assert record is not None and record.record_id == begun.record_id
    assert record.endpoint == "/mcp/action/v1"
    assert record.operation_kind == "upstream_mcp"
    assert (
        await service.get_action_record(
            wallet_id="foreign", internal_key=identity.idempotency_key
        )
        is None
    )
    legacy = await service.get_governed_mcp_record(
        wallet_id=wallet.wallet_id, idempotency_key=identity.idempotency_key
    )
    assert legacy is not None and legacy.record_id != record.record_id
    original_hash = record.request_hash
    with pytest.raises(IdempotencyInProgressError):
        await service.begin_action_with_record(
            wallet_id=wallet.wallet_id, identity=identity
        )
    changed = replace(
        identity,
        request_payload={**identity.request_payload, "action_payload_hash": "b" * 64},
    )
    with pytest.raises(IdempotencyConflictError):
        await service.begin_action_with_record(
            wallet_id=wallet.wallet_id, identity=changed
        )
    await service.complete_action(
        wallet_id=wallet.wallet_id,
        internal_key=identity.idempotency_key,
        response_reference="receipt-fixture",
        response_json={"done": True},
    )
    replay = await service.begin_action_with_record(
        wallet_id=wallet.wallet_id, identity=identity
    )
    assert replay.record_id == begun.record_id and replay.request_hash == original_hash
    assert replay.replay is not None and replay.replay.response_json == {"done": True}
    record = await service.get_action_record(
        wallet_id=wallet.wallet_id, internal_key=identity.idempotency_key
    )
    assert record is not None and record.request_hash == original_hash
    assert (
        await service.get_governed_mcp_record(
            wallet_id=wallet.wallet_id, idempotency_key=identity.idempotency_key
        )
    ).response_json is None


@pytest.mark.anyio
async def test_action_begin_refuses_wrong_namespace_before_database():
    service = get_idempotency_service()
    identity = action_execution_identity(action_permit(), BINDING)
    with pytest.raises(ValueError, match="invalid_action_execution_namespace"):
        await service.begin_action_with_record(
            wallet_id=identity.request_payload["subject_wallet_id"],
            identity=replace(identity, endpoint="/mcp/invoke"),
        )


@pytest.mark.anyio
async def test_concurrent_action_begin_keeps_one_owner(clean_database):
    import asyncio

    wallet = await get_agent_money().create_sponsor_wallet(
        sponsor_name="Action Race", email="action-race@example.invalid"
    )
    service = get_idempotency_service()
    identity = action_execution_identity(
        action_permit(subject_wallet_id=wallet.wallet_id), BINDING
    )

    async def begin():
        try:
            return await service.begin_action_with_record(
                wallet_id=wallet.wallet_id, identity=identity
            )
        except IdempotencyInProgressError:
            return None

    results = await asyncio.gather(*(begin() for _ in range(4)))
    owners = [result for result in results if result is not None]
    assert len(owners) == 1
    record = await service.get_action_record(
        wallet_id=wallet.wallet_id, internal_key=identity.idempotency_key
    )
    assert record is not None and record.record_id == owners[0].record_id
    assert record.request_hash == owners[0].request_hash
    await service.complete_action(
        wallet_id=wallet.wallet_id,
        internal_key=identity.idempotency_key,
        response_reference=None,
        response_json={"done": True},
    )
    replay = await service.begin_action_with_record(
        wallet_id=wallet.wallet_id, identity=identity
    )
    assert (
        replay.record_id == record.record_id
        and replay.request_hash == record.request_hash
    )


@pytest.mark.anyio
async def test_action_begin_refuses_foreign_wallet_before_database():
    identity = action_execution_identity(action_permit(), BINDING)
    with pytest.raises(ValueError, match="action_execution_wallet_mismatch"):
        await get_idempotency_service().begin_action_with_record(
            wallet_id="foreign", identity=identity
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "mutation",
    [
        "owner_key",
        "owner_hash",
        "signature",
        "caller_wallet",
        "caller_key",
        "destination",
    ],
)
async def test_recovery_never_adopts_changed_authority(
    action_runtime, monkeypatch, mutation
):
    from decimal import Decimal

    accepted, attempt = await prepare_action(action_runtime, ARGS)
    assert accepted.allowed
    owner = (await action_rows())[0]
    async with get_session_factory()() as session:
        if mutation in {"owner_key", "owner_hash"}:
            stored = await session.get(type(owner), owner.record_id)
            if mutation == "owner_key":
                stored.idempotency_key = "act1-" + "c" * 64
            else:
                stored.request_hash = "c" * 64
        elif mutation == "signature":
            permit = await session.get(PermitModel, action_runtime[2])
            permit.signature = "invalid"
        await session.commit()
    execute = AsyncSession.execute
    injected = False

    async def fail_once(self, statement, *args, **kwargs):
        nonlocal injected
        if not injected and getattr(statement, "_for_update_arg", None) is not None:
            injected = True
            raise OperationalError(
                "synthetic recovery entry", {}, RuntimeError("offline")
            )
        return await execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", fail_once)
    with pytest.raises(DispatchPrepareCommitUncertainError):
        await get_mcp_dispatch_attempt_service().authorize_reserve_and_prepare(
            idempotency_record_id=owner.record_id,
            wallet_id="other" if mutation == "caller_wallet" else owner.wallet_id,
            permit_id=attempt.permit_id,
            key_id="other" if mutation == "caller_key" else attempt.key_id,
            public_tool_id=attempt.public_tool_id,
            upstream_tool_name=attempt.upstream_tool_name,
            upstream_origin="https://other.invalid"
            if mutation == "destination"
            else attempt.upstream_origin,
            request_hash=owner.request_hash,
            credits_authorized=Decimal("2"),
            arguments=ARGS,
        )
    assert injected
    assert action_runtime[3].dispatch_count == 0


@pytest.mark.anyio
async def test_receipt_conflicts_include_action_binding(action_runtime):
    from app.services.receipts import ReceiptError, get_receipt_service
    from app.schemas.trust import ActionPermitFields
    from app.services.mcp_dispatch_reconciliation import (
        get_mcp_dispatch_reconciliation_service,
    )
    from app.db.models import McpDispatchAttemptModel
    from sqlalchemy import select

    await invoke_action(action_runtime, "standard", "original")
    owner = (await action_rows())[0]
    service = get_receipt_service()
    original = await service.get_receipt(owner.response_reference)
    async with get_session_factory()() as session:
        model = await session.get(ReceiptModel, original.receipt_id)
        model.action_payload_hash = "b" * 64
        await session.commit()
        assert not await service.verify_model(model, session=session)
        attempt = (await session.execute(select(McpDispatchAttemptModel))).scalar_one()
    expected = await service.action_binding_for_permit(action_runtime[2])
    with pytest.raises(ReceiptError, match="receipt_action_binding_conflict"):
        service._assert_idempotent_match(
            model,
            idempotency_record_id=owner.record_id,
            dispatch_attempt_id=model.dispatch_attempt_id,
            permit_id=model.permit_id,
            wallet_id=model.wallet_id,
            key_id=model.key_id,
            tool=model.tool,
            request_hash=model.request_hash,
            response_hash=model.response_hash,
            ledger_entry_id=model.ledger_entry_id,
            credits_authorized=model.credits_authorized,
            credits_charged=model.credits_charged,
            outcome=model.outcome,
            reason_code=model.reason_code,
            audit_event_id=model.audit_event_id,
            approval_id=model.approval_id,
            action_binding=expected,
        )
    bad = original.model_copy(update={"action_payload_hash": "b" * 64})
    with pytest.raises(ReceiptError, match="receipt_action_binding_conflict"):
        await service.assert_action_receipt_binding(bad)
    with pytest.raises(ValueError, match="incomplete_action_binding"):
        ActionPermitFields(action_contract_version=1)
    with pytest.raises(ValueError, match="unsupported_action_contract"):
        ActionPermitFields.model_validate({**expected, "action_contract_version": 2})
    # A syntactically valid binding with a valid signature for the wrong digest
    # must also be refused by reconciliation's adoption path.
    from app.services.signing_keys import get_signing_key_service

    async with get_session_factory()() as session:
        model = await session.get(ReceiptModel, original.receipt_id)
        payload = service._verification_payload(model, include_linkage=True)
        for field in ("alg", "kid", "payload_hash"):
            payload.pop(field)
        signature, kid, _ = await get_signing_key_service().sign_payload(payload)
        model.signature, model.signature_key_id = signature, kid
        await session.commit()
    with pytest.raises(ReceiptError, match="receipt_action_binding_conflict"):
        await get_mcp_dispatch_reconciliation_service()._get_or_create_receipt(
            attempt=attempt,
            result_payload=None,
            outcome=model.outcome,
            audit_event_id=model.audit_event_id,
        )


@pytest.mark.anyio
async def test_process_restart_preserves_native_key_and_offline_bundle(action_runtime):
    import os
    import subprocess
    import sys
    from pathlib import Path
    from tests.test_receipt_portability import _portable_bundle_and_keys

    await invoke_action(action_runtime, "standard", "original")
    owner = (await action_rows())[0]
    bundle, keys = await _portable_bundle_and_keys(
        action_runtime[0], action_runtime[1], owner.response_reference
    )
    async with get_session_factory()() as session:
        permit = await session.get(PermitModel, action_runtime[2])
    script = """
import json, socket, sys
from app.services.action_permits import action_execution_identity
from app.db.models import PermitModel
from tests.test_action_permits import BINDING
from b2a_sdk.receipt_verifier import verify_bundle, key_set_from_document, VerificationStatus
payload = json.load(sys.stdin)
def no_network(*args, **kwargs):
    raise AssertionError("offline process attempted network")
socket.socket.connect = no_network
socket.create_connection = no_network
socket.getaddrinfo = no_network
bundle, keys = payload["bundle"], key_set_from_document(payload["keys"])
assert verify_bundle(bundle, keys).ok
assert verify_bundle(bundle, {}).status is VerificationStatus.UNKNOWN_KEY
changed = dict(bundle)
changed["signing_input"] = bundle["signing_input"].replace('"action_contract_version":1', '"action_contract_version":2')
assert not verify_bundle(changed, keys).ok
permit = PermitModel.model_validate(payload["permit"])
assert action_execution_identity(permit, BINDING).native_idempotency_key == payload["native_key"]
print("offline action receipt verified; tamper/unknown key refused; native key stable")
"""
    env = {
        **os.environ,
        "PYTHONPATH": str(Path.cwd().parent / "sdk/python/src")
        + os.pathsep
        + str(Path.cwd()),
    }
    run = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps(
            {
                "bundle": bundle,
                "keys": keys,
                "permit": permit.model_dump(mode="json"),
                "native_key": action_runtime[3].calls[0]["idempotency_key"],
            }
        ),
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    assert run.returncode == 0, run.stderr
    assert "native key stable" in run.stdout
