"""
API Key Management Router

Handles API key creation, rotation, and revocation for wallet security.
"""

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status

from ..core.dependencies import get_agent_money

from ..core.auth import AuthContext, get_auth_context
from ..services.api_key_service import (
    get_api_key_service,
    InvalidRotationRequestError,
    KeyNotFoundError,
    WalletNotFoundError,
)
from .http_idempotency import begin_http_idempotency
from ..schemas.billing import (
    CreateAPIKeyRequest,
    APIKeyResponse,
    APIKeyWithSecret,
    APIKeyListResponse,
    RotateAPIKeyRequest,
    RotationResponse,
    KeyRotationLogEntry,
    EmergencyKeyRevocationRequest,
    RotationType,
)

router = APIRouter(
    prefix="/v1/api-keys",
    tags=["API Key Management"],
    responses={
        401: {"description": "Missing API key"},
        403: {"description": "Insufficient permissions"},
    },
)


def _not_found(error: str, message: str) -> dict:
    """Standardized 404 error payload."""
    return {"error": error, "message": message}


def _invalid_request(error: str, message: str) -> dict:
    """Standardized 422 error payload."""
    return {"error": error, "message": message}


def _refuse_jwt_minter(auth: AuthContext) -> None:
    # API keys cannot carry a JWT's scope set or short lifetime. Even rotating
    # its own originating key would turn delegated access into wider authority.
    if auth.source == "jwt":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "jwt_cannot_mint_api_key",
                "message": "Present an authorized API key to create replacement credentials.",
            },
        )


async def _refuse_bounded_minter(auth: AuthContext) -> None:
    """Refuse to mint a fresh key for a caller whose own key is bounded.

    POST /v1/api-keys and rotate without key_id issue a key carrying only
    the bounds the request names, and both are reachable with the wallet's
    own key (or a JWT derived from it). A key with a use budget or an expiry
    could otherwise mint an unlimited, never-expiring sibling and outlive its
    own limits. Rotating the bounded key itself (key_id set) stays allowed:
    that path carries its remaining bounds over. Bootstrap admins and
    unbounded wallet keys are unaffected.
    """
    _refuse_jwt_minter(auth)
    if auth.is_bootstrap_admin:
        return
    if not await get_api_key_service().is_key_bounded(auth.key_id):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "error": "bounded_key_cannot_mint",
            "message": (
                "This API key has a use budget or an expiry, so it cannot "
                "mint new keys. Rotate it with key_id and revoke_old to carry "
                "its remaining bounds over, or ask a bootstrap admin for a key "
                "with fresh bounds."
            ),
        },
    )


async def _require_wallet_exists(wallet_id: str) -> None:
    """404 before an idempotency record is opened for an unknown wallet.

    ``idempotency_records.wallet_id`` references ``wallets``; beginning a
    record for a missing wallet would fail the foreign key and answer 500
    where the unkeyed path answers 404. Same body as the service's own error.
    """
    if await get_agent_money().get_wallet(wallet_id) is None:
        raise HTTPException(
            status_code=404,
            detail=_not_found("wallet_not_found", str(WalletNotFoundError(wallet_id))),
        )


def _redact_secret(body: dict) -> dict:
    """Stored replay copy of an issuance response, without the plaintext key.

    Keys are persisted hashed by design, so the idempotency record must not
    carry the secret either. A replay therefore proves the first call minted
    the key (same ``key_id``/prefix, no duplicate live key) but returns
    ``api_key: null``; a caller that lost the secret rotates or revokes it.
    """
    redacted = dict(body)
    if "api_key" in redacted:
        redacted["api_key"] = None
    new_key = redacted.get("new_key")
    if isinstance(new_key, dict) and "api_key" in new_key:
        redacted["new_key"] = {**new_key, "api_key": None}
    return redacted


