from __future__ import annotations

from dataclasses import asdict
from datetime import datetime

from fastapi import APIRouter, Depends, Query

from app.core.auth import AuthContext, get_auth_context
from app.schemas.audit import (
    AuditEventListResponse,
    AuditEventResponse,
    AuditSummaryResponse,
)
from app.schemas.trust import AuditChainVerifyRequest, AuditChainVerifyResponse
from app.trust import (
    count_audit_events,
    count_audit_events_grouped,
    list_audit_events,
    summarize_audit_events,
    verify_audit_chain,
)

router = APIRouter(prefix="/v1/audit", tags=["Control Plane Audit"])


def _authorize_audit_events_request(
    *,
    auth: AuthContext,
    wallet_id: str | None,
    key_id: str | None,
    summary: bool,
) -> tuple[str | None, str | None]:
    if auth.is_bootstrap_admin:
        return wallet_id, key_id

    # A wallet key reads its own events, including the summarized form: the
    # aggregate is computed over the same scoped rows, so summarizing reveals
    # nothing the unsummarized list would not. Only a caller with no wallet at
    # all (an unscoped, cross-tenant read) still needs an operator key.
    effective_wallet_id = wallet_id or auth.wallet_id
    if not effective_wallet_id:
        auth.require_bootstrap_admin()

    assert effective_wallet_id is not None
    auth.require_wallet_access(effective_wallet_id)
    return effective_wallet_id, None


@router.get("/events", response_model=AuditEventListResponse)
async def get_audit_events(
    event: str | None = Query(None),
    wallet_id: str | None = Query(None),
    key_id: str | None = Query(None),
    tool: str | None = Query(None),
    endpoint: str | None = Query(None),
    policy_decision_id: str | None = Query(None),
    request_id: str | None = Query(None),
    ok: bool | None = Query(None),
    created_after: datetime | None = Query(None),
    created_before: datetime | None = Query(None),
    summary: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    auth: AuthContext = Depends(get_auth_context),
) -> AuditEventListResponse:
    wallet_id, key_id = _authorize_audit_events_request(
        auth=auth,
        wallet_id=wallet_id,
        key_id=key_id,
        summary=summary,
    )
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
    response_summary = None
    if summary:
        # Counted in SQL over every matching event, not tallied from a capped
        # row read, so ok + failed always equals total.
        buckets = await count_audit_events_grouped(
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
        by_event: dict[str, int] = {}
        ok_count = 0
        failed_count = 0
        for bucket in buckets:
            by_event[bucket.event] = by_event.get(bucket.event, 0) + bucket.count
            if bucket.ok:
                ok_count += bucket.count
            else:
                failed_count += bucket.count
        response_summary = {
            "total": total,
            "ok": ok_count,
            "failed": failed_count,
            "by_event": by_event,
        }
    return AuditEventListResponse(
        events=[AuditEventResponse(**asdict(event)) for event in events],
        total=total,
        limit=limit,
        offset=offset,
        has_more=next_offset is not None,
        next_offset=next_offset,
        summary=response_summary,
    )


@router.get("/summary", response_model=AuditSummaryResponse)
async def get_audit_summary(
    created_after: datetime | None = Query(None),
    created_before: datetime | None = Query(None),
    auth: AuthContext = Depends(get_auth_context),
) -> AuditSummaryResponse:
    # Cross-tenant totals stay an operator view; a wallet key gets the same
    # shape computed over its own events only.
    wallet_id = None
    if not auth.is_bootstrap_admin:
        if not auth.wallet_id:
            auth.require_bootstrap_admin()
        wallet_id = auth.wallet_id
    summary = await summarize_audit_events(
        created_after=created_after,
        created_before=created_before,
        wallet_id=wallet_id,
    )
    return AuditSummaryResponse(**summary)


@router.post("/verify-chain", response_model=AuditChainVerifyResponse)
async def verify_chain(
    request: AuditChainVerifyRequest,
    auth: AuthContext = Depends(get_auth_context),
) -> AuditChainVerifyResponse:
    wallet_id = request.wallet_id
    if wallet_id:
        auth.require_wallet_access(wallet_id)
    elif auth.wallet_id:
        # Verifying your own chain is the point of publishing it; requiring an
        # operator key to do so made the tamper-evidence claim unusable by the
        # tenant it protects.
        wallet_id = auth.wallet_id
    else:
        auth.require_bootstrap_admin()
    result = await verify_audit_chain(
        wallet_id=wallet_id,
        created_after=request.created_after,
        created_before=request.created_before,
    )
    return AuditChainVerifyResponse(**asdict(result))
