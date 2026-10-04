"""Permit issuance and retry compatibility across the schema-040 rollout."""

from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core.config import Settings, get_settings
from app.core.time import utc_now
from app.db.database import get_session_factory
from app.db.models import IdempotencyRecordModel, PermitModel
from app.main import app
from app.schemas.trust import ActionPermitFields, PermitCreateRequest
from app.services.idempotency import IdempotencyConflictError, get_idempotency_service
from app.services.permits import PermitError, get_permit_service
from app.services.signing_keys import sha256_hex
from tests.test_trust_helpers import provision_agent_wallet


@pytest.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


def _request(wallet):
    return {
        "issuer_wallet_id": wallet["agent_wallet_id"],
        "subject_wallet_id": wallet["agent_wallet_id"],
        "subject_key_id": wallet["key_id"],
        "allowed_tools": ["rollout-test"],
        "max_credits": 10,
        "expires_at": (utc_now() + timedelta(hours=1)).isoformat(),
    }


def test_repeat_window_issuance_defaults_off(monkeypatch):
    monkeypatch.delenv("ENABLE_PERMIT_REPEAT_WINDOW_ISSUANCE", raising=False)
    assert Settings(_env_file=None).ENABLE_PERMIT_REPEAT_WINDOW_ISSUANCE is False


