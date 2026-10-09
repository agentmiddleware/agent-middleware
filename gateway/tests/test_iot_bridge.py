"""Focused unit tests for the IoT protocol bridge service layer.

Covers the main public behavior of app.services.iot_bridge without any
network or database: topic ACL matching (including the deny-by-default
edge case), device row round-trips, the protocol translators, and the
ProtocolBridge orchestration paths with a stubbed registry.
"""

import json

import pytest

from app.schemas.iot import ACLPermission, ProtocolType
from app.services.iot_bridge import (
    ACLViolation,
    CoAPTranslator,
    MQTTTranslator,
    ProtocolBridge,
    RegisteredDevice,
    TopicACLEngine,
    _device_to_row,
    _row_to_device,
)
from app.db.models import IoTDeviceModel


def _device(**overrides) -> RegisteredDevice:
    kwargs = {
        "device_id": "dev-1",
        "protocol": ProtocolType.MQTT,
        "broker_url": "mqtt://broker:1883",
        "topic_acl": {"sensors/+/telemetry": ACLPermission.WRITE},
        "metadata": {"model": "sensor-v2"},
    }
    kwargs.update(overrides)
    return RegisteredDevice(**kwargs)


# ---------------------------------------------------------------------------
# TopicACLEngine
# ---------------------------------------------------------------------------


def test_empty_acl_denies_everything():
    assert TopicACLEngine.check({}, "sensors/a/telemetry", ACLPermission.WRITE) is False
    assert TopicACLEngine.check({}, "sensors/a/telemetry", ACLPermission.READ) is False


def test_plus_matches_single_level_only():
    acl = {"sensors/+/telemetry": ACLPermission.WRITE}
    assert TopicACLEngine.check(acl, "sensors/room/telemetry", ACLPermission.WRITE)
    assert not TopicACLEngine.check(
        acl, "sensors/room/floor/telemetry", ACLPermission.WRITE
    )


def test_hash_matches_remaining_levels():
    acl = {"control/#": ACLPermission.READ}
    assert TopicACLEngine.check(acl, "control/room/camera", ACLPermission.READ)
    assert not TopicACLEngine.check(acl, "control/room/camera", ACLPermission.WRITE)


def test_read_write_satisfies_either_direction():
    acl = {"device/commands": ACLPermission.READ_WRITE}
    assert TopicACLEngine.check(acl, "device/commands", ACLPermission.READ)
    assert TopicACLEngine.check(acl, "device/commands", ACLPermission.WRITE)


def test_unmatched_topic_is_denied():
    acl = {"sensors/+/telemetry": ACLPermission.READ_WRITE}
    assert not TopicACLEngine.check(acl, "other/topic", ACLPermission.READ)


# ---------------------------------------------------------------------------
# Row conversions
# ---------------------------------------------------------------------------


def test_device_row_round_trip_preserves_fields():
    device = _device(owner_wallet_id="wallet-1")
    restored = _row_to_device(_device_to_row(device))
    assert restored.device_id == device.device_id
    assert restored.protocol == device.protocol
    assert restored.topic_acl == device.topic_acl
    assert restored.owner_wallet_id == "wallet-1"


def test_client_supplied_owner_key_is_dropped():
    device = _device(
        metadata={"__owner_wallet_id": "attacker", "model": "x"},
        owner_wallet_id="real-owner",
    )
    row = _device_to_row(device)
    stored = json.loads(row.metadata_json)
    assert stored["__owner_wallet_id"] == "real-owner"
    restored = _row_to_device(row)
    assert restored.owner_wallet_id == "real-owner"
    assert "__owner_wallet_id" not in restored.metadata


def test_row_with_bad_protocol_defaults_to_mqtt():
    row = IoTDeviceModel(device_id="d", protocol="nope", topic_acl_json="{}")
    assert _row_to_device(row).protocol == ProtocolType.MQTT


def test_row_with_invalid_acl_entries_and_bad_json():
    row = IoTDeviceModel(
        device_id="d",
        protocol="mqtt",
        topic_acl_json='{"a/b": "write", "c/d": "bogus"}',
        metadata_json="not-json",
    )
    device = _row_to_device(row)
    assert device.topic_acl == {"a/b": ACLPermission.WRITE}
    assert device.metadata == {}


# ---------------------------------------------------------------------------
# Translators
# ---------------------------------------------------------------------------


async def test_mqtt_publish_dict_and_str_payloads():
    translator = MQTTTranslator("mqtt://broker:1883")
    await translator.connect()
    dict_response = await translator.publish("sensors/t", {"temp": 21})
    assert dict_response["ack"] is True
    assert dict_response["payload_size"] == len(json.dumps({"temp": 21}).encode())
    str_response = await translator.publish("sensors/t", "hi", qos=2, retain=True)
    assert str_response["qos"] == 2
    assert str_response["retained"] is True


