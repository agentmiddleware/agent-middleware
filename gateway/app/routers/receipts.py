from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.auth import AuthContext, get_auth_context
from app.core.config import public_api_origin
from app.schemas.trust import (
    PortableReceiptResponse,
    ReceiptEvidenceResponse,
    ReceiptListResponse,
    ReceiptResponse,
    RefundReconciliationListResponse,
    RefundReconciliationRetryResponse,
    ReceiptVerifyRequest,
    ReceiptVerifyResponse,
)
from app.trust import (
    RECEIPT_CANONICALIZATION,
    RefundReconciliationError,
    get_permit_service,
    get_receipt_service,
    get_refund_reconciliation_service,
    record_audit_event,
)
from app.trust.evidence import authorize_receipt_access, build_receipt_evidence

router = APIRouter(prefix="/v1/receipts", tags=["Trust Receipts"])
logger = logging.getLogger(__name__)


async def _authorize_receipt_list(
    *,
    auth: AuthContext,
    wallet_id: str | None,
    permit_id: str | None,
) -> None:
    if permit_id:
        permit = await get_permit_service().get_permit(permit_id)
        if not permit:
            raise HTTPException(status_code=404, detail="permit_not_found")
        if wallet_id and wallet_id not in {
            permit.issuer_wallet_id,
            permit.subject_wallet_id,
        }:
            auth.require_bootstrap_admin()
            return
        if auth.is_bootstrap_admin:
            return
        if auth.wallet_id in {permit.issuer_wallet_id, permit.subject_wallet_id}:
            return
    if wallet_id:
        auth.require_wallet_access(wallet_id)
        return
    auth.require_bootstrap_admin()


@router.get("", response_model=ReceiptListResponse)
async def list_receipts(
    permit_id: str | None = Query(None),
    wallet_id: str | None = Query(None),
    tool: str | None = Query(None),
    outcome: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    auth: AuthContext = Depends(get_auth_context),
) -> ReceiptListResponse:
    await _authorize_receipt_list(auth=auth, wallet_id=wallet_id, permit_id=permit_id)
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


@router.get("/permit/{permit_id}", response_model=ReceiptListResponse)
async def list_receipts_for_permit(
    permit_id: str,
    wallet_id: str | None = Query(None),
    tool: str | None = Query(None),
    outcome: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    auth: AuthContext = Depends(get_auth_context),
) -> ReceiptListResponse:
    await _authorize_receipt_list(auth=auth, wallet_id=wallet_id, permit_id=permit_id)
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


@router.get(
    "/reconciliation/refunds",
    response_model=RefundReconciliationListResponse,
)
async def list_refund_reconciliations(
    status: Literal["pending", "resolved"] | None = Query("pending"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    auth: AuthContext = Depends(get_auth_context),
) -> RefundReconciliationListResponse:
    # Money owed back to a wallet is that wallet's business. Operators still
    # get the cross-tenant queue; a wallet key sees only its own items. The
    # retry below stays operator-only — it moves money, and this move is about
    # reads.
    wallet_id = None
    if not auth.is_bootstrap_admin:
        if not auth.wallet_id:
            auth.require_bootstrap_admin()
        wallet_id = auth.wallet_id
    try:
        items, total = await get_refund_reconciliation_service().list_items(
            status=status,
            wallet_id=wallet_id,
            limit=limit,
            offset=offset,
        )
    except RefundReconciliationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.reason) from exc
    next_offset = offset + len(items) if offset + len(items) < total else None
    return RefundReconciliationListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        has_more=next_offset is not None,
        next_offset=next_offset,
    )


