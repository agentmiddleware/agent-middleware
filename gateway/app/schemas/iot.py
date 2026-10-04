"""
Schemas for the IoT Protocol Bridge service.
All models are Pydantic v2 for automatic OpenAPI generation.
"""

from pydantic import BaseModel, Field, field_validator
from enum import Enum
from datetime import datetime
from urllib.parse import urlsplit
import re

# RED TEAM FIX: Safe identifier pattern — blocks path traversal chars (/, .., \)
SAFE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")


class ProtocolType(str, Enum):
    """Supported legacy protocols for bridging."""

    MQTT = "mqtt"
    COAP = "coap"
    ZIGBEE = "zigbee"
    ZWAVE = "zwave"
    MODBUS = "modbus"
    BLUETOOTH_LE = "ble"


class ACLPermission(str, Enum):
    """Topic-level access control permissions."""

    READ = "read"
    WRITE = "write"
    READ_WRITE = "read_write"
    DENY = "deny"


class DeviceRegistration(BaseModel):
    """Register a new IoT device for protocol bridging."""

    device_id: str = Field(
        ...,
        description=(
            "Unique identifier for the device (e.g., MAC address or serial). "
            "Alphanumeric, hyphens, underscores, dots only."
        ),
        examples=["romo-vacuum-001"],
    )

    @field_validator("device_id", mode="before")
    @classmethod
    def validate_device_id(cls, v: object) -> str:
        """RED TEAM FIX: Block path traversal chars AND reject non-string types."""
        if not isinstance(v, str):
            raise ValueError("device_id must be a string, not " + type(v).__name__)
        if not SAFE_ID_PATTERN.match(v):
            raise ValueError(
                "device_id must be 1-128 characters, alphanumeric with "
                "hyphens/underscores/dots. Path separators (/, \\, ..) are "
                "forbidden."
            )
        return v

    protocol: ProtocolType = Field(
        ...,
        description="The legacy protocol this device communicates over.",
    )
    broker_url: str | None = Field(
        None,
        description=(
            "Override broker URL for this device. Uses system default if omitted."
        ),
        examples=["mqtt://192.168.1.50:1883"],
    )

    @field_validator("broker_url")
    @classmethod
    def validate_broker_url(cls, v: str | None) -> str | None:
        """Refuse credentials in broker_url: it is stored and cached verbatim.

        Any '@' (userinfo, with or without a scheme), query, or fragment is
        rejected rather than stripped, so a secret is never persisted.
        """
        if v is None:
            return v
        parsed = urlsplit(v)
        if "@" in v or parsed.query or parsed.fragment:
            raise ValueError(
                "broker_url must not embed credentials ('@'), a query string, "
                "or a fragment; use scheme://host[:port]."
            )
        return v

    topic_acl: dict[str, ACLPermission] = Field(
        default_factory=dict,
        description=(
            "Topic-level access control list. Maps topic patterns to permissions. "
            "CRITICAL: Prevents unauthorized access to sensitive device feeds "
            "(cameras, microphones, floor plans). Empty dict = deny-all default."
        ),
        examples=[
            {
                "device/+/telemetry": "read",
                "device/+/command": "write",
                "device/+/camera": "deny",
            }
        ],
    )
    metadata: dict = Field(
        default_factory=dict,
        description="Arbitrary key-value metadata for agent consumption.",
    )


class DeviceResponse(BaseModel):
    """Response after device registration or lookup."""

    device_id: str
    protocol: ProtocolType
    bridge_endpoint: str = Field(
        ...,
        description=(
            "The unified REST endpoint to interact with this device via the bridge."
        ),
        examples=["https://api.yourdomain.com/v1/iot/devices/romo-vacuum-001/messages"],
    )
    topic_acl: dict[str, ACLPermission]
    status: str = Field(default="registered")
    registered_at: datetime


class BridgeMessage(BaseModel):
    """Send a message to a device through the protocol bridge."""

    topic: str = Field(
        ...,
        description=(
            "The topic/channel to publish to. Must match an allowed ACL "
            "pattern. Path traversal sequences are rejected."
        ),
        examples=["device/romo-vacuum-001/command"],
    )

    @field_validator("topic")
    @classmethod
    def validate_topic(cls, v: str) -> str:
        """RED TEAM FIX: Block path traversal in MQTT topics."""
        if ".." in v or "\\" in v or v.startswith("/"):
            raise ValueError(
                "Topic must not contain path traversal sequences (.., \\) "
                "or start with /."
            )
        return v

    payload: dict | str = Field(
        ...,
        description="Message payload. Dict will be JSON-serialized; str sent as-is.",
    )
    qos: int = Field(
        default=1,
        ge=0,
        le=2,
        description=(
            "Quality of Service level (0=at most once, 1=at least once, "
            "2=exactly once)."
        ),
    )
    retain: bool = Field(
        default=False,
        description="Whether the broker should retain this message.",
    )


class BridgeMessageResponse(BaseModel):
    """Confirmation of a bridged message."""

    message_id: str
    device_id: str
    topic: str
    status: str = Field(default="delivered")
    delivered_at: datetime
    protocol_native_response: dict | None = Field(
        None,
        description="Raw response from the underlying protocol, if available.",
    )


class DeviceListResponse(BaseModel):
    """Paginated list of registered devices."""

    devices: list[DeviceResponse]
    total: int
    page: int
    per_page: int
