"""Direct service-level tests for app.services.kyc_service.KYCService.

Route-level coverage lives in test_kyc.py; these tests exercise the service
itself: session creation, status reads, webhook handling and manual reject,
with Stripe and notifications mocked so nothing touches the network.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import stripe
from sqlalchemy import select

from app.db.database import get_session_factory
from app.db.models import KYCVerificationModel, WalletModel
from app.services.agent_money import WalletNotFoundError
from app.services.kyc_service import (
    KYCNotRequiredError,
    KYCService,
    KYCVerificationError,
    get_kyc_service,
)

_wallet_seq = 0


async def _make_wallet(wallet_type="sponsor", kyc_status="pending", status="active"):
    """Insert a wallet row directly so only the KYC service is under test."""
    global _wallet_seq
    _wallet_seq += 1
    wallet_id = f"w-kyc-svc-{_wallet_seq}"
    async with get_session_factory()() as session:
        session.add(
            WalletModel(
                wallet_id=wallet_id,
                wallet_type=wallet_type,
                owner_name="KYC Service",
                kyc_status=kyc_status,
                status=status,
            )
        )
        await session.commit()
    return wallet_id


@pytest.fixture
async def sponsor_pending_id(clean_database):
    return await _make_wallet()


def _stripe_session(session_id="vs_test_service"):
    mock_session = MagicMock()
    mock_session.id = session_id
    mock_session.url = "https://verify.stripe.com/test_session"
    return mock_session


async def _wallet_row(wallet_id):
    async with get_session_factory()() as session:
        result = await session.execute(
            select(WalletModel).where(WalletModel.wallet_id == wallet_id)
        )
        return result.scalar_one()


async def test_create_session_success(sponsor_pending_id):
    service = KYCService()
    with patch(
        "app.services.kyc_service.stripe.identity.VerificationSession.create",
        return_value=_stripe_session(),
    ) as mock_create:
        result = await service.create_verification_session(
            sponsor_pending_id,
            "https://example.com/callback",
            document_type="passport",
        )

    assert result["wallet_id"] == sponsor_pending_id
    assert result["session_id"] == "vs_test_service"
    assert result["session_url"] == "https://verify.stripe.com/test_session"
    assert result["status"] == "pending"
    assert result["expires_at"] > result["created_at"]
    kwargs = mock_create.call_args.kwargs
    assert kwargs["options"]["document"]["allowed_types"] == ["passport"]

    async with get_session_factory()() as session:
        stored = (
            await session.execute(
                select(KYCVerificationModel).where(
                    KYCVerificationModel.verification_id == result["verification_id"]
                )
            )
        ).scalar_one()
        assert stored.stripe_session_id == "vs_test_service"

    wallet = await _wallet_row(sponsor_pending_id)
    assert wallet.status == "pending_kyc"


async def test_create_session_without_document_type_omits_allowed_types(
    sponsor_pending_id,
):
    service = KYCService()
    with patch(
        "app.services.kyc_service.stripe.identity.VerificationSession.create",
        return_value=_stripe_session("vs_no_doctype"),
    ) as mock_create:
        await service.create_verification_session(
            sponsor_pending_id, "https://example.com/callback"
        )
    assert "allowed_types" not in mock_create.call_args.kwargs["options"]["document"]


async def test_create_session_unknown_wallet_raises(clean_database):
    service = KYCService()
    with pytest.raises(WalletNotFoundError):
        await service.create_verification_session(
            "no-such-wallet", "https://example.com/callback"
        )


async def test_create_session_for_agent_wallet_is_not_required(clean_database):
    agent_id = await _make_wallet(wallet_type="agent", kyc_status="not_required")
    with pytest.raises(KYCNotRequiredError):
        await KYCService().create_verification_session(
            agent_id, "https://example.com/callback"
        )


async def test_create_session_when_kyc_not_required_raises(clean_database):
    wallet_id = await _make_wallet(kyc_status="not_required")
    with pytest.raises(KYCNotRequiredError):
        await KYCService().create_verification_session(
            wallet_id, "https://example.com/callback"
        )


async def test_create_session_when_already_verified_raises(clean_database):
    wallet_id = await _make_wallet(kyc_status="verified")
    with pytest.raises(KYCNotRequiredError):
        await KYCService().create_verification_session(
            wallet_id, "https://example.com/callback"
        )


async def test_create_session_stripe_failure_raises(sponsor_pending_id):
    service = KYCService()
    with (
        patch(
            "app.services.kyc_service.stripe.identity.VerificationSession.create",
            side_effect=stripe.error.StripeError("stripe is down"),
        ),
        pytest.raises(KYCVerificationError),
    ):
        await service.create_verification_session(
            sponsor_pending_id, "https://example.com/callback"
        )


async def test_get_status_pending_requires_verification(sponsor_pending_id):
    status = await KYCService().get_verification_status(sponsor_pending_id)
    assert status["wallet_id"] == sponsor_pending_id
    assert status["kyc_status"] == "pending"
    assert status["requires_verification"] is True
    assert "verification" in status["message"].lower()


async def test_get_status_unknown_wallet_raises(clean_database):
    with pytest.raises(WalletNotFoundError):
        await KYCService().get_verification_status("no-such-wallet")


async def test_get_verification_details_unknown_returns_none(clean_database):
    assert await KYCService().get_verification_details("no-such-verification") is None


async def test_get_verification_details_round_trip(sponsor_pending_id):
    service = KYCService()
    with patch(
        "app.services.kyc_service.stripe.identity.VerificationSession.create",
        return_value=_stripe_session("vs_details"),
    ):
        created = await service.create_verification_session(
            sponsor_pending_id, "https://example.com/callback"
        )
    details = await service.get_verification_details(created["verification_id"])
    assert details["wallet_id"] == sponsor_pending_id
    assert details["status"] == "pending"
    assert details["stripe_session_id"] == "vs_details"


async def test_handle_webhook_bad_signature_returns_false(sponsor_pending_id):
    service = KYCService()
    assert await service.handle_webhook(b"{garbage", "not-a-signature") is False


async def test_handle_webhook_unknown_event_is_ignored(sponsor_pending_id):
    service = KYCService()
    event = {"type": "charge.succeeded", "data": {"object": {"id": "ch_1"}}}
    with patch(
        "app.services.kyc_service.stripe.Webhook.construct_event",
        return_value=event,
    ):
        assert await service.handle_webhook(b"{}", "sig") is True
    status = await service.get_verification_status(sponsor_pending_id)
    assert status["kyc_status"] == "pending"


async def test_handle_webhook_verified_marks_wallet(sponsor_pending_id):
    service = KYCService()
    with patch(
        "app.services.kyc_service.stripe.identity.VerificationSession.create",
        return_value=_stripe_session("vs_verified"),
    ):
        await service.create_verification_session(
            sponsor_pending_id, "https://example.com/callback"
        )
    event = {
        "type": "identity.verification_session.verified",
        "data": {"object": {"id": "vs_verified"}},
    }
    notifications = AsyncMock()
    with (
        patch(
            "app.services.kyc_service.stripe.Webhook.construct_event",
            return_value=event,
        ),
        patch(
            "app.services.notifications.get_notification_service",
            return_value=notifications,
        ),
    ):
        assert await service.handle_webhook(b"{}", "sig") is True

    wallet = await _wallet_row(sponsor_pending_id)
    assert wallet.kyc_status == "verified"
    assert wallet.status == "active"
    notifications.send_kyc_approved_alert.assert_awaited_once_with(
        wallet_id=sponsor_pending_id
    )


async def test_reject_verification_suspends_wallet(sponsor_pending_id):
    service = KYCService()
    with patch(
        "app.services.kyc_service.stripe.identity.VerificationSession.create",
        return_value=_stripe_session("vs_reject"),
    ):
        await service.create_verification_session(
            sponsor_pending_id, "https://example.com/callback"
        )
    notifications = AsyncMock()
    with patch(
        "app.services.notifications.get_notification_service",
        return_value=notifications,
    ):
        await service.reject_verification(sponsor_pending_id, "blurry photo")

    wallet = await _wallet_row(sponsor_pending_id)
    assert wallet.kyc_status == "rejected"
    assert wallet.status == "suspended"
    status = await service.get_verification_status(sponsor_pending_id)
    assert status["rejection_reason"] == "blurry photo"
    notifications.send_kyc_rejected_alert.assert_awaited_once()


def test_status_message_unknown_status():
    assert KYCService()._get_status_message("bogus", False) == "Unknown KYC status."


def test_get_kyc_service_singleton():
    assert get_kyc_service() is get_kyc_service()
