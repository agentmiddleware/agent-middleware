"""Trust-plane facade: human approval gate (Sentinel-backed).

Re-exports the canonical implementation from
:mod:`app.services.human_approval`.
"""

from __future__ import annotations

from app.services.human_approval import (
    APPROVAL_STATUS_APPROVED,
    APPROVAL_STATUS_EXPIRED,
    APPROVAL_STATUS_PENDING,
    APPROVAL_STATUS_REJECTED,
    ApprovalCheck,
    HumanApprovalError,
    HumanApprovalService,
    HumanApprovalUnavailableError,
    SentinelClient,
    approval_window_seconds,
    get_human_approval_service,
    human_approval_available,
    human_approval_configured,
)

__all__ = [
    "APPROVAL_STATUS_APPROVED",
    "APPROVAL_STATUS_EXPIRED",
    "APPROVAL_STATUS_PENDING",
    "APPROVAL_STATUS_REJECTED",
    "ApprovalCheck",
    "HumanApprovalError",
    "HumanApprovalService",
    "HumanApprovalUnavailableError",
    "SentinelClient",
    "approval_window_seconds",
    "get_human_approval_service",
    "human_approval_available",
    "human_approval_configured",
]
