"""Durable rows for the AMW V2 human approval gate (SEP-2322).

One row per paused irreversible MCP tool call. The sealed ``requestState`` the
client retries with is authenticated by HMAC, but the decision and the
single-use consumption must survive a gateway restart, so they live here
instead of only in memory. See ``app/services/approval_gate.py`` and
migration ``045_approval_gate_approvals``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint
from sqlmodel import Field, SQLModel

from app.core.time import utc_now
from app.db.types import NaiveUTCDateTime

APPROVAL_GATE_STATUS_PENDING = "pending"
APPROVAL_GATE_STATUS_APPROVED = "approved"
APPROVAL_GATE_STATUS_DENIED = "denied"
APPROVAL_GATE_STATUS_EXPIRED = "expired"


class ApprovalGateModel(SQLModel, table=True):
    """One paused tool call awaiting (or holding) a human decision.

    ``nonce`` is the primary key, so it is unique by definition: one row per
    pause, and the atomic consume (``UPDATE ... WHERE status='approved' AND
    consumed_at IS NULL``, rowcount checked) makes the approval single-use
    even across a restart or concurrent retries. Raw arguments are never
    stored, only their canonical hash.
    """

    __tablename__ = "approval_gate_approvals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'denied', 'expired')",
            name="ck_approval_gate_approvals_status",
        ),
    )

    nonce: str = Field(primary_key=True, max_length=64)
    idempotency_key: str = Field(max_length=128, index=True)
    args_hash: str = Field(max_length=128)
    principal: str = Field(max_length=255)
    tool: str = Field(max_length=128)
    session_id: str = Field(max_length=128)
    status: str = Field(default="pending", max_length=16, index=True)
    created_at: datetime = Field(sa_type=NaiveUTCDateTime, default_factory=utc_now)
    deadline: datetime = Field(sa_type=NaiveUTCDateTime)
    decided_at: Optional[datetime] = Field(sa_type=NaiveUTCDateTime, default=None)
    consumed_at: Optional[datetime] = Field(sa_type=NaiveUTCDateTime, default=None)

    model_config = {"arbitrary_types_allowed": True}
