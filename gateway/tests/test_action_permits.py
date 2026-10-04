"""Strict single-action authority and legacy signature compatibility."""

from dataclasses import replace
from decimal import Decimal

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from app.schemas.trust import ActionPermitFields
from app.services.action_permits import (
    ActionToolBinding,
    action_payload_hash,
    canonical_action_payload,
    upstream_action_binding_hash,
)
from app.services.permits import PermitService
from app.services.signing_keys import canonical_json
from tests.test_permit_signing_input_snapshot import _base_model


MONEY_SCHEMA = {
    "type": "object",
    "properties": {
        "amount_minor": {"type": "integer"},
        "recipient": {"type": "string"},
        "currency": {"type": "string", "default": "USD"},
    },
    "required": ["amount_minor", "recipient"],
    "additionalProperties": False,
}
BINDING = ActionToolBinding(
    "test-plane",
    "partner.pay",
    upstream_action_binding_hash(
        deployment_authority="test-plane",
        public_tool_id="partner.pay",
        upstream_origin="https://fixture.invalid",
        upstream_tool_name="pay",
        schema_id="money",
        schema_version="1",
        input_schema=MONEY_SCHEMA,
    ),
    "money",
    "1",
    MONEY_SCHEMA,
)
FIELDS = dict(
    action_contract_version=1,
    action_payload_hash="b" * 64,
    action_schema_id="money",
    action_schema_version="1",
    action_public_tool_id="partner.pay",
    action_upstream_binding_hash="a" * 64,
)


def test_strict_action_digest():
    args = {"amount_minor": 1, "recipient": "alice"}
    digest = action_payload_hash(BINDING, "wallet", args)
    assert digest == action_payload_hash(
        BINDING, "wallet", dict(reversed(list(args.items())))
    )
    assert digest == action_payload_hash(BINDING, "wallet", {**args, "currency": "USD"})
    for changed in ({**args, "recipient": "bob"}, {**args, "amount_minor": 2}):
        assert digest != action_payload_hash(BINDING, "wallet", changed)
    for value in ("1", True, 1.0, float("nan"), float("inf"), Decimal("1"), object()):
        with pytest.raises(ValueError):
            action_payload_hash(BINDING, "wallet", {**args, "amount_minor": value})
    with pytest.raises(ValueError):
        action_payload_hash(BINDING, "wallet", {1: "bad", **args})
    for binding in (
        replace(BINDING, public_tool_id="other"),
        replace(BINDING, schema_version="2"),
        replace(BINDING, upstream_binding_hash="c" * 64),
    ):
        assert digest != action_payload_hash(binding, "wallet", args)
    assert digest != action_payload_hash(BINDING, "other-wallet", args)
    assert '"currency":"USD"' in canonical_action_payload(BINDING, "wallet", args)


def test_number_representations_remain_distinct():
    binding = replace(
        BINDING,
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "number"}},
            "required": ["value"],
        },
    )
    assert action_payload_hash(binding, "wallet", {"value": 1}) != action_payload_hash(
        binding, "wallet", {"value": 1.0}
    )
    with pytest.raises(ValueError):
        action_payload_hash(binding, "wallet", {"value": "1"})


def test_action_fields_all_or_none():
    assert ActionPermitFields().model_dump() == dict.fromkeys(FIELDS)
    assert ActionPermitFields(**FIELDS).action_contract_version == 1
    for field in FIELDS:
        with pytest.raises(ValidationError):
            ActionPermitFields(**{k: v for k, v in FIELDS.items() if k != field})
    for version in (2, True, "1"):
        with pytest.raises(ValidationError):
            ActionPermitFields(**{**FIELDS, "action_contract_version": version})
    with pytest.raises(ValidationError):
        ActionPermitFields(**{**FIELDS, "action_payload_hash": "bad"})
    with pytest.raises(ValueError):
        PermitService._unsigned_payload(_base_model(action_contract_version=1))


def test_action_signature_covers_every_binding_field():
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    model = _base_model(**FIELDS)
    payload = PermitService._verification_payload(model)
    assert all(payload[field] == value for field, value in FIELDS.items())
    signature = key.sign(canonical_json(payload).encode())
    key.public_key().verify(signature, canonical_json(payload).encode())
    for field in FIELDS:
        tampered = dict(payload)
        tampered[field] = 2 if field == "action_contract_version" else "changed"
        with pytest.raises(InvalidSignature):
            key.public_key().verify(signature, canonical_json(tampered).encode())
    legacy = PermitService._unsigned_payload(_base_model())
    assert not set(FIELDS).intersection(legacy)


