"""Only proven pre-persistence refusals may release issuance ownership."""

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import idempotency, permits, signing_keys
from app.core.time import utc_now
from app.db.database import get_session_factory
from app.db.models import IdempotencyRecordModel, PermitModel
from app.main import app
from app.services.idempotency import get_idempotency_service
from app.services.permits import PermitError, get_permit_service
from app.services.service_registry import get_service_registry
from app.services.signing_keys import get_signing_key_service
from tests.test_action_integration import action_registry as _action_registry
from tests.test_action_permits import _action_request
from tests.test_trust_helpers import BOOTSTRAP_HEADERS, provision_agent_wallet

action_registry = _action_registry


@pytest.fixture(autouse=True)
def isolated_issuance_services(monkeypatch):
    # Restoring an instance method patch writes its old bound method onto that
    # instance, shadowing later class patches. Keep these fault-injection
    # instances local to each test instead of altering shared singletons.
    monkeypatch.setattr(idempotency, "_service", idempotency.IdempotencyService())
    monkeypatch.setattr(permits, "_service", permits.PermitService())
    monkeypatch.setattr(
        signing_keys, "_signing_key_service", signing_keys.SigningKeyService()
    )


@pytest.fixture(params=["envelope", "action"])
async def issuance(request, clean_database, action_registry):
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        wallets = await provision_agent_wallet(client)
        fields = dict(
            issuer_wallet_id=wallets["sponsor_wallet_id"],
            subject_wallet_id=wallets["agent_wallet_id"],
            subject_key_id=wallets["key_id"],
            max_credits=5,
            expires_at=(utc_now() + timedelta(hours=1)).isoformat(),
            nonce="issuance-retry-nonce",
        )
        payload = (
            _action_request(**fields).model_dump(mode="json")
            if request.param == "action"
            else {**fields, "allowed_tools": ["partner.pay"]}
        )
        path = "/v1/action-permits" if request.param == "action" else "/v1/permits"
        identity = dict(
            wallet_id=wallets["sponsor_wallet_id"],
            endpoint=path,
            idempotency_key="issuance-retry",
        )
        headers = {**BOOTSTRAP_HEADERS, "Idempotency-Key": identity["idempotency_key"]}
        yield client, wallets, payload, path, headers, identity


@pytest.mark.anyio
@pytest.mark.parametrize(
    "invalid,reason",
    [
        ("expired", "permit_expired_at_creation"),
        ("budget", "permit_budget_exceeds_wallet_balance"),
        ("subject", "subject_wallet_not_found"),
    ],
)
async def test_rejected_issuance_retries_and_corrects_same_key(
    issuance, invalid, reason
):
    client, wallets, payload, path, headers, identity = issuance
    changes = {
        "expired": {"expires_at": (utc_now() - timedelta(hours=1)).isoformat()},
        "budget": {"max_credits": 1001},
        "subject": {"subject_wallet_id": "missing-subject"},
    }
    rejected = {**payload, **changes[invalid]}
    rejected_identity = {**identity, "wallet_id": rejected["issuer_wallet_id"]}
    for _ in range(2):
        response = await client.post(path, json=rejected, headers=headers)
        assert response.status_code == 400, response.text
        assert response.json()["detail"] == reason
        assert await get_idempotency_service().get_record(**rejected_identity) is None
        assert (
            await get_permit_service().list_permits(
                wallet_id=wallets["agent_wallet_id"]
            )
        )[1] == 0
    corrected = await client.post(path, json=payload, headers=headers)
    assert corrected.status_code == 201, corrected.text
    replay = await client.post(path, json=payload, headers=headers)
    assert replay.status_code == 201 and replay.json() == corrected.json()
    assert corrected.json()["nonce"] == payload["nonce"]
    assert (
        await get_permit_service().list_permits(wallet_id=wallets["agent_wallet_id"])
    )[1] == 1


