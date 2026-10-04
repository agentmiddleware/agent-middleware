"""Trust-plane facade: wallet ledger and spend metering.

Re-exports the canonical wallet/ledger engine from
:mod:`app.services.agent_money`.
"""

from __future__ import annotations

from app.services.agent_money import (
    DEFAULT_PRICING,
    AgentMoney,
    InsufficientFundsError,
    KYCVerificationRequiredError,
    WalletNotFoundError,
    get_agent_money,
)

__all__ = [
    "DEFAULT_PRICING",
    "AgentMoney",
    "InsufficientFundsError",
    "KYCVerificationRequiredError",
    "WalletNotFoundError",
    "get_agent_money",
]
