"""
Tests for the IoT Protocol Bridge endpoints.
Validates device registration, ACL enforcement, and message bridging.
"""

import uuid

import pytest
from httpx import AsyncClient, ASGITransport
from app.core.config import get_settings
from app.main import app
from app.schemas.iot import ACLPermission, ProtocolType
from app.services.iot_bridge import DeviceRegistry, ProtocolBridge, RegisteredDevice


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def api_headers():
    return {"X-API-Key": "test-key"}


@pytest.fixture
def sample_device():
    return {
        "device_id": "test-sensor-001",
        "protocol": "mqtt",
        "topic_acl": {
            "device/+/telemetry": "read",
            "device/+/command": "write",
            "device/+/camera": "deny",
        },
        "metadata": {"location": "warehouse-A"},
    }


# --- Device Registration ---


@pytest.mark.anyio
async def test_register_device(client, api_headers, sample_device):
    resp = await client.post("/v1/iot/devices", json=sample_device, headers=api_headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["device_id"] == "test-sensor-001"
    assert data["protocol"] == "mqtt"
    assert data["status"] == "registered"
    assert "/messages" in data["bridge_endpoint"]


@pytest.mark.anyio
async def test_register_duplicate_device(client, api_headers, sample_device):
    # First registration
    await client.post("/v1/iot/devices", json=sample_device, headers=api_headers)
    # Duplicate should fail
    resp = await client.post("/v1/iot/devices", json=sample_device, headers=api_headers)
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "device_exists"


@pytest.mark.anyio
async def test_list_devices(client, api_headers, sample_device):
    await client.post("/v1/iot/devices", json=sample_device, headers=api_headers)
    resp = await client.get("/v1/iot/devices", headers=api_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert len(data["devices"]) >= 1


@pytest.mark.anyio
async def test_get_device(client, api_headers, sample_device):
    await client.post("/v1/iot/devices", json=sample_device, headers=api_headers)
    resp = await client.get(
        f"/v1/iot/devices/{sample_device['device_id']}", headers=api_headers
    )
    assert resp.status_code == 200
    assert resp.json()["device_id"] == sample_device["device_id"]


@pytest.mark.anyio
async def test_get_nonexistent_device(client, api_headers):
    resp = await client.get("/v1/iot/devices/nonexistent", headers=api_headers)
    assert resp.status_code == 404


# --- Message Sending with ACL ---


@pytest.mark.anyio
async def test_send_message_allowed_topic(client, api_headers, sample_device):
    await client.post("/v1/iot/devices", json=sample_device, headers=api_headers)
    msg = {
        "topic": "device/test-sensor-001/command",
        "payload": {"action": "report_status"},
        "qos": 1,
    }
    resp = await client.post(
        f"/v1/iot/devices/{sample_device['device_id']}/messages",
        json=msg,
        headers=api_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "delivered"


@pytest.mark.anyio
async def test_send_message_denied_topic(client, api_headers, sample_device):
    await client.post("/v1/iot/devices", json=sample_device, headers=api_headers)
    msg = {
        "topic": "device/test-sensor-001/camera",
        "payload": {"action": "stream"},
        "qos": 1,
    }
    resp = await client.post(
        f"/v1/iot/devices/{sample_device['device_id']}/messages",
        json=msg,
        headers=api_headers,
    )
    assert resp.status_code == 403
    assert "acl" in resp.json()["detail"]["error"]


@pytest.mark.anyio
async def test_send_message_empty_acl_denies_all(client, api_headers):
    device = {
        "device_id": "locked-device",
        "protocol": "mqtt",
        "topic_acl": {},
    }
    await client.post("/v1/iot/devices", json=device, headers=api_headers)
    msg = {"topic": "any/topic", "payload": "test"}
    resp = await client.post(
        "/v1/iot/devices/locked-device/messages",
        json=msg,
        headers=api_headers,
    )
    assert resp.status_code == 403


# --- Auth ---


@pytest.mark.anyio
async def test_missing_api_key(client):
    resp = await client.get("/v1/iot/devices")
    assert resp.status_code == 401


# --- Registration input validation ---


def _unique_device_id(label: str = "dev") -> str:
    return f"{label}-{uuid.uuid4().hex[:12]}"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "broker_url",
    [
        "mqtt://user:pw@10.0.0.5:1883",
        "mqtt://token@10.0.0.5:1883",
        "user:pw@10.0.0.5:1883",
        "mqtt://10.0.0.5:1883?password=pw",
        "mqtt://10.0.0.5:1883#pw",
    ],
)
async def test_register_rejects_broker_url_with_credentials(
    client, api_headers, broker_url
):
    device_id = _unique_device_id("cred")
    resp = await client.post(
        "/v1/iot/devices",
        json={"device_id": device_id, "protocol": "mqtt", "broker_url": broker_url},
        headers=api_headers,
    )
    assert resp.status_code == 422
    # Nothing was persisted.
    got = await client.get(f"/v1/iot/devices/{device_id}", headers=api_headers)
    assert got.status_code == 404


@pytest.mark.anyio
async def test_register_accepts_plain_broker_url(client, api_headers):
    resp = await client.post(
        "/v1/iot/devices",
        json={
            "device_id": _unique_device_id("plain"),
            "protocol": "mqtt",
            "broker_url": "mqtt://10.0.0.5:1883",
        },
        headers=api_headers,
    )
    assert resp.status_code == 201


# --- Subscribe ---


async def _register_subscribable(client, api_headers) -> tuple[str, str]:
    device_id = _unique_device_id("sub")
    topic = f"device/{device_id}/telemetry"
    resp = await client.post(
        "/v1/iot/devices",
        json={
            "device_id": device_id,
            "protocol": "mqtt",
            "topic_acl": {topic: "read", f"device/{device_id}/camera": "deny"},
        },
        headers=api_headers,
    )
    assert resp.status_code == 201
    return device_id, topic


@pytest.mark.anyio
async def test_subscribe_websocket_url_derived_from_request(
    client, api_headers, monkeypatch
):
    monkeypatch.setattr(get_settings(), "PUBLIC_URL", "")
    device_id, topic = await _register_subscribable(client, api_headers)
    resp = await client.post(
        f"/v1/iot/devices/{device_id}/subscribe",
        params={"topic": topic},
        headers=api_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "yourdomain" not in resp.text
    assert body["websocket_url"] == (
        f"ws://test/v1/iot/subscriptions/{body['subscription_id']}/ws"
    )


@pytest.mark.anyio
async def test_subscribe_websocket_url_derived_from_public_url(
    client, api_headers, monkeypatch
):
    monkeypatch.setattr(get_settings(), "PUBLIC_URL", "https://api.example.com/")
    device_id, topic = await _register_subscribable(client, api_headers)
    resp = await client.post(
        f"/v1/iot/devices/{device_id}/subscribe",
        params={"topic": topic},
        headers=api_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["websocket_url"] == (
        f"wss://api.example.com/v1/iot/subscriptions/{body['subscription_id']}/ws"
    )


@pytest.mark.anyio
async def test_subscribe_denied_topic_records_acl_violation(client, api_headers):
    device_id, _ = await _register_subscribable(client, api_headers)
    denied_topic = f"device/{device_id}/camera"
    resp = await client.post(
        f"/v1/iot/devices/{device_id}/subscribe",
        params={"topic": denied_topic},
        headers=api_headers,
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "acl_denied"

    events = await DeviceRegistry().recent_events(limit=100)
    assert any(
        e["event"] == "acl_violation"
        and e["device_id"] == device_id
        and e.get("topic") == denied_topic
        and e.get("action") == "read"
        for e in events
    )


@pytest.mark.anyio
async def test_subscribe_refused_when_iot_simulation_disabled(monkeypatch):
    device_id = _unique_device_id("sim")
    topic = f"device/{device_id}/telemetry"
    bridge = ProtocolBridge(mqtt_broker_url="mqtt://localhost:1883")
    await bridge.registry.register(
        RegisteredDevice(
            device_id=device_id,
            protocol=ProtocolType.MQTT,
            broker_url=None,
            topic_acl={topic: ACLPermission.READ},
            metadata={},
        )
    )
    monkeypatch.setattr(get_settings(), "SIMULATION_MODE_IOT_BRIDGE", False)
    # The stub MQTT subscribe must not hand out a fake subscription id once
    # the bridge is declared real, exactly like send_message.
    with pytest.raises(NotImplementedError):
        await bridge.subscribe(device_id, topic)
