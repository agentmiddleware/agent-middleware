"""A KYC session must not widen what a wallet is allowed to do.

``create_verification_session`` moves a wallet to ``pending_kyc``, and
``pending_kyc`` is a *spendable* status -- it is explicitly admitted by the
allowlist the debit paths check. Writing it unconditionally therefore let an
ordinary API call undo three separate controls: the velocity anomaly auto-freeze, an
operator suspension, and wallet closure. No race was required.

The wallet is also read *before* the Stripe network call and written after, so
the same write clobbered anything committed while that call was in flight.

These tests pin both halves of the rule: KYC may claim a status that is already
spendable, or a suspension KYC itself imposed, and nothing else.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.database import get_engine, get_session_factory
from app.db.models import WalletModel
from app.services.kyc_service import KYCService

pytestmark = pytest.mark.anyio


def _stripe_session() -> MagicMock:
    """A stand-in for the Stripe Identity session object."""
    fake = MagicMock()
    fake.id = f"vs_{uuid.uuid4().hex[:12]}"
    fake.url = "https://verify.stripe.test/session"
    return fake


async def _seed_wallet(status: str, kyc_status: str = "pending") -> str:
    """Persist a sponsor wallet in an exact status, returning its id."""
    wallet_id = f"spn-kyc-{uuid.uuid4().hex[:12]}"
    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            session.add(
                WalletModel(
                    wallet_id=wallet_id,
                    wallet_type="sponsor",
                    owner_name="KYC authority probe",
                    email=f"{wallet_id}@example.com",
                    balance=Decimal("100"),
                    status=status,
                    kyc_status=kyc_status,
                )
            )
    return wallet_id


async def _status(wallet_id: str) -> str:
    factory = get_session_factory()
    async with factory() as session:
        wallet = await session.get(WalletModel, wallet_id)
        assert wallet is not None
        return wallet.status


async def _start_kyc(wallet_id: str) -> None:
    with patch(
        "stripe.identity.VerificationSession.create",
        return_value=_stripe_session(),
    ):
        await KYCService().create_verification_session(
            wallet_id, "https://example.test/return"
        )


@pytest_asyncio.fixture(autouse=True)
async def _clean(clean_database):
    """Every case starts from an empty wallets table."""
    yield


@pytest.mark.parametrize(
    ("status", "kyc_status"),
    [
        ("frozen", "pending"),
        ("suspended", "pending"),
        ("closed", "pending"),
        ("operator_hold", "pending"),
        ("operator_hold", "rejected"),
        ("operator_hold", "expired"),
    ],
)
async def test_kyc_session_does_not_lift_a_control_it_does_not_own(
    status: str, kyc_status: str
) -> None:
    """A frozen, operator-suspended, closed, or unknown control stays in force.

    The freeze is a critical case: it is the anomaly control that
    fires on a concurrent spend burst, and lifting it by starting KYC would
    hand the burst its wallet back.
    """
    wallet_id = await _seed_wallet(status, kyc_status)

    await _start_kyc(wallet_id)

    assert await _status(wallet_id) == status


@pytest.mark.parametrize("kyc_status", ["rejected", "expired"])
async def test_kyc_session_may_reclaim_a_suspension_it_imposed(
    kyc_status: str,
) -> None:
    """Re-verifying after a rejection or expiry must stay reachable.

    ``handle_rejected`` and ``handle_expired`` suspend the wallet themselves,
    so this suspension IS KYC's to lift -- and the success path only
    reactivates from ``pending_kyc``. Refusing the transition here would strand
    every rejected sponsor in a state they could never verify out of.
    """
    wallet_id = await _seed_wallet("suspended", kyc_status)

    await _start_kyc(wallet_id)

    assert await _status(wallet_id) == "pending_kyc"


@pytest.mark.parametrize("status", ["active", "pending_kyc"])
async def test_kyc_session_claims_a_spendable_wallet_normally(status: str) -> None:
    """The ordinary path is unchanged."""
    wallet_id = await _seed_wallet(status, "pending")

    await _start_kyc(wallet_id)

    assert await _status(wallet_id) == "pending_kyc"


@pytest.mark.parametrize("status", ["frozen", "operator_hold"])
async def test_a_control_status_landing_during_the_stripe_call_survives(
    status: str,
) -> None:
    """A control committed while Stripe is being called must not be clobbered.

    The wallet is read before the network call and written after, so this is
    the window the guarded write closes: the status is decided by the row at
    write time, not by the copy this request read.
    """
    wallet_id = await _seed_wallet("active", "pending")
    app_engine = get_engine()
    assert app_engine is not None
    database_url = app_engine.url

    async def _set_control_status() -> None:
        # The blocking portal owns another event loop. Its independent writer
        # must not borrow asyncpg connections created on the application loop.
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                async with session.begin():
                    wallet = await session.get(WalletModel, wallet_id)
                    assert wallet is not None
                    wallet.status = status
        finally:
            await engine.dispose()

    controlled_session = _stripe_session()

    class _SetControlOnCreate:
        """Commit the control status at the moment Stripe would be answering."""

        def __call__(self, *args: object, **kwargs: object) -> MagicMock:
            import anyio.from_thread

            with anyio.from_thread.start_blocking_portal() as portal:
                portal.call(_set_control_status)
            return controlled_session

    with patch(
        "stripe.identity.VerificationSession.create", side_effect=_SetControlOnCreate()
    ):
        await KYCService().create_verification_session(
            wallet_id, "https://example.test/return"
        )

    assert await _status(wallet_id) == status