@pytest.mark.asyncio
async def test_repeat_window_is_refused_until_issuance_enabled(
    client, clean_database, monkeypatch
):
    wallet = await provision_agent_wallet(client)
    payload = {**_request(wallet), "repeat_window_seconds": 60}
    headers = {**wallet["agent_headers"], "Idempotency-Key": "disabled-window"}
    for _ in range(2):
        response = await client.post("/v1/permits", json=payload, headers=headers)
        assert response.status_code == 400
        assert response.json()["detail"] == "repeat_window_issuance_disabled"

    idem = get_idempotency_service()
    assert (
        await idem.get_record(
            wallet_id=wallet["agent_wallet_id"],
            endpoint="/v1/permits",
            idempotency_key="disabled-window",
        )
        is None
    )
    async with get_session_factory()() as session:
        assert list((await session.execute(select(PermitModel))).scalars()) == []

    monkeypatch.setattr(get_settings(), "ENABLE_PERMIT_REPEAT_WINDOW_ISSUANCE", True)
    response = await client.post("/v1/permits", json=payload, headers=headers)
    assert response.status_code == 201
    assert response.json()["repeat_window_seconds"] == 60


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_null", [False, True])
@pytest.mark.parametrize("stored_null", [False, True])
@pytest.mark.parametrize("completed", [False, True])
async def test_legacy_permit_creation_replays_with_omitted_or_null_window(
    client, clean_database, explicit_null, stored_null, completed
):
    wallet = await provision_agent_wallet(client)
    payload = _request(wallet)
    request = PermitCreateRequest(**payload)
    permit = await get_permit_service().create_permit(request)
    legacy_payload = request.model_dump(
        mode="json", exclude=set(ActionPermitFields.model_fields)
    )
    if not stored_null:
        legacy_payload.pop("repeat_window_seconds")
    legacy_response = permit.model_dump(
        mode="json", exclude=set(ActionPermitFields.model_fields)
    )
    legacy_response.pop("repeat_window_seconds", None)
    idem = get_idempotency_service()
    identity = {
        "wallet_id": wallet["agent_wallet_id"],
        "endpoint": "/v1/permits",
        "idempotency_key": "legacy-replay",
    }
    await idem.begin(**identity, request_payload=legacy_payload)
    if completed:
        await idem.complete(
            **identity,
            response_reference=permit.permit_id,
            response_json=legacy_response,
            status_code=201,
        )
    if explicit_null:
        payload["repeat_window_seconds"] = None
    headers = {**wallet["agent_headers"], "Idempotency-Key": "legacy-replay"}
    response = await client.post("/v1/permits", json=payload, headers=headers)
    if completed:
        assert response.status_code == 201
        assert response.json()["permit_id"] == permit.permit_id
        assert response.json()["repeat_window_seconds"] is None
    else:
        assert response.status_code == 409
        assert response.json()["detail"] == "idempotency_in_progress"
    conflict = await client.post(
        "/v1/permits", json={**payload, "max_credits": 11}, headers=headers
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "idempotency_key_reused"
    async with get_session_factory()() as session:
        records = list(
            (await session.execute(select(IdempotencyRecordModel))).scalars()
        )
        assert len(records) == 1
        assert records[0].request_hash == sha256_hex(legacy_payload)
        permits = list((await session.execute(select(PermitModel))).scalars())
        assert len(permits) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_null", [False, True])
async def test_new_windowless_permits_keep_pre040_request_hash(
    client, clean_database, explicit_null
):
    wallet = await provision_agent_wallet(client)
    payload = _request(wallet)
    if explicit_null:
        payload["repeat_window_seconds"] = None
    headers = {**wallet["agent_headers"], "Idempotency-Key": "new-windowless"}
    created = await client.post("/v1/permits", json=payload, headers=headers)
    assert created.status_code == 201
    replay = await client.post("/v1/permits", json=payload, headers=headers)
    assert replay.status_code == 201
    assert replay.json() == created.json()
    canonical = PermitCreateRequest(**payload).model_dump(
        mode="json", exclude=set(ActionPermitFields.model_fields)
    )
    canonical.pop("repeat_window_seconds")
    async with get_session_factory()() as session:
        records = list(
            (await session.execute(select(IdempotencyRecordModel))).scalars()
        )
        assert len(records) == 1
        assert records[0].request_hash == sha256_hex(canonical)
        permits = list((await session.execute(select(PermitModel))).scalars())
        assert len(permits) == 1


@pytest.mark.asyncio
async def test_compatible_replay_does_not_recreate_a_disappearing_record(
    client, clean_database, monkeypatch
):
    wallet = await provision_agent_wallet(client)
    payload = _request(wallet)
    idem = get_idempotency_service()
    identity = {
        "wallet_id": wallet["agent_wallet_id"],
        "endpoint": "/v1/permits",
        "idempotency_key": "disappearing-windowless",
    }
    await idem.begin(
        **identity,
        request_payload=PermitCreateRequest(**payload).model_dump(
            mode="json", exclude=set(ActionPermitFields.model_fields)
        ),
    )
    record = await idem.get_record(**identity)
    original_begin = idem.begin_with_record

    async def remove_after_conflict(**kwargs):
        try:
            return await original_begin(**kwargs)
        except IdempotencyConflictError:
            async with get_session_factory()() as session:
                await session.execute(
                    delete(IdempotencyRecordModel).where(
                        IdempotencyRecordModel.record_id == record.record_id
                    )
                )
                await session.commit()
            raise

    monkeypatch.setattr(idem, "begin_with_record", remove_after_conflict)
    response = await client.post(
        "/v1/permits",
        json=payload,
        headers={
            **wallet["agent_headers"],
            "Idempotency-Key": identity["idempotency_key"],
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "idempotency_key_reused"
    assert await idem.get_record(**identity) is None
    async with get_session_factory()() as session:
        assert list((await session.execute(select(PermitModel))).scalars()) == []


@pytest.mark.asyncio
async def test_disabling_issuance_preserves_completed_replay_and_verification(
    client, clean_database, monkeypatch
):
    wallet = await provision_agent_wallet(client)
    payload = {**_request(wallet), "repeat_window_seconds": 60}
    headers = {**wallet["agent_headers"], "Idempotency-Key": "issued-window"}
    monkeypatch.setattr(get_settings(), "ENABLE_PERMIT_REPEAT_WINDOW_ISSUANCE", True)
    created = await client.post("/v1/permits", json=payload, headers=headers)
    assert created.status_code == 201
    permit = created.json()
    assert permit["repeat_window_seconds"] == 60
    monkeypatch.setattr(get_settings(), "ENABLE_PERMIT_REPEAT_WINDOW_ISSUANCE", False)
    replay = await client.post("/v1/permits", json=payload, headers=headers)
    assert replay.status_code == 201
    assert replay.json() == permit
    conflict = await client.post(
        "/v1/permits", json={**payload, "repeat_window_seconds": 61}, headers=headers
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "idempotency_key_reused"
    with pytest.raises(PermitError, match="^repeat_window_issuance_disabled$"):
        await get_permit_service().create_permit(PermitCreateRequest(**payload))

    verify = {
        "permit_id": permit["permit_id"],
        "wallet_id": wallet["agent_wallet_id"],
        "tool": "rollout-test",
        "estimated_credits": 1,
    }
    response = await client.post(
        "/v1/permits/verify", json=verify, headers=wallet["agent_headers"]
    )
    assert response.status_code == 200
    assert response.json()["valid"] is True
    async with get_session_factory()() as session:
        permits = list((await session.execute(select(PermitModel))).scalars())
        assert len(permits) == 1
        permits[0].repeat_window_seconds = 61
        await session.commit()
    tampered = await client.post(
        "/v1/permits/verify", json=verify, headers=wallet["agent_headers"]
    )
    assert tampered.json()["valid"] is False
    assert tampered.json()["reason"] == "permit_signature_invalid"


@pytest.mark.asyncio
async def test_disabled_issuance_preserves_in_progress_key(client, clean_database):
    wallet = await provision_agent_wallet(client)
    payload = {**_request(wallet), "repeat_window_seconds": 60}
    await get_idempotency_service().begin(
        wallet_id=wallet["agent_wallet_id"],
        endpoint="/v1/permits",
        idempotency_key="pending-window",
        request_payload=PermitCreateRequest(**payload).model_dump(
            mode="json", exclude=set(ActionPermitFields.model_fields)
        ),
    )
    response = await client.post(
        "/v1/permits",
        json=payload,
        headers={**wallet["agent_headers"], "Idempotency-Key": "pending-window"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "idempotency_in_progress"


@pytest.mark.asyncio
@pytest.mark.parametrize("window", [0, -1, True, 31536001])
async def test_repeat_window_invalid_input_is_rejected(client, clean_database, window):
    wallet = await provision_agent_wallet(client)
    response = await client.post(
        "/v1/permits",
        json={**_request(wallet), "repeat_window_seconds": window},
        headers={**wallet["agent_headers"], "Idempotency-Key": "invalid-window"},
    )
    assert response.status_code == 422
    assert (
        await get_idempotency_service().get_record(
            wallet_id=wallet["agent_wallet_id"],
            endpoint="/v1/permits",
            idempotency_key="invalid-window",
        )
        is None
    )


@pytest.mark.asyncio
async def test_disabled_issuance_does_not_bypass_wallet_authority(
    client, clean_database
):
    attacker = await provision_agent_wallet(client)
    victim = await provision_agent_wallet(client)
    payload = {
        **_request(attacker),
        "subject_wallet_id": victim["agent_wallet_id"],
        "repeat_window_seconds": 60,
    }
    response = await client.post(
        "/v1/permits",
        json=payload,
        headers={**attacker["agent_headers"], "Idempotency-Key": "foreign-window"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "subject_wallet_access_denied"
