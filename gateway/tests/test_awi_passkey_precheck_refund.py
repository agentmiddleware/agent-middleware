"""Issue 353: /passkey/verify refunds rejections it makes before doing any work.

A missing credential id, or a challenge already verified, failed or expired,
is rejected without assertion work and without changing challenge state. The
result is known and nothing happened, so the admission debit is refunded and
the receipt reads ``failed_refunded``, not ``delivery_uncertain``. A real
failed assertion still marks the challenge failed and keeps its charge.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.time import utc_now
from app.db.database import get_session_factory
from app.db.models import LedgerEntryModel, PermitModel
from app.main import app
from app.services.receipts import get_receipt_service
from app.services.webauthn_provider import ChallengeStatus, get_webauthn_provider
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet

pytestmark = [pytest.mark.proof, pytest.mark.anyio]

PATH = "/v1/awi/passkey/verify"


@pytest.fixture
async def client():
    http_app = FastAPI()
    http_app.include_router(app.router)
    async with AsyncClient(
        transport=ASGITransport(app=http_app), base_url="http://test"
    ) as client:
        yield client


async def _prepare(client):
    actor = await provision_agent_wallet(client)
    wallet = actor["agent_wallet_id"]
    permit = await create_tool_permit(
        client,
        wallet_id=wallet,
        key_id=actor["key_id"],
        tool_name="awi_passkey_verify",
        max_credits=50,
        idem_key="permit-passkey-precheck",
    )
    session = await client.post(
        "/v1/awi/sessions",
        json={"target_url": "https://synthetic.invalid", "wallet_id": wallet},
        headers=actor["agent_headers"],
    )
    assert session.status_code == 201, session.text
    sid = session.json()["session_id"]
    created = await get_webauthn_provider().create_challenge(
        session_id=sid, action="checkout"
    )
    headers = {
        **actor["agent_headers"],
        "X-Wallet-Id": wallet,
        "X-Permit-Id": permit["permit_id"],
    }
    return wallet, permit["permit_id"], created["challenge_id"], headers


async def _ledger(wallet):
    async with get_session_factory()() as db:
        return (
            (
                await db.execute(
                    select(LedgerEntryModel).where(
                        LedgerEntryModel.wallet_id == wallet,
                        LedgerEntryModel.request_path == "POST " + PATH,
                    )
                )
            )
            .scalars()
            .all()
        )


async def _spent(permit_id):
    async with get_session_factory()() as db:
        return (await db.get(PermitModel, permit_id)).spent_credits


async def _assert_refunded(response, wallet, permit_id, message):
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["message"] == message
    assert detail["receipt"]["outcome"] == "failed_refunded"
    receipt = await get_receipt_service().get_receipt(detail["receipt"]["receipt_id"])
    assert receipt.credits_charged == 0
    ledger = await _ledger(wallet)
    # One admission debit and its refund: the wallet nets to zero.
    assert len(ledger) == 2
    assert sum(entry.amount for entry in ledger) == 0
    assert await _spent(permit_id) == 0


async def test_missing_credential_id_is_refunded(client, clean_database):
    wallet, permit_id, challenge_id, headers = await _prepare(client)
    body = {"challenge_id": challenge_id, "credential": {}}
    first = await client.post(
        PATH, json=body, headers={**headers, "Idempotency-Key": "pk-missing-id"}
    )
    await _assert_refunded(first, wallet, permit_id, "Missing credential ID")
    # The challenge is untouched: a pre-check changes no state.
    challenge = get_webauthn_provider().get_challenge(challenge_id)
    assert challenge.status == ChallengeStatus.PENDING
    replay = await client.post(
        PATH, json=body, headers={**headers, "Idempotency-Key": "pk-missing-id"}
    )
    assert replay.json() == first.json()
    assert len(await _ledger(wallet)) == 2


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (ChallengeStatus.VERIFIED, "Challenge already verified"),
        (ChallengeStatus.FAILED, "Challenge verification previously failed"),
        (ChallengeStatus.EXPIRED, "Challenge expired"),
    ],
)
async def test_settled_challenge_is_refunded(client, clean_database, status, message):
    wallet, permit_id, challenge_id, headers = await _prepare(client)
    get_webauthn_provider().get_challenge(challenge_id).status = status
    response = await client.post(
        PATH,
        json={"challenge_id": challenge_id, "credential": {"id": "cred"}},
        headers={**headers, "Idempotency-Key": f"pk-{status.value}"},
    )
    await _assert_refunded(response, wallet, permit_id, message)


async def test_lapsed_challenge_is_refunded(client, clean_database):
    wallet, permit_id, challenge_id, headers = await _prepare(client)
    challenge = get_webauthn_provider().get_challenge(challenge_id)
    challenge.expires_at = utc_now() - timedelta(seconds=1)
    response = await client.post(
        PATH,
        json={"challenge_id": challenge_id, "credential": {"id": "cred"}},
        headers={**headers, "Idempotency-Key": "pk-lapsed"},
    )
    await _assert_refunded(response, wallet, permit_id, "Challenge expired")


async def test_failed_assertion_keeps_its_charge(client, clean_database):
    wallet, permit_id, challenge_id, headers = await _prepare(client)
    response = await client.post(
        PATH,
        json={"challenge_id": challenge_id, "credential": {"id": "cred"}},
        headers={**headers, "Idempotency-Key": "pk-bad-assertion"},
    )
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["message"] == "Credential verification failed"
    # Assertion work ran and the challenge is now spent, so the charge stays.
    assert detail["receipt"]["outcome"] == "delivery_uncertain"
    assert (
        get_webauthn_provider().get_challenge(challenge_id).status
        == ChallengeStatus.FAILED
    )
    ledger = await _ledger(wallet)
    assert len(ledger) == 1 and ledger[0].amount < 0
    assert await _spent(permit_id) > 0
