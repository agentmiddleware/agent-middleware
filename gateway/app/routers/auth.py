"""JWT authentication endpoints.

Modern token-based auth alongside legacy API keys.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select, update
from sqlmodel import col

from app.core.jwt import JWTError, get_jwt_service
from app.core.time import utc_now
from app.db.database import get_session_factory
from app.db.models import APIKeyModel, RefreshTokenModel
from app.schemas.auth import (
    TokenExchangeRequest,
    TokenExchangeResponse,
    TokenRefreshRequest,
    TokenRefreshResponse,
    TokenRevokeRequest,
)

router = APIRouter(prefix="/v1/auth", tags=["Authentication"])


def _exp_to_datetime(ts: int) -> datetime:
    """Convert JWT exp (int) to timezone-aware datetime."""
    return datetime.fromtimestamp(ts, tz=timezone.utc)


@router.post("/token", response_model=TokenExchangeResponse)
async def exchange_api_key_for_tokens(
    request: TokenExchangeRequest,
) -> TokenExchangeResponse:
    """Exchange a wallet API key for short-lived JWT tokens.

    Returns an access token (15 min) and refresh token (7 days).
    The refresh token can be used at /v1/auth/refresh to obtain
    a new access token without re-presenting the API key.
    """
    from app.services.api_key_service import get_api_key_service

    # Validate the API key
    key_svc = get_api_key_service()
    db_key = await key_svc.validate_key(request.api_key)
    if not db_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_api_key", "message": "API key is not valid."},
        )

    jwt_svc = get_jwt_service()

    # Default scopes: all billing and tool invoke
    scopes = (
        request.scopes
        if request.scopes is not None
        else ["billing:charge", "tool:invoke"]
    )

    access_token = jwt_svc.create_access_token(
        wallet_id=db_key.wallet_id,
        key_id=db_key.key_id,
        scopes=scopes,
    )
    refresh_token = jwt_svc.create_refresh_token(
        wallet_id=db_key.wallet_id, scopes=scopes
    )

    # Store refresh token JTI for revocation
    refresh_payload = jwt_svc.verify_refresh_token(refresh_token)
    factory = get_session_factory()
    async with factory() as session:
        model = RefreshTokenModel(
            jti=refresh_payload.jti,
            wallet_id=db_key.wallet_id,
            # Bind the chain to the key that minted it, so revoking that key
            # invalidates what it produced even when sibling keys stay active.
            key_id=db_key.key_id,
            expires_at=_exp_to_datetime(refresh_payload.exp),
        )
        session.add(model)
        await session.commit()

    return TokenExchangeResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="Bearer",
        expires_in=900,  # 15 minutes
        scope=" ".join(scopes),
    )


@router.post("/refresh", response_model=TokenRefreshResponse)
async def refresh_access_token(
    request: TokenRefreshRequest,
) -> TokenRefreshResponse:
    """Exchange a refresh token for a new access token.

    Refresh token rotation: a new refresh token is issued on each use,
    and the old one is marked as revoked.
    """
    jwt_svc = get_jwt_service()

    try:
        payload = jwt_svc.verify_refresh_token(request.refresh_token)
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_refresh_token", "message": str(e)},
        ) from e

    # Claim the old refresh token: revoke it only if it is still live, in one
    # conditional UPDATE. Reading the row, checking ``revoked`` and writing it
    # back let concurrent presentations of the same token all pass the check
    # and each mint a new chain, so a replayed stolen token survived rotation.
    # The guarded UPDATE matches for exactly one caller; every other one (and
    # an unknown jti) sees rowcount 0 and is refused.
    #
    # The claim, the originating-key liveness check, and the successor insert
    # below all run in ONE transaction with a single commit. Committing the
    # claim early would strand the client when a later step hits a transient
    # DB error: RT1 revoked, no RT2 stored, 500, logged out despite a valid
    # token. Business denials (unbound/dead origin key) still burn the token —
    # the claim is committed before refusing — but an infrastructure failure
    # before the single commit rolls everything back, so the client can retry
    # with the same refresh token.
    from app.services.api_key_service import _key_is_live

    factory = get_session_factory()
    async with factory() as session:
        claimed = await session.execute(
            update(RefreshTokenModel)
            .where(
                col(RefreshTokenModel.jti) == payload.jti,
                col(RefreshTokenModel.revoked).is_(False),
            )
            .values(revoked=True)
            .execution_options(synchronize_session=False)
        )
        if (cast(Any, claimed).rowcount or 0) != 1:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "error": "revoked_refresh_token",
                    "message": "Token has been revoked.",
                },
            )

        # key_id never changes for a row, so reading it after the claim is safe.
        origin_key_id = (
            await session.execute(
                select(col(RefreshTokenModel.key_id)).where(
                    col(RefreshTokenModel.jti) == payload.jti
                )
            )
        ).scalar_one()

        # A JWT is derived authority: it exists only because an API key was
        # presented at /token. Refreshing checked the signature and the revoked
        # flag but never re-checked the underlying credential, so tokens minted
        # from a stolen key kept renewing for the refresh lifetime after that key
        # was revoked — revocation did not contain the compromise.
        #
        # Check the ORIGINATING key, not merely the wallet. Wallet-level liveness is
        # too coarse: a wallet with several keys stays live after the compromised one
        # is revoked, and auto_rotate_on_suspicious_activity revokes the suspect key
        # while issuing a replacement, so the wallet is never keyless and the
        # attacker's chain would survive the rotation meant to contain it. Binding
        # also makes the revoke/refresh race benign — a token that wins the timing
        # window is still bound to the revoked key, so it cannot be renewed.
        #
        # Tokens issued before binding existed carry no key_id. They cannot be
        # safely associated with a still-live credential, so fail closed and force
        # the caller to exchange an API key again. The migration also revokes all
        # such rows, but this guard protects partially migrated/restored databases.
        if origin_key_id is None:
            # Unbound legacy token: burn it, then fail closed.
            await session.commit()
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "error": "unbound_refresh_token",
                    "message": (
                        "This refresh token predates API-key binding and cannot be "
                        "renewed; authenticate with an active API key."
                    ),
                },
            )

        # Liveness of the originating key, read in THIS transaction so a
        # failing key store cannot burn the token silently: without a commit
        # nothing is persisted and the client can retry.
        origin_key = await session.get(APIKeyModel, origin_key_id)
        if origin_key is None or not _key_is_live(origin_key, utc_now()):
            # Dead origin: burn the token, then refuse renewal.
            await session.commit()
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "error": "no_active_api_key",
                    "message": (
                        "The API key this token was issued from is no longer active; "
                        "it cannot be renewed."
                    ),
                },
            )

        # Issue new tokens (pure JWT operations, no I/O) and store the
        # successor in the same transaction, carrying the binding forward so
        # rotation cannot launder a chain into an unbound one. The single
        # commit below persists the claim and the successor together.
        new_access = jwt_svc.create_access_token(
            wallet_id=payload.sub,
            key_id=origin_key_id,
            scopes=payload.scopes,
        )
        new_refresh = jwt_svc.create_refresh_token(
            wallet_id=payload.sub, scopes=payload.scopes
        )
        new_refresh_payload = jwt_svc.verify_refresh_token(new_refresh)
        session.add(
            RefreshTokenModel(
                jti=new_refresh_payload.jti,
                wallet_id=payload.sub,
                key_id=origin_key_id,
                expires_at=_exp_to_datetime(new_refresh_payload.exp),
            )
        )
        await session.commit()

    return TokenRefreshResponse(
        access_token=new_access,
        refresh_token=new_refresh,
        token_type="Bearer",
        expires_in=900,
    )


@router.post("/revoke")
async def revoke_refresh_token(
    request: TokenRevokeRequest,
) -> dict[str, str]:
    """Revoke a refresh token by JTI.

    Prevents the token from being used at /v1/auth/refresh.
    """
    jwt_svc = get_jwt_service()

    try:
        payload = jwt_svc.verify_refresh_token(request.refresh_token)
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_refresh_token", "message": str(e)},
        ) from e

    factory = get_session_factory()
    async with factory() as session:
        record = await session.get(RefreshTokenModel, payload.jti)
        if record:
            record.revoked = True
            await session.commit()

    return {"status": "revoked"}
