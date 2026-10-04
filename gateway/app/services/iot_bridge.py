"""
PROOF SURFACE — frozen. Do not expand; see docs/PROOF_SURFACES.md.
IoT Protocol Bridge — Service Layer
=====================================
Handles the actual protocol translation between REST and native IoT protocols.
This is where the "bridge" lives: HTTP in, MQTT/CoAP/etc out.

Security-first design:
- Deny-by-default topic ACLs
- Per-device credential isolation
- Message payload sanitization
- Audit logging on every bridge operation

In production, swap the in-memory stores for Redis (device state)
and PostgreSQL (audit log, ACL rules).
"""

import asyncio
import json
import os
import re
import time
import uuid
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, ClassVar, cast

from sqlalchemy import select, func
from sqlalchemy.sql.elements import ColumnElement

from ..core.runtime_mode import require_simulation
from ..db.database import get_session_factory, is_database_configured
from ..db.models import IoTDeviceEventModel, IoTDeviceModel

from ..schemas.iot import ACLPermission, ProtocolType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ACL Engine
# ---------------------------------------------------------------------------


class ACLViolation(Exception):
    """Raised when a message violates topic-level access controls."""

    def __init__(self, device_id: str, topic: str, required: str):
        self.device_id = device_id
        self.topic = topic
        self.required = required
        super().__init__(
            f"ACL violation: device '{device_id}' cannot {required} topic '{topic}'"
        )


class TopicACLEngine:
    """
    Evaluates MQTT-style topic ACL rules with wildcard support.

    Rules:
    - '+' matches a single topic level  (e.g., device/+/telemetry)
    - '#' matches all remaining levels   (e.g., device/#)
    - Empty ACL dict = deny-all (secure default)
    - Explicit 'deny' overrides any allow
    """

    @staticmethod
    def _topic_to_regex(pattern: str) -> str:
        """Convert MQTT topic pattern to regex."""
        # Escape dots, replace MQTT wildcards
        escaped = re.escape(pattern)
        escaped = escaped.replace(r"\+", "[^/]+")
        escaped = escaped.replace(r"\#", ".*")
        return f"^{escaped}$"

    @staticmethod
    def check(
        acl: dict[str, ACLPermission],
        topic: str,
        required: ACLPermission,
    ) -> bool:
        """
        Check if a topic access is permitted.

        Returns True if allowed, raises ACLViolation if denied.
        """
        if not acl:
            return False  # Deny-by-default

        # Denials apply across all matching rules, even when an allow is more
        # specific or appeared first in the supplied mapping.
        for pattern, permission in acl.items():
            if permission == ACLPermission.DENY and (
                pattern == topic
                or re.match(TopicACLEngine._topic_to_regex(pattern), topic)
            ):
                return False

        matched_permission: ACLPermission | None = None

        # Check exact match first
        if topic in acl:
            matched_permission = acl[topic]
        else:
            # Check wildcard patterns (most specific wins)
            best_specificity = -1
            for pattern, perm in acl.items():
                if "+" not in pattern and "#" not in pattern:
                    continue
                regex = TopicACLEngine._topic_to_regex(pattern)
                if re.match(regex, topic):
                    # More segments = more specific
                    specificity = pattern.count("/")
                    if specificity > best_specificity:
                        best_specificity = specificity
                        matched_permission = perm

        if matched_permission is None or matched_permission == ACLPermission.DENY:
            return False

        return matched_permission in (required, ACLPermission.READ_WRITE)


# ---------------------------------------------------------------------------
# Device Registry
# ---------------------------------------------------------------------------


@dataclass
class RegisteredDevice:
    """Internal representation of a registered device."""

    device_id: str
    protocol: ProtocolType
    broker_url: str | None
    topic_acl: dict[str, ACLPermission]
    metadata: dict
    status: str = "registered"
    registered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_message_at: datetime | None = None
    message_count: int = 0
    # Wallet that registered the device; None for bootstrap-admin registrations
    # and rows that predate ownership (reachable by bootstrap admins only).
    owner_wallet_id: str | None = None