async def test_mqtt_subscribe_returns_id():
    translator = MQTTTranslator("mqtt://broker:1883")
    subscription_id = await translator.subscribe("sensors/#")
    assert isinstance(subscription_id, str) and subscription_id


async def test_coap_get_and_put():
    coap = CoAPTranslator()
    assert (await coap.get("coap://dev/sensor"))["status"] == "2.05"
    response = await coap.put("coap://dev/sensor", {"on": True})
    assert response["status"] == "2.04"
    assert response["payload"] == {"on": True}


# ---------------------------------------------------------------------------
# ProtocolBridge orchestration (stubbed registry, no DB)
# ---------------------------------------------------------------------------


class _StubRegistry:
    def __init__(self, device=None):
        self.device = device
        self.events: list[tuple] = []
        self.stats_updated: list[str] = []

    async def get(self, device_id):
        return self.device

    async def record_event(self, device_id, event_type, topic=None, payload=None):
        self.events.append((device_id, event_type, topic, payload))

    async def update_message_stats(self, device_id):
        self.stats_updated.append(device_id)


def _bridge_with(device) -> tuple[ProtocolBridge, _StubRegistry]:
    bridge = ProtocolBridge(mqtt_broker_url="mqtt://broker:1883")
    stub = _StubRegistry(device)
    bridge.registry = stub
    return bridge, stub


async def test_send_message_mqtt_happy_path():
    bridge, stub = _bridge_with(_device())
    message = await bridge.send_message("dev-1", "sensors/room/telemetry", {"t": 1})
    assert message.status == "delivered"
    assert message.protocol == ProtocolType.MQTT
    assert message.native_response["ack"] is True
    assert stub.stats_updated == ["dev-1"]
    assert ("dev-1", "message_sent", "sensors/room/telemetry", message.message_id) == (
        stub.events[-1][0],
        stub.events[-1][1],
        stub.events[-1][2],
        stub.events[-1][3]["message_id"],
    )


async def test_send_message_unknown_device_raises_value_error():
    bridge, _stub = _bridge_with(None)
    with pytest.raises(ValueError, match="not found"):
        await bridge.send_message("ghost", "sensors/a/telemetry", {})


async def test_send_message_acl_denied_records_violation():
    bridge, stub = _bridge_with(_device())
    with pytest.raises(ACLViolation):
        await bridge.send_message("dev-1", "control/room/x", {})
    assert stub.events[-1][1] == "acl_violation"
    assert stub.stats_updated == []


async def test_send_message_empty_acl_denies_by_default():
    bridge, stub = _bridge_with(_device(topic_acl={}))
    with pytest.raises(ACLViolation):
        await bridge.send_message("dev-1", "sensors/room/telemetry", {})
    assert stub.events[-1][1] == "acl_violation"


async def test_send_message_coap_and_fallback_protocols():
    coap_device = _device(
        device_id="coap-1",
        protocol=ProtocolType.COAP,
        topic_acl={"sensors/+/telemetry": ACLPermission.WRITE},
    )
    bridge, _stub = _bridge_with(coap_device)
    coap_message = await bridge.send_message(
        "coap-1", "sensors/room/telemetry", "raw-string"
    )
    assert coap_message.native_response["status"] == "2.04"

    zigbee_device = _device(
        device_id="zig-1",
        protocol=ProtocolType.ZIGBEE,
        topic_acl={"sensors/+/telemetry": ACLPermission.WRITE},
    )
    bridge2, _stub2 = _bridge_with(zigbee_device)
    fallback = await bridge2.send_message("zig-1", "sensors/room/telemetry", {})
    assert fallback.native_response["status"] == "simulated"


async def test_subscribe_happy_path_and_acl_denied():
    read_device = _device(
        topic_acl={
            "sensors/+/telemetry": ACLPermission.WRITE,
            "control/#": ACLPermission.READ,
        }
    )
    bridge, _stub = _bridge_with(read_device)
    result = await bridge.subscribe("dev-1", "control/room/camera")
    assert result["status"] == "active"
    assert result["device_id"] == "dev-1"

    bridge2, stub2 = _bridge_with(read_device)
    with pytest.raises(ACLViolation):
        await bridge2.subscribe("dev-1", "sensors/room/telemetry")
    assert stub2.events[-1][1] == "acl_violation"


async def test_subscribe_unknown_device_raises_value_error():
    bridge, _stub = _bridge_with(None)
    with pytest.raises(ValueError, match="not found"):
        await bridge.subscribe("ghost", "control/x")
