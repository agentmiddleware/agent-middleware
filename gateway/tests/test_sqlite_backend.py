"""
Tests for SQLite backend in durable state store.
"""

import os
import tempfile

import pytest

from app.core.durable_state import DurableStateStore
from app.core.config import get_settings
from app.services import agent_comms
from app.services.agent_comms import (
    AgentMessage,
    AgentRegistry,
    MessagePriority,
    MessageRouter,
    MessageType,
)


@pytest.fixture(autouse=True)
def reset_settings():
    """Reset settings cache before each test."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def sqlite_store():
    """Create a SQLite-based DurableStateStore for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    os.environ["SQLITE_URL"] = db_path
    os.environ["STATE_BACKEND"] = "sqlite"
    os.environ["DATABASE_URL"] = ""
    os.environ["REDIS_URL"] = ""

    store = DurableStateStore()
    yield store

    import asyncio

    asyncio.run(store.close())

    try:
        os.unlink(db_path)
    except FileNotFoundError:
        pass


@pytest.mark.asyncio
async def test_sqlite_backend_save_and_load(sqlite_store):
    """Test saving and loading JSON data with SQLite backend."""
    await sqlite_store._ensure_ready()
    assert sqlite_store.backend == "sqlite"

    test_data = {"key": "value", "number": 42, "nested": {"a": 1, "b": 2}}
    await sqlite_store.save_json("test_key", test_data)

    loaded = await sqlite_store.load_json("test_key")
    assert loaded == test_data


@pytest.mark.asyncio
async def test_sqlite_backend_delete(sqlite_store):
    """Test deleting a key from SQLite backend."""
    await sqlite_store._ensure_ready()

    await sqlite_store.save_json("delete_test", {"data": "test"})
    loaded = await sqlite_store.load_json("delete_test")
    assert loaded is not None

    await sqlite_store.delete("delete_test")
    loaded = await sqlite_store.load_json("delete_test")
    assert loaded is None


@pytest.mark.asyncio
async def test_sqlite_backend_health(sqlite_store):
    """Test SQLite backend health report."""
    await sqlite_store._ensure_ready()

    health = await sqlite_store.health_report()
    assert health["ok"] is True
    assert health["backend"] == "sqlite"
    assert health["enabled"] is True


@pytest.mark.asyncio
async def test_sqlite_backend_overwrite(sqlite_store):
    """Test overwriting existing key in SQLite backend."""
    await sqlite_store._ensure_ready()

    await sqlite_store.save_json("overwrite_key", {"version": 1})
    await sqlite_store.save_json("overwrite_key", {"version": 2})

    loaded = await sqlite_store.load_json("overwrite_key")
    assert loaded == {"version": 2}


@pytest.mark.asyncio
async def test_sqlite_backend_multiple_keys(sqlite_store):
    """Test storing multiple keys in SQLite backend."""
    await sqlite_store._ensure_ready()

    data = {
        "key1": {"data": "value1"},
        "key2": {"data": "value2"},
        "key3": {"data": "value3"},
    }

    for k, v in data.items():
        await sqlite_store.save_json(k, v)

    for k, v in data.items():
        loaded = await sqlite_store.load_json(k)
        assert loaded == v


@pytest.mark.asyncio
async def test_sqlite_backend_list_keys_by_prefix(sqlite_store):
    """State services can enumerate row-keyed records without a global blob."""
    await sqlite_store._ensure_ready()

    await sqlite_store.save_json("comms.inbox.agent-a.msg-1", {"message": 1})
    await sqlite_store.save_json("comms.inbox.agent-a.msg-2", {"message": 2})
    await sqlite_store.save_json("comms.inbox.agent-b.msg-3", {"message": 3})

    keys = await sqlite_store.list_keys("comms.inbox.agent-a.")

    assert keys == ["comms.inbox.agent-a.msg-1", "comms.inbox.agent-a.msg-2"]


@pytest.mark.asyncio
async def test_agent_comms_persists_messages_as_individual_rows(
    sqlite_store, monkeypatch
):
    """Sending one message must not rewrite the global inbox for all agents."""
    await sqlite_store._ensure_ready()
    monkeypatch.setattr(agent_comms, "get_durable_state", lambda: sqlite_store)

    registry = AgentRegistry()
    router = MessageRouter(registry)
    sender = await registry.register("sender", ["send"])
    receiver = await registry.register("receiver", ["receive"])

    message = AgentMessage(
        message_id="msg-row-keyed",
        from_agent=sender.agent_id,
        to_agent=receiver.agent_id,
        message_type=MessageType.REQUEST,
        priority=MessagePriority.NORMAL,
        subject="row keyed",
        body={"ok": True},
    )

    await router.send(message)

    inbox_keys = await sqlite_store.list_keys(f"comms.inbox.{receiver.agent_id}.")
    assert inbox_keys == [f"comms.inbox.{receiver.agent_id}.msg-row-keyed"]
    assert await sqlite_store.load_json("comms.inbox") is None

    second_router = MessageRouter(registry)
    polled = await second_router.poll(receiver.agent_id)

    assert [msg.message_id for msg in polled] == ["msg-row-keyed"]
