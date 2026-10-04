"""
PROOF SURFACE — frozen. Do not expand; see docs/PROOF_SURFACES.md.
Durable agent comms API (Phase 1): DB-backed send + inbox.

``/v1/comms/*`` remains the original surface; this router adds
``/v1/agent-comms/send`` and ``/v1/agent-comms/inbox`` with audit hooks.
"""

from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from ..audit.lightweight import record_audit
from ..core.auth import AuthContext, get_auth_context
from ..core.dependencies import get_agent_comms
from ..core.runtime_mode import is_simulation
from ..services.agent_comms import AgentComms, MessagePriority, MessageType
from .comms import require_agent_owner

router = APIRouter(
    prefix="/v1/agent-comms",
    tags=["Agent Communications (durable)"],
    responses={
        401: {"description": "Missing API key"},
        403: {"description": "Invalid API key"},
    },
)


class AgentCommsSendRequest(BaseModel):
    from_agent: str = Field(..., description="Sender agent id")
    to_agent: str = Field(..., description="Recipient agent id")
    message_type: MessageType = Field(default=MessageType.REQUEST)
    priority: MessagePriority = Field(default=MessagePriority.NORMAL)
    subject: str = Field(...)
    body: dict = Field(...)
    correlation_id: str | None = None
    reply_to: str | None = None


class AgentCommsSendResponse(BaseModel):
    message_id: str
    from_agent: str
    to_agent: str
    status: str
    payload_hash: str | None = Field(
        None,
        description="Set when SIMULATION_MODE_AGENT_COMMS is false (DB mode).",
    )
    delivered_at: str | None = None
    created_at: str


class InboxMessageItem(BaseModel):
    message_id: str
    from_agent: str
    to_agent: str
    message_type: str
    priority: str
    subject: str
    body: dict
    correlation_id: str | None
    reply_to: str | None
    status: str
    payload_hash: str | None
    created_at: str
    delivered_at: str | None


class AgentCommsInboxResponse(BaseModel):
    agent_id: str
    messages: list[InboxMessageItem]
    total: int
    limit: int
    offset: int


def _audit_hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


@router.post(
    "/send",
    response_model=AgentCommsSendResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Send message (durable store in real mode)",
)
async def agent_comms_send(
    request: AgentCommsSendRequest,
    auth: AuthContext = Depends(get_auth_context),
    comms: AgentComms = Depends(get_agent_comms),
):
    await require_agent_owner(comms, auth, request.from_agent)
    msg = await comms.send_message(
        from_agent=request.from_agent,
        to_agent=request.to_agent,
        message_type=request.message_type,
        subject=request.subject,
        body=request.body,
        priority=request.priority,
        correlation_id=request.correlation_id,
        reply_to=request.reply_to,
    )
    audit_basis = {
        "body": msg.body,
        "correlation_id": msg.correlation_id,
        "from_agent": msg.from_agent,
        "message_type": msg.message_type.value,
        "priority": msg.priority.value,
        "reply_to": msg.reply_to,
        "subject": msg.subject,
        "to_agent": msg.to_agent,
    }
    req_hash = _audit_hash(audit_basis)
    ph = None if is_simulation("agent_comms") else req_hash
    record_audit(
        "agent_comms.send",
        actor_source=auth.source,
        key_id=auth.key_id,
        wallet_id=auth.wallet_id,
        outcome=msg.delivery_status.value,
        payload_hash=req_hash,
        message_id=msg.message_id,
    )
    return AgentCommsSendResponse(
        message_id=msg.message_id,
        from_agent=msg.from_agent,
        to_agent=msg.to_agent,
        status=msg.delivery_status.value,
        payload_hash=ph,
        delivered_at=msg.delivered_at.isoformat() if msg.delivered_at else None,
        created_at=msg.created_at.isoformat(),
    )


@router.get(
    "/inbox",
    response_model=AgentCommsInboxResponse,
    summary="List inbox (persisted in real mode)",
)
async def agent_comms_inbox(
    agent_id: str = Query(..., description="Recipient agent id"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    auth: AuthContext = Depends(get_auth_context),
    comms: AgentComms = Depends(get_agent_comms),
):
    await require_agent_owner(comms, auth, agent_id)
    audit_basis = {"agent_id": agent_id, "limit": limit, "offset": offset}
    audit_h = _audit_hash(audit_basis)
    rows, total, _ = await comms.list_inbox_for_http(agent_id, limit, offset)
    record_audit(
        "agent_comms.inbox.list",
        actor_source=auth.source,
        key_id=auth.key_id,
        wallet_id=auth.wallet_id,
        outcome="ok",
        payload_hash=audit_h,
        agent_id=agent_id,
        total=total,
    )
    items = [
        InboxMessageItem(
            message_id=r["message_id"],
            from_agent=r["from_agent"],
            to_agent=r["to_agent"],
            message_type=r["message_type"],
            priority=r["priority"],
            subject=r["subject"],
            body=r["body"],
            correlation_id=r["correlation_id"],
            reply_to=r["reply_to"],
            status=r["status"],
            payload_hash=r["payload_hash"],
            created_at=r["created_at"].isoformat()
            if hasattr(r["created_at"], "isoformat")
            else str(r["created_at"]),
            delivered_at=r["delivered_at"].isoformat()
            if r.get("delivered_at") and hasattr(r["delivered_at"], "isoformat")
            else (str(r["delivered_at"]) if r.get("delivered_at") else None),
        )
        for r in rows
    ]
    return AgentCommsInboxResponse(
        agent_id=agent_id,
        messages=items,
        total=total,
        limit=limit,
        offset=offset,
    )
