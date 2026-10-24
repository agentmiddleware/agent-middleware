"""Resolve an omitted wallet ID from a wallet-scoped authenticated key."""

from fastapi import HTTPException, status

from .auth import AuthContext


def resolve_wallet_id(wallet_id: str | None, auth: AuthContext) -> str:
    resolved = wallet_id if wallet_id is not None else auth.wallet_id
    if resolved is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "missing_wallet_id",
                "message": "Pass wallet_id when using a key without a bound wallet.",
            },
        )
    auth.require_wallet_access(resolved)
    return resolved
