from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.auth import AuthContext, get_auth_context
from app.core.time import utc_now
from app.schemas.audit import AuditEventListResponse, AuditEventResponse
from app.schemas.billing import AlertListResponse
from app.schemas.trust import (
    AuthoritySummaryResponse,
    PermitListResponse,
    PermitRequestListResponse,
    QuoteListResponse,
    ReceiptListResponse,
)
from app.trust import (
    count_audit_events,
    get_agent_money,
    get_permit_request_service,
    get_permit_service,
    get_quote_service,
    get_receipt_service,
    list_audit_events,
    list_policy_bundles,
    request_to_response,
)

router = APIRouter(prefix="/v1/me", tags=["Agent Self Inspection"])


def _require_wallet_key(auth: AuthContext) -> tuple[str, str | None]:
    if auth.is_bootstrap_admin or not auth.wallet_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "wallet_key_required",
                "message": "This endpoint requires a DB-backed wallet API key.",
            },
        )
    return auth.wallet_id, auth.key_id


@router.get("/alerts", response_model=AlertListResponse)
async def list_my_alerts(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    auth: AuthContext = Depends(get_auth_context),
) -> AlertListResponse:
    wallet_id, _ = _require_wallet_key(auth)
    money = get_agent_money()
    alerts = await money.get_alerts(wallet_id)
    # Apply pagination
    total = len(alerts)
    paginated = alerts[offset : offset + limit]
    unacknowledged = sum(1 for a in alerts if not a.acknowledged)
    return AlertListResponse(
        alerts=paginated,
        total=total,
        unacknowledged=unacknowledged,
    )


@router.get(
    "/authority",
    response_model=AuthoritySummaryResponse,
    responses={
        401: {"description": "Missing or invalid API key"},
        403: {"description": "Key is not wallet-scoped (wallet_key_required)"},
        404: {"description": "Key's wallet no longer exists (wallet_not_found)"},
    },
)
async def get_my_authority(
    auth: AuthContext = Depends(get_auth_context),
) -> AuthoritySummaryResponse:
    """What authority does this key currently hold?

    One read answering the questions a planning agent asks before acting:
    what may I do (policies, active permits), what will pause for a human
    (human_approval_required), what have I already asked for (pending permit
    requests), and what can I spend (balance, daily spend). Read-only and
    scoped to the caller's own wallet; listing never advances a decision or
    pages a human.
    """
    wallet_id, key_id = _require_wallet_key(auth)
    money = get_agent_money()
    wallet = await money.get_wallet(wallet_id)
    if wallet is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "wallet_not_found"},
        )
    balance = (
        Decimal(wallet.balance_exact)
        if wallet.balance_exact
        else Decimal(str(wallet.balance))
    )
    policies = [p for p in await list_policy_bundles(wallet_id) if p.is_active]
    # A permit past expires_at keeps status="active" in storage (expiry is
    # enforced dynamically at invoke time), so bound the read to unexpired
    # rows — this summary must never present authority every invoke rejects.
    permits, permits_total = await get_permit_service().list_permits(
        wallet_id=wallet_id,
        status="active",
        subject_key_id=key_id,
        expires_after=utc_now(),
        limit=50,
        offset=0,
    )
    pending_requests, requests_total = await get_permit_request_service().list_requests(
        subject_wallet_id=wallet_id,
        status="pending",
        limit=50,
        offset=0,
    )
    return AuthoritySummaryResponse(
        wallet_id=wallet_id,
        balance=balance,
        daily_spend_used=await money.get_daily_spend(wallet_id),
        human_approval_required=any(p.human_approval_required for p in policies),
        policies=policies,
        active_permits=permits,
        active_permits_total=permits_total,
        pending_permit_requests=[
            await request_to_response(model) for model in pending_requests
        ],
        pending_permit_requests_total=requests_total,
    )


@router.get("/permits", response_model=PermitListResponse)
async def list_my_permits(
    status: str | None = Query(None),
    created_after: datetime | None = Query(None),
    created_before: datetime | None = Query(None),
    expires_after: datetime | None = Query(None),
    expires_before: datetime | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    auth: AuthContext = Depends(get_auth_context),
) -> PermitListResponse:
    wallet_id, key_id = _require_wallet_key(auth)
    permits, total = await get_permit_service().list_permits(
        wallet_id=wallet_id,
        status=status,
        subject_key_id=key_id,
        created_after=created_after,
        created_before=created_before,
        expires_after=expires_after,
        expires_before=expires_before,
        limit=limit,
        offset=offset,
    )
    next_offset = offset + len(permits) if offset + len(permits) < total else None
    return PermitListResponse(
        permits=permits,
        total=total,
        limit=limit,
        offset=offset,
        has_more=next_offset is not None,
        next_offset=next_offset,
    )