# The owning wallet rides in metadata_json under this reserved key so the
# frozen iot_devices schema needs no new column. It is server-controlled:
# _device_to_row always drops any client-supplied value before writing the
# authenticated owner, and _row_to_device lifts it out of user metadata.
_OWNER_METADATA_KEY = "__owner_wallet_id"


def _device_to_row(device: RegisteredDevice) -> IoTDeviceModel:
    acl_json = {
        topic: (p.value if hasattr(p, "value") else str(p))
        for topic, p in device.topic_acl.items()
    }
    stored_metadata = {
        k: v for k, v in (device.metadata or {}).items() if k != _OWNER_METADATA_KEY
    }
    if device.owner_wallet_id is not None:
        stored_metadata[_OWNER_METADATA_KEY] = device.owner_wallet_id
    return IoTDeviceModel(
        device_id=device.device_id,
        protocol=device.protocol.value,
        broker_url=device.broker_url,
        topic_acl_json=json.dumps(acl_json),
        metadata_json=json.dumps(stored_metadata, default=str),
        status=device.status,
        registered_at=device.registered_at,
        last_message_at=device.last_message_at,
        message_count=device.message_count,
    )


def _row_to_device(row: IoTDeviceModel) -> RegisteredDevice:
    acl: dict[str, ACLPermission] = {}
    if row.topic_acl_json:
        try:
            raw = json.loads(row.topic_acl_json)
            if isinstance(raw, dict):
                for topic, perm in raw.items():
                    try:
                        acl[topic] = ACLPermission(perm)
                    except ValueError:
                        continue
        except json.JSONDecodeError:
            pass

    metadata: dict = {}
    if row.metadata_json:
        try:
            parsed = json.loads(row.metadata_json)
            if isinstance(parsed, dict):
                metadata = parsed
        except json.JSONDecodeError:
            pass
    owner = metadata.pop(_OWNER_METADATA_KEY, None)

    try:
        protocol = ProtocolType(row.protocol)
    except ValueError:
        protocol = ProtocolType.MQTT

    return RegisteredDevice(
        device_id=row.device_id,
        protocol=protocol,
        broker_url=row.broker_url,
        topic_acl=acl,
        metadata=metadata,
        status=row.status,
        registered_at=row.registered_at,
        last_message_at=row.last_message_at,
        message_count=row.message_count,
        owner_wallet_id=owner if isinstance(owner, str) else None,
    )


class _DeviceCache:
    """Optional Redis cache in front of iot_devices.

    Silently no-ops if REDIS_URL is unset or the connection fails — the
    registry stays functional, just hits PG on every read.

    A failed invalidate is not silent: the key may still hold a deregistered
    device or a superseded topic ACL, so cache reads are suspended
    process-wide (every request builds its own bridge) until any such entry
    has expired by TTL, and reads fall through to PG meanwhile.
    """

    # Monotonic deadline shared by every instance in this process.
    _reads_suspended_until: ClassVar[float] = 0.0

    def __init__(self, ttl_seconds: int = 300):
        self._ttl = ttl_seconds
        self._url = os.environ.get("REDIS_URL", "").strip()
        self._client: Any = None
        self._disabled = not self._url
        self._init_lock = asyncio.Lock()

    @staticmethod
    def _key(device_id: str) -> str:
        return f"iot:device:{device_id}"

    async def _ensure_client(self) -> Any:
        if self._disabled or self._client is not None:
            return self._client
        async with self._init_lock:
            if self._client is not None or self._disabled:
                return self._client
            try:
                import redis.asyncio as redis

                client = redis.from_url(self._url, decode_responses=True)
                await client.ping()
                self._client = client
            except Exception as exc:
                logger.warning("IoT device cache disabled (Redis unavailable): %s", exc)
                self._disabled = True
        return self._client

    async def get(self, device_id: str) -> RegisteredDevice | None:
        if time.monotonic() < _DeviceCache._reads_suspended_until:
            return None
        client = await self._ensure_client()
        if client is None:
            return None
        try:
            raw = await client.get(self._key(device_id))
        except Exception:
            return None
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        # Rehydrate through IoTDeviceModel → RegisteredDevice so
        # enum-parsing and defaults stay consistent with PG reads.
        if data.get("registered_at"):
            data["registered_at"] = datetime.fromisoformat(data["registered_at"])
        if data.get("last_message_at"):
            data["last_message_at"] = datetime.fromisoformat(data["last_message_at"])
        row = IoTDeviceModel(**data)
        return _row_to_device(row)

    async def set(self, device: RegisteredDevice) -> None:
        client = await self._ensure_client()
        if client is None:
            return
        row = _device_to_row(device)
        serialized = json.dumps(
            {
                "device_id": row.device_id,
                "protocol": row.protocol,
                "broker_url": row.broker_url,
                "topic_acl_json": row.topic_acl_json,
                "metadata_json": row.metadata_json,
                "status": row.status,
                "registered_at": row.registered_at.isoformat(),
                "last_message_at": (
                    row.last_message_at.isoformat() if row.last_message_at else None
                ),
                "message_count": row.message_count,
            },
            default=str,
        )
        try:
            await client.set(self._key(device.device_id), serialized, ex=self._ttl)
        except Exception as exc:
            logger.debug("Redis SET failed (cache skipped): %s", exc)

    async def invalidate(self, device_id: str) -> None:
        client = await self._ensure_client()
        if client is None:
            return
        try:
            await client.delete(self._key(device_id))
        except Exception as exc:
            _DeviceCache._reads_suspended_until = time.monotonic() + self._ttl
            logger.warning(
                "IoT device cache invalidate failed for %s; serving reads from "
                "PG for %ss so a stale entry is not used: %s",
                device_id,
                self._ttl,
                exc,
            )


