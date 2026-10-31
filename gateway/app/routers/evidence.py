from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.core.auth import AuthContext, get_auth_context
from app.schemas.trust import EvidenceBundleResponse
from app.trust import get_receipt_service
from app.trust.evidence import (
    authorize_receipt_access,
    build_evidence_bundle,
    build_receipt_evidence,
)

router = APIRouter(prefix="/v1/evidence", tags=["Trust / Evidence"])


@router.get(
    "/{receipt_id}",
    response_model=EvidenceBundleResponse,
    summary="Buyer-facing trust evidence bundle",
    description=(
        "Returns the single, verifiable artifact for a receipt: the signed "
        "receipt, its permit, the linked ledger entry and audit event, and a "
        "`verification` map (receipt signature, permit signature, audit chain, "
        "request hash). This is the artifact a buyer can hand to an auditor. "
        "The `audit_chain` verification covers the receipt's own audit event "
        "(payload hash, signature, chain hash) and its link to the immediate "
        "predecessor. It does not walk the wallet's whole chain, so its cost "
        "does not grow with history; `verification.audit_chain_scope` names that "
        "scope (`receipt_event_link`) and is absent when no audit event is "
        "linked. Use `POST /v1/audit/verify-chain` to walk the chain in "
        "bounded pages."
    ),
)
async def get_evidence_bundle(
    receipt_id: str,
    auth: AuthContext = Depends(get_auth_context),
) -> EvidenceBundleResponse:
    receipt = await get_receipt_service().get_receipt(receipt_id)
    if not receipt:
        raise HTTPException(status_code=404, detail="receipt_not_found")
    await authorize_receipt_access(auth=auth, receipt=receipt)
    evidence = await build_receipt_evidence(receipt=receipt, auth=auth)
    return build_evidence_bundle(evidence)
