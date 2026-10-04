"""Permit requests: an agent asks, a human approves, the middleware mints.

The invoke-time gate (``app.services.human_approval``) pauses a call under a
permit that already exists. This module covers the step before that: an agent
that has *no* authority asks a human for some, and gets a signed permit only
if a human says yes.

    request -> notify -> approve -> mint -> poll

    POST /v1/permit-requests   agent states scope, budget, and why
                               -> Sentinel pages the human, 202 pending
    GET  /v1/permit-requests/{id}
                               -> agent polls; on approval the permit is
                                  minted from the STORED terms and returned

Three properties carry the security of the flow:

**The human's decision binds to the reviewed terms.** Scopes, tools, budget,
and permit expiry are frozen on the row at request time and hashed into
``request_hash``. Minting reads that row — never the polling request — so
there is no window in which an approved request can be re-aimed.

**Minting happens exactly once.** The ``pending -> minting`` transition is a
conditional UPDATE, so one caller wins even with concurrent pollers or two
workers. The permit id is reserved when the human is paged and used as the
minted permit's primary key, which makes a mint retried after a crash collide
on that key rather than issue a second permit carrying the same authority.

**Expiry is enforced here.** Sentinel has no expired state (a timed-out
approval stays "pending" there forever), so ``expires_at`` is checked locally
before every poll and a decision arriving after the window is not honored.

Fail-closed posture matches the invoke gate: simulated approvals are refused
in production-like environments, real mode without Sentinel configuration
refuses at request time, and a Sentinel outage is retryable rather than
silently minting.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any, cast

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql.elements import ColumnElement

from app.core.config import get_settings, public_api_origin
from app.core.credits import credit_amount_fits_storage
from app.core.runtime_mode import is_simulation
from app.core.time import to_naive_utc, utc_now
from app.core.trust_mode import is_production_like_environment
from app.db.database import get_session_factory
from app.db.models import PermitRequestModel
from app.schemas.trust import (
    PermitCreateRequest,
    PermitRequestResponse,
    PermitResponse,
)
from app.services.approval_card import (
    ApprovalCardView,
    render_email_html,
    render_text_summary,
    safe_approval_url,
)
from app.services.human_approval import (
    HumanApprovalUnavailableError,
    SentinelClient,
    human_approval_available,
    sentinel_client_from_settings,
)
from app.services.permits import PermitError, get_permit_service
from app.services.signing_keys import sha256_hex

logger = logging.getLogger(__name__)

REQUEST_STATUS_PENDING = "pending"
# The single mint claim is held; a crash here is recovered by reclaim below.
REQUEST_STATUS_MINTING = "minting"
REQUEST_STATUS_APPROVED = "approved"
REQUEST_STATUS_REJECTED = "rejected"
REQUEST_STATUS_EXPIRED = "expired"
# Approved by a human, but minting could not produce a permit (e.g. the
# subject wallet no longer covers the budget). Terminal: the agent must ask
# again, and a human must decide again.
REQUEST_STATUS_FAILED = "failed"

TERMINAL_STATUSES = frozenset(
    {
        REQUEST_STATUS_APPROVED,
        REQUEST_STATUS_REJECTED,
        REQUEST_STATUS_EXPIRED,
        REQUEST_STATUS_FAILED,
    }
)

# Sentinel bounds its timeout_seconds to 1..86400.
_TIMEOUT_MIN = 60
_TIMEOUT_MAX = 86400
# A mint claim older than this is assumed to belong to a dead worker and may be
# retaken. Safe because the reserved permit id makes the mint idempotent.
_MINT_RECLAIM_SECONDS = 120


class PermitRequestError(Exception):
    """Terminal request failure; ``reason`` is the client-facing error code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PermitRequestConflictError(Exception):
    """The idempotency key was reused with different requested terms."""

    def __init__(self, reason: str = "permit_request_terms_conflict") -> None:
        super().__init__(reason)
        self.reason = reason