class DeviceRegistry:
    """
    PG-backed device registry with an optional Redis cache in front of
    ``get()`` (the hot ACL-check path). PG is the source of truth.
    Register/deregister/update_message_stats invalidate the cache key.
    """

    def __init__(self):
        self._cache = _DeviceCache()

    @staticmethod
    def _require_db() -> None:
        if not is_database_configured():
            raise RuntimeError(
                "iot_bridge.DeviceRegistry requires a configured database. "
                "Set DATABASE_URL."
            )

    @staticmethod
    async def _write_event(
        session,
        device_id: str,
        event_type: str,
        topic: str | None = None,
        payload: dict | None = None,
    ) -> None:
        session.add(
            IoTDeviceEventModel(
                event_id=uuid.uuid4().hex,
                device_id=device_id,
                event_type=event_type,
                topic=topic,
                payload_json=json.dumps(payload) if payload else None,
            )
        )

    async def register(self, device: RegisteredDevice) -> RegisteredDevice:
        self._require_db()
        factory = get_session_factory()
        async with factory() as session:
            existing = await session.get(IoTDeviceModel, device.device_id)
            if existing is not None:
                raise ValueError(f"Device '{device.device_id}' already registered")
            session.add(_device_to_row(device))
            await self._write_event(
                session,
                device.device_id,
                "register",
                payload={"protocol": device.protocol.value},
            )
            await session.commit()

        await self._cache.set(device)
        logger.info(f"Device registered: {device.device_id} ({device.protocol})")
        return device

    async def get(self, device_id: str) -> RegisteredDevice | None:
        self._require_db()

        cached = await self._cache.get(device_id)
        if cached is not None:
            return cached

        factory = get_session_factory()
        async with factory() as session:
            row = await session.get(IoTDeviceModel, device_id)
        if row is None:
            return None
        device = _row_to_device(row)
        await self._cache.set(device)
        return device

    async def list_all(
        self,
        page: int = 1,
        per_page: int = 50,
        *,
        owner_wallet_id: str | None = None,
    ) -> tuple[list[RegisteredDevice], int]:
        """Page through devices; ``owner_wallet_id`` limits it to one wallet's.

        The owner lives inside metadata_json, so the scoped listing filters
        after decoding rather than in SQL.
        """
        self._require_db()
        factory = get_session_factory()
        offset = max(0, (page - 1) * per_page)
        if owner_wallet_id is not None:
            async with factory() as session:
                result = await session.execute(
                    select(IoTDeviceModel).order_by(
                        cast(ColumnElement[Any], IoTDeviceModel.registered_at).asc()
                    )
                )
                rows = list(result.scalars().all())
            owned = [
                d
                for d in (_row_to_device(r) for r in rows)
                if d.owner_wallet_id == owner_wallet_id
            ]
            return owned[offset : offset + per_page], len(owned)
        async with factory() as session:
            total = (
                await session.scalar(select(func.count()).select_from(IoTDeviceModel))
                or 0
            )
            result = await session.execute(
                select(IoTDeviceModel)
                .order_by(cast(ColumnElement[Any], IoTDeviceModel.registered_at).asc())
                .offset(offset)
                .limit(per_page)
            )
            rows = list(result.scalars().all())
        return [_row_to_device(r) for r in rows], int(total)

    async def deregister(self, device_id: str) -> bool:
        self._require_db()
        factory = get_session_factory()
        async with factory() as session:
            existing = await session.get(IoTDeviceModel, device_id)
            if existing is None:
                return False
            await session.delete(existing)
            await self._write_event(session, device_id, "deregister")
            await session.commit()

        await self._cache.invalidate(device_id)
        logger.info(f"Device deregistered: {device_id}")
        return True

    async def update_message_stats(self, device_id: str) -> None:
        self._require_db()
        factory = get_session_factory()
        async with factory() as session:
            row = await session.get(IoTDeviceModel, device_id)
            if row is None:
                return
            row.last_message_at = datetime.now(timezone.utc)
            row.message_count += 1
            await session.commit()

        await self._cache.invalidate(device_id)

    async def record_event(
        self,
        device_id: str,
        event_type: str,
        topic: str | None = None,
        payload: dict | None = None,
    ) -> None:
        """Append an audit event row. Used by ProtocolBridge to persist
        acl_violation, message_sent, etc. into iot_device_events."""
        self._require_db()
        factory = get_session_factory()
        async with factory() as session:
            await self._write_event(session, device_id, event_type, topic, payload)
            await session.commit()

    async def recent_events(self, limit: int = 100) -> list[dict]:
        """Return the most recent audit entries as dicts."""
        self._require_db()
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(IoTDeviceEventModel)
                .order_by(
                    cast(ColumnElement[Any], IoTDeviceEventModel.timestamp).desc()
                )
                .limit(limit)
            )
            rows = list(result.scalars().all())

        out: list[dict] = []
        for r in rows:
            entry: dict[str, Any] = {
                "event": r.event_type,
                "device_id": r.device_id,
                "timestamp": r.timestamp.isoformat(),
            }
            if r.topic is not None:
                entry["topic"] = r.topic
            if r.payload_json:
                try:
                    extra = json.loads(r.payload_json)
                    if isinstance(extra, dict):
                        entry.update(extra)
                except json.JSONDecodeError:
                    pass
            out.append(entry)
        # Callers expect oldest-first within the returned window.
        out.reverse()
        return out


