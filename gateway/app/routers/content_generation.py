"""
PROOF SURFACE — frozen. Do not expand; see docs/PROOF_SURFACES.md.

Text generation API backed by Content Factory (durable + LLM).
"""

from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException, status

from ..audit.lightweight import record_audit
from ..core.auth import AuthContext, get_auth_context
from ..core.config import get_settings
from ..core.dependencies import get_content_factory
from ..schemas.content_generation import (
    ContentGenerateRequest,
    ContentGenerateResponse,
    ContentRecordResponse,
)
from ..services.content_factory import ContentFactory
from ..services.content_factory_generation import generate_text

router = APIRouter(
    prefix="/v1/content",
    tags=["Content Factory — Text Generation"],
    responses={
        401: {"description": "Missing API key"},
        403: {"description": "Invalid API key"},
    },
)


def _audit_hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


def _model_allowed(requested: str | None, auth: AuthContext) -> bool:
    """Whether the caller may send ``requested`` to the provider.

    The provider call runs on the operator's ``LLM_API_KEY``, so a
    wallet-scoped caller may only use the configured ``LLM_MODEL`` (or omit
    ``model`` to get it). Naming any other deployment is a bootstrap-admin
    override.
    """
    return (
        not requested
        or auth.is_bootstrap_admin
        or requested == get_settings().LLM_MODEL
    )


def _may_read(auth: AuthContext, owner_wallet_id: str | None) -> bool:
    """Owner or bootstrap admin only; ownerless rows are admin-only."""
    try:
        auth.require_wallet_access(owner_wallet_id)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_403_FORBIDDEN:
            return False
        raise
    return True


@router.post(
    "/generate",
    response_model=ContentGenerateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Generate text via LLM (durable when simulation is off)",
)
async def generate_content_text(
    request: ContentGenerateRequest,
    auth: AuthContext = Depends(get_auth_context),
    factory: ContentFactory = Depends(get_content_factory),
):
    if not _model_allowed(request.model, auth):
        record_audit(
            "content_factory.generate",
            actor_source=auth.source,
            key_id=auth.key_id,
            wallet_id=auth.wallet_id,
            outcome="denied",
            payload_hash=_audit_hash(
                {
                    "error": "model_not_allowed",
                    "model": request.model,
                    "prompt": request.prompt,
                }
            ),
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": "model_not_allowed",
                "message": (
                    "Only the configured default model may be requested with "
                    "this API key; omit 'model' to use it."
                ),
                "allowed_models": [get_settings().LLM_MODEL],
            },
        )

    try:
        # Called on the factory's store directly (not generate_llm_text) so
        # the owning wallet is persisted atomically with the row.
        result = await generate_text(
            store=factory.generation_store,
            prompt=request.prompt,
            model=request.model,
            owner_wallet_id=auth.wallet_id,
        )
    except RuntimeError as exc:
        record_audit(
            "content_factory.generate",
            actor_source=auth.source,
            key_id=auth.key_id,
            wallet_id=auth.wallet_id,
            outcome="error",
            payload_hash=_audit_hash(
                {"error": str(exc), "model": request.model, "prompt": request.prompt}
            ),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "llm_unavailable",
                "message": str(exc),
            },
        ) from exc

    audit_basis = {
        "content_id": result["content_id"],
        "model": result["model"],
        "prompt": request.prompt,
        "requested_model": request.model,
    }
    record_audit(
        "content_factory.generate",
        actor_source=auth.source,
        key_id=auth.key_id,
        wallet_id=auth.wallet_id,
        outcome="ok",
        payload_hash=_audit_hash(audit_basis),
        content_id=result["content_id"],
    )
    return ContentGenerateResponse(
        content_id=result["content_id"],
        text=result["text"],
        model=result["model"],
        prompt_hash=result["prompt_hash"],
        output_hash=result["output_hash"],
        provenance=result["provenance"],
    )


@router.get(
    "/{content_id}",
    response_model=ContentRecordResponse,
    summary="Get persisted generation by id",
)
async def get_content_record(
    content_id: str,
    auth: AuthContext = Depends(get_auth_context),
    factory: ContentFactory = Depends(get_content_factory),
):
    audit_h = _audit_hash({"content_id": content_id})
    row = await factory.get_llm_generation(content_id)
    # A row the caller may not read gets the same 404 as a missing one, so
    # the endpoint is neither an existence oracle nor a source of the owning
    # wallet id. The audit log (operator-only) keeps the distinction.
    if not row or not _may_read(auth, row.get("owner_wallet_id")):
        record_audit(
            "content_factory.get",
            actor_source=auth.source,
            key_id=auth.key_id,
            wallet_id=auth.wallet_id,
            outcome="denied" if row else "not_found",
            payload_hash=audit_h,
            content_id=content_id,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": "Unknown content_id."},
        )

    record_audit(
        "content_factory.get",
        actor_source=auth.source,
        key_id=auth.key_id,
        wallet_id=auth.wallet_id,
        outcome="ok",
        payload_hash=audit_h,
        content_id=content_id,
    )
    ca = row["created_at"]
    ua = row.get("updated_at")
    return ContentRecordResponse(
        content_id=row["content_id"],
        prompt_hash=row["prompt_hash"],
        output_hash=row["output_hash"],
        model=row["model"],
        provenance=row["provenance"],
        text=row["text"],
        created_at=ca.isoformat() if hasattr(ca, "isoformat") else str(ca),
        updated_at=ua.isoformat()
        if ua is not None and hasattr(ua, "isoformat")
        else (str(ua) if ua is not None else None),
    )
