"""Single walk over the sponsor -> agent -> child wallet hierarchy.

``parent_wallet_id`` is written once, when a wallet is created, and is never
updated afterwards. Both creators check the caller owns the parent first
(``create_agent_wallet`` requires the sponsor, ``create_child_wallet`` requires
the parent; a bootstrap admin may name any parent). The walk therefore reads a
chain nobody can re-point after the fact.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import WalletModel

MAX_WALLET_HIERARCHY_DEPTH = 32


@dataclass(frozen=True)
class WalletAncestry:
    """Ancestors of a wallet, nearest first, and whether the chain is sound.

    ``complete`` is True only when the walk reached a wallet with no parent.
    A cycle, a parent id with no row, a missing starting wallet or a chain
    deeper than the cap leaves it False.
    """

    ancestors: tuple[str, ...]
    complete: bool


async def walk_wallet_ancestry(
    session: AsyncSession,
    wallet_id: str,
    *,
    max_depth: int = MAX_WALLET_HIERARCHY_DEPTH,
) -> WalletAncestry:
    ancestors: list[str] = []
    seen: set[str] = set()
    current = wallet_id
    for _ in range(max_depth):
        if current in seen:
            return WalletAncestry(tuple(ancestors), False)
        seen.add(current)
        wallet = await session.get(WalletModel, current)
        if wallet is None:
            return WalletAncestry(tuple(ancestors), False)
        if not wallet.parent_wallet_id:
            return WalletAncestry(tuple(ancestors), True)
        ancestors.append(wallet.parent_wallet_id)
        current = wallet.parent_wallet_id
    return WalletAncestry(tuple(ancestors), False)


async def wallet_is_or_descends_from(
    session: AsyncSession,
    wallet_id: str,
    ancestor_wallet_id: str,
    *,
    max_depth: int = MAX_WALLET_HIERARCHY_DEPTH,
) -> bool:
    """True if ``wallet_id`` is ``ancestor_wallet_id`` or below it.

    This is the permit-creation rule: it stops at the first match, so a
    malformed chain above the match does not change the answer and a direct
    child costs one lookup. It reads the chain one wallet at a time instead of
    through ``walk_wallet_ancestry``, which loads the whole chain for the
    strict rule below; keep the two apart so this hot path stays cheap and the
    strict rule stays untouched.
    """
    if wallet_id == ancestor_wallet_id:
        return True
    current = wallet_id
    seen: set[str] = set()
    for _ in range(max_depth):
        if current in seen:
            return False
        seen.add(current)
        wallet = await session.get(WalletModel, current)
        if wallet is None or not wallet.parent_wallet_id:
            return False
        if wallet.parent_wallet_id == ancestor_wallet_id:
            return True
        current = wallet.parent_wallet_id
    return False


async def wallet_strictly_governs(
    session: AsyncSession,
    ancestor_wallet_id: str,
    wallet_id: str,
    *,
    max_depth: int = MAX_WALLET_HIERARCHY_DEPTH,
) -> bool:
    """True only if ``ancestor_wallet_id`` is a strict ancestor of ``wallet_id``.

    Fails closed: equal ids, the reverse direction, siblings, unrelated
    wallets, a missing wallet, a cycle, a dangling parent and an over-deep
    chain all return False. Unlike the permit-creation rule, the whole chain
    must be sound, so a tampered chain grants nothing even below the match.
    """
    if not ancestor_wallet_id or not wallet_id or ancestor_wallet_id == wallet_id:
        return False
    ancestry = await walk_wallet_ancestry(session, wallet_id, max_depth=max_depth)
    return ancestry.complete and ancestor_wallet_id in ancestry.ancestors
