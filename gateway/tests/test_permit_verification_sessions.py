"""Caller-owned session coverage for permit and receipt verification."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.db.database import get_session_factory
from app.db.models import PermitModel, ReceiptModel, SigningKeyModel
from app.main import app
from app.services import signing_keys
from app.services.permits import get_permit_service
from app.services.receipts import get_receipt_service
from app.services.signing_keys import SigningKeyError, get_signing_key_service
from tests.test_trust_helpers import (
    BOOTSTRAP_HEADERS,
    create_tool_permit,
    provision_agent_wallet,
)


@pytest.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as value:
        yield value


def _reject_nested_session():
    raise AssertionError("verification attempted a nested database session")


@pytest.mark.anyio
@pytest.mark.parametrize("case", ["valid", "tampered", "disabled"])
async def test_permit_verification_uses_caller_session_and_fails_closed(
    client: AsyncClient,
    clean_database,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    provisioned = await provision_agent_wallet(client)
    tool_name = "session-verification-tool"
    permit_response = await client.post(
        "/v1/permits",
        json={
            "issuer_wallet_id": provisioned["agent_wallet_id"],
            "subject_wallet_id": provisioned["agent_wallet_id"],
            "subject_key_id": provisioned["key_id"],
            "allowed_tools": [tool_name],
            "scopes": [f"tool:{tool_name}:invoke", "billing:charge"],
            "max_credits": 10,
            "aggregate_value_cap": 10,
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=30)
            ).isoformat(),
        },
        headers={**BOOTSTRAP_HEADERS, "Idempotency-Key": f"permit-session-{case}"},
    )
    assert permit_response.status_code == 201
    permit_id = permit_response.json()["permit_id"]
    service = get_permit_service()
    factory = get_session_factory()

    async with factory() as session:
        model = await session.get(PermitModel, permit_id)
        assert model is not None
        key = await session.get(SigningKeyModel, model.key_id)
        assert key is not None
        if case == "tampered":
            model.max_credits += Decimal("1")
        elif case == "disabled":
            key.status = "disabled"
        await session.flush()

        with monkeypatch.context() as patch:
            patch.setattr(signing_keys, "get_session_factory", _reject_nested_session)
            valid = await service._validate_model_for_action(
                session=session,
                model=model,
                wallet_id=provisioned["agent_wallet_id"],
                tool_name=tool_name,
                estimated_credits=Decimal("1"),
                key_id=provisioned["key_id"],
            )
            replay = await service.validate_replay_access(
                permit_id=permit_id,
                wallet_id=provisioned["agent_wallet_id"],
                tool_name=tool_name,
                key_id=provisioned["key_id"],
                session=session,
            )

        expected = case == "valid"
        assert valid.allowed is expected
        assert replay.allowed is expected
        if not expected:
            assert valid.reason == "permit_signature_invalid"
            assert replay.reason == "permit_signature_invalid"
        assert session.in_transaction()
        await session.rollback()


@pytest.mark.anyio
async def test_receipt_verification_uses_caller_session(
    client: AsyncClient,
    clean_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provisioned = await provision_agent_wallet(client)
    receipt_service = get_receipt_service()
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="session-receipt-tool",
        idem_key="permit-session-receipt",
    )
    receipt = await receipt_service.create_receipt(
        permit_id=permit["permit_id"],
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool="session-receipt-tool",
        request_payload={"value": "request"},
        response_payload={"value": "response"},
        ledger_entry_id=None,
        credits_authorized=Decimal("0"),
        credits_charged=Decimal("0"),
        outcome="denied",
        reason_code="session_test_denied",
        audit_event_id=None,
    )
    factory = get_session_factory()
    async with factory() as session:
        model = await session.get(ReceiptModel, receipt.receipt_id)
        assert model is not None
        with monkeypatch.context() as patch:
            patch.setattr(signing_keys, "get_session_factory", _reject_nested_session)
            assert await receipt_service.verify_model(model, session=session)
            assert (
                await receipt_service.signing_input_for_model(model, session=session)
                is not None
            )
        assert session.in_transaction()


@pytest.mark.anyio
async def test_prepared_signing_key_is_revalidated_in_caller_transaction(
    client: AsyncClient,
    clean_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="prepared-key-session-tool",
        idem_key="permit-prepared-key-session",
    )
    signing_key = await get_signing_key_service().ensure_active_key()
    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            stored_key = await session.get(SigningKeyModel, signing_key.key_id)
            assert stored_key is not None
            stored_key.status = "disabled"
            session.add(stored_key)

    async with factory() as session:
        async with session.begin():
            with monkeypatch.context() as patch:
                patch.setattr(
                    signing_keys, "get_session_factory", _reject_nested_session
                )
                with pytest.raises(SigningKeyError, match="signing_key_disabled"):
                    await get_receipt_service().create_receipt(
                        permit_id=permit["permit_id"],
                        wallet_id=provisioned["agent_wallet_id"],
                        key_id=provisioned["key_id"],
                        tool="prepared-key-session-tool",
                        request_payload={"value": "request"},
                        response_payload={"error": "refund_failed"},
                        ledger_entry_id=None,
                        credits_authorized=Decimal("0"),
                        credits_charged=Decimal("0"),
                        outcome="failed_unrefunded",
                        reason_code="refund_failed",
                        audit_event_id=None,
                        session=session,
                        prepared_signing_key_id=signing_key.key_id,
                    )
            receipt_count = await session.scalar(
                select(func.count()).select_from(ReceiptModel)
            )
            assert int(receipt_count or 0) == 0
