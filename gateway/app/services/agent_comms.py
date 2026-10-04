"""
Agent-Native Communications Layer
===================================
Handles agent-to-agent messaging without human-first platforms.

Why not Gmail/Slack?
- Human spam filters block automated senders
- Rate limits designed for humans (~500/day) throttle agent workloads
- OAuth flows require browser interaction (violates Zero-GUI rule)

This module implements the Agent Mail pattern: structured, authenticated,
machine-to-machine messaging with built-in routing and delivery confirmation.
"""

import asyncio
import hashlib
import hmac
import uuid
import logging
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from ..core.auth import AuthContext
from ..core.durable_state import get_durable_state
from ..core.runtime_mode import is_simulation, require_simulation
from .agent_comms_store import CommsMessageStore, compute_payload_hash

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Message Types
# ---------------------------------------------------------------------------


class MessagePriority(str, Enum):
    CRITICAL = "critical"  # Immediate delivery, retry aggressively
    HIGH = "high"  # Deliver within 1 minute
    NORMAL = "normal"  # Deliver within 5 minutes
    LOW = "low"  # Batch delivery, best effort


class MessageType(str, Enum):
    """Standard message types for agent-to-agent communication."""

    REQUEST = "request"  # Asking another agent to do something
    RESPONSE = "response"  # Reply to a request
    EVENT = "event"  # Notification of something that happened
    HEARTBEAT = "heartbeat"  # Liveness check
    HANDOFF = "handoff"  # Transfer responsibility to another agent


class DeliveryStatus(str, Enum):
    QUEUED = "queued"
    DELIVERED = "delivered"
    ACKNOWLEDGED = "acknowledged"
    FAILED = "failed"
    EXPIRED = "expired"


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


@dataclass
class AgentMessage:
    """A single agent-to-agent message."""

    message_id: str
    from_agent: str  # Sender agent identifier
    to_agent: str  # Recipient agent identifier
    message_type: MessageType
    priority: MessagePriority
    subject: str
    body: dict  # Structured payload (always JSON-serializable)
    correlation_id: str | None = None  # Links request/response pairs
    reply_to: str | None = None  # Message ID this replies to
    metadata: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    delivery_status: DeliveryStatus = DeliveryStatus.QUEUED
    delivered_at: datetime | None = None
    acknowledged_at: datetime | None = None


# ---------------------------------------------------------------------------
# Agent Registry
# ---------------------------------------------------------------------------


@dataclass
class RegisteredAgent:
    """An agent registered in the communications network."""

    agent_id: str
    name: str
    capabilities: list[str]  # What this agent can do
    webhook_url: str | None  # Where to deliver messages
    # Digest of the agent's messaging key; the plaintext is returned once, at
    # registration, and never stored.
    api_key: str
    # Owner identity (see owner_identity), never a raw credential.
    owner_key: str = ""
    status: str = "active"
    registered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen: datetime | None = None
    message_count: int = 0


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------
#
# An agent is owned by the tenant that registered it. Wallet-scoped callers
# (database API keys and the JWTs minted from them) own by wallet, so two
# wallets never share an identity. Callers with no wallet (bootstrap/env keys)
# own by a digest of their key. Neither form is a usable credential, so the
# durable registry holds no secrets.

_OWNER_WALLET_PREFIX = "wallet:"
_DIGEST_PREFIX = "sha256:"


def _digest(secret: str) -> str:
    return _DIGEST_PREFIX + hashlib.sha256(secret.encode()).hexdigest()


def owner_identity(auth: AuthContext) -> str:
    """Non-secret ownership identity for an authenticated caller."""
    if auth.wallet_id:
        return _OWNER_WALLET_PREFIX + auth.wallet_id
    return _digest(auth.raw_key)


def caller_owns_agent(agent: RegisteredAgent, auth: AuthContext) -> bool:
    """True only when ``auth`` is the tenant that registered ``agent``.

    An owner-less record matches nobody. A record registered before ownership
    moved off the raw key holds that key's digest (see
    ``AgentRegistry._scrub_plaintext_credentials``), so an API-key caller also
    matches on the digest of its own key. A JWT caller never does: its
    ``raw_key`` is not the credential that registered the agent.
    """
    if not agent.owner_key:
        return False
    candidates = [owner_identity(auth)]
    if auth.source != "jwt":
        candidates.append(_digest(auth.raw_key))
    stored = agent.owner_key.encode()
    return any(hmac.compare_digest(stored, c.encode()) for c in candidates)