@pytest.mark.anyio
@pytest.mark.parametrize("invalid", ["binding", "arguments"])
async def test_action_binding_rejection_releases_issuance_owner(
    clean_database, action_registry, monkeypatch, invalid
):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        wallets = await provision_agent_wallet(client)
        payload = _action_request(
            issuer_wallet_id=wallets["sponsor_wallet_id"],
            subject_wallet_id=wallets["agent_wallet_id"],
        ).model_dump(mode="json")
        headers = {**BOOTSTRAP_HEADERS, "Idempotency-Key": "binding-retry"}
        rejected = {**payload, "arguments": {}} if invalid == "arguments" else payload
        with monkeypatch.context() as patch:
            if invalid == "binding":
                patch.setattr(
                    get_service_registry(), "get_action_binding", lambda _: None
                )
            for _ in range(2):
                response = await client.post(
                    "/v1/action-permits", json=rejected, headers=headers
                )
                assert response.status_code == 400, response.text
        corrected = await client.post(
            "/v1/action-permits", json=payload, headers=headers
        )
        assert corrected.status_code == 201, corrected.text
        assert (
            await get_permit_service().list_permits(
                wallet_id=wallets["agent_wallet_id"]
            )
        )[1] == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "fault",
    [
        "signing",
        "commit_ack",
        "refresh",
        "generic_after_mint",
        "response_after_mint",
        "completion",
    ],
)
async def test_ambiguous_issuance_failure_retains_owner(issuance, monkeypatch, fault):
    client, wallets, payload, path, headers, identity = issuance
    service = get_permit_service()
    idem = get_idempotency_service()
    abandon = AsyncMock(wraps=idem.abandon)
    monkeypatch.setattr(idem, "abandon", abandon)
    with monkeypatch.context() as patch:
        if fault == "signing":
            patch.setattr(
                get_signing_key_service(),
                "sign_payload",
                AsyncMock(side_effect=PermitError("ambiguous_signing_failure")),
            )
        elif fault == "commit_ack":
            original_commit = AsyncSession.commit

            async def lose_commit_ack(session):
                minting = any(isinstance(row, PermitModel) for row in session.new)
                await original_commit(session)
                if minting:
                    raise RuntimeError("synthetic permit commit acknowledgement loss")

            patch.setattr(AsyncSession, "commit", lose_commit_ack)
        elif fault == "refresh":
            original_refresh = AsyncSession.refresh

            async def fail_refresh(session, instance, *args, **kwargs):
                if isinstance(instance, PermitModel):
                    raise RuntimeError("synthetic post-commit refresh failure")
                return await original_refresh(session, instance, *args, **kwargs)

            patch.setattr(AsyncSession, "refresh", fail_refresh)
        elif fault in {"generic_after_mint", "response_after_mint"}:
            original_persist = service._persist_permit

            async def fail_after_mint(*args, **kwargs):
                await original_persist(*args, **kwargs)
                error = PermitError if fault == "generic_after_mint" else RuntimeError
                raise error("synthetic response failure after mint")

            patch.setattr(service, "_persist_permit", fail_after_mint)
        else:
            patch.setattr(
                idem,
                "complete",
                AsyncMock(side_effect=RuntimeError("synthetic completion failure")),
            )
        first = await client.post(path, json=payload, headers=headers)
        assert first.status_code == (
            400 if fault in {"signing", "generic_after_mint"} else 500
        ), first.text
    record = await idem.get_record(**identity)
    assert record is not None and record.response_json is None
    retry = await client.post(path, json=payload, headers=headers)
    assert (
        retry.status_code == 409 and retry.json()["detail"] == "idempotency_in_progress"
    )
    assert (await idem.get_record(**identity)).record_id == record.record_id
    abandon.assert_not_awaited()
    assert (await service.list_permits(wallet_id=wallets["agent_wallet_id"]))[1] == (
        0 if fault == "signing" else 1
    )


@pytest.mark.anyio
@pytest.mark.parametrize("race", ["replaced", "completed", "cleanup_failure"])
async def test_rejection_cleanup_only_releases_exact_unfinished_owner(
    issuance, monkeypatch, race
):
    client, _, payload, path, headers, identity = issuance
    payload["expires_at"] = (utc_now() - timedelta(hours=1)).isoformat()
    idem = get_idempotency_service()
    original_abandon = idem.abandon

    async def changed_owner(**kwargs):
        async with get_session_factory()() as session:
            owner = await session.get(
                IdempotencyRecordModel, kwargs["expected_record_id"]
            )
            assert owner is not None
            if race == "replaced":
                owner.record_id = "replacement-issuance-owner"
            elif race == "completed":
                owner.response_json = '{"complete": true}'
            await session.commit()
        if race == "cleanup_failure":
            raise RuntimeError("synthetic cleanup unavailable")
        return await original_abandon(**kwargs)

    monkeypatch.setattr(idem, "abandon", changed_owner)
    response = await client.post(path, json=payload, headers=headers)
    assert response.status_code == (500 if race == "cleanup_failure" else 409)
    owner = await idem.get_record(**identity)
    assert owner is not None
    if race == "replaced":
        assert owner.record_id == "replacement-issuance-owner"
    elif race == "completed":
        assert owner.response_json == '{"complete": true}'