# ---------------------------------------------------------------------------
# Protocol Translators
# ---------------------------------------------------------------------------


@dataclass
class BridgedMessage:
    """A message that has been translated and sent through the bridge."""

    message_id: str
    device_id: str
    topic: str
    payload: Any
    protocol: ProtocolType
    status: str
    delivered_at: datetime
    native_response: dict | None = None


class MQTTTranslator:
    """
    Translates REST API calls into MQTT publish/subscribe operations.

    In production, this wraps aiomqtt for actual broker communication.
    Currently returns simulated responses for scaffold validation.
    """

    def __init__(self, broker_url: str, default_qos: int = 1):
        self.broker_url = broker_url
        self.default_qos = default_qos
        self._connected = False

    async def connect(self):
        """Connect to the MQTT broker."""
        # Production: self._client = aiomqtt.Client(self.broker_url)
        self._connected = True
        logger.info(f"MQTT translator connected to {self.broker_url}")

    async def publish(
        self,
        topic: str,
        payload: dict | str,
        qos: int | None = None,
        retain: bool = False,
    ) -> dict:
        """Publish a message to the MQTT broker."""
        if isinstance(payload, dict):
            payload_bytes = json.dumps(payload).encode()
        else:
            payload_bytes = payload.encode()

        # Production: await self._client.publish(
        #     topic, payload_bytes, qos=qos or self.default_qos
        # )
        logger.info(
            f"MQTT publish: {topic} ({len(payload_bytes)} bytes, "
            f"qos={qos or self.default_qos})"
        )

        return {
            "ack": True,
            "broker": self.broker_url,
            "topic": topic,
            "payload_size": len(payload_bytes),
            "qos": qos or self.default_qos,
            "retained": retain,
        }

    async def subscribe(self, topic: str, qos: int | None = None) -> str:
        """Subscribe to a topic. Returns a subscription ID."""
        subscription_id = str(uuid.uuid4())
        # Production: await self._client.subscribe(topic, qos=qos or self.default_qos)
        logger.info(f"MQTT subscribe: {topic} → {subscription_id}")
        return subscription_id


