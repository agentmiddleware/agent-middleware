"""
KYC Verification Router
Handles identity verification for sponsor wallets.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from ..core.auth import AuthContext, get_auth_context
from ..services.kyc_service import (
    get_kyc_service,
    KYCNotRequiredError,
    KYCVerificationError,
)
from ..services.agent_money import WalletNotFoundError
from ..schemas.billing import (
    CreateKYCSessionRequest,
    KYCSessionResponse,
    KYCStatusResponse,
    KYCVerificationDetails,
    KYCStatus,
)

router = APIRouter(
    prefix="/v1/kyc",
    tags=["KYC Verification"],
    responses={
        401: {"description": "Missing API key"},
        403: {"description": "KYC verification required"},
    },
)


@router.post(
    "/sessions",
    response_model=KYCSessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create KYC verification session",
    description=(
        "Create a Stripe Identity verification session for a sponsor wallet. "
        "This returns a URL that the user should be redirected to for completing "
        "identity verification. After verification, the wallet's kyc_status will "
        "be updated via webhook.\n\n"
        "Only sponsor wallets require KYC. Agent and child wallets inherit trust."
    ),
)
async def create_kyc_session(
    request: CreateKYCSessionRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Create a Stripe Identity verification session.

    Flow:
    1. Client calls this endpoint with wallet_id and return_url
    2. Server creates Stripe Identity session and returns session_url
    3. Client redirects user to session_url for verification
    4. After completion, Stripe redirects to return_url
    5. Stripe sends webhook with verification result
    """
    auth.require_wallet_access(request.wallet_id)
    kyc_service = get_kyc_service()

    try:
        result = await kyc_service.create_verification_session(
            wallet_id=request.wallet_id,
            return_url=request.return_url,
            document_type=request.document_type,
        )
        return KYCSessionResponse(
            verification_id=result["verification_id"],
            wallet_id=result["wallet_id"],
            session_id=result["session_id"],
            session_url=result["session_url"],
            status=KYCStatus.PENDING,
            created_at=result["created_at"],
            expires_at=result["expires_at"],
        )
    except WalletNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except KYCNotRequiredError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "kyc_not_required", "message": str(e)},
        )
    except KYCVerificationError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "verification_error", "message": str(e)},
        )


@router.get(
    "/status/{wallet_id}",
    response_model=KYCStatusResponse,
    summary="Get KYC status for a wallet",
    description="Get the current KYC verification status for a wallet.",
)
async def get_kyc_status(
    wallet_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Get the current KYC verification status for a wallet.

    Returns:
    - kyc_status: Current verification status
    - requires_verification: Whether KYC is needed for top-ups
    - message: Human-readable status message
    """
    auth.require_wallet_access(wallet_id)
    kyc_service = get_kyc_service()

    try:
        result = await kyc_service.get_verification_status(wallet_id)
        return KYCStatusResponse(
            wallet_id=result["wallet_id"],
            kyc_status=KYCStatus(result["kyc_status"]),
            verification_id=result["verification_id"],
            stripe_session_id=result["stripe_session_id"],
            last_verified_at=result["last_verified_at"],
            rejection_reason=result["rejection_reason"],
            requires_verification=result["requires_verification"],
            message=result["message"],
        )
    except WalletNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get(
    "/verifications/{verification_id}",
    response_model=KYCVerificationDetails,
    summary="Get verification details",
    description="Get detailed information about a specific verification.",
)
async def get_verification_details(
    verification_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    """Get detailed information about a verification session."""
    kyc_service = get_kyc_service()

    result = await kyc_service.get_verification_details(verification_id)

    # A verification is visible only to the wallet it belongs to (or a
    # bootstrap admin). A foreign one must answer exactly like a missing one:
    # the 403 from require_wallet_access confirmed the id exists and echoed
    # the owner's wallet_id in its detail.
    if result is None or not (
        auth.is_bootstrap_admin
        or (auth.wallet_id is not None and auth.wallet_id == result["wallet_id"])
    ):
        raise HTTPException(
            status_code=404,
            detail={
                "error": "verification_not_found",
                "message": f"Verification {verification_id} not found",
            },
        )

    return KYCVerificationDetails(
        verification_id=result["verification_id"],
        wallet_id=result["wallet_id"],
        status=KYCStatus(result["status"]),
        stripe_session_id=result["stripe_session_id"],
        document_type=result["document_type"],
        first_verified_at=result["first_verified_at"],
        last_verified_at=result["last_verified_at"],
        rejection_reason=result["rejection_reason"],
        created_at=result["created_at"],
        updated_at=result["updated_at"],
    )
