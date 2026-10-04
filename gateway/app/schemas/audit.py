from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AuditEventResponse(BaseModel):
    event_id: str
    created_at: datetime
    event: str
    wallet_id: str | None
    tool: str | None
    endpoint: str | None
    auth_source: str | None
    key_id: str | None
    policy_decision_id: str | None
    request_id: str | None
    ok: bool
    error: str | None
    metadata: dict[str, Any]
    payload_hash: str | None = None
    previous_hash: str | None = None
    chain_hash: str | None = None
    signature: str | None = None
    signature_key_id: str | None = None


class AuditEventListResponse(BaseModel):
    events: list[AuditEventResponse]
    total: int
    limit: int = 50
    offset: int = 0
    has_more: bool = False
    next_offset: int | None = None
    summary: dict[str, Any] | None = None


class AuditSummaryResponse(BaseModel):
    total: int
    by_event: dict[str, int]
    by_outcome: dict[str, int]
    by_wallet: dict[str, int]
    by_policy_reason: dict[str, int]
    by_policy_reason_truncated: bool = Field(
        default=False,
        description=(
            "True when by_policy_reason was tallied from only the newest "
            "events (a row cap applies to that bucket alone), so its counts "
            "are a lower bound. total, by_event, by_outcome and by_wallet "
            "always cover every matching event."
        ),
    )