class CoAPTranslator:
    """CoAP protocol translator stub. Wire up aiocoap in production."""

    async def get(self, uri: str) -> dict:
        return {"status": "2.05", "payload": {}, "uri": uri}

    async def put(self, uri: str, payload: dict) -> dict:
        return {"status": "2.04", "payload": payload, "uri": uri}


# ---------------------------------------------------------------------------
# Bridge Orchestrator
# ---------------------------------------------------------------------------


class ProtocolBridge:
    """
    Main orchestrator: routes messages through the correct protocol translator
    after ACL validation.
    """

    def __init__(self, mqtt_broker_url: str, mqtt_default_qos: int = 1):
        self.registry = DeviceRegistry()
        self.acl_engine = TopicACLEngine()
        self.mqtt = MQTTTranslator(mqtt_broker_url, mqtt_default_qos)
        self.coap = CoAPTranslator()

    async def initialize(self):
        """Start protocol connections."""
        await self.mqtt.connect()
        logger.info("Protocol bridge initialized")

    async def send_message(
        self,
        device_id: str,
        topic: str,
        payload: dict | str,
        qos: int = 1,
        retain: bool = False,
    ) -> BridgedMessage:
        """
        Send a message through the bridge with full ACL enforcement.
        """
        require_simulation("iot_bridge")
        device = await self.registry.get(device_id)
        if not device:
            raise ValueError(f"Device '{device_id}' not found")

        # ACL check
        if not self.acl_engine.check(device.topic_acl, topic, ACLPermission.WRITE):
            await self.registry.record_event(
                device_id,
                "acl_violation",
                topic=topic,
                payload={"action": "write"},
            )
            raise ACLViolation(device_id, topic, "write")

        # Route to correct translator
        native_response = None
        if device.protocol == ProtocolType.MQTT:
            native_response = await self.mqtt.publish(topic, payload, qos, retain)
        elif device.protocol == ProtocolType.COAP:
            payload_data = payload if isinstance(payload, dict) else {"data": payload}
            native_response = await self.coap.put(topic, payload_data)
        else:
            # For protocols without a translator yet, log and simulate
            native_response = {"status": "simulated", "protocol": device.protocol.value}

        await self.registry.update_message_stats(device_id)

        message = BridgedMessage(
            message_id=str(uuid.uuid4()),
            device_id=device_id,
            topic=topic,
            payload=payload,
            protocol=device.protocol,
            status="delivered",
            delivered_at=datetime.now(timezone.utc),
            native_response=native_response,
        )

        await self.registry.record_event(
            device_id,
            "message_sent",
            topic=topic,
            payload={
                "message_id": message.message_id,
                "protocol": device.protocol.value,
            },
        )

        return message

    async def subscribe(self, device_id: str, topic: str) -> dict:
        """Subscribe to device messages with ACL enforcement."""
        require_simulation("iot_bridge")
        device = await self.registry.get(device_id)
        if not device:
            raise ValueError(f"Device '{device_id}' not found")

        if not self.acl_engine.check(device.topic_acl, topic, ACLPermission.READ):
            await self.registry.record_event(
                device_id,
                "acl_violation",
                topic=topic,
                payload={"action": "read"},
            )
            raise ACLViolation(device_id, topic, "read")

        subscription_id = await self.mqtt.subscribe(topic)

        return {
            "subscription_id": subscription_id,
            "device_id": device_id,
            "topic": topic,
            "status": "active",
        }

    async def get_audit_log(self, limit: int = 100) -> list[dict]:
        """Return recent audit log entries from iot_device_events."""
        return await self.registry.recent_events(limit=limit)
