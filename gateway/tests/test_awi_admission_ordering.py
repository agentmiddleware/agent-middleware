"""SEC-008: real accounting must precede every synthetic AWI effect."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.time import utc_now
from app.db.database import get_session_factory
from app.db.models import LedgerEntryModel, PermitModel, WalletModel
from app.main import app
from app.routers.awi_enhanced import _DOM_SESSION_WALLETS
from app.services.agent_money import get_agent_money
from app.services.awi_http_governance import AWI_HTTP_TOOL_CREDITS
from app.services.awi_playwright_bridge import get_playwright_bridge
from app.services.awi_rag_engine import get_awi_rag_engine
from app.services.awi_session import get_awi_session_manager
from app.services.billing_engine import LedgerWriteContendedError
from app.services.idempotency import get_idempotency_service
from app.services.permits import PermitError, get_permit_service
from app.services.receipts import get_receipt_service
from app.services.webauthn_provider import get_webauthn_provider
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet

pytestmark = [pytest.mark.proof, pytest.mark.anyio]
TOOLS = tuple(AWI_HTTP_TOOL_CREDITS)


async def prepare(client, monkeypatch, tool="awi_execute", *, max_credits=50):
    actor = await provision_agent_wallet(client)
    wallet = actor["agent_wallet_id"]
    permit = await create_tool_permit(
        client,
        wallet_id=wallet,
        key_id=actor["key_id"],
        tool_name=tool,
        max_credits=max_credits,
        idem_key="permit-admission",
    )
    session = await client.post(
        "/v1/awi/sessions",
        json={"target_url": "https://synthetic.invalid", "wallet_id": wallet},
        headers=actor["agent_headers"],
    )
    assert session.status_code == 201
    sid = session.json()["session_id"]
    manager, rag = get_awi_session_manager(), get_awi_rag_engine()
    webauthn, bridge = get_webauthn_provider(), get_playwright_bridge()
    monkeypatch.setattr(webauthn, "requires_passkey", AsyncMock(return_value=True))
    monkeypatch.setattr(
        webauthn, "get_challenge", lambda _: SimpleNamespace(session_id=sid)
    )
    monkeypatch.setitem(_DOM_SESSION_WALLETS, sid, wallet)
    monkeypatch.setattr(
        bridge, "get_session", AsyncMock(return_value=SimpleNamespace(session_id=sid))
    )
    monkeypatch.setattr(bridge, "translate_action", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        bridge, "extract_state_representation", AsyncMock(return_value={})
    )
    monkeypatch.setattr(rag, "get_memory", AsyncMock(return_value=None))
    targets = {
        "awi_execute": (
            manager,
            "execute_action",
            "/execute",
            {
                "session_id": sid,
                "action": "navigate_to",
                "parameters": {"url": "https://synthetic.invalid/next"},
            },
            {"status": "success"},
        ),
        "awi_passkey_challenge": (
            webauthn,
            "create_challenge",
            "/passkey/challenge",
            {"session_id": sid, "action": "checkout"},
            {
                "challenge_id": "synthetic",
                "challenge": "synthetic",
                "rp_id": "synthetic.invalid",
                "rp_name": "Synthetic",
                "timeout": 1000,
                "public_key_cred_params": [],
                "authenticator_selection": {},
            },
        ),
        "awi_passkey_verify": (
            webauthn,
            "verify_response",
            "/passkey/verify",
            {"challenge_id": "synthetic", "credential": {"id": "synthetic"}},
            {
                "verified": True,
                "challenge_id": "synthetic",
                "session_id": sid,
                "action": "checkout",
                "verified_at": utc_now().isoformat(),
            },
        ),
        "awi_dom_sync": (
            bridge,
            "execute_commands",
            "/dom/sync",
            {
                "session_id": sid,
                "action": "navigate_to",
                "parameters": {"url": "https://synthetic.invalid/next"},
            },
            SimpleNamespace(
                success=True, commands_executed=1, new_url=None, error=None
            ),
        ),
        "awi_memory_index": (
            rag,
            "index_session",
            "/rag/index",
            {
                "session_id": sid,
                "session_type": "shopping",
                "action_history": [],
                "state_snapshots": [],
            },
            "synthetic-memory",
        ),
        "awi_rag_query": (rag, "search", "/rag/query", {"query": "synthetic"}, []),
    }
    owner, name, path, body, result = targets[tool]
    effect = AsyncMock(return_value=result)
    monkeypatch.setattr(owner, name, effect)
    return SimpleNamespace(
        actor=actor,
        wallet=wallet,
        permit=permit["permit_id"],
        tool=tool,
        effect=effect,
        path="/v1/awi" + path,
        body=body,
        headers={
            **actor["agent_headers"],
            "X-Wallet-Id": wallet,
            "X-Permit-Id": permit["permit_id"],
            "Idempotency-Key": "awi-admission",
        },
    )


@pytest.fixture
async def client():
    # Preserve all route/auth dependencies while allowing cancellation to reach
    # the test instead of BaseHTTPMiddleware converting it to a transport error.
    http_app = FastAPI()
    http_app.include_router(app.router)
    async with AsyncClient(
        transport=ASGITransport(app=http_app), base_url="http://test"
    ) as client:
        yield client


async def records(case):
    async with get_session_factory()() as db:
        ledger = (
            (
                await db.execute(
                    select(LedgerEntryModel).where(
                        LedgerEntryModel.wallet_id == case.wallet,
                        LedgerEntryModel.request_path == "POST " + case.path,
                    )
                )
            )
            .scalars()
            .all()
        )
        permit = await db.get(PermitModel, case.permit)
        idem = await get_idempotency_service().get_record(
            wallet_id=case.wallet,
            endpoint="POST " + case.path,
            idempotency_key=case.headers["Idempotency-Key"],
        )
        return ledger, permit, idem


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize(
    "fault", ["empty_wallet", "reservation_contention", "ledger_contention"]
)
async def test_every_callback_requires_admission(
    client, clean_database, monkeypatch, tool, fault
):
    case = await prepare(client, monkeypatch, tool)
    if fault == "empty_wallet":
        async with get_session_factory()() as db:
            wallet = await db.get(WalletModel, case.wallet)
            wallet.balance = Decimal("0")
            await db.commit()
    elif fault == "reservation_contention":
        owner, name, error = (
            get_permit_service(),
            "reserve_budget",
            PermitError("permit_write_contended"),
        )
    else:
        owner, name, error = (
            get_agent_money(),
            "charge",
            LedgerWriteContendedError("ledger_write_contended"),
        )
    if fault != "empty_wallet":
        original = getattr(owner, name)
        monkeypatch.setattr(type(owner), name, AsyncMock(side_effect=error))
    first = await client.post(case.path, json=case.body, headers=case.headers)
    assert first.status_code == (402 if fault == "empty_wallet" else 503), first.text
    case.effect.assert_not_awaited()
    ledger, permit, _ = await records(case)
    assert not ledger
    assert permit.spent_credits == 0
    if fault != "empty_wallet":
        monkeypatch.setattr(type(owner), name, staticmethod(original))
        for _ in range(2):
            retry = await client.post(case.path, json=case.body, headers=case.headers)
            assert retry.status_code == (201 if tool == "awi_memory_index" else 200), (
                retry.text
            )
        case.effect.assert_awaited_once()
        ledger, permit, idem = await records(case)
        assert len(ledger) == 1
        assert ledger[0].operation_key == idem.record_id
        assert permit.spent_credits == AWI_HTTP_TOOL_CREDITS[tool]


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("failure", ["runtime", "http"])
async def test_callback_exception_retains_admission_and_uncertain_receipt(
    client, clean_database, monkeypatch, tool, failure
):
    case = await prepare(client, monkeypatch, tool)

    async def fail(*args, **kwargs):
        ledger, permit, idem = await records(case)
        assert len(ledger) == 1
        assert idem.ledger_entry_id == ledger[0].entry_id
        assert permit.spent_credits == AWI_HTTP_TOOL_CREDITS[tool]
        if failure == "http":
            raise HTTPException(400, {"error": "synthetic_failure"})
        raise RuntimeError("synthetic failure after effect")

    case.effect.side_effect = fail
    first = await client.post(case.path, json=case.body, headers=case.headers)
    assert first.status_code == (400 if failure == "http" else 500), first.text
    assert first.json()["detail"]["receipt"]["outcome"] == "delivery_uncertain"
    replay = await client.post(case.path, json=case.body, headers=case.headers)
    assert replay.json() == first.json()
    case.effect.assert_awaited_once()
    ledger, permit, _ = await records(case)
    assert len(ledger) == 1
    assert permit.spent_credits == AWI_HTTP_TOOL_CREDITS[tool]


@pytest.mark.parametrize("fault", ["charge_ack", "checkpoint", "cancel"])
async def test_interrupted_admission_never_dispatches_or_reopens(
    client, clean_database, monkeypatch, fault
):
    case = await prepare(client, monkeypatch)
    owner = get_agent_money() if fault == "charge_ack" else get_idempotency_service()
    name = "charge" if fault == "charge_ack" else "mark_charged"
    original = getattr(owner, name)

    async def interrupt(*args, **kwargs):
        if fault == "charge_ack":
            await original(*args, **kwargs)
        if fault == "cancel":
            raise asyncio.CancelledError()
        raise RuntimeError("synthetic commit/checkpoint failure")

    monkeypatch.setattr(type(owner), name, staticmethod(interrupt))
    if fault == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await client.post(case.path, json=case.body, headers=case.headers)
    else:
        response = await client.post(case.path, json=case.body, headers=case.headers)
        assert response.status_code == 503
    monkeypatch.setattr(type(owner), name, staticmethod(original))
    case.effect.assert_not_awaited()
    ledger, permit, idem = await records(case)
    assert len(ledger) == 1
    assert idem.response_json is None
    assert permit.spent_credits == 3
    _, needs_review = await get_idempotency_service().reconcile_stuck_records(
        idle_seconds=0
    )
    assert needs_review >= 1
    _, _, recovered = await records(case)
    assert recovered.ledger_entry_id == ledger[0].entry_id
    retry = await client.post(case.path, json=case.body, headers=case.headers)
    assert retry.status_code == 409
    case.effect.assert_not_awaited()


@pytest.mark.parametrize("boundary", ["callback", "receipt", "complete"])
async def test_post_dispatch_crash_never_reexecutes(
    client, clean_database, monkeypatch, boundary
):
    case = await prepare(client, monkeypatch)
    if boundary == "callback":
        case.effect.side_effect = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await client.post(case.path, json=case.body, headers=case.headers)
    else:
        owner = (
            get_receipt_service()
            if boundary == "receipt"
            else get_idempotency_service()
        )
        name = "create_receipt" if boundary == "receipt" else "complete"
        original = getattr(owner, name)
        monkeypatch.setattr(
            type(owner),
            name,
            AsyncMock(side_effect=RuntimeError("synthetic storage failure")),
        )
        response = await client.post(case.path, json=case.body, headers=case.headers)
        assert response.status_code == 503
        monkeypatch.setattr(type(owner), name, staticmethod(original))
    repaired, needs_review = await get_idempotency_service().reconcile_stuck_records(
        idle_seconds=0
    )
    if boundary == "complete":
        assert repaired >= 1
    else:
        assert needs_review >= 1
    retry = await client.post(case.path, json=case.body, headers=case.headers)
    assert retry.status_code == (200 if boundary == "complete" else 409), retry.text
    case.effect.assert_awaited_once()
    ledger, permit, _ = await records(case)
    assert len(ledger) == 1
    assert permit.spent_credits == 3


async def test_receipt_commit_ack_loss_reuses_one_receipt(
    client, clean_database, monkeypatch
):
    case = await prepare(client, monkeypatch)
    receipts = get_receipt_service()
    original = receipts.create_receipt
    ids = []

    async def lose_ack(**kwargs):
        receipt = await original(**kwargs)
        ids.append(receipt.receipt_id)
        if len(ids) == 1:
            raise RuntimeError("synthetic receipt ack loss")
        return receipt

    monkeypatch.setattr(type(receipts), "create_receipt", staticmethod(lose_ack))
    response = await client.post(case.path, json=case.body, headers=case.headers)
    assert response.status_code == 200, response.text
    assert len(ids) == 2 and len(set(ids)) == 1
    case.effect.assert_awaited_once()


@pytest.mark.parametrize("mode", ["same_key", "permit_cap", "wallet_cap"])
async def test_concurrent_admission_and_replay(
    client, clean_database, monkeypatch, mode
):
    case = await prepare(
        client, monkeypatch, max_credits=3 if mode == "permit_cap" else 50
    )
    if mode == "wallet_cap":
        async with get_session_factory()() as db:
            wallet = await db.get(WalletModel, case.wallet)
            wallet.balance = Decimal("3")
            await db.commit()
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return {"status": "success"}

    case.effect.side_effect = blocked
    winner = asyncio.create_task(
        client.post(case.path, json=case.body, headers=case.headers)
    )
    await asyncio.wait_for(entered.wait(), timeout=5)
    try:
        headers = (
            case.headers
            if mode == "same_key"
            else {**case.headers, "Idempotency-Key": "second-key"}
        )
        loser = await client.post(case.path, json=case.body, headers=headers)
        assert (
            loser.status_code
            == {"same_key": 409, "permit_cap": 403, "wallet_cap": 402}[mode]
        ), loser.text
    finally:
        release.set()
    assert (await winner).status_code == 200
    replay = await client.post(case.path, json=case.body, headers=case.headers)
    assert replay.status_code == 200, replay.text
    case.effect.assert_awaited_once()
    ledger, permit, _ = await records(case)
    assert len(ledger) == 1 and permit.spent_credits == 3


async def test_proven_no_dispatch_refunds_once(client, clean_database, monkeypatch):
    case = await prepare(client, monkeypatch)
    case.effect.return_value = {
        "status": "error",
        "effect_status": "not_dispatched",
        "error": "dry_run_unsupported",
    }
    first = await client.post(case.path, json=case.body, headers=case.headers)
    assert first.status_code == 200, first.text
    assert first.json()["receipt"]["outcome"] == "failed_refunded"
    replay = await client.post(case.path, json=case.body, headers=case.headers)
    assert replay.json() == first.json()
    ledger, permit, _ = await records(case)
    assert len(ledger) == 2 and permit.spent_credits == 0
    assert sum(entry.amount for entry in ledger) == 0
    receipt = await get_receipt_service().get_receipt(
        first.json()["receipt"]["receipt_id"]
    )
    assert receipt.credits_charged == 0
    async with get_session_factory()() as db:
        refund = (
            (
                await db.execute(
                    select(LedgerEntryModel).where(
                        LedgerEntryModel.correlation_id == ledger[0].entry_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(refund) == 1 and refund[0].amount == 3
    case.effect.assert_awaited_once()


@pytest.mark.parametrize("constraint", ["revoked", "expired"])
async def test_new_call_denied_but_prior_receipt_remains_replayable(
    client, clean_database, monkeypatch, constraint
):
    case = await prepare(client, monkeypatch)
    first = await client.post(case.path, json=case.body, headers=case.headers)
    assert first.status_code == 200
    if constraint == "revoked":
        await get_permit_service().revoke_permit(case.permit)
    else:
        # Mutable expiry is signed; advancing the validation clock preserves
        # signature identity and models expiry without corrupting the permit.
        monkeypatch.setattr(
            "app.services.permits.utc_now", lambda: utc_now() + timedelta(days=2)
        )
    replay = await client.post(case.path, json=case.body, headers=case.headers)
    assert replay.json() == first.json()
    new = await client.post(
        case.path, json=case.body, headers={**case.headers, "Idempotency-Key": "new"}
    )
    assert new.status_code == 403
    case.effect.assert_awaited_once()
