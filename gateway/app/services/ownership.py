"""Caller identity and ownership enforcement for money and permit services.

Pattern 3 fix. Routes used to be the only place that checked whether the
caller owned the wallet or permit a mutation touched; the services trusted
whatever id they were handed. Every service function in this module's scope
now takes a :class:`CallerIdentity` and enforces ownership itself, so a
route (or a future caller) that forgets its check is denied at the row
instead of spending someone else's money.

The identity is a plain value: wallet id, key id, and whether the caller is
a bootstrap admin. Engine paths that already authorized the wallet another
way (permit validation, an admitted governed context, a reconciled attempt
row) pass :meth:`CallerIdentity.for_wallet` with that proven wallet id, so
the service still verifies the row belongs to it. There is no silent
system bypass: ``is_bootstrap_admin`` is the only exemption, and only real
bootstrap credentials construct it (see ``from_auth``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class OwnershipDeniedError(RuntimeError):
    """The caller owns neither the wallet nor the permit a mutation touches.

    A plain ``RuntimeError`` on purpose: it must not be caught by the
    ``except ValueError`` handlers around money writes, which map bad input
    to 400. Routers map this to 403.
    """

    def __init__(self, reason: str = "owner_access_denied") -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class CallerIdentity:
    """Who is asking a money or permit service to mutate a row."""

    wallet_id: str | None = None
    key_id: str | None = None
    is_bootstrap_admin: bool = False

    @classmethod
    def from_auth(cls, auth: Any) -> CallerIdentity:
        """Build the service-layer identity from a route's ``AuthContext``."""
        return cls(
            wallet_id=auth.wallet_id,
            key_id=auth.key_id,
            is_bootstrap_admin=bool(auth.is_bootstrap_admin),
        )

    @classmethod
    def for_wallet(
        cls, wallet_id: str | None, key_id: str | None = None
    ) -> CallerIdentity:
        """Identity for an engine path whose wallet was already authorized.

        The wallet id must come from proof the engine holds (a validated
        permit, an admitted governed context, a reconciled attempt row),
        never from an unverified request field.
        """
        return cls(wallet_id=wallet_id, key_id=key_id)

    @classmethod
    def admin(cls) -> CallerIdentity:
        """Bootstrap operator identity. Routes never build this from input."""
        return cls(is_bootstrap_admin=True)


def require_wallet_owner(caller: CallerIdentity, wallet_id: str | None) -> None:
    """Allow only the owning wallet (or a bootstrap admin) to mutate it."""
    if caller.is_bootstrap_admin:
        return
    if (
        caller.wallet_id is not None
        and wallet_id is not None
        and caller.wallet_id == wallet_id
    ):
        return
    raise OwnershipDeniedError("wallet_access_denied")


def require_permit_party(
    caller: CallerIdentity,
    *,
    issuer_wallet_id: str | None,
    subject_wallet_id: str | None,
    error: type[RuntimeError] = OwnershipDeniedError,
    reason: str = "permit_access_denied",
) -> None:
    """Allow only a wallet the permit belongs to (issuer or subject)."""
    if caller.is_bootstrap_admin:
        return
    parties = {issuer_wallet_id, subject_wallet_id} - {None}
    if caller.wallet_id is not None and caller.wallet_id in parties:
        return
    raise error(reason)


def require_permit_issuer(
    caller: CallerIdentity,
    *,
    issuer_wallet_id: str | None,
    error: type[RuntimeError] = OwnershipDeniedError,
    reason: str = "permit_access_denied",
) -> None:
    """Allow only the wallet that issued the permit (revocation owns authority)."""
    if caller.is_bootstrap_admin:
        return
    if (
        caller.wallet_id is not None
        and issuer_wallet_id is not None
        and caller.wallet_id == issuer_wallet_id
    ):
        return
    raise error(reason)
