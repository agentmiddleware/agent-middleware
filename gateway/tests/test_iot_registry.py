"""
Tests for the PG-backed DeviceRegistry + optional Redis cache layer
(issue #32).

test_iot.py covers the HTTP surface (9 tests). These exercise the
registry directly: state persistence, audit event shape, list
pagination, cache hit/miss behavior with a fake Redis.
"""

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.db.database import get_session_factory
from app.db.models import IoTDeviceEventModel, IoTDeviceModel
from app.schemas.iot import ACLPermission, ProtocolType
from app.services.iot_bridge import (
    DeviceRegistry,
    RegisteredDevice,
)


@pytest_asyncio.fixture(autouse=True)
async def _clean_iot_tables():
    factory = get_session_factory()
    async with factory() as session:
        await session.execute(delete(IoTDeviceEventModel))
        await session.execute(delete(IoTDeviceModel))
        await session.commit()
    yield


def _device(device_id: str = "dev-1") -> RegisteredDevice:
    return RegisteredDevice(
        device_id=device_id,
        protocol=ProtocolType.MQTT,
        broker_url="mqtt://broker:1883",
        topic_acl={
            "sensors/+/telemetry": ACLPermission.WRITE,
            "control/#": ACLPermission.READ,
        },
        metadata={"tenant": "acme", "model": "sensor-v2"},
    )


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_register_round_trip_preserves_acl_and_metadata():
    reg = DeviceRegistry()
    await reg.register(_device())

    got = await reg.get("dev-1")
    assert got is not None
    assert got.protocol == ProtocolType.MQTT
    assert got.broker_url == "mqtt://broker:1883"
    assert got.topic_acl == {
        "sensors/+/telemetry": ACLPermission.WRITE,
        "control/#": ACLPermission.READ,
    }
    assert got.metadata == {"tenant": "acme", "model": "sensor-v2"}


@pytest.mark.anyio
async def test_register_duplicate_raises():
    reg = DeviceRegistry()
    await reg.register(_device())
    with pytest.raises(ValueError, match="already registered"):
        await reg.register(_device())


@pytest.mark.anyio
async def test_deregister_removes_device_and_returns_true():
    reg = DeviceRegistry()
    await reg.register(_device())
    assert await reg.deregister("dev-1") is True
    assert await reg.get("dev-1") is None
    assert await reg.deregister("dev-1") is False


@pytest.mark.anyio
async def test_list_all_paginates():
    reg = DeviceRegistry()
    for i in range(5):
        await reg.register(_device(device_id=f"dev-{i}"))

    page1, total = await reg.list_all(page=1, per_page=2)
    page2, _ = await reg.list_all(page=2, per_page=2)
    page3, _ = await reg.list_all(page=3, per_page=2)

    assert total == 5
    assert len(page1) == 2
    assert len(page2) == 2
    assert len(page3) == 1
    all_ids = {d.device_id for d in page1 + page2 + page3}
    assert all_ids == {f"dev-{i}" for i in range(5)}


@pytest.mark.anyio
async def test_update_message_stats_increments_counter():
    reg = DeviceRegistry()
    await reg.register(_device())

    for _ in range(3):
        await reg.update_message_stats("dev-1")

    got = await reg.get("dev-1")
    assert got is not None
    assert got.message_count == 3
    assert got.last_message_at is not None


# ---------------------------------------------------------------------------
# Audit events
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_register_writes_register_event():
    reg = DeviceRegistry()
    await reg.register(_device())

    events = await reg.recent_events(limit=10)
    assert any(e["event"] == "register" and e["device_id"] == "dev-1" for e in events)


@pytest.mark.anyio
async def test_record_event_custom_payload_round_trips():
    reg = DeviceRegistry()
    await reg.register(_device())
    await reg.record_event(
        "dev-1",
        "acl_violation",
        topic="forbidden/topic",
        payload={"action": "write", "correlation_id": "req-42"},
    )

    events = await reg.recent_events(limit=10)
    violations = [e for e in events if e["event"] == "acl_violation"]
    assert len(violations) == 1
    v = violations[0]
    assert v["topic"] == "forbidden/topic"
    assert v["action"] == "write"
    assert v["correlation_id"] == "req-42"


@pytest.mark.anyio
async def test_recent_events_window_ordering():
    reg = DeviceRegistry()
    await reg.register(_device("dev-1"))
    await reg.record_event("dev-1", "message_sent", topic="a")
    await reg.record_event("dev-1", "message_sent", topic="b")
    await reg.record_event("dev-1", "message_sent", topic="c")

    events = await reg.recent_events(limit=2)
    # Returns the two most-recent events, oldest-first within the window.
    topics = [e.get("topic") for e in events]
    assert topics == ["b", "c"]


# ---------------------------------------------------------------------------
# Cache layer
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Minimal stand-in for redis.asyncio that records calls."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []

    async def ping(self):
        return True

    async def get(self, key):
        self.calls.append(("get", key))
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.calls.append(("set", key))
        self.store[key] = value
        return True

    async def delete(self, *keys):
        for k in keys:
            self.calls.append(("delete", k))
            self.store.pop(k, None)
        return len(keys)