@pytest.mark.parametrize(
    "invalid",
    [
        {"oneOf": []},
        {"format": "email"},
        {"pattern": ".*"},
        {"additionalProperties": {"type": "string"}},
    ],
)
def test_unsupported_schema_rejects(invalid):
    schema = {**MONEY_SCHEMA, **invalid}
    with pytest.raises(ValueError):
        action_payload_hash(
            replace(BINDING, input_schema=schema),
            "wallet",
            {"amount_minor": 1, "recipient": "alice"},
        )


def test_nested_defaults_constraints_and_raw_json_rejection():
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "minItems": 1,
                "maxItems": 2,
                "items": {
                    "type": "object",
                    "properties": {
                        "amount": {"type": "integer", "minimum": 1, "maximum": 3},
                        "currency": {
                            "type": "string",
                            "default": "USD",
                            "enum": ["USD"],
                        },
                    },
                    "required": ["amount"],
                },
            }
        },
        "required": ["items"],
    }
    binding = replace(BINDING, input_schema=schema)
    original = {"items": [{"amount": 1}]}
    assert action_payload_hash(binding, "wallet", original) == action_payload_hash(
        binding, "wallet", {"items": [{"amount": 1, "currency": "USD"}]}
    )
    assert original == {"items": [{"amount": 1}]}
    for invalid in (
        {"items": []},
        {"items": [{"amount": 0}]},
        {"items": [{"amount": 1, "currency": "EUR"}]},
        {"items": [{"amount": 1, "other": None}]},
        {"items": [{"amount": 1}], "extra": object()},
    ):
        with pytest.raises(ValueError):
            action_payload_hash(binding, "wallet", invalid)
    invalid_default = {
        "type": "object",
        "properties": {"value": {"type": "integer", "default": "1"}},
    }
    with pytest.raises(ValueError):
        action_payload_hash(
            replace(BINDING, input_schema=invalid_default), "wallet", {}
        )


@pytest.mark.anyio
async def test_generic_service_action_issuance_is_closed():
    from datetime import datetime, timedelta, timezone
    from app.schemas.trust import PermitCreateRequest
    from app.services.permits import PermitError

    request = PermitCreateRequest(
        issuer_wallet_id="issuer",
        subject_wallet_id="subject",
        max_credits=1,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        **FIELDS,
    )
    with pytest.raises(PermitError, match="action_permit_requires_trusted_issuance"):
        await PermitService().create_permit(request)


@pytest.mark.anyio
@pytest.mark.parametrize("stored_window_null", [False, True])
async def test_historical_envelope_issuance_record_replays(
    clean_database, stored_window_null
):
    from datetime import datetime, timedelta, timezone
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import select
    from app.db.database import get_session_factory
    from app.db.models import PermitModel
    from app.main import app
    from app.schemas.trust import PermitCreateRequest
    from app.services.idempotency import get_idempotency_service
    from app.services.signing_keys import sha256_hex
    from tests.test_trust_helpers import provision_agent_wallet

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        wallet = await provision_agent_wallet(client)
        payload = dict(
            issuer_wallet_id=wallet["agent_wallet_id"],
            subject_wallet_id=wallet["agent_wallet_id"],
            subject_key_id=wallet["key_id"],
            allowed_tools=["historical-echo"],
            max_credits=10,
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        )
        request = PermitCreateRequest(**payload)
        permit = await PermitService().create_permit(request)
        historical_payload = request.model_dump(mode="json", exclude=set(FIELDS))
        if not stored_window_null:
            historical_payload.pop("repeat_window_seconds")
        historical_response = permit.model_dump(mode="json", exclude=set(FIELDS))
        identity = dict(
            wallet_id=wallet["agent_wallet_id"],
            endpoint="/v1/permits",
            idempotency_key="historical-envelope",
        )
        idem = get_idempotency_service()
        await idem.begin(**identity, request_payload=historical_payload)
        await idem.complete(
            **identity,
            response_reference=permit.permit_id,
            response_json=historical_response,
            status_code=201,
        )
        headers = {
            **wallet["agent_headers"],
            "Idempotency-Key": identity["idempotency_key"],
        }
        for explicit_null in (False, True):
            replay_payload = (
                {**payload, **dict.fromkeys(FIELDS)} if explicit_null else payload
            )
            replay = await client.post(
                "/v1/permits", json=replay_payload, headers=headers
            )
            assert replay.status_code == 201, replay.text
            assert replay.json()["permit_id"] == permit.permit_id
        conflict = await client.post(
            "/v1/permits", json={**payload, "max_credits": 11}, headers=headers
        )
        assert conflict.status_code == 409
        record = await idem.get_record(**identity)
        assert record.request_hash == sha256_hex(historical_payload)
        async with get_session_factory()() as session:
            assert (
                len(list((await session.execute(select(PermitModel))).scalars())) == 1
            )
        action = await client.post(
            "/v1/permits",
            json={**payload, **FIELDS},
            headers={**headers, "Idempotency-Key": "action-input-refused"},
        )
        assert action.status_code == 400
        assert action.json()["detail"] == "action_permit_requires_trusted_issuance"
        assert (
            await idem.get_record(
                **{**identity, "idempotency_key": "action-input-refused"}
            )
            is None
        )


