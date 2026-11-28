from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.core.auth import AuthContext, get_auth_context
from app.schemas.trust import SigningKeyResponse
from app.trust import SigningKeyError, get_signing_key_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/signing-keys", tags=["Trust Signing Keys"])


def _key_response(key) -> SigningKeyResponse:
    return SigningKeyResponse(
        key_id=key.key_id,
        alg=key.alg,
        public_key_b64=key.public_key_b64,
        status=key.status,
        created_at=key.created_at,
        activated_at=key.activated_at,
        retired_at=key.retired_at,
    )


@router.get("/active", response_model=SigningKeyResponse)
async def get_active_signing_key(
    auth: AuthContext = Depends(get_auth_context),
) -> SigningKeyResponse:
    """Return public metadata for the active transaction-evidence signing key.

    A replica whose configured key cannot sign (retired, disabled, bound to
    other key material) answers 503 ``signing_key_unavailable``, the same
    reason the governed paths give, rather than an unclassified 500. The
    underlying reason stays in the server log.
    """

    try:
        key = await get_signing_key_service().get_active_key()
    except SigningKeyError as exc:
        logger.warning("signing_key_active_unavailable reason=%s", exc)
        raise HTTPException(
            status_code=503, detail={"error": "signing_key_unavailable"}
        ) from exc
    return _key_response(key)


@router.get("/{key_id}", response_model=SigningKeyResponse)
async def get_signing_key(
    key_id: str,
    auth: AuthContext = Depends(get_auth_context),
) -> SigningKeyResponse:
    """Return public metadata for an active or retired signing key."""

    key = await get_signing_key_service().get_public_key(key_id)
    if not key:
        raise HTTPException(status_code=404, detail="signing_key_not_found")
    return _key_response(key)
