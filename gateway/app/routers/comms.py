"""
PROOF SURFACE — frozen. Do not expand; see docs/PROOF_SURFACES.md.
Agent-Native Communications Router
------------------------------------
Machine-to-machine messaging that bypasses human-first platforms.
Agents register, discover each other by capability, send structured
messages, and hand off tasks to specialists.

This is the nervous system of swarm intelligence.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from datetime import datetime

from ..core.auth import AuthContext, get_auth_context
from ..core.dependencies import get_agent_comms
from ..services.agent_comms import (
    AgentComms,
    MessageType,
    MessagePriority,
    caller_owns_agent,
    owner_identity,
)

router = APIRouter(
    prefix="/v1/comms",
    tags=["Agent Communications"],
    responses={
        401: {"description": "Missing API key"},
        403: {"description": "Invalid API key"},
    },
)


# --- Request/Response Schemas ---


class AgentRegistrationRequest(BaseModel):
    """Register an agent in the communication network."""

    name: str = Field(
        ...,
        description="Human-readable agent name.",
        examples=["iot-monitor-agent"],
    )
    capabilities: list[str] = Field(
        ...,
        description=(
            "List of capabilities this agent provides. "
            "Other agents use these to discover and hand off tasks."
        ),
        examples=[["iot-monitoring", "mqtt-bridging", "device-management"]],
    )
    webhook_url: str | None = Field(
        None,
        description="URL for push-based message delivery. If null, agent must poll.",
        examples=["https://my-agent.example.com/webhook/messages"],
    )


class AgentRegistrationResponse(BaseModel):
    agent_id: str
    name: str
    capabilities: list[str]
    api_key: str = Field(
        ...,
        description=(
            "Agent-specific API key for sending/receiving messages. Store securely."
        ),
    )
    webhook_url: str | None
    status: str
    registered_at: datetime


class SendMessageRequest(BaseModel):
    """Send a message to another agent."""

    to_agent: str = Field(
        ...,
        description="Recipient agent ID.",
    )
    message_type: MessageType = Field(
        default=MessageType.REQUEST,
        description="Type of message: request, response, event, heartbeat, or handoff.",
    )
    priority: MessagePriority = Field(
        default=MessagePriority.NORMAL,
        description="Delivery priority. Critical = immediate retry, Low = batched.",
    )
    subject: str = Field(
        ...,
        description="Brief subject line for the message.",
        examples=["Anomaly detected in IoT bridge"],
    )
    body: dict = Field(
        ...,
        description="Structured message payload. Must be JSON-serializable.",
    )
    correlation_id: str | None = Field(
        None,
        description="Links related messages (e.g., request/response pairs).",
    )
    reply_to: str | None = Field(
        None,
        description="Message ID this is replying to.",
    )


class MessageResponse(BaseModel):
    message_id: str
    from_agent: str
    to_agent: str
    message_type: str
    priority: str
    subject: str
    delivery_status: str
    created_at: datetime
    delivered_at: datetime | None


class HandoffRequest(BaseModel):
    """Request a task handoff to an agent with a specific capability."""

    capability: str = Field(
        ...,
        description="The capability needed. System finds the best available agent.",
        examples=["video-transcription"],
    )
    context: dict = Field(
        ...,
        description="Context for the handoff — what needs to be done and why.",
        examples=[
            {"video_id": "abc-123", "language": "en", "reason": "hooks need transcript"}
        ],
    )


class HandoffResponse(BaseModel):
    status: str
    message: MessageResponse | None = None
    target_agent_id: str | None = None
    target_agent_name: str | None = None
    error: str | None = None


# --- Auth helpers ---


async def require_agent_owner(
    comms: AgentComms, auth: AuthContext, agent_id: str
) -> None:
    """Reject the request unless the caller owns this agent.

    Applied to every endpoint that acts on behalf of an agent (send, ack,
    handoff, inbox, here and on ``/v1/agent-comms``) so an authenticated
    caller cannot impersonate an agent they do not own. Fails closed: an
    unregistered id is 404 and an owner-less record is 403, because neither
    has a tenant the caller could prove it belongs to. Bootstrap admins
    (operator keys, outside every tenant) may still address those two, but
    not an agent another caller registered.
    """
    agent = await comms.registry.get(agent_id)
    if agent is None:
        if auth.is_bootstrap_admin:
            return
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "agent_not_found",
                "message": f"Agent '{agent_id}' is not registered.",
            },
        )
    if caller_owns_agent(agent, auth):
        return
    if not agent.owner_key and auth.is_bootstrap_admin:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "error": "access_denied",
            "message": "You do not own this agent.",
        },
    )


# --- Endpoints ---


@router.post(
    "/agents",
    response_model=AgentRegistrationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register an agent",
    description=(
        "Register a new agent in the communication network. "
        "The agent receives a unique ID and API key for messaging. "
        "Capabilities are used by other agents to discover and hand off tasks."
    ),
)
async def register_agent(
    request: AgentRegistrationRequest,
    auth: AuthContext = Depends(get_auth_context),
    comms: AgentComms = Depends(get_agent_comms),
):
    agent = await comms.register_agent(
        name=request.name,
        capabilities=request.capabilities,
        webhook_url=request.webhook_url,
        owner_key=owner_identity(auth),
    )
    return AgentRegistrationResponse(
        agent_id=agent.agent_id,
        name=agent.name,
        capabilities=agent.capabilities,
        api_key=agent.api_key,
        webhook_url=agent.webhook_url,
        status=agent.status,
        registered_at=agent.registered_at,
    )


@router.get(
    "/agents",
    summary="List registered agents",
    description=(
        "List the agents the caller registered (bootstrap admins see every "
        "agent), optionally filtered by capability."
    ),
)
async def list_agents(
    capability: str | None = Query(
        None,
        description="Filter agents by capability (e.g., 'video-transcription').",
    ),
    auth: AuthContext = Depends(get_auth_context),
    comms: AgentComms = Depends(get_agent_comms),
):
    if capability:
        agents = await comms.registry.find_by_capability(capability)
    else:
        agents = await comms.registry.list_all()
    if not auth.is_bootstrap_admin:
        # Tenants never enumerate each other's agents. Capability handoff
        # routing reads the registry server-side and is unaffected.
        agents = [a for a in agents if caller_owns_agent(a, auth)]

    return {
        "agents": [
            {
                "agent_id": a.agent_id,
                "name": a.name,
                "capabilities": a.capabilities,
                "status": a.status,
                "registered_at": a.registered_at.isoformat(),
                "last_seen": a.last_seen.isoformat() if a.last_seen else None,
            }
            for a in agents
        ],
        "total": len(agents),
    }


@router.post(
    "/messages",
    response_model=MessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Send a message to another agent",
    description=(
        "Send a structured message to a registered agent. "
        "Messages are routed based on priority: critical messages "
        "are delivered immediately with aggressive retry, low priority "
        "messages are batched. If the recipient has a webhook, delivery "
        "is push-based; otherwise the recipient must poll."
    ),
)
async def send_message(
    from_agent: str,
    request: SendMessageRequest,
    auth: AuthContext = Depends(get_auth_context),
    comms: AgentComms = Depends(get_agent_comms),
):
    await require_agent_owner(comms, auth, from_agent)
    msg = await comms.send_message(
        from_agent=from_agent,
        to_agent=request.to_agent,
        message_type=request.message_type,
        subject=request.subject,
        body=request.body,
        priority=request.priority,
        correlation_id=request.correlation_id,
        reply_to=request.reply_to,
    )
    return MessageResponse(
        message_id=msg.message_id,
        from_agent=msg.from_agent,
        to_agent=msg.to_agent,
        message_type=msg.message_type.value,
        priority=msg.priority.value,
        subject=msg.subject,
        delivery_status=msg.delivery_status.value,
        created_at=msg.created_at,
        delivered_at=msg.delivered_at,
    )


@router.get(
    "/messages/{agent_id}/inbox",
    summary="Poll for messages",
    description=(
        "Poll an agent's inbox for pending messages. "
        "Messages are marked as delivered upon retrieval. "
        "Use this if the agent does not have a webhook configured."
    ),
)
async def poll_inbox(
    agent_id: str,
    limit: int = Query(50, ge=1, le=200, description="Max messages to return"),
    auth: AuthContext = Depends(get_auth_context),
    comms: AgentComms = Depends(get_agent_comms),
):
    await require_agent_owner(comms, auth, agent_id)
    messages = await comms.router.poll(agent_id, limit=limit)
    return {
        "agent_id": agent_id,
        "messages": [
            {
                "message_id": m.message_id,
                "from_agent": m.from_agent,
                "message_type": m.message_type.value,
                "priority": m.priority.value,
                "subject": m.subject,
                "body": m.body,
                "correlation_id": m.correlation_id,
                "delivery_status": m.delivery_status.value,
                "created_at": m.created_at.isoformat(),
            }
            for m in messages
        ],
        "count": len(messages),
    }


@router.post(
    "/messages/{agent_id}/ack/{message_id}",
    summary="Acknowledge a message",
    description="Confirm receipt and processing of a message.",
)
async def acknowledge_message(
    agent_id: str,
    message_id: str,
    auth: AuthContext = Depends(get_auth_context),
    comms: AgentComms = Depends(get_agent_comms),
):
    await require_agent_owner(comms, auth, agent_id)
    acked = await comms.router.acknowledge(agent_id, message_id)
    if not acked:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "message_not_found",
                "message": f"Message '{message_id}' not found in inbox.",
            },
        )
    return {"message_id": message_id, "status": "acknowledged"}


@router.post(
    "/handoff",
    response_model=HandoffResponse,
    summary="Request a capability-based task handoff",
    description=(
        "Find an agent with the required capability and hand off work to it. "
        "This is the core swarm intelligence mechanism: instead of one 'God model' "
        "doing everything, tasks are routed to specialist agents. "
        "Returns the handoff message and target agent details."
    ),
)
async def request_handoff(
    from_agent: str,
    request: HandoffRequest,
    auth: AuthContext = Depends(get_auth_context),
    comms: AgentComms = Depends(get_agent_comms),
):
    await require_agent_owner(comms, auth, from_agent)
    msg = await comms.request_handoff(
        from_agent=from_agent,
        capability=request.capability,
        context=request.context,
    )

    if not msg:
        return HandoffResponse(
            status="no_agent_found",
            error=f"No active agent found with capability '{request.capability}'.",
        )

    target = await comms.registry.get(msg.to_agent)
    return HandoffResponse(
        status="handoff_sent",
        message=MessageResponse(
            message_id=msg.message_id,
            from_agent=msg.from_agent,
            to_agent=msg.to_agent,
            message_type=msg.message_type.value,
            priority=msg.priority.value,
            subject=msg.subject,
            delivery_status=msg.delivery_status.value,
            created_at=msg.created_at,
            delivered_at=msg.delivered_at,
        ),
        target_agent_id=target.agent_id if target else None,
        target_agent_name=target.name if target else None,
    )