@pytest.mark.anyio
async def test_get_populates_cache_and_second_read_skips_db(monkeypatch):
    """First get hits PG + writes cache; the second reads from cache.
    Verified by breaking the session factory after the first call —
    the second still succeeds."""
    reg = DeviceRegistry()
    fake = _FakeRedis()
    reg._cache._client = fake
    reg._cache._disabled = False

    await reg.register(_device())

    first = await reg.get("dev-1")
    assert first is not None
    assert ("set", "iot:device:dev-1") in fake.calls

    from app.services import iot_bridge as iot_mod

    def _boom():
        raise AssertionError("session factory should not be called on cache hit")

    monkeypatch.setattr(iot_mod, "get_session_factory", _boom)

    second = await reg.get("dev-1")
    assert second is not None
    assert second.device_id == "dev-1"


@pytest.mark.anyio
async def test_cache_disabled_without_redis_url(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    reg = DeviceRegistry()
    assert reg._cache._disabled is True

    await reg.register(_device())
    got = await reg.get("dev-1")
    assert got is not None


@pytest.mark.anyio
async def test_deregister_invalidates_cache():
    reg = DeviceRegistry()
    fake = _FakeRedis()
    reg._cache._client = fake
    reg._cache._disabled = False

    await reg.register(_device())
    await reg.get("dev-1")  # populates cache
    assert "iot:device:dev-1" in fake.store

    await reg.deregister("dev-1")
    assert "iot:device:dev-1" not in fake.store


class _FailingDeleteRedis(_FakeRedis):
    """Redis that serves reads but drops DEL (a transient outage)."""

    async def delete(self, *keys):
        raise ConnectionError("redis DEL failed")


def _registry_on(fake) -> DeviceRegistry:
    reg = DeviceRegistry()
    reg._cache._client = fake
    reg._cache._disabled = False
    return reg


@pytest.mark.anyio
async def test_failed_invalidate_does_not_serve_stale_device(monkeypatch, caplog):
    from app.services.iot_bridge import _DeviceCache

    # Process-wide state: make sure it starts clear and is restored after.
    monkeypatch.setattr(_DeviceCache, "_reads_suspended_until", 0.0, raising=False)
    fake = _FailingDeleteRedis()
    reg = _registry_on(fake)

    await reg.register(_device())
    await reg.get("dev-1")  # populates cache
    assert "iot:device:dev-1" in fake.store

    with caplog.at_level("WARNING", logger="app.services.iot_bridge"):
        assert await reg.deregister("dev-1") is True

    # DEL failed, so the deregistered device is still in Redis. Neither this
    # registry nor a fresh one (each request builds its own bridge) may serve
    # it from the cache: PG is the source of truth.
    assert "iot:device:dev-1" in fake.store
    assert await reg.get("dev-1") is None
    assert await _registry_on(fake).get("dev-1") is None
    # The failure is surfaced, not swallowed.
    assert any("invalidate" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


def _owned(device_id: str, owner: str | None) -> RegisteredDevice:
    device = _device(device_id)
    device.owner_wallet_id = owner
    return device


@pytest.mark.anyio
async def test_owner_round_trips_and_stays_out_of_metadata():
    reg = DeviceRegistry()
    await reg.register(_owned("dev-1", "wallet-a"))

    got = await reg.get("dev-1")
    assert got is not None
    assert got.owner_wallet_id == "wallet-a"
    assert got.metadata == {"tenant": "acme", "model": "sensor-v2"}


@pytest.mark.anyio
async def test_client_metadata_cannot_set_owner():
    from app.services.iot_bridge import _OWNER_METADATA_KEY

    reg = DeviceRegistry()
    device = _owned("dev-1", None)
    device.metadata = {**device.metadata, _OWNER_METADATA_KEY: "wallet-victim"}
    await reg.register(device)

    got = await reg.get("dev-1")
    assert got is not None
    assert got.owner_wallet_id is None
    assert _OWNER_METADATA_KEY not in got.metadata


@pytest.mark.anyio
async def test_list_all_scoped_to_owner_paginates():
    reg = DeviceRegistry()
    for i in range(3):
        await reg.register(_owned(f"a-{i}", "wallet-a"))
    for i in range(2):
        await reg.register(_owned(f"b-{i}", "wallet-b"))
    await reg.register(_owned("ownerless", None))

    page1, total = await reg.list_all(page=1, per_page=2, owner_wallet_id="wallet-a")
    page2, total2 = await reg.list_all(page=2, per_page=2, owner_wallet_id="wallet-a")
    assert total == total2 == 3
    assert len(page1) == 2 and len(page2) == 1
    assert {d.device_id for d in page1 + page2} == {"a-0", "a-1", "a-2"}

    _, unscoped_total = await reg.list_all(page=1, per_page=50)
    assert unscoped_total == 6
