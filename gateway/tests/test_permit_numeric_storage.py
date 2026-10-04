"""Signed authority must retain exactly the credits that were requested."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import re

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import select

from app.db.database import get_session_factory
from app.db.models import (
    IdempotencyRecordModel,
    PermitModel,
    PermitRequestModel,
    WalletModel,
)
from app.main import app
from app.schemas.trust import (
    ActionPermitCreateRequest,
    PermitCreateRequest,
    PermitRequestCreate,
)
from app.services.permit_requests import PermitRequestError, PermitRequestService
from app.services.permits import PermitCreationRejectedError, get_permit_service
from tests.test_trust_helpers import provision_agent_wallet


UNSTORABLE = (
    "100000000000.12345678",
    "999999999999.99999999",
    "1000000000000",
    "0.123456789",
    "NaN",
    "Infinity",
    "-1",
    "0",
)
STORABLE = ("0.00000001", "1.12345678", "100000000000.125", "999999999999")


@pytest.mark.parametrize(
    "schema,field",
    [
        (PermitCreateRequest, "max_credits"),
        (PermitCreateRequest, "aggregate_value_cap"),
        (ActionPermitCreateRequest, "max_credits"),
        (PermitRequestCreate, "max_credits"),
    ],
)
def test_permit_credit_json_schema_preserves_positive_numeric_and_scale_bounds(
    schema, field
):
    contract = schema.model_json_schema()["properties"][field]
    branches = contract["anyOf"]
    nullable = field == "aggregate_value_cap"
    assert {branch.get("type") for branch in branches} == (
        {"number", "string", "null"} if nullable else {"number", "string"}
    )
    number = next(branch for branch in branches if branch["type"] == "number")
    assert number["exclusiveMinimum"] == 0
    text = next(branch for branch in branches if branch["type"] == "string")
    for amount in ("0.00000001", "1.12345678", "999999999999.99999999"):
        assert re.fullmatch(text["pattern"], amount)
    for amount in ("0.000000001", "1.123456789", "1000000000000", "NaN", "Infinity"):
        assert re.fullmatch(text["pattern"], amount) is None
    if nullable:
        assert contract["default"] is None


@pytest.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as value:
        yield value


def _body(wallet_id="synthetic", **changes):
    return {
        "issuer_wallet_id": wallet_id,
        "subject_wallet_id": wallet_id,
        "max_credits": "1",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        **changes,
    }


@pytest.mark.parametrize("amount", UNSTORABLE)
@pytest.mark.parametrize(
    "schema,field,extra",
    [
        (PermitCreateRequest, "max_credits", {}),
        (PermitCreateRequest, "aggregate_value_cap", {}),
        (
            ActionPermitCreateRequest,
            "max_credits",
            {"tool_name": "synthetic", "arguments": {}},
        ),
        (
            PermitRequestCreate,
            "max_credits",
            {"allowed_tools": ["synthetic"], "justification": "storage check"},
        ),
    ],
)
def test_permit_schemas_reject_credit_terms_that_change_in_storage(
    schema, field, extra, amount
):
    with pytest.raises(ValidationError):
        schema(**_body(**extra, **{field: amount}))


@pytest.mark.anyio
@pytest.mark.parametrize(
    "endpoint,field,extra",
    [
        ("/v1/permits", "max_credits", {}),
        ("/v1/permits", "aggregate_value_cap", {}),
        (
            "/v1/action-permits",
            "max_credits",
            {"tool_name": "synthetic", "arguments": {}},
        ),
        (
            "/v1/permit-requests",
            "max_credits",
            {"allowed_tools": ["synthetic"], "justification": "storage check"},
        ),
    ],
)
async def test_lossy_permit_terms_are_rejected_before_acceptance(
    client, clean_database, endpoint, field, extra, action_permit_route
):
    owner = await provision_agent_wallet(client)
    response = await client.post(
        endpoint,
        json=_body(owner["agent_wallet_id"], **extra, **{field: UNSTORABLE[0]}),
        headers={**owner["agent_headers"], "Idempotency-Key": "numeric-storage"},
    )
    assert response.status_code == 422, response.text
    async with get_session_factory()() as session:
        for model in (PermitModel, PermitRequestModel, IdempotencyRecordModel):
            assert list((await session.execute(select(model))).scalars()) == []


@pytest.mark.anyio
@pytest.mark.parametrize("field", ["max_credits", "aggregate_value_cap"])
@pytest.mark.parametrize("amount", STORABLE)
async def test_storable_permit_terms_round_trip_and_verify(
    client, clean_database, field, amount
):
    owner = await provision_agent_wallet(client)
    wallet_id = owner["agent_wallet_id"]
    async with get_session_factory()() as session:
        wallet = await session.get(WalletModel, wallet_id)
        wallet.balance = Decimal("999999999999")
        await session.commit()
    response = await client.post(
        "/v1/permits",
        json=_body(wallet_id, allowed_tools=["synthetic"], **{field: amount}),
        headers={**owner["agent_headers"], "Idempotency-Key": "numeric-storage-safe"},
    )
    assert response.status_code == 201, response.text
    assert Decimal(response.json()[field]) == Decimal(amount)
    async with get_session_factory()() as session:
        permit = await session.get(PermitModel, response.json()["permit_id"])
        assert getattr(permit, field) == Decimal(amount)
        assert await get_permit_service().verify_signature(permit, session=session)


@pytest.mark.anyio
@pytest.mark.parametrize("field", ["max_credits", "aggregate_value_cap"])
@pytest.mark.parametrize("amount", UNSTORABLE[:6])
async def test_permit_service_refuses_unvalidated_unstorable_terms(
    clean_database, monkeypatch, field, amount
):
    request = PermitCreateRequest(**_body())
    setattr(request, field, Decimal(amount))

    async def no_signing(*args, **kwargs):
        pytest.fail("invalid authority must be refused before signing")

    monkeypatch.setattr(
        "app.services.signing_keys.SigningKeyService.sign_payload", no_signing
    )
    with pytest.raises(PermitCreationRejectedError, match=f"^{field}_not_storable$"):
        await get_permit_service().create_permit(request)
    async with get_session_factory()() as session:
        assert list((await session.execute(select(PermitModel))).scalars()) == []


@pytest.mark.anyio
@pytest.mark.parametrize("amount", UNSTORABLE[:6])
async def test_permit_request_service_rejects_unstorable_terms_before_provider_calls(
    clean_database, monkeypatch, amount
):
    monkeypatch.setattr(
        "app.services.permit_requests.human_approval_available", lambda: (True, None)
    )
    service = PermitRequestService()

    def no_provider():
        pytest.fail("invalid authority must be refused before paging a human")

    monkeypatch.setattr(service, "_sentinel", no_provider)
    with pytest.raises(PermitRequestError, match="^max_credits_not_storable$"):
        await service.create(
            issuer_wallet_id="synthetic",
            subject_wallet_id="synthetic",
            subject_key_id=None,
            idempotency_key="numeric-request",
            allowed_tools=["synthetic"],
            scopes=[],
            max_credits=Decimal(amount),
            permit_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            justification="storage check",
        )
    async with get_session_factory()() as session:
        assert list((await session.execute(select(PermitRequestModel))).scalars()) == []
