"""Resolve an omitted wallet ID from a wallet-scoped authenticated key."""

from typing import TYPE_CHECKING

from fastapi import HTTPException, status

from .auth import AuthContext

if TYPE_CHECKING:
    from ..services.agent_money import AgentMoney


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


async def require_governing_wallet_access(
    auth: AuthContext, wallet_id: str | None, money: "AgentMoney"
) -> bool:
    """Allow what ``require_wallet_access`` allows, plus a strict ancestor.

    Returns False when exact access passed (a bootstrap admin or the wallet
    itself) and True when the caller was admitted only because its own wallet
    is a strict ancestor of ``wallet_id`` in the sponsor -> agent -> child
    hierarchy. Every other case raises the same 403 ``require_wallet_access``
    raises, so a missing wallet, a foreign wallet, a sibling, a descendant
    reaching up and a malformed chain are indistinguishable to the caller.

    Only a caller with its own wallet can pass by ancestry, so a context that
    fails ``require_wallet_access`` at its own wallet gains nothing here.
    Routes opt in one by one; every other route keeps exact access.
    """
    try:
        auth.require_wallet_access(wallet_id)
        return False
    except HTTPException as denied:
        if (
            auth.wallet_id is not None
            and wallet_id is not None
            and await money.wallet_strictly_governs(auth.wallet_id, wallet_id)
        ):
            return True
        raise denied