@router.post(
    "",
    response_model=APIKeyWithSecret,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new API key",
    description=(
        "Create a new API key for a wallet. The key is only shown once "
        "- store it securely."
    ),
)
async def create_api_key(
    request: CreateAPIKeyRequest,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Create a new API key for a wallet.

    Returns the full API key which is only shown once.
    Store it securely - it cannot be retrieved later.

    Opt-in ``Idempotency-Key``: a retried create with the same key replays
    the first key's metadata (``api_key`` is ``null`` on replay; the secret
    is never stored) instead of minting a second live key.
    """
    auth.require_wallet_access(request.wallet_id)
    await _refuse_bounded_minter(auth)
    await _require_wallet_exists(request.wallet_id)
    guard, replay = await begin_http_idempotency(
        idempotency_key=idempotency_key,
        wallet_id=request.wallet_id,
        endpoint="/v1/api-keys",
        request_payload=request.model_dump(mode="json"),
    )
    if replay is not None:
        return replay
    service = get_api_key_service()

    try:
        result = await service.create_key(
            wallet_id=request.wallet_id,
            key_name=request.key_name,
            expires_in_days=request.expires_in_days,
            max_uses=request.max_uses,
        )
        response = APIKeyWithSecret(
            key_id=result["key_id"],
            wallet_id=result["wallet_id"],
            api_key=result["api_key"],
            key_prefix=result["key_prefix"],
            status=result["status"],
            key_name=result["key_name"],
            created_at=result["created_at"],
            expires_at=result["expires_at"],
            max_uses=result["max_uses"],
        )
    except WalletNotFoundError as e:
        not_found = _not_found("wallet_not_found", str(e))
        await guard.complete({"detail": not_found}, 404)
        raise HTTPException(status_code=404, detail=not_found)
    await guard.complete(
        _redact_secret(response.model_dump(mode="json")),
        201,
        response_reference=response.key_id,
    )
    return response


@router.get(
    "/{wallet_id}",
    response_model=APIKeyListResponse,
    summary="List API keys for a wallet",
    description="Get all API keys for a wallet. Keys are masked for security.",
)
async def list_api_keys(
    wallet_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    """List all API keys for a wallet."""
    auth.require_wallet_access(wallet_id)
    service = get_api_key_service()

    try:
        result = await service.get_keys(wallet_id)
        return APIKeyListResponse(
            wallet_id=result["wallet_id"],
            keys=[APIKeyResponse(**k) for k in result["keys"]],
            total_active=result["total_active"],
            total_revoked=result["total_revoked"],
        )
    except WalletNotFoundError as e:
        raise HTTPException(
            status_code=404,
            detail=_not_found("wallet_not_found", str(e)),
        )


@router.post(
    "/rotate",
    response_model=RotationResponse,
    summary="Rotate an API key",
    description=(
        "Rotate an API key by creating a new one and optionally revoking the old one. "
        "Returns the new key which is only shown once."
    ),
)
async def rotate_api_key(
    request: RotateAPIKeyRequest,
    http_request: Request,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Rotate an API key.

    Creates a new key and optionally revokes the old one.

    Opt-in ``Idempotency-Key``: a retried rotation with the same key replays
    the first rotation (``new_key.api_key`` is ``null`` on replay) instead of
    minting another key or failing on the already-revoked ``key_id``.
    """
    auth.require_wallet_access(request.wallet_id)
    _refuse_jwt_minter(auth)
    if request.key_id is None or request.key_id != auth.key_id:
        # Without key_id this is a plain create with no bounds to inherit.
        # With another key's id, the new key inherits THAT key's bounds, so a
        # bounded caller could adopt an unbounded sibling's authority (with
        # or without revoke_old). A bounded caller may rotate only itself.
        await _refuse_bounded_minter(auth)
    await _require_wallet_exists(request.wallet_id)
    guard, replay = await begin_http_idempotency(
        idempotency_key=idempotency_key,
        wallet_id=request.wallet_id,
        endpoint="/v1/api-keys/rotate",
        request_payload=request.model_dump(mode="json"),
    )
    if replay is not None:
        return replay
    service = get_api_key_service()

    client_ip = http_request.client.host if http_request.client else None

    try:
        result = await service.rotate_key(
            wallet_id=request.wallet_id,
            key_id=request.key_id,
            revoke_old=request.revoke_old,
            reason=request.reason,
            triggered_by="user",
            ip_address=client_ip,
        )

        new_key = None
        if result["new_key"]:
            new_key = APIKeyWithSecret(
                key_id=result["new_key"]["key_id"],
                wallet_id=result["new_key"]["wallet_id"],
                api_key=result["new_key"]["api_key"],
                key_prefix=result["new_key"]["key_prefix"],
                status=result["new_key"]["status"],
                key_name=result["new_key"]["key_name"],
                created_at=result["new_key"]["created_at"],
                expires_at=result["new_key"]["expires_at"],
                max_uses=result["new_key"]["max_uses"],
            )

        response = RotationResponse(
            rotation_id=result["rotation_id"],
            wallet_id=result["wallet_id"],
            old_key_id=result["old_key_id"],
            new_key=new_key,
            rotation_type=RotationType(result["rotation_type"]),
            revoked_keys=result["revoked_keys"],
            created_at=result["created_at"],
        )
    except WalletNotFoundError as e:
        not_found = _not_found("wallet_not_found", str(e))
        await guard.complete({"detail": not_found}, 404)
        raise HTTPException(status_code=404, detail=not_found)
    except KeyNotFoundError as e:
        not_found = _not_found("key_not_found", str(e))
        await guard.complete({"detail": not_found}, 404)
        raise HTTPException(status_code=404, detail=not_found)
    except InvalidRotationRequestError as e:
        invalid = _invalid_request("invalid_rotation_request", str(e))
        await guard.complete({"detail": invalid}, 422)
        raise HTTPException(status_code=422, detail=invalid)
    await guard.complete(
        _redact_secret(response.model_dump(mode="json")),
        200,
        response_reference=response.rotation_id,
    )
    return response


