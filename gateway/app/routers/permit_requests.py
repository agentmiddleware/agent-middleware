"""Permit requests — the endpoints an agent uses to ask a human for authority.

Two endpoints carry the flow (request -> notify -> approve -> mint -> poll):

    POST /v1/permit-requests        state the ask; a human is paged
    GET  /v1/permit-requests/{id}   poll until the permit is minted

A third read-only surface, ``GET /v1/permit-requests/{id}/card``, renders the
approver's view of the pending decision as HTML — the same card the
notification email carries. It shows the decision; it does not take it.
Approve/reject lives in Sentinel.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from fastapi.responses import HTMLResponse

from app.core.auth import AuthContext, get_auth_context
from app.db.models import PermitRequestModel
from app.schemas.trust import PermitRequestCreate, PermitRequestResponse
from app.trust import (
    AgentMoney,
    HumanApprovalUnavailableError,
    PermitRequestConflictError,
    PermitRequestError,
    REQUEST_STATUS_MINTING,
    REQUEST_STATUS_PENDING,
    card_view,
    get_agent_money,
    get_permit_request_service,
    render_page_html,
    request_to_response,
)

router = APIRouter(prefix="/v1/permit-requests", tags=["Trust Permits"])

# Statuses that mean "keep polling"; everything else is terminal.
_IN_FLIGHT = {REQUEST_STATUS_PENDING, REQUEST_STATUS_MINTING}


def _status_code(model: PermitRequestModel) -> int:
    """202 while the decision is outstanding, 200 once it is settled."""
    return (
        status.HTTP_202_ACCEPTED if model.status in _IN_FLIGHT else status.HTTP_200_OK
    )


def _authorize_inspection(*, auth: AuthContext, model: PermitRequestModel) -> None:
    if auth.is_bootstrap_admin:
        return
    if auth.wallet_id in {model.issuer_wallet_id, model.subject_wallet_id}:
        return
    auth.require_bootstrap_admin()


@router.post(
    "",
    response_model=PermitRequestResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_permit_request(
    request: PermitRequestCreate,
    response: Response,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    auth: AuthContext = Depends(get_auth_context),
    money: AgentMoney = Depends(get_agent_money),
) -> PermitRequestResponse:
    """Ask a human for a permit this agent cannot mint itself.

    The caller must hold the subject wallet — it is asking for authority over
    its own spend. The issuer must be that wallet or one above it in the
    sponsor -> agent -> child hierarchy, mirroring ``POST /v1/permits``: an
    agent may ask its funder for authority, never an unrelated wallet.
    """
    auth.require_wallet_access(request.subject_wallet_id)
    if not auth.is_bootstrap_admin and not await money.is_wallet_or_descendant(
        request.subject_wallet_id, request.issuer_wallet_id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "issuer_wallet_access_denied",
                "message": (
                    "The issuer wallet must be the subject wallet or a wallet "
                    "that funds it."
                ),
            },
        )

    service = get_permit_request_service()
    try:
        model = await service.create(
            issuer_wallet_id=request.issuer_wallet_id,
            subject_wallet_id=request.subject_wallet_id,
            subject_key_id=auth.key_id,
            idempotency_key=idempotency_key,
            allowed_tools=request.allowed_tools,
            scopes=request.scopes,
            max_credits=request.max_credits,
            permit_expires_at=request.expires_at,
            justification=request.justification,
            requires_human_approval=request.requires_human_approval,
        )
    except PermitRequestConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.reason)
    except PermitRequestError as exc:
        raise HTTPException(status_code=400, detail=exc.reason)
    except HumanApprovalUnavailableError as exc:
        # Nothing was recorded and nobody was paged: retrying the same
        # Idempotency-Key once Sentinel recovers is the correct move.
        raise HTTPException(status_code=503, detail=exc.reason)

    response.status_code = _status_code(model)
    return await request_to_response(model)


@router.get("/{request_id}", response_model=PermitRequestResponse)
async def get_permit_request(
    request_id: str,
    response: Response,
    auth: AuthContext = Depends(get_auth_context),
) -> PermitRequestResponse:
    """Poll a request; returns the minted permit once a human has approved."""
    service = get_permit_request_service()
    model = await service.get(request_id)
    if model is None:
        raise HTTPException(status_code=404, detail="permit_request_not_found")
    _authorize_inspection(auth=auth, model=model)

    try:
        model = await service.advance(model)
    except HumanApprovalUnavailableError as exc:
        # The request is intact; only the decision lookup failed.
        raise HTTPException(status_code=503, detail=exc.reason)
    except PermitRequestError as exc:
        raise HTTPException(status_code=400, detail=exc.reason)

    response.status_code = _status_code(model)
    return await request_to_response(model)


@router.get("/{request_id}/card", response_class=HTMLResponse)
async def get_permit_request_card(
    request_id: str,
    auth: AuthContext = Depends(get_auth_context),
) -> HTMLResponse:
    """The approver's card for this request, as a hosted page.

    Same rendering as the notification email, so the two surfaces cannot show
    different terms for one decision. Read-only: the approve/reject action
    lives in Sentinel.
    """
    model = await get_permit_request_service().get(request_id)
    if model is None:
        raise HTTPException(status_code=404, detail="permit_request_not_found")
    _authorize_inspection(auth=auth, model=model)
    return HTMLResponse(
        content=render_page_html(card_view(model)),
        # The card discloses scope, budget, and justification for one wallet.
        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"},
    )