def _action_request(**overrides):
    from datetime import timedelta
    from app.core.time import utc_now
    from app.schemas.trust import ActionPermitCreateRequest

    return ActionPermitCreateRequest(
        **{
            "issuer_wallet_id": "sponsor",
            "subject_wallet_id": "agent",
            "tool_name": "partner.pay",
            "arguments": {"amount_minor": 1, "recipient": "alice"},
            "max_credits": 5,
            "expires_at": utc_now() + timedelta(hours=1),
            **overrides,
        }
    )


@pytest.mark.anyio
async def test_subject_cannot_self_issue_action(monkeypatch):
    from fastapi import HTTPException
    from app.core.auth import AuthContext
    from app.services.action_permits import create_action_permit

    with pytest.raises(HTTPException) as denied:
        await create_action_permit(
            _action_request(issuer_wallet_id="agent"),
            AuthContext("test", "", wallet_id="agent"),
        )
    assert denied.value.status_code == 403


@pytest.mark.anyio
async def test_service_issuance_enforces_authority(monkeypatch):
    from fastapi import HTTPException
    from unittest.mock import AsyncMock
    from app.core.auth import AuthContext
    from app.services.action_permits import create_action_permit
    from app.services.agent_money import get_agent_money

    monkeypatch.setattr(
        get_agent_money(), "is_wallet_or_descendant", AsyncMock(return_value=False)
    )
    for wallet in ("agent", "foreign", "sponsor"):
        with pytest.raises(HTTPException) as denied:
            await create_action_permit(
                _action_request(), AuthContext("test", "", wallet_id=wallet)
            )
        assert denied.value.status_code == 403


@pytest.mark.anyio
async def test_sponsor_can_issue_for_descendant(monkeypatch):
    from unittest.mock import AsyncMock
    from app.core.auth import AuthContext
    from app.services.action_permits import create_action_permit
    from app.services.agent_money import get_agent_money
    from app.services.permits import get_permit_service
    from app.services.service_registry import get_service_registry

    monkeypatch.setattr(
        get_agent_money(), "is_wallet_or_descendant", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        get_service_registry(), "get", AsyncMock(return_value={"trusted": True})
    )
    monkeypatch.setattr(
        get_service_registry(), "get_action_binding", lambda record: BINDING
    )
    persist = AsyncMock(side_effect=lambda request: request)
    monkeypatch.setattr(get_permit_service(), "_persist_permit", persist)
    for auth in (
        AuthContext("test", "", wallet_id="sponsor", key_id="sponsor-key"),
        AuthContext("test", "", is_bootstrap_admin=True),
    ):
        permit = await create_action_permit(
            _action_request(subject_key_id="agent-key"), auth
        )
        assert permit.action_payload_hash == action_payload_hash(
            BINDING, "agent", _action_request().arguments
        )
        assert permit.allowed_tools == ["partner.pay"]
        assert permit.max_calls_per_tool == {"partner.pay": 1}
        assert permit.subject_key_id == "agent-key"


def test_action_quote_approval_repeat_options_rejected():
    for option in (
        {"quote_id": "q"},
        {"requires_human_approval": True},
        {"allow_identical_repeats": True},
        {"repeat_window_seconds": 60},
        {"action_payload_hash": "b" * 64},
        {"allowed_tools": ["other"]},
    ):
        with pytest.raises(ValidationError):
            _action_request(**option)


