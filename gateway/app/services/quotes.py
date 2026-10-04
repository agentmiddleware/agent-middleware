"""Signed quotes: a price an agent can rely on for the next ten minutes.

Tool prices can move. An agent that budgets against a price it read a moment
ago, or that must clear a human approval before it can spend, has no way to
know what it will actually be charged. A quote closes that gap: it is a signed
statement that *this wallet* calling *this tool* costs *this many credits*
until it expires, and presenting it on the governed invoke charges that number
even if the registered price has changed since.

    POST /v1/quotes            -> signed quote, valid QUOTE_TTL_SECONDS
    tools/call  mcpContext.quote_id
                               -> the charge uses the quoted credits

Two properties make the lock safe to offer:

**Single use.** The quote is consumed by an atomic ``active -> consumed``
UPDATE that also requires ``expires_at > now``, so one quote authorizes one
charge at the quoted price. Without that, a cheap quote would be a standing
right to the old price for the whole window, at any volume.

**Fail loud, not quietly repriced.** A quote that is expired, spent, or
belongs to another wallet or tool denies the invoke rather than silently
charging the live price — the caller asked for a specific number, and
substituting a different one is the one outcome a price lock must never
produce.

The signature is over the same canonical payload shape permits and receipts
use, so a quote is verifiable offline against the published key document
(``/.well-known/trust-keys.json``): an agent can prove what it was promised
without trusting the issuer's read endpoint.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.sql.elements import ColumnElement

from app.core.config import get_settings
from app.core.time import utc_now
from app.db.database import get_session_factory
from app.db.models import QuoteModel
from app.schemas.trust import QuoteResponse
from app.services.pricing import credit_amount_fits_storage
from app.services.signing_keys import get_signing_key_service, sha256_hex

logger = logging.getLogger(__name__)

QUOTE_STATUS_ACTIVE = "active"
QUOTE_STATUS_CONSUMED = "consumed"
QUOTE_STATUS_EXPIRED = "expired"

# Denial reasons surfaced on the invoke path. Each names what the caller can do
# about it: re-quote, use the right wallet, or stop replaying a spent quote.
QUOTE_REASON_NOT_FOUND = "quote_not_found"
QUOTE_REASON_WALLET_MISMATCH = "quote_wallet_mismatch"
QUOTE_REASON_TOOL_MISMATCH = "quote_tool_mismatch"
QUOTE_REASON_EXPIRED = "quote_expired"
QUOTE_REASON_CONSUMED = "quote_already_consumed"

_TTL_MIN = 30
_TTL_MAX = 3600


class QuoteError(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class QuoteValidation:
    """Read-only verdict on whether a quote may price an invoke."""

    allowed: bool
    reason: str | None
    quote: QuoteModel | None

    @property
    def quoted_credits(self) -> Decimal | None:
        return self.quote.quoted_credits if self.quote and self.allowed else None


def quote_model_to_response(model: QuoteModel) -> QuoteResponse:
    return QuoteResponse(
        quote_id=model.quote_id,
        wallet_id=model.wallet_id,
        tool=model.tool,
        quoted_credits=model.quoted_credits,
        category=model.category,
        status=model.status,  # type: ignore[arg-type]
        issued_at=model.issued_at,
        expires_at=model.expires_at,
        consumed_at=model.consumed_at,
        signature=model.signature,
        key_id=model.key_id,
    )


def quote_ttl_seconds() -> int:
    raw = get_settings().QUOTE_TTL_SECONDS
    return max(_TTL_MIN, min(int(raw), _TTL_MAX))


def _signing_payload(model: QuoteModel) -> dict[str, Any]:
    """The signed statement: who, what tool, how much, until when.

    ``status`` is pinned to ``active`` rather than read from the row: the
    signature covers the *commitment*, which does not change when the quote is
    later spent. Consuming a quote must not invalidate the proof of what was
    promised.
    """
    return {
        "quote_id": model.quote_id,
        "wallet_id": model.wallet_id,
        "tool": model.tool,
        "quoted_credits": model.quoted_credits,
        "category": model.category,
        "status": QUOTE_STATUS_ACTIVE,
        "issued_at": model.issued_at,
        "expires_at": model.expires_at,
    }


class QuoteService:
    """Issue, read, validate, and spend signed price commitments."""

    async def create_quote(
        self,
        *,
        wallet_id: str,
        tool: str,
        quoted_credits: Decimal,
        category: str,
    ) -> QuoteResponse:
        if quoted_credits.is_finite() and quoted_credits < Decimal("0"):
            raise QuoteError("quoted_credits_must_not_be_negative")
        if not credit_amount_fits_storage(quoted_credits) or quoted_credits == 0:
            raise QuoteError("quoted_credits_invalid")

        now = utc_now()
        model = QuoteModel(
            quote_id=f"quote-{uuid.uuid4().hex[:16]}",
            wallet_id=wallet_id,
            tool=tool,
            quoted_credits=quoted_credits,
            category=category,
            status=QUOTE_STATUS_ACTIVE,
            issued_at=now,
            expires_at=now + timedelta(seconds=quote_ttl_seconds()),
            signature="",
            key_id="",
        )
        signature, key_id, _ = await get_signing_key_service().sign_payload(
            _signing_payload(model)
        )
        model.signature = signature
        model.key_id = key_id

        factory = get_session_factory()
        async with factory() as session:
            session.add(model)
            await session.commit()
            await session.refresh(model)
        return quote_model_to_response(model)

    async def get_quote(self, quote_id: str) -> QuoteResponse | None:
        model = await self._load(quote_id)
        if model is None:
            return None
        if model.status == QUOTE_STATUS_ACTIVE and utc_now() >= model.expires_at:
            # Report the truth to a reader without needing a sweeper; the
            # authoritative expiry check is in the atomic consume.
            model.status = QUOTE_STATUS_EXPIRED
        return quote_model_to_response(model)

    async def _load(self, quote_id: str) -> QuoteModel | None:
        factory = get_session_factory()
        async with factory() as session:
            return await session.get(QuoteModel, quote_id)

    async def list_quotes(
        self,
        *,
        wallet_id: str,
        status: str | None = None,
        tool: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[QuoteResponse], int]:
        """A wallet's quotes, newest first.

        Reported statuses are honest about the window: an ``active`` row whose
        window has passed reads as ``expired``, matching what the invoke path
        would do with it. Filtering by ``expired`` therefore also matches rows
        still stored as ``active`` but past their window.
        """
        factory = get_session_factory()
        now = utc_now()
        async with factory() as session:
            filters = [cast(ColumnElement[bool], QuoteModel.wallet_id == wallet_id)]
            if tool:
                filters.append(cast(ColumnElement[bool], QuoteModel.tool == tool))
            if status == QUOTE_STATUS_ACTIVE:
                filters.append(
                    cast(ColumnElement[bool], QuoteModel.status == QUOTE_STATUS_ACTIVE)
                )
                filters.append(cast(ColumnElement[bool], QuoteModel.expires_at > now))
            elif status == QUOTE_STATUS_EXPIRED:
                filters.append(
                    cast(
                        ColumnElement[bool],
                        (QuoteModel.status == QUOTE_STATUS_EXPIRED)
                        | (
                            (QuoteModel.status == QUOTE_STATUS_ACTIVE)
                            & (QuoteModel.expires_at <= now)
                        ),
                    )
                )
            elif status:
                filters.append(cast(ColumnElement[bool], QuoteModel.status == status))

            total = (
                await session.execute(
                    select(func.count()).select_from(QuoteModel).where(*filters)
                )
            ).scalar_one()
            rows = (
                (
                    await session.execute(
                        select(QuoteModel)
                        .where(*filters)
                        .order_by(cast(Any, QuoteModel.issued_at).desc())
                        .limit(limit)
                        .offset(offset)
                    )
                )
                .scalars()
                .all()
            )

        quotes = []
        for model in rows:
            if model.status == QUOTE_STATUS_ACTIVE and now >= model.expires_at:
                model.status = QUOTE_STATUS_EXPIRED
            quotes.append(quote_model_to_response(model))
        return quotes, int(total)

    async def validate_for_action(
        self,
        *,
        quote_id: str,
        wallet_id: str,
        tool_name: str,
    ) -> QuoteValidation:
        """Can this quote price this call? Read-only — nothing is spent here.

        Called before the permit check so the permit budget is evaluated
        against the price that will actually be charged.
        """
        model = await self._load(quote_id)
        if model is None:
            return QuoteValidation(False, QUOTE_REASON_NOT_FOUND, None)
        if model.wallet_id != wallet_id:
            # Deliberately not "not found": the caller owns the wallet it named,
            # so naming the mismatch leaks nothing it cannot already see.
            return QuoteValidation(False, QUOTE_REASON_WALLET_MISMATCH, model)
        if model.tool != tool_name:
            return QuoteValidation(False, QUOTE_REASON_TOOL_MISMATCH, model)
        if model.status == QUOTE_STATUS_CONSUMED:
            return QuoteValidation(False, QUOTE_REASON_CONSUMED, model)
        if model.status != QUOTE_STATUS_ACTIVE:
            return QuoteValidation(False, QUOTE_REASON_EXPIRED, model)
        if utc_now() >= model.expires_at:
            return QuoteValidation(False, QUOTE_REASON_EXPIRED, model)
        return QuoteValidation(True, None, model)

    async def consume(
        self, quote_id: str, *, idempotency_key: str | None = None
    ) -> bool:
        """Spend the quote — atomic, single use, expiry-checked.

        The one place a quoted price becomes a charge. ``WHERE status='active'
        AND expires_at > now`` makes this the serialization point for two
        concurrent invokes and for the active-vs-expired boundary at once:
        exactly one caller wins.
        """
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                update(QuoteModel)
                .where(
                    cast(ColumnElement[bool], QuoteModel.quote_id == quote_id),
                    cast(
                        ColumnElement[bool],
                        QuoteModel.status == QUOTE_STATUS_ACTIVE,
                    ),
                    cast(ColumnElement[bool], QuoteModel.expires_at > utc_now()),
                )
                .values(
                    status=QUOTE_STATUS_CONSUMED,
                    consumed_at=utc_now(),
                    consumed_by_idempotency_key=idempotency_key,
                )
            )
            await session.commit()
            return cast(Any, result).rowcount == 1

    async def release(self, quote_id: str) -> bool:
        """Return a consumed quote to active after a charge that never landed.

        Compensation only: the invoke path consumes before charging, so a
        failed charge would otherwise burn a commitment the wallet never spent.
        Guarded on ``consumed`` and on the window still being open, so it can
        neither revive an expired quote nor disturb a live one.
        """
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                update(QuoteModel)
                .where(
                    cast(ColumnElement[bool], QuoteModel.quote_id == quote_id),
                    cast(
                        ColumnElement[bool],
                        QuoteModel.status == QUOTE_STATUS_CONSUMED,
                    ),
                    cast(ColumnElement[bool], QuoteModel.expires_at > utc_now()),
                )
                .values(
                    status=QUOTE_STATUS_ACTIVE,
                    consumed_at=None,
                    consumed_by_idempotency_key=None,
                )
            )
            await session.commit()
            return cast(Any, result).rowcount == 1

    async def verify_signature(self, model: QuoteModel) -> bool:
        payload = _signing_payload(model)
        payload["alg"] = "Ed25519"
        payload["kid"] = model.key_id
        payload["payload_hash"] = sha256_hex(payload)
        return await get_signing_key_service().verify_payload(
            payload,
            signature=model.signature,
            key_id=model.key_id,
        )


_service: QuoteService | None = None


def get_quote_service() -> QuoteService:
    global _service
    if _service is None:
        _service = QuoteService()
    return _service