@router.post(
    "/reconciliation/refunds/{receipt_id}/retry",
    response_model=RefundReconciliationRetryResponse,
)
async def retry_refund_reconciliation(
    receipt_id: str,
    auth: AuthContext = Depends(get_auth_context),
) -> RefundReconciliationRetryResponse:
    auth.require_bootstrap_admin()
    service = get_refund_reconciliation_service()
    item = None
    try:
        item = await service.get_item(receipt_id)
        resolved, replayed = await service.retry(receipt_id)
    except RefundReconciliationError as exc:
        try:
            await record_audit_event(
                event="mcp.refund_reconciliation.failed",
                wallet_id=item.wallet_id if item else None,
                endpoint=f"/v1/receipts/reconciliation/refunds/{receipt_id}/retry",
                auth_source=auth.source,
                key_id=auth.key_id,
                ok=False,
                error=exc.reason,
                metadata={
                    "receipt_id": receipt_id,
                    "ledger_entry_id": item.ledger_entry_id if item else None,
                    "permit_id": item.permit_id if item else None,
                    "status": item.status if item else "invalid",
                },
            )
        except Exception:
            logger.exception(
                "Failed to audit rejected refund reconciliation for %s",
                receipt_id,
            )
        raise HTTPException(status_code=exc.status_code, detail=exc.reason) from exc
    except Exception as exc:
        try:
            await record_audit_event(
                event="mcp.refund_reconciliation.failed",
                wallet_id=item.wallet_id if item else None,
                endpoint=f"/v1/receipts/reconciliation/refunds/{receipt_id}/retry",
                auth_source=auth.source,
                key_id=auth.key_id,
                ok=False,
                error="refund_reconciliation_failed",
                metadata={
                    "receipt_id": receipt_id,
                    "ledger_entry_id": item.ledger_entry_id if item else None,
                    "permit_id": item.permit_id if item else None,
                    "status": item.status if item else "unknown",
                },
            )
        except Exception:
            logger.exception(
                "Failed to audit errored refund reconciliation for %s",
                receipt_id,
            )
        logger.exception("Refund reconciliation failed for %s", receipt_id)
        raise HTTPException(
            status_code=503,
            detail="refund_reconciliation_failed",
        ) from exc

    await record_audit_event(
        event="mcp.refund_reconciliation.resolved",
        wallet_id=resolved.wallet_id,
        endpoint=f"/v1/receipts/reconciliation/refunds/{receipt_id}/retry",
        auth_source=auth.source,
        key_id=auth.key_id,
        ok=True,
        metadata={
            "receipt_id": resolved.receipt_id,
            "ledger_entry_id": resolved.ledger_entry_id,
            "refund_entry_id": resolved.refund_entry_id,
            "permit_id": resolved.permit_id,
            "credits": str(resolved.credits),
            "status": resolved.status,
            "replayed": replayed,
        },
    )
    return RefundReconciliationRetryResponse(item=resolved, replayed=replayed)


@router.get("/{receipt_id}", response_model=ReceiptResponse)
async def get_receipt(
    receipt_id: str,
    auth: AuthContext = Depends(get_auth_context),
) -> ReceiptResponse:
    receipt = await get_receipt_service().get_receipt(receipt_id)
    if not receipt:
        raise HTTPException(status_code=404, detail="receipt_not_found")
    await authorize_receipt_access(auth=auth, receipt=receipt)
    return receipt


@router.get("/{receipt_id}/evidence", response_model=ReceiptEvidenceResponse)
async def get_receipt_evidence(
    receipt_id: str,
    auth: AuthContext = Depends(get_auth_context),
) -> ReceiptEvidenceResponse:
    receipt = await get_receipt_service().get_receipt(receipt_id)
    if not receipt:
        raise HTTPException(status_code=404, detail="receipt_not_found")
    await authorize_receipt_access(auth=auth, receipt=receipt)
    return await build_receipt_evidence(receipt=receipt, auth=auth)


@router.get("/{receipt_id}/portable", response_model=PortableReceiptResponse)
async def get_portable_receipt(
    receipt_id: str,
    auth: AuthContext = Depends(get_auth_context),
) -> PortableReceiptResponse:
    """Export a receipt as evidence that survives leaving this plane.

    Reading a receipt is authorized like any other tenant data. What the
    caller gets back is not: the bundle can be handed to a downstream agent,
    an auditor, or an operator who holds no credential here, and they can
    still check the signature against the published keys.
    """
    receipt = await get_receipt_service().get_receipt(receipt_id)
    if not receipt:
        raise HTTPException(status_code=404, detail="receipt_not_found")
    await authorize_receipt_access(auth=auth, receipt=receipt)

    signing_input = await get_receipt_service().signing_input(receipt_id)
    if signing_input is None:
        # The stored signature does not verify. Refuse rather than hand back a
        # bundle that would fail in the holder's verifier and read as tampering
        # by whoever presented it.
        raise HTTPException(status_code=409, detail="receipt_signature_invalid")

    return PortableReceiptResponse(
        receipt_id=receipt.receipt_id,
        issuer=public_api_origin(),
        kid=receipt.signature_key_id,
        canonicalization=RECEIPT_CANONICALIZATION,
        signing_input=signing_input,
        signature=receipt.signature,
        keys_url="/.well-known/trust-keys.json",
    )


@router.post("/verify", response_model=ReceiptVerifyResponse)
async def verify_receipt(
    request: ReceiptVerifyRequest,
    auth: AuthContext = Depends(get_auth_context),
) -> ReceiptVerifyResponse:
    valid, reason, receipt = await get_receipt_service().verify_receipt(
        request.receipt_id
    )
    if receipt:
        await authorize_receipt_access(auth=auth, receipt=receipt)
    else:
        auth.require_bootstrap_admin()
    return ReceiptVerifyResponse(valid=valid, reason=reason, receipt=receipt)