def test_registry_binding_is_trusted_snapshot():
    from app.services.service_registry import ServiceRegistry
    from app.schemas.billing import ServiceCategory

    registry = ServiceRegistry()
    schema = dict(MONEY_SCHEMA)
    record = registry.register_upstream(
        service_id="partner.pay",
        name="Pay",
        description="fixture",
        category=next(iter(ServiceCategory)),
        executor=object(),
        input_schema=schema,
        output_schema=None,
        credits_per_unit=1,
        upstream_tool_name="pay",
        upstream_origin="https://fixture.invalid",
        action_binding=BINDING,
    )
    assert registry.get_action_binding(record) == BINDING
    schema["type"] = "string"
    assert registry.get_action_binding(record) == BINDING
    record["upstream_tool_name"] = "other"
    with pytest.raises(ValueError, match="action_binding_registry_mismatch"):
        registry.get_action_binding(record)
    assert registry.get_action_binding({"action_binding": BINDING}) is None


@pytest.mark.anyio
async def test_action_route_persists_and_replays(
    clean_database, monkeypatch, action_permit_route
):
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    from app.services.service_registry import get_service_registry
    from tests.test_trust_helpers import BOOTSTRAP_HEADERS, provision_agent_wallet

    registry = get_service_registry()
    monkeypatch.setattr(
        registry,
        "get",
        __import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock(
            return_value={"fixture": True}
        ),
    )
    monkeypatch.setattr(registry, "get_action_binding", lambda record: BINDING)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        wallets = await provision_agent_wallet(client)
        payload = _action_request(
            issuer_wallet_id=wallets["sponsor_wallet_id"],
            subject_wallet_id=wallets["agent_wallet_id"],
            subject_key_id=wallets["key_id"],
        ).model_dump(mode="json")
        denied = await client.post(
            "/v1/action-permits",
            json=payload,
            headers={**wallets["agent_headers"], "Idempotency-Key": "denied"},
        )
        assert denied.status_code == 403
        headers = {**BOOTSTRAP_HEADERS, "Idempotency-Key": "trusted"}
        response = await client.post(
            "/v1/action-permits", json=payload, headers=headers
        )
        assert response.status_code == 201, response.text
        permit = response.json()
        assert permit["action_payload_hash"] == action_payload_hash(
            BINDING, wallets["agent_wallet_id"], payload["arguments"]
        )
        assert permit["max_calls_per_tool"] == {"partner.pay": 1}
        replay = await client.post("/v1/action-permits", json=payload, headers=headers)
        assert replay.json() == permit
        changed = await client.post(
            "/v1/action-permits",
            json={**payload, "arguments": {"amount_minor": 2, "recipient": "alice"}},
            headers=headers,
        )
        assert changed.status_code == 409


@pytest.mark.anyio
async def test_generic_permit_cannot_set_action_fields():
    from datetime import timedelta
    from app.core.time import utc_now
    from app.schemas.trust import PermitCreateRequest
    from app.services.permits import PermitError

    ordinary = PermitCreateRequest(
        issuer_wallet_id="agent",
        subject_wallet_id="agent",
        max_credits=5,
        expires_at=utc_now() + timedelta(hours=1),
    )
    for field, value in FIELDS.items():
        # Internal callers using model_copy must not bypass the generic boundary.
        with pytest.raises(
            PermitError, match="action_permit_requires_trusted_issuance"
        ):
            await PermitService().create_permit(
                ordinary.model_copy(update={field: value})
            )


@pytest.mark.parametrize(
    "changed",
    [{"upstream_origin": "https://other.invalid"}, {"upstream_tool_name": "other"}],
)
def test_registry_re_registration_cannot_reuse_old_destination_hash(changed):
    from app.services.service_registry import ServiceRegistry
    from app.schemas.billing import ServiceCategory

    registration = dict(
        service_id="partner.pay",
        name="Pay",
        description="fixture",
        category=next(iter(ServiceCategory)),
        executor=object(),
        input_schema=MONEY_SCHEMA,
        output_schema=None,
        credits_per_unit=1,
        upstream_tool_name="pay",
        upstream_origin="https://fixture.invalid",
        action_binding=BINDING,
    )
    registry = ServiceRegistry()
    registry.register_upstream(**registration)
    for instance in (registry, ServiceRegistry()):
        with pytest.raises(ValueError, match="action_binding_registry_mismatch"):
            instance.register_upstream(**{**registration, **changed})