def permit_request_hash(
    *,
    issuer_wallet_id: str,
    subject_wallet_id: str,
    allowed_tools: list[str],
    scopes: list[str],
    max_credits: Decimal,
    permit_expires_at: Any,
    requires_human_approval: bool,
    justification: str,
) -> str:
    """Hash the terms the human reviews, so nothing can be swapped later.

    Uses the signing layer's canonical-JSON hash, so key ordering or number
    formatting cannot be used to forge a match.
    """
    return sha256_hex(
        {
            "issuer_wallet_id": issuer_wallet_id,
            "subject_wallet_id": subject_wallet_id,
            "allowed_tools": sorted(allowed_tools),
            "scopes": sorted(scopes),
            "max_credits": max_credits,
            "permit_expires_at": permit_expires_at,
            "requires_human_approval": requires_human_approval,
            "justification": justification,
        }
    )


def sentinel_request_key(
    request_hash: str, *, subject_wallet_id: str, idempotency_key: str
) -> str:
    """Deterministic Sentinel Idempotency-Key for one permit request.

    Bind the caller's wallet-scoped request identity and reviewed terms. A new
    client key needs a new approval, while retrying after a lost create response
    must still resolve to the original approval even before a row was persisted.
    """
    identity = sha256_hex(
        {
            "subject_wallet_id": subject_wallet_id,
            "idempotency_key": idempotency_key,
            "request_hash": request_hash,
        }
    )
    return f"mw-preq-{identity[:44]}"