@router.delete(
    "/{wallet_id}/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an API key",
    description="Immediately revoke an API key. This action cannot be undone.",
)
async def revoke_api_key(
    wallet_id: str,
    key_id: str,
    reason: str = "user_request",
    auth: AuthContext = Depends(get_auth_context),
):
    """Revoke an API key immediately."""
    auth.require_wallet_access(wallet_id)
    service = get_api_key_service()

    try:
        await service.revoke_key(wallet_id, key_id, reason)
    except WalletNotFoundError as e:
        raise HTTPException(
            status_code=404,
            detail=_not_found("wallet_not_found", str(e)),
        )
    except KeyNotFoundError as e:
        raise HTTPException(
            status_code=404,
            detail=_not_found("key_not_found", str(e)),
        )


@router.post(
    "/emergency-revoke",
    summary="Emergency key revocation",
    description=(
        "Immediately revoke ALL API keys for a wallet. Use for security incidents."
    ),
)
async def emergency_revoke(
    request: EmergencyKeyRevocationRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Emergency revocation - revoke all keys for a wallet.

    Use this when a wallet may be compromised.
    Optionally creates a new emergency key.
    """
    auth.require_wallet_access(request.wallet_id)
    if request.create_new_key:
        _refuse_jwt_minter(auth)
    service = get_api_key_service()

    try:
        result = await service.emergency_revocation(
            wallet_id=request.wallet_id,
            reason=request.reason,
            create_new_key=request.create_new_key,
            # A wallet-scoped caller's replacement is bounded by its own key,
            # never by a looser sibling it never held. Bootstrap admins keep
            # the wallet-wide donor rule.
            bounding_key_id=(None if auth.is_bootstrap_admin else (auth.key_id or "")),
        )

        new_key = None
        if result["new_key"]:
            new_key = APIKeyWithSecret(
                key_id=result["new_key"]["key_id"],
                wallet_id=result["new_key"]["wallet_id"],
                api_key=result["new_key"]["api_key"],
                key_prefix=result["new_key"]["key_prefix"],
                status=result["new_key"]["status"],
                key_name=result["new_key"]["key_name"],
                created_at=result["new_key"]["created_at"],
                expires_at=result["new_key"]["expires_at"],
                max_uses=result["new_key"]["max_uses"],
            )

        return {
            "wallet_id": result["wallet_id"],
            "revoked_keys": result["revoked_keys"],
            "new_key": new_key,
            "created_at": result["created_at"].isoformat(),
        }
    except WalletNotFoundError as e:
        raise HTTPException(
            status_code=404,
            detail=_not_found("wallet_not_found", str(e)),
        )


@router.get(
    "/{wallet_id}/logs",
    response_model=list[KeyRotationLogEntry],
    summary="Get rotation audit logs",
    description="Get audit logs for API key rotations on a wallet.",
)
async def get_rotation_logs(
    wallet_id: str,
    limit: int = Query(50, ge=1, le=200),
    auth: AuthContext = Depends(get_auth_context),
):
    """Get rotation audit logs for a wallet."""
    auth.require_wallet_access(wallet_id)
    service = get_api_key_service()

    try:
        logs = await service.get_rotation_logs(wallet_id, limit)
        return [KeyRotationLogEntry(**log) for log in logs]
    except WalletNotFoundError as e:
        raise HTTPException(
            status_code=404,
            detail=_not_found("wallet_not_found", str(e)),
        )