@router.get("/permit-requests", response_model=PermitRequestListResponse)
async def list_my_permit_requests(
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    auth: AuthContext = Depends(get_auth_context),
) -> PermitRequestListResponse:
    """Authority this wallet has asked a human for.

    Read-only: unlike polling one request by id, listing never advances a
    decision, so an agent can survey what is outstanding without paging anyone
    or minting anything.
    """
    wallet_id, _ = _require_wallet_key(auth)
    requests, total = await get_permit_request_service().list_requests(
        subject_wallet_id=wallet_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    next_offset = offset + len(requests) if offset + len(requests) < total else None
    return PermitRequestListResponse(
        requests=[await request_to_response(model) for model in requests],
        total=total,
        limit=limit,
        offset=offset,
        has_more=next_offset is not None,
        next_offset=next_offset,
    )


@router.get("/quotes", response_model=QuoteListResponse)
async def list_my_quotes(
    status: str | None = Query(None),
    tool: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    auth: AuthContext = Depends(get_auth_context),
) -> QuoteListResponse:
    """Price commitments this wallet holds — filter `status=active` for spendable."""
    wallet_id, _ = _require_wallet_key(auth)
    quotes, total = await get_quote_service().list_quotes(
        wallet_id=wallet_id,
        status=status,
        tool=tool,
        limit=limit,
        offset=offset,
    )
    next_offset = offset + len(quotes) if offset + len(quotes) < total else None
    return QuoteListResponse(
        quotes=quotes,
        total=total,
        limit=limit,
        offset=offset,
        has_more=next_offset is not None,
        next_offset=next_offset,
    )


@router.get("/receipts", response_model=ReceiptListResponse)
async def list_my_receipts(
    permit_id: str | None = Query(None),
    tool: str | None = Query(None),
    outcome: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    auth: AuthContext = Depends(get_auth_context),
) -> ReceiptListResponse:
    wallet_id, _ = _require_wallet_key(auth)
    receipts, total = await get_receipt_service().list_receipts(
        permit_id=permit_id,
        wallet_id=wallet_id,
        tool=tool,
        outcome=outcome,
        limit=limit,
        offset=offset,
    )
    next_offset = offset + len(receipts) if offset + len(receipts) < total else None
    return ReceiptListResponse(
        receipts=receipts,
        total=total,
        limit=limit,
        offset=offset,
        has_more=next_offset is not None,
        next_offset=next_offset,
    )


@router.get("/audit/events", response_model=AuditEventListResponse)
async def list_my_audit_events(
    event: str | None = Query(None),
    tool: str | None = Query(None),
    endpoint: str | None = Query(None),
    policy_decision_id: str | None = Query(None),
    request_id: str | None = Query(None),
    ok: bool | None = Query(None),
    created_after: datetime | None = Query(None),
    created_before: datetime | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    auth: AuthContext = Depends(get_auth_context),
) -> AuditEventListResponse:
    wallet_id, key_id = _require_wallet_key(auth)
    events = await list_audit_events(
        event=event,
        wallet_id=wallet_id,
        key_id=key_id,
        tool=tool,
        endpoint=endpoint,
        policy_decision_id=policy_decision_id,
        request_id=request_id,
        ok=ok,
        created_after=created_after,
        created_before=created_before,
        limit=limit,
        offset=offset,
    )
    total = await count_audit_events(
        event=event,
        wallet_id=wallet_id,
        key_id=key_id,
        tool=tool,
        endpoint=endpoint,
        policy_decision_id=policy_decision_id,
        request_id=request_id,
        ok=ok,
        created_after=created_after,
        created_before=created_before,
    )
    next_offset = offset + len(events) if offset + len(events) < total else None
    return AuditEventListResponse(
        events=[AuditEventResponse(**asdict(event)) for event in events],
        total=total,
        limit=limit,
        offset=offset,
        has_more=next_offset is not None,
        next_offset=next_offset,
    )