class PermitRequestService:
    """Create, decide, and mint permit requests."""

    def __init__(self) -> None:
        self._client: SentinelClient | None = None

    # --- infrastructure -------------------------------------------------

    def _sentinel(self) -> SentinelClient:
        if self._client is None:
            self._client = sentinel_client_from_settings()
        return self._client

    @staticmethod
    def _timeout_seconds() -> int:
        raw = get_settings().PERMIT_REQUEST_TIMEOUT_SECONDS
        return max(_TIMEOUT_MIN, min(int(raw), _TIMEOUT_MAX))

    @staticmethod
    def _approvers() -> list[str]:
        raw = get_settings().SENTINEL_APPROVERS or ""
        return [entry.strip() for entry in raw.split(",") if entry.strip()]

    async def _load(self, request_id: str) -> PermitRequestModel | None:
        factory = get_session_factory()
        async with factory() as session:
            return await session.get(PermitRequestModel, request_id)

    async def _load_by_idempotency(
        self, subject_wallet_id: str, idempotency_key: str
    ) -> PermitRequestModel | None:
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(PermitRequestModel).where(
                    cast(
                        ColumnElement[bool],
                        PermitRequestModel.subject_wallet_id == subject_wallet_id,
                    ),
                    cast(
                        ColumnElement[bool],
                        PermitRequestModel.idempotency_key == idempotency_key,
                    ),
                )
            )
            return result.scalar_one_or_none()

    async def _persist_new(
        self, model: PermitRequestModel
    ) -> tuple[PermitRequestModel, bool]:
        """Insert the request; on a concurrent-insert race return the winner.

        The bool reports whether this caller created the row — only the
        creator sends the approver notification, so a racing retry cannot page
        the human a second time.
        """
        factory = get_session_factory()
        try:
            async with factory() as session:
                session.add(model)
                await session.commit()
                await session.refresh(model)
            return model, True
        except IntegrityError:
            existing = await self._load_by_idempotency(
                model.subject_wallet_id, model.idempotency_key
            )
            if existing is None:  # pragma: no cover - repair path
                raise
            return existing, False

    # --- request creation -----------------------------------------------

    async def create(
        self,
        *,
        issuer_wallet_id: str,
        subject_wallet_id: str,
        subject_key_id: str | None,
        idempotency_key: str,
        allowed_tools: list[str],
        scopes: list[str],
        max_credits: Decimal,
        permit_expires_at: Any,
        justification: str,
        requires_human_approval: bool = False,
    ) -> PermitRequestModel:
        """Record the request and page a human for the decision."""
        simulated = is_simulation("human_approval")

        available, reason = human_approval_available()
        if not available:
            # No way to reach a human: refuse rather than bank a request that
            # can never be decided.
            raise PermitRequestError(reason or "human_approval_not_configured")

        expires_at = to_naive_utc(permit_expires_at)
        now = utc_now()
        if expires_at <= now:
            raise PermitRequestError("permit_expired_at_request")
        if max_credits.is_finite() and max_credits <= Decimal("0"):
            raise PermitRequestError("max_credits_must_be_positive")
        if not credit_amount_fits_storage(max_credits):
            raise PermitRequestError("max_credits_not_storable")

        effective_scopes = scopes or [f"tool:{tool}:invoke" for tool in allowed_tools]
        if "billing:charge" not in effective_scopes:
            effective_scopes = [*effective_scopes, "billing:charge"]

        req_hash = permit_request_hash(
            issuer_wallet_id=issuer_wallet_id,
            subject_wallet_id=subject_wallet_id,
            allowed_tools=allowed_tools,
            scopes=effective_scopes,
            max_credits=max_credits,
            permit_expires_at=expires_at,
            requires_human_approval=requires_human_approval,
            justification=justification,
        )

        existing = await self._load_by_idempotency(subject_wallet_id, idempotency_key)
        if existing is not None:
            # Same key, different ask — the human reviewed the first one.
            if existing.request_hash != req_hash:
                raise PermitRequestConflictError()
            return await self.advance(existing)

        model = PermitRequestModel(
            request_id=f"preq-{uuid.uuid4().hex[:16]}",
            issuer_wallet_id=issuer_wallet_id,
            subject_wallet_id=subject_wallet_id,
            subject_key_id=subject_key_id,
            idempotency_key=idempotency_key,
            scopes_json=json.dumps(effective_scopes),
            allowed_tools_json=json.dumps(allowed_tools),
            max_credits=max_credits,
            permit_expires_at=expires_at,
            requires_human_approval=requires_human_approval,
            justification=justification,
            request_hash=req_hash,
            original_request_hash=req_hash,
            status=REQUEST_STATUS_PENDING,
            simulated=simulated,
            reserved_permit_id=f"permit-{uuid.uuid4().hex[:16]}",
            requested_at=now,
            expires_at=now + timedelta(seconds=self._timeout_seconds()),
        )

        if simulated:
            # Local/dev only — human_approval_available() already refused this
            # combination in a production-like environment.
            model.decided_by = "simulation"
            model.reason = "simulated_auto_approval"
            model.decided_at = now
            stored, _created = await self._persist_new(model)
            return await self._decide(stored, REQUEST_STATUS_APPROVED)

        await self._create_remote(model)
        stored, created = await self._persist_new(model)
        if created:
            await self._notify_approvers(stored)
        return stored

    async def _create_remote(self, model: PermitRequestModel) -> None:
        """Page the human via Sentinel; annotate the row with what came back."""
        settings = get_settings()
        try:
            payload = await self._sentinel().create_approval(
                function_name="permit_request",
                arguments={
                    "request_id": model.request_id,
                    "agent_wallet_id": model.subject_wallet_id,
                    "issuer_wallet_id": model.issuer_wallet_id,
                    "allowed_tools": json.loads(model.allowed_tools_json),
                    "scopes": json.loads(model.scopes_json),
                    "max_credits": str(model.max_credits),
                    "permit_expires_at": model.permit_expires_at.isoformat(),
                    "requires_human_approval": model.requires_human_approval,
                    "justification": model.justification,
                    "middleware": settings.PUBLIC_URL or "agent-middleware-api",
                },
                risk_level=settings.SENTINEL_RISK_LEVEL or "high",
                approvers=self._approvers(),
                timeout_seconds=self._timeout_seconds(),
                idempotency_key=sentinel_request_key(
                    model.request_hash,
                    subject_wallet_id=model.subject_wallet_id,
                    idempotency_key=model.idempotency_key,
                ),
            )
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if 400 <= status_code < 500:
                logger.error(
                    "sentinel_permit_request_rejected status=%s body=%s",
                    status_code,
                    exc.response.text[:500],
                )
                raise PermitRequestError("human_approval_request_rejected") from exc
            raise HumanApprovalUnavailableError() from exc
        except httpx.HTTPError as exc:
            raise HumanApprovalUnavailableError() from exc

        action_id = payload.get("action_id") or payload.get("id")
        if not action_id:
            logger.error("sentinel_permit_request_malformed: keys=%s", sorted(payload))
            raise HumanApprovalUnavailableError()
        model.sentinel_action_id = str(action_id)
        url = payload.get("approval_url") or payload.get("url")
        if isinstance(url, str):
            # The link is rendered as the approver's primary action, so the
            # Sentinel response does not get to pick its scheme. The URL is
            # not logged: it carries the magic-link token.
            model.approval_url = safe_approval_url(url[:512])
            if model.approval_url is None:
                logger.warning(
                    "sentinel_approval_url_rejected request_id=%s",
                    model.request_id,
                )

    async def _notify_approvers(self, model: PermitRequestModel) -> None:
        """Email the approval card. Best effort — Sentinel is the decision channel."""
        recipients = [
            entry.split(":", 1)[1] if entry.lower().startswith("mailto:") else entry
            for entry in self._approvers()
        ]
        recipients = [
            entry for entry in recipients if "@" in entry and ":" not in entry
        ]
        if not recipients:
            return

        view = card_view(model)
        html = render_email_html(view)
        text = render_text_summary(view)
        subject = (
            f"[Permit request] {model.subject_wallet_id} asks for "
            f"{model.max_credits} credits"
        )
        from app.services.notifications import get_notification_service

        service = get_notification_service()
        for recipient in recipients:
            try:
                await service.send_email(
                    to=recipient, subject=subject, body=text, html=html
                )
            except Exception as exc:  # pragma: no cover - notification is advisory
                logger.warning("permit_request_email_failed to=%s: %s", recipient, exc)

    # --- decision + mint --------------------------------------------------

    async def advance(self, model: PermitRequestModel) -> PermitRequestModel:
        """Move a request as far toward a terminal state as it can go now.

        Called on every poll: enforces local expiry, reads the Sentinel
        decision, and mints on approval. Terminal requests are returned
        untouched.
        """
        if model.status == REQUEST_STATUS_APPROVED and model.permit_id:
            return model
        if model.status in TERMINAL_STATUSES:
            return model

        if model.simulated and is_production_like_environment(
            get_settings().ENVIRONMENT
        ):
            # A request banked in dev must not mint authority once the
            # environment is production-like — same rule the invoke gate
            # applies to a simulated approval on reload.
            raise PermitRequestError("human_approval_not_configured")

        if model.status == REQUEST_STATUS_MINTING:
            return await self._resume_mint(model)

        if utc_now() >= model.expires_at:
            # Sentinel keeps a timed-out approval "pending" forever, so the
            # window is enforced here — before spending a poll on it.
            return await self._decide(
                model, REQUEST_STATUS_EXPIRED, reason="approval_window_elapsed"
            )

        if model.simulated:  # pragma: no cover - simulated rows decide at create
            return model

        if not model.sentinel_action_id:
            raise HumanApprovalUnavailableError()

        try:
            payload = await self._sentinel().get_approval(model.sentinel_action_id)
        except httpx.HTTPError as exc:
            raise HumanApprovalUnavailableError() from exc

        if utc_now() >= model.expires_at:
            return await self._decide(
                model, REQUEST_STATUS_EXPIRED, reason="approval_window_elapsed"
            )

        decision = payload.get("status") or payload.get("decision") or ""
        if decision not in {REQUEST_STATUS_APPROVED, REQUEST_STATUS_REJECTED}:
            return model

        model.decided_by = payload.get("decided_by")
        model.reason = payload.get("reason")
        model.decided_at = utc_now()
        return await self._decide(model, decision)

    async def _decide(
        self,
        model: PermitRequestModel,
        decision: str,
        *,
        reason: str | None = None,
    ) -> PermitRequestModel:
        """Apply a decision to a still-pending request, then mint if approved.

        An approval moves the row straight to ``minting``: that conditional
        UPDATE is the single serialization point, so concurrent pollers cannot
        both mint. A caller that loses the race reloads the winner's row.
        """
        now = utc_now()
        if decision == REQUEST_STATUS_APPROVED and now >= model.expires_at:
            decision = REQUEST_STATUS_EXPIRED
            reason = "approval_window_elapsed"
        target = (
            REQUEST_STATUS_MINTING if decision == REQUEST_STATUS_APPROVED else decision
        )
        decided_at = (
            now if target == REQUEST_STATUS_EXPIRED else model.decided_at or now
        )
        values: dict[str, Any] = {
            "status": target,
            "decided_at": decided_at,
            "decided_by": model.decided_by,
            "reason": reason if reason is not None else model.reason,
        }
        if target == REQUEST_STATUS_MINTING:
            values["mint_started_at"] = now

        conditions = [
            cast(
                ColumnElement[bool], PermitRequestModel.request_id == model.request_id
            ),
            cast(
                ColumnElement[bool], PermitRequestModel.status == REQUEST_STATUS_PENDING
            ),
        ]
        if target == REQUEST_STATUS_MINTING:
            conditions.append(
                cast(ColumnElement[bool], PermitRequestModel.expires_at > now)
            )

        factory = get_session_factory()
        overdue_claim = False
        async with factory() as session:
            result = await session.execute(
                update(PermitRequestModel)
                .where(*conditions)
                .values(**values)
                .returning(cast(ColumnElement[Any], PermitRequestModel.expires_at))
            )
            claimed_deadline = result.scalar_one_or_none()
            won = claimed_deadline is not None
            # The UPDATE can wait behind another transaction after evaluating
            # the pre-wait timestamp. Do not commit a provisional mint claim
            # that completed after its persisted deadline.
            overdue_claim = (
                target == REQUEST_STATUS_MINTING
                and claimed_deadline is not None
                and utc_now() >= claimed_deadline
            )
            if overdue_claim:
                await session.rollback()
            else:
                await session.commit()

        if overdue_claim:
            return await self._decide(
                model, REQUEST_STATUS_EXPIRED, reason="approval_window_elapsed"
            )

        reloaded = await self._load(model.request_id)
        if reloaded is None:  # pragma: no cover - row cannot vanish
            return model
        if not won:
            # Someone else decided this request first; report their outcome.
            return await self.advance(reloaded)
        if reloaded.status != REQUEST_STATUS_MINTING:
            return reloaded
        return await self._mint(reloaded)

    async def _resume_mint(self, model: PermitRequestModel) -> PermitRequestModel:
        """Take over a mint claim abandoned by a crashed worker.

        Retaking is safe only because the permit id was reserved up front: if
        the dead worker did mint, the retry adopts that permit instead of
        issuing a second one.
        """
        started = model.mint_started_at
        if started is not None and utc_now() - started < timedelta(
            seconds=_MINT_RECLAIM_SECONDS
        ):
            return model  # a live worker holds the claim

        now = utc_now()
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                update(PermitRequestModel)
                .where(
                    cast(
                        ColumnElement[bool],
                        PermitRequestModel.request_id == model.request_id,
                    ),
                    cast(
                        ColumnElement[bool],
                        PermitRequestModel.status == REQUEST_STATUS_MINTING,
                    ),
                    cast(
                        ColumnElement[bool],
                        PermitRequestModel.mint_started_at == started,
                    ),
                )
                .values(mint_started_at=now)
            )
            await session.commit()
            if cast(Any, result).rowcount != 1:
                reloaded = await self._load(model.request_id)
                return reloaded or model

        model.mint_started_at = now
        logger.warning("permit_request_mint_reclaimed request_id=%s", model.request_id)
        return await self._mint(model)

    async def _mint(self, model: PermitRequestModel) -> PermitRequestModel:
        """Issue the permit from the stored terms, under the reserved id."""
        permits = get_permit_service()

        existing = await permits.get_permit(model.reserved_permit_id)
        if existing is not None:
            # A previous attempt already minted it; adopt rather than reissue.
            return await self._settle_minted(model, model.reserved_permit_id)

        scopes = json.loads(model.scopes_json)
        allowed_tools = json.loads(model.allowed_tools_json)

        # First integrity check: did the stored hash survive unchanged?
        if model.request_hash != model.original_request_hash:
            logger.error(
                "permit_request_hash_anchor_mismatch request_id=%s original=%s current=%s",
                model.request_id,
                model.original_request_hash,
                model.request_hash,
            )
            return await self._settle_failed(
                model, "permit_request_hash_anchor_violation"
            )

        # Second integrity check: do the terms match the hash?
        verified_hash = permit_request_hash(
            issuer_wallet_id=model.issuer_wallet_id,
            subject_wallet_id=model.subject_wallet_id,
            allowed_tools=allowed_tools,
            scopes=scopes,
            max_credits=model.max_credits,
            permit_expires_at=model.permit_expires_at,
            requires_human_approval=model.requires_human_approval,
            justification=model.justification,
        )
        if verified_hash != model.request_hash:
            logger.error(
                "permit_request_terms_tampered request_id=%s stored_hash=%s computed_hash=%s",
                model.request_id,
                model.request_hash,
                verified_hash,
            )
            return await self._settle_failed(
                model, "permit_request_terms_integrity_violation"
            )

        request = PermitCreateRequest(
            issuer_wallet_id=model.issuer_wallet_id,
            subject_wallet_id=model.subject_wallet_id,
            subject_key_id=model.subject_key_id,
            scopes=scopes,
            allowed_tools=allowed_tools,
            max_credits=model.max_credits,
            expires_at=model.permit_expires_at,
            requires_human_approval=model.requires_human_approval,
        )
        try:
            await permits.create_permit(
                request,
                subject_key_id=model.subject_key_id,
                permit_id=model.reserved_permit_id,
            )
        except PermitError as exc:
            # The human approved terms the world no longer supports (expired
            # window, wallet drained). Terminal: asking again needs a new
            # human decision, which is the honest outcome.
            logger.warning(
                "permit_request_mint_failed request_id=%s reason=%s",
                model.request_id,
                exc.reason,
            )
            return await self._settle_failed(model, exc.reason)
        except IntegrityError:
            # Lost a mint race on the reserved id — the other side's permit is
            # this request's permit.
            return await self._settle_minted(model, model.reserved_permit_id)

        return await self._settle_minted(model, model.reserved_permit_id)

    async def _settle(
        self, request_id: str, values: dict[str, Any]
    ) -> PermitRequestModel:
        factory = get_session_factory()
        async with factory() as session:
            await session.execute(
                update(PermitRequestModel)
                .where(
                    cast(
                        ColumnElement[bool],
                        PermitRequestModel.request_id == request_id,
                    ),
                    cast(
                        ColumnElement[bool],
                        PermitRequestModel.status == REQUEST_STATUS_MINTING,
                    ),
                )
                .values(**values)
            )
            await session.commit()
        reloaded = await self._load(request_id)
        if reloaded is None:  # pragma: no cover - row cannot vanish
            raise PermitRequestError("permit_request_not_found")
        return reloaded

    async def _settle_minted(
        self, model: PermitRequestModel, permit_id: str
    ) -> PermitRequestModel:
        return await self._settle(
            model.request_id,
            {
                "status": REQUEST_STATUS_APPROVED,
                "permit_id": permit_id,
                "mint_started_at": None,
            },
        )

    async def _settle_failed(
        self, model: PermitRequestModel, reason: str
    ) -> PermitRequestModel:
        return await self._settle(
            model.request_id,
            {
                "status": REQUEST_STATUS_FAILED,
                "reason": reason,
                "mint_started_at": None,
            },
        )

    # --- read paths -------------------------------------------------------

    async def list_requests(
        self,
        *,
        subject_wallet_id: str,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[PermitRequestModel], int]:
        """A subject wallet's requests, newest first.

        Read-only: it never advances a request, so listing cannot page a human
        or mint a permit as a side effect of looking.
        """
        factory = get_session_factory()
        async with factory() as session:
            filters = [
                cast(
                    ColumnElement[bool],
                    PermitRequestModel.subject_wallet_id == subject_wallet_id,
                )
            ]
            if status:
                filters.append(
                    cast(ColumnElement[bool], PermitRequestModel.status == status)
                )
            total = (
                await session.execute(
                    select(func.count()).select_from(PermitRequestModel).where(*filters)
                )
            ).scalar_one()
            rows = (
                (
                    await session.execute(
                        select(PermitRequestModel)
                        .where(*filters)
                        .order_by(cast(Any, PermitRequestModel.requested_at).desc())
                        .limit(limit)
                        .offset(offset)
                    )
                )
                .scalars()
                .all()
            )
        return list(rows), int(total)

    async def get(self, request_id: str) -> PermitRequestModel | None:
        return await self._load(request_id)

    async def poll(self, request_id: str) -> PermitRequestModel | None:
        model = await self._load(request_id)
        if model is None:
            return None
        return await self.advance(model)

    async def minted_permit(self, model: PermitRequestModel) -> PermitResponse | None:
        if not model.permit_id:
            return None
        return await get_permit_service().get_permit(model.permit_id)


