"""Shared authority for wallet spending and velocity-freeze transitions."""

from ..schemas.billing import WalletStatus

# Unknown/future control states fail closed until explicitly admitted here.
# Use this same allowlist in Python prechecks and atomic SQL write guards.
SPENDABLE_WALLET_STATUSES = frozenset(
    {WalletStatus.ACTIVE.value, WalletStatus.PENDING_KYC.value}
)
