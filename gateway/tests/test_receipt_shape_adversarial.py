"""Receipt shapes and replay evidence stay consistent with signed money state."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.config import DuplicateGuardMode, get_settings
from app.db.database import get_session_factory
from app.db.models import LedgerEntryModel, ReceiptModel, SigningKeyModel, WalletModel
from app.main import app
from app.services.audit_log import record_audit_event
from app.services.idempotency import get_idempotency_service
from app.services.receipts import ReceiptError, get_receipt_service
from app.services.signing_keys import get_signing_key_service
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet

pytestmark = pytest.mark.anyio

TOOL = "receipt-shape-adversarial"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as value:
        yield value


async def _setup(client):
    owner = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=owner["agent_wallet_id"],
        key_id=owner["key_id"],
        tool_name=TOOL,
        idem_key="receipt-shape-permit",
    )
    return owner, permit


def _kwargs(owner, permit, **overrides):
    values = {
        "permit_id": permit["permit_id"],
        "wallet_id": owner["agent_wallet_id"],
        "key_id": owner["key_id"],
        "tool": TOOL,
        "request_payload": {"attempt": "shape"},
        "response_payload": {"ok": True},
        "ledger_entry_id": None,
        "credits_authorized": Decimal("1"),
        "credits_charged": Decimal("0"),
        "outcome": "success",
        "audit_event_id": None,
    }
    values.update(overrides)
    return values


async def _money_and_receipt_state(wallet_id):
    async with get_session_factory()() as session:
        wallet = await session.get(WalletModel, wallet_id)
        assert wallet is not None
        ledger = (
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
        receipts = (await session.execute(select(ReceiptModel))).scalars().all()
        keys = (await session.execute(select(SigningKeyModel))).scalars().all()
        return (
            (wallet.balance, wallet.lifetime_credits, wallet.lifetime_debits),
            tuple(
                sorted(
                    (row.entry_id, row.action, row.amount, row.balance_after)
                    for row in ledger
                )
            ),
            tuple(
                sorted(
                    (
                        row.receipt_id,
                        row.signature,
                        row.signature_key_id,
                        row.constraints_evaluated_json,
                        row.outcome,
                        row.credits_charged,
                        row.response_hash,
                    )
                    for row in receipts
                )
            ),
            tuple(
                sorted(
                    (row.key_id, row.status, row.activated_at, row.retired_at)
                    for row in keys
                )
            ),
        )


async def _idempotent_kwargs(owner, permit, suffix):
    request = {"attempt": suffix}
    begun = await get_idempotency_service().begin_with_record(
        wallet_id=owner["agent_wallet_id"],
        endpoint="/mcp/messages",
        idempotency_key=f"receipt-shape-{suffix}",
        request_payload=request,
    )
    assert begun.replay is None
    return _kwargs(
        owner,
        permit,
        request_payload=request,
        idempotency_record_id=begun.record_id,
    )


@pytest.mark.parametrize(
    "outcome", ["bogus", "SUCCESS", "success ", " denied", "", "denied\n"]
)
async def test_unknown_outcome_rejected_before_enrichment_or_write(
    client, clean_database, monkeypatch, outcome
):
    owner, permit = await _setup(client)
    before = await _money_and_receipt_state(owner["agent_wallet_id"])
    monkeypatch.setattr(get_settings(), "JEV_RISK_GUARD", DuplicateGuardMode.LOG)

    async def unexpected_enrichment(*args, **kwargs):
        raise AssertionError("invalid outcome reached JEV enrichment")

    monkeypatch.setattr(
        "app.services.receipts.load_jev_guard_metadata", unexpected_enrichment
    )
    with pytest.raises(ReceiptError, match="^receipt_outcome_invalid$"):
        await get_receipt_service().create_receipt(
            **_kwargs(owner, permit, outcome=outcome)
        )
    assert await _money_and_receipt_state(owner["agent_wallet_id"]) == before


@pytest.mark.parametrize("outcome", ["denied", "insufficient_funds", "failed_refunded"])
async def test_charged_uncharged_outcome_rejected_without_mutation(
    client, clean_database, monkeypatch, outcome
):
    owner, permit = await _setup(client)
    before = await _money_and_receipt_state(owner["agent_wallet_id"])
    monkeypatch.setattr(get_settings(), "JEV_RISK_GUARD", DuplicateGuardMode.LOG)

    async def unexpected_enrichment(*args, **kwargs):
        raise AssertionError("charged uncharged outcome reached JEV enrichment")

    monkeypatch.setattr(
        "app.services.receipts.load_jev_guard_metadata", unexpected_enrichment
    )
    with pytest.raises(ReceiptError, match="^receipt_denied_charge_invalid$"):
        await get_receipt_service().create_receipt(
            **_kwargs(
                owner,
                permit,
                outcome=outcome,
                credits_charged=Decimal("0.5"),
                reason_code="upstream_error",
            )
        )
    assert await _money_and_receipt_state(owner["agent_wallet_id"]) == before


@pytest.mark.parametrize(
    ("outcome", "charged"),
    [
        ("success", Decimal("1")),
        ("denied", Decimal("0")),
        ("insufficient_funds", Decimal("0")),
        ("failed_refunded", Decimal("0")),
        ("failed_unrefunded", Decimal("0.5")),
        ("delivery_uncertain", Decimal("0.5")),
        ("response_rejected", Decimal("0.5")),
    ],
)
async def test_supported_outcome_keeps_signed_charge(
    client, clean_database, outcome, charged
):
    owner, permit = await _setup(client)
    service = get_receipt_service()
    receipt = await service.create_receipt(
        **_kwargs(
            owner,
            permit,
            outcome=outcome,
            credits_charged=charged,
            reason_code=None if outcome == "success" else "upstream_error",
        )
    )
    assert receipt.outcome == outcome
    assert receipt.credits_charged == charged
    assert (await service.verify_receipt(receipt.receipt_id))[0] is True
    signed = await service.signing_input(receipt.receipt_id)
    assert signed is not None
    assert json.loads(signed)["outcome"] == outcome


async def test_constraints_replay_compares_signed_semantics(client, clean_database):
    owner, permit = await _setup(client)
    kwargs = await _idempotent_kwargs(owner, permit, "constraints")
    service = get_receipt_service()
    first = await service.create_receipt(
        **kwargs,
        constraints_evaluated={"policy": {"enabled": True, "count": 2}},
    )
    reordered = await service.create_receipt(
        **kwargs,
        constraints_evaluated={"policy": {"count": 2, "enabled": True}},
    )
    assert reordered.receipt_id == first.receipt_id
    before = await _money_and_receipt_state(owner["agent_wallet_id"])
    for changed in (
        {"policy": {"enabled": True, "count": 3}},
        {"policy": {"enabled": 1, "count": 2}},
    ):
        with pytest.raises(ReceiptError, match="^receipt_idempotency_conflict$"):
            await service.create_receipt(**kwargs, constraints_evaluated=changed)
        assert await _money_and_receipt_state(owner["agent_wallet_id"]) == before


@pytest.mark.parametrize("caller_owned", [False, True])
@pytest.mark.parametrize("conflicting", [False, True])
async def test_replay_checks_existing_evidence_before_signing(
    client, clean_database, monkeypatch, caller_owned, conflicting
):
    owner, permit = await _setup(client)
    kwargs = await _idempotent_kwargs(owner, permit, "before-signing")
    service = get_receipt_service()
    first = await service.create_receipt(
        **kwargs, constraints_evaluated={"checked": True}
    )
    before = await _money_and_receipt_state(owner["agent_wallet_id"])

    async def forbidden_sign(*_args, **_kwargs):
        raise AssertionError("replay reached signing")

    def forbidden_prepared_sign(*_args, **_kwargs):
        raise AssertionError("replay reached prepared signing")

    signing = get_signing_key_service()
    monkeypatch.setattr(signing, "sign_payload", forbidden_sign)
    monkeypatch.setattr(signing, "sign_payload_with_key_id", forbidden_prepared_sign)

    async def check_replay(session=None):
        replay = {
            **kwargs,
            "constraints_evaluated": {"checked": not conflicting},
            "session": session,
            "prepared_signing_key_id": first.signature_key_id if caller_owned else None,
        }
        if conflicting:
            with pytest.raises(ReceiptError, match="^receipt_idempotency_conflict$"):
                await service.create_receipt(**replay)
        else:
            again = await service.create_receipt(**replay)
            assert again.receipt_id == first.receipt_id

    if caller_owned:
        async with get_session_factory()() as session:
            await check_replay(session)
    else:
        await check_replay()
    assert await _money_and_receipt_state(owner["agent_wallet_id"]) == before


@pytest.mark.parametrize(("first", "replay"), [(None, {}), ({}, None)])
async def test_none_and_empty_constraints_replay_equally(
    client, clean_database, first, replay
):
    owner, permit = await _setup(client)
    kwargs = await _idempotent_kwargs(owner, permit, "empty")
    service = get_receipt_service()
    receipt = await service.create_receipt(**kwargs, constraints_evaluated=first)
    again = await service.create_receipt(**kwargs, constraints_evaluated=replay)
    assert again.receipt_id == receipt.receipt_id
    assert again.constraints_evaluated == {}


async def test_jev_enriched_constraints_are_compared_after_enrichment(
    client, clean_database, monkeypatch
):
    owner, permit = await _setup(client)
    monkeypatch.setattr(get_settings(), "JEV_RISK_GUARD", DuplicateGuardMode.LOG)
    guard = {"verdict": "pass", "advisory": True}
    audit = await record_audit_event(
        event="mcp.invoke",
        wallet_id=owner["agent_wallet_id"],
        tool=TOOL,
        metadata={"jev_risk_guard": guard},
    )
    kwargs = await _idempotent_kwargs(owner, permit, "jev")
    kwargs["audit_event_id"] = audit.event_id
    service = get_receipt_service()
    first = await service.create_receipt(
        **kwargs, constraints_evaluated={"rule": {"allow": True}}
    )
    assert first.constraints_evaluated == {
        "rule": {"allow": True},
        "jev_risk_guard": guard,
    }
    same = await service.create_receipt(
        **kwargs,
        constraints_evaluated={
            "rule": {"allow": True},
            "jev_risk_guard": {"verdict": "caller-supplied"},
        },
    )
    assert same.receipt_id == first.receipt_id
    monkeypatch.setattr(get_settings(), "JEV_RISK_GUARD", DuplicateGuardMode.OFF)
    after_mode_change = await service.create_receipt(
        **kwargs, constraints_evaluated={"rule": {"allow": True}}
    )
    assert after_mode_change.receipt_id == first.receipt_id
    with pytest.raises(ReceiptError, match="^receipt_idempotency_conflict$"):
        await service.create_receipt(
            **kwargs, constraints_evaluated={"rule": {"allow": False}}
        )
    assert (await service.verify_receipt(first.receipt_id))[0] is True


async def test_signed_evidence_and_response_hash_mutation_fail_closed(
    client, clean_database
):
    owner, permit = await _setup(client)
    kwargs = await _idempotent_kwargs(owner, permit, "signed")
    service = get_receipt_service()
    first = await service.create_receipt(
        **kwargs, constraints_evaluated={"checked": True}
    )
    with pytest.raises(ReceiptError, match="^receipt_idempotency_conflict$"):
        await service.create_receipt(
            **{**kwargs, "response_payload": {"ok": False}},
            constraints_evaluated={"checked": True},
        )
    async with get_session_factory()() as session:
        row = await session.get(ReceiptModel, first.receipt_id)
        assert row is not None
        row.constraints_evaluated_json = json.dumps({"checked": False})
        await session.commit()
    assert (await service.verify_receipt(first.receipt_id))[0] is False
    assert await service.signing_input(first.receipt_id) is None
    with pytest.raises(ReceiptError, match="^receipt_signature_invalid$"):
        await service.create_receipt(**kwargs, constraints_evaluated={"checked": False})