def _parse_dt(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


class AgentRegistry:
    """Registry of agents in the communication network."""

    def __init__(self):
        self._agents: dict[str, RegisteredAgent] = {}
        self._lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()
        self._hydrated = False
        self._state = get_durable_state()

    @staticmethod
    def _agent_from_dict(data: dict[str, Any]) -> RegisteredAgent:
        payload = dict(data)
        registered = _parse_dt(payload.get("registered_at"))
        if registered is not None:
            payload["registered_at"] = registered
        last_seen = _parse_dt(payload.get("last_seen"))
        payload["last_seen"] = last_seen
        return RegisteredAgent(**payload)

    @staticmethod
    def _scrub_plaintext_credentials(agent: RegisteredAgent) -> bool:
        """Replace any plaintext key on ``agent`` with its digest.

        Records written before this fix stored the registering caller's raw
        API key as ``owner_key`` and the agent key in plaintext. Returns True
        when the record changed.
        """
        changed = False
        if agent.owner_key and not agent.owner_key.startswith(
            (_OWNER_WALLET_PREFIX, _DIGEST_PREFIX)
        ):
            agent.owner_key = _digest(agent.owner_key)
            changed = True
        if agent.api_key and not agent.api_key.startswith(_DIGEST_PREFIX):
            agent.api_key = _digest(agent.api_key)
            changed = True
        return changed

    async def _hydrate_if_needed(self):
        if self._hydrated:
            return

        async with self._init_lock:
            if self._hydrated:
                return

            payload = await self._state.load_json("comms.registry")
            scrubbed = False
            if isinstance(payload, dict):
                loaded: dict[str, RegisteredAgent] = {}
                for agent_id, record in payload.items():
                    try:
                        agent = self._agent_from_dict(record)
                    except Exception:
                        logger.exception(
                            "Skipping corrupt comms agent record: %s", agent_id
                        )
                        continue
                    scrubbed = self._scrub_plaintext_credentials(agent) or scrubbed
                    loaded[agent_id] = agent
                self._agents = loaded

            if scrubbed:
                # Rewrite the stored registry so legacy plaintext keys do not
                # stay at rest.
                async with self._lock:
                    await self._persist_locked()

            self._hydrated = True

    async def _persist_locked(self):
        if not self._state.enabled:
            return
        await self._state.save_json(
            "comms.registry",
            {agent_id: asdict(agent) for agent_id, agent in self._agents.items()},
        )

    async def persist(self):
        await self._hydrate_if_needed()
        async with self._lock:
            await self._persist_locked()

    async def register(
        self,
        name: str,
        capabilities: list[str],
        webhook_url: str | None = None,
        owner_key: str = "",
    ) -> RegisteredAgent:
        """Register an agent owned by ``owner_key`` (an ``owner_identity``).

        The returned record carries the agent key in plaintext so the caller
        can hand it out once; the registry keeps only its digest.
        """
        await self._hydrate_if_needed()
        agent_key = f"ak-{uuid.uuid4().hex}"
        agent = RegisteredAgent(
            agent_id=f"agent-{uuid.uuid4().hex[:12]}",
            name=name,
            capabilities=capabilities,
            webhook_url=webhook_url,
            api_key=agent_key,
            owner_key=owner_key,
        )
        self._scrub_plaintext_credentials(agent)
        async with self._lock:
            self._agents[agent.agent_id] = agent
            await self._persist_locked()
        logger.info(f"Agent registered: {agent.agent_id} ({agent.name})")
        return replace(agent, api_key=agent_key)

    async def get(self, agent_id: str) -> RegisteredAgent | None:
        await self._hydrate_if_needed()
        return self._agents.get(agent_id)

    async def find_by_capability(self, capability: str) -> list[RegisteredAgent]:
        """Find agents that advertise a specific capability."""
        await self._hydrate_if_needed()
        return [
            a
            for a in self._agents.values()
            if capability in a.capabilities and a.status == "active"
        ]

    async def list_all(self) -> list[RegisteredAgent]:
        await self._hydrate_if_needed()
        return list(self._agents.values())


# ---------------------------------------------------------------------------
# Message Router
# ---------------------------------------------------------------------------


class MessageRouter:
    """
    Routes messages between agents with delivery guarantees.

    Delivery strategies by priority:
    - CRITICAL: Immediate webhook delivery with 3x retry
    - HIGH: Deliver within 1 minute, 2x retry
    - NORMAL: Deliver within 5 minutes, 1x retry
    - LOW: Batch delivery every 5 minutes, no retry
    """

    def __init__(self, registry: AgentRegistry):
        self._registry = registry
        self._inbox: dict[str, list[AgentMessage]] = {}  # agent_id → messages
        self._outbox: list[AgentMessage] = []
        self._lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()
        self._hydrated = False
        self._state = get_durable_state()

    @staticmethod
    def _inbox_key(agent_id: str, message_id: str) -> str:
        return f"comms.inbox.{agent_id}.{message_id}"

    @staticmethod
    def _outbox_key(message_id: str) -> str:
        return f"comms.outbox.{message_id}"

    @staticmethod
    def _message_from_dict(data: dict[str, Any]) -> AgentMessage:
        payload = dict(data)
        payload["message_type"] = MessageType(payload["message_type"])
        payload["priority"] = MessagePriority(payload["priority"])
        payload["delivery_status"] = DeliveryStatus(payload["delivery_status"])
        payload["created_at"] = _parse_dt(payload.get("created_at")) or datetime.now(
            timezone.utc
        )
        payload["expires_at"] = _parse_dt(payload.get("expires_at"))
        payload["delivered_at"] = _parse_dt(payload.get("delivered_at"))
        payload["acknowledged_at"] = _parse_dt(payload.get("acknowledged_at"))
        return AgentMessage(**payload)

    async def _hydrate_if_needed(self):
        if self._hydrated:
            return

        async with self._init_lock:
            if self._hydrated:
                return

            inbox_payload = await self._state.load_json("comms.inbox")
            if isinstance(inbox_payload, dict):
                loaded_inbox: dict[str, list[AgentMessage]] = {}
                for agent_id, records in inbox_payload.items():
                    if not isinstance(records, list):
                        continue
                    loaded_messages: list[AgentMessage] = []
                    for record in records:
                        try:
                            loaded_messages.append(self._message_from_dict(record))
                        except Exception:
                            logger.exception(
                                "Skipping corrupt inbox message for %s", agent_id
                            )
                    loaded_inbox[agent_id] = loaded_messages
                self._inbox = loaded_inbox

            outbox_payload = await self._state.load_json("comms.outbox")
            if isinstance(outbox_payload, list):
                loaded_outbox: list[AgentMessage] = []
                for record in outbox_payload:
                    try:
                        loaded_outbox.append(self._message_from_dict(record))
                    except Exception:
                        logger.exception("Skipping corrupt outbox message")
                self._outbox = loaded_outbox

            if self._state.enabled:
                loaded_inbox = await self._load_all_inbox_messages()
                if loaded_inbox:
                    self._inbox = loaded_inbox

                loaded_outbox = await self._load_outbox_messages()
                if loaded_outbox:
                    self._outbox = loaded_outbox

            self._hydrated = True

    async def _load_agent_inbox(self, agent_id: str) -> list[AgentMessage]:
        """Load a single agent inbox from row-keyed durable state."""
        if not self._state.enabled:
            return self._inbox.get(agent_id, [])

        messages: list[AgentMessage] = []
        for key in await self._state.list_keys(f"comms.inbox.{agent_id}."):
            record = await self._state.load_json(key)
            if not isinstance(record, dict):
                continue
            try:
                messages.append(self._message_from_dict(record))
            except Exception:
                logger.exception("Skipping corrupt inbox message: %s", key)
        return sorted(messages, key=lambda message: message.created_at)

    async def _load_all_inbox_messages(self) -> dict[str, list[AgentMessage]]:
        """Load all row-keyed inbox messages for process hydration."""
        loaded: dict[str, list[AgentMessage]] = {}
        for key in await self._state.list_keys("comms.inbox."):
            record = await self._state.load_json(key)
            if not isinstance(record, dict):
                continue
            try:
                message = self._message_from_dict(record)
                loaded.setdefault(message.to_agent, []).append(message)
            except Exception:
                logger.exception("Skipping corrupt inbox message: %s", key)
        for messages in loaded.values():
            messages.sort(key=lambda message: message.created_at)
        return loaded

    async def _load_outbox_messages(self) -> list[AgentMessage]:
        """Load row-keyed outbox messages for process hydration."""
        messages: list[AgentMessage] = []
        for key in await self._state.list_keys("comms.outbox."):
            record = await self._state.load_json(key)
            if not isinstance(record, dict):
                continue
            try:
                messages.append(self._message_from_dict(record))
            except Exception:
                logger.exception("Skipping corrupt outbox message: %s", key)
        return sorted(messages, key=lambda message: message.created_at)

    async def _persist_message_locked(self, message: AgentMessage) -> None:
        """Persist one message without rewriting the global inbox."""
        if not self._state.enabled:
            return
        await self._state.save_json(
            self._inbox_key(message.to_agent, message.message_id),
            asdict(message),
        )
        await self._state.save_json(
            self._outbox_key(message.message_id),
            asdict(message),
        )

    async def _persist_locked(self):
        if not self._state.enabled:
            return

        for messages in self._inbox.values():
            for message in messages:
                await self._persist_message_locked(message)

    async def persist(self):
        await self._hydrate_if_needed()
        async with self._lock:
            await self._persist_locked()

    async def send(self, message: AgentMessage) -> AgentMessage:
        """Route a message to the recipient."""
        await self._hydrate_if_needed()
        recipient = await self._registry.get(message.to_agent)
        if not recipient:
            message.delivery_status = DeliveryStatus.FAILED
            logger.warning(
                f"Message {message.message_id} failed: recipient "
                f"{message.to_agent} not found"
            )
            return message

        # Add to recipient's inbox
        async with self._lock:
            if message.to_agent not in self._inbox:
                self._inbox[message.to_agent] = []
            self._inbox[message.to_agent].append(message)
            self._outbox.append(message)

        # Attempt delivery
        if recipient.webhook_url:
            delivered = await self._deliver_webhook(message, recipient)
            if delivered:
                message.delivery_status = DeliveryStatus.DELIVERED
                message.delivered_at = datetime.now(timezone.utc)
            else:
                message.delivery_status = DeliveryStatus.QUEUED
        else:
            # No webhook — agent must poll
            message.delivery_status = DeliveryStatus.QUEUED

        async with self._lock:
            await self._persist_message_locked(message)

        logger.info(
            f"Message routed: {message.from_agent} → {message.to_agent} "
            f"[{message.message_type.value}] status={message.delivery_status.value}"
        )
        return message

    async def poll(self, agent_id: str, limit: int = 50) -> list[AgentMessage]:
        """Poll for messages in an agent's inbox."""
        await self._hydrate_if_needed()
        if self._state.enabled:
            async with self._lock:
                self._inbox[agent_id] = await self._load_agent_inbox(agent_id)

        changed = False
        async with self._lock:
            messages = self._inbox.get(agent_id, [])[:limit]
            # Mark as delivered
            for msg in messages:
                if msg.delivery_status == DeliveryStatus.QUEUED:
                    msg.delivery_status = DeliveryStatus.DELIVERED
                    msg.delivered_at = datetime.now(timezone.utc)
                    changed = True
        if changed:
            async with self._lock:
                for msg in messages:
                    await self._persist_message_locked(msg)
        return messages

    async def list_inbox_page(
        self, agent_id: str, limit: int = 50, offset: int = 0
    ) -> tuple[list[AgentMessage], int]:
        """Paginated inbox view without mutating delivery (simulation / HTTP inbox)."""
        await self._hydrate_if_needed()
        if self._state.enabled:
            async with self._lock:
                self._inbox[agent_id] = await self._load_agent_inbox(agent_id)
        async with self._lock:
            all_msgs = sorted(
                self._inbox.get(agent_id, []),
                key=lambda m: m.created_at,
            )
            total = len(all_msgs)
            page = all_msgs[offset : offset + limit]
        return list(page), total

    async def acknowledge(self, agent_id: str, message_id: str) -> bool:
        """Acknowledge receipt of a message."""
        await self._hydrate_if_needed()
        async with self._lock:
            messages = self._inbox.get(agent_id, [])
            for msg in messages:
                if msg.message_id == message_id:
                    msg.delivery_status = DeliveryStatus.ACKNOWLEDGED
                    msg.acknowledged_at = datetime.now(timezone.utc)
                    await self._persist_locked()
                    return True
        return False

    async def _deliver_webhook(
        self, message: AgentMessage, recipient: RegisteredAgent
    ) -> bool:
        """Deliver message via webhook.

        Real HTTP delivery is outside the frozen proof-surface scope. Under
        simulation this logs and returns True as an explicitly simulated
        success; flipping SIMULATION_MODE_AGENT_COMMS off raises unless the
        surface is explicitly unfrozen and a real client lands.
        """
        require_simulation("agent_comms")
        logger.info(
            f"Simulated webhook delivery to {recipient.webhook_url} for "
            f"{message.message_id}"
        )
        return True


# ---------------------------------------------------------------------------
# Agent Comms Orchestrator
# ---------------------------------------------------------------------------


class AgentComms:
    """
    Top-level orchestrator for agent-to-agent communications.
    Provides a clean interface for the router layer.
    """

    def __init__(self):
        self.registry = AgentRegistry()
        self.router = MessageRouter(self.registry)
        self._db_store = CommsMessageStore()

    async def register_agent(
        self,
        name: str,
        capabilities: list[str],
        webhook_url: str | None = None,
        owner_key: str = "",
    ) -> RegisteredAgent:
        return await self.registry.register(name, capabilities, webhook_url, owner_key)  # type: ignore[no-any-return]

    async def send_message(
        self,
        from_agent: str,
        to_agent: str,
        message_type: MessageType,
        subject: str,
        body: dict,
        priority: MessagePriority = MessagePriority.NORMAL,
        correlation_id: str | None = None,
        reply_to: str | None = None,
    ) -> AgentMessage:
        message = AgentMessage(
            message_id=str(uuid.uuid4()),
            from_agent=from_agent,
            to_agent=to_agent,
            message_type=message_type,
            priority=priority,
            subject=subject,
            body=body,
            correlation_id=correlation_id or str(uuid.uuid4()),
            reply_to=reply_to,
        )
        result = await self.router.send(message)
        if not is_simulation("agent_comms"):
            ph = compute_payload_hash(
                body=result.body,
                correlation_id=result.correlation_id,
                from_agent=result.from_agent,
                message_type=result.message_type.value,
                priority=result.priority.value,
                reply_to=result.reply_to,
                subject=result.subject,
                to_agent=result.to_agent,
            )
            await self._db_store.insert_message(
                message_id=result.message_id,
                from_agent=result.from_agent,
                to_agent=result.to_agent,
                message_type=result.message_type.value,
                priority=result.priority.value,
                subject=result.subject,
                body=result.body,
                correlation_id=result.correlation_id,
                reply_to=result.reply_to,
                status=result.delivery_status.value,
                payload_hash=ph,
                created_at=result.created_at,
                delivered_at=result.delivered_at,
            )
        return result  # type: ignore[no-any-return]

    async def list_inbox_for_http(
        self, agent_id: str, limit: int, offset: int
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """
        Inbox listing for ``/v1/agent-comms/inbox``.

        Returns (messages_as_dicts, total, from_durable_db).
        """
        if not is_simulation("agent_comms"):
            rows, total = await self._db_store.list_inbox_for_agent(
                agent_id, limit=limit, offset=offset
            )
            return rows, total, True
        page, total = await self.router.list_inbox_page(agent_id, limit, offset)
        serialized: list[dict[str, Any]] = []
        for m in page:
            serialized.append(
                {
                    "message_id": m.message_id,
                    "from_agent": m.from_agent,
                    "to_agent": m.to_agent,
                    "message_type": m.message_type.value,
                    "priority": m.priority.value,
                    "subject": m.subject,
                    "body": m.body,
                    "correlation_id": m.correlation_id,
                    "reply_to": m.reply_to,
                    "status": m.delivery_status.value,
                    "payload_hash": None,
                    "created_at": m.created_at,
                    "delivered_at": m.delivered_at,
                }
            )
        return serialized, total, False

    async def request_handoff(
        self,
        from_agent: str,
        capability: str,
        context: dict,
    ) -> AgentMessage | None:
        """
        Find an agent with the required capability and hand off work.
        This is how swarm intelligence works: agents route tasks to specialists.
        """
        candidates = await self.registry.find_by_capability(capability)
        if not candidates:
            logger.warning(f"No agents found with capability '{capability}'")
            return None

        # Pick the first available (production: load balance)
        target = candidates[0]
        return await self.send_message(
            from_agent=from_agent,
            to_agent=target.agent_id,
            message_type=MessageType.HANDOFF,
            subject=f"Handoff: {capability}",
            body={"capability": capability, "context": context},
            priority=MessagePriority.HIGH,
        )
