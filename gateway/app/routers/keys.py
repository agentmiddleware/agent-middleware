from __future__ import annotations

"""Transaction-evidence signing-key metadata endpoints.

Trust model: these endpoints publish public verification metadata only
(key id, algorithm, public key, status, timestamps). They never return
private key material. The same keys are published without any credential
at ``/.well-known/trust-keys.json`` (and ``/.well-known/jwks.json``), so
that a party holding no credential here can still verify a receipt
offline. The endpoints below therefore intentionally do not apply wallet
scoping: any authenticated credential may read any key's metadata.
Authentication is still required so anonymous callers learn nothing here
and must use the well-known documents instead.
"""

from fastapi import APIRouter, Depends, HTTPException

from app.core.auth import AuthContext, get_auth_context
from app.schemas.trust import SigningKeyResponse
from app.trust import get_signing_key_service

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

    Public by design: the response carries only verification metadata
    (key id, algorithm, public key, status, timestamps), never private
    material. It is intentionally not wallet scoped, so any authenticated
    credential may read it. The same key is also published without a
    credential at ``/.well-known/trust-keys.json`` for offline verifiers.
    """

    key = await get_signing_key_service().get_active_key()
    return _key_response(key)


@router.get("/{key_id}", response_model=SigningKeyResponse)
async def get_signing_key(
    key_id: str,
    auth: AuthContext = Depends(get_auth_context),
) -> SigningKeyResponse:
    """Return public metadata for an active or retired signing key.

    Public by design: the response carries only verification metadata
    (key id, algorithm, public key, status, timestamps), never private
    material. It is intentionally not wallet scoped, so any authenticated
    credential may read any key's metadata, including keys belonging to
    other wallets' receipts. Retired keys stay readable so historical
    receipts remain verifiable. The same keys are also published without
    a credential at ``/.well-known/trust-keys.json`` for offline verifiers.
    """

    key = await get_signing_key_service().get_public_key(key_id)
    if not key:
        raise HTTPException(status_code=404, detail="signing_key_not_found")
    return _key_response(key)