def poll_url(request_id: str) -> str:
    return f"{public_api_origin()}/v1/permit-requests/{request_id}"


async def request_to_response(model: PermitRequestModel) -> PermitRequestResponse:
    """Project a stored request into the API shape, permit included once minted.

    Shared by the request endpoint and the wallet's own view so both report a
    request identically — the poll response and the list are the same record.
    """
    return PermitRequestResponse(
        request_id=model.request_id,
        status=model.status,  # type: ignore[arg-type]
        issuer_wallet_id=model.issuer_wallet_id,
        subject_wallet_id=model.subject_wallet_id,
        subject_key_id=model.subject_key_id,
        allowed_tools=json.loads(model.allowed_tools_json),
        scopes=json.loads(model.scopes_json),
        max_credits=model.max_credits,
        permit_expires_at=model.permit_expires_at,
        requires_human_approval=model.requires_human_approval,
        justification=model.justification,
        requested_at=model.requested_at,
        expires_at=model.expires_at,
        decided_at=model.decided_at,
        decided_by=model.decided_by,
        reason=model.reason,
        simulated=model.simulated,
        permit_id=model.permit_id,
        permit=await get_permit_request_service().minted_permit(model),
        poll_url=poll_url(model.request_id),
    )


def card_view(model: PermitRequestModel) -> ApprovalCardView:
    """Project a stored request into the shared approver card."""
    return ApprovalCardView(
        request_id=model.request_id,
        subject_wallet_id=model.subject_wallet_id,
        issuer_wallet_id=model.issuer_wallet_id,
        allowed_tools=json.loads(model.allowed_tools_json),
        scopes=json.loads(model.scopes_json),
        max_credits=model.max_credits,
        permit_expires_at=model.permit_expires_at,
        justification=model.justification,
        requested_at=model.requested_at,
        decide_by=model.expires_at,
        status=model.status,
        requires_human_approval=model.requires_human_approval,
        simulated=model.simulated,
        approval_url=model.approval_url,
        decided_by=model.decided_by,
        decided_at=model.decided_at,
        permit_id=model.permit_id,
        reason=model.reason,
    )


_service: PermitRequestService | None = None


def get_permit_request_service() -> PermitRequestService:
    global _service
    if _service is None:
        _service = PermitRequestService()
    return _service
