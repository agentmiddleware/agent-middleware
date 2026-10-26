"""Startup must register the flag-gated dogfood tool (issue #176).

A fresh gateway with ENABLE_DOGFOOD_TOOL=true has to expose
``partner.notes.write`` in the local registry as soon as the lifespan
completes, without any discovery route (tools/list) running first.
"""

from __future__ import annotations

from starlette.testclient import TestClient

from app.core.config import get_settings
from app.main import app
from app.services import dogfood_tool
from app.services.service_registry import get_service_registry


def test_lifespan_registers_dogfood_tool_without_discovery(
    clean_database, monkeypatch, tmp_path
):
    monkeypatch.setenv("ENABLE_DOGFOOD_TOOL", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(dogfood_tool, "DOGFOOD_NOTES_PATH", tmp_path / "notes.jsonl")
    registry = get_service_registry()
    missing = object()
    tool_id = dogfood_tool.DOGFOOD_TOOL_ID
    saved_entries = (
        (registry._local_registry, registry._local_registry.get(tool_id, missing)),
        (registry._func_registry, registry._func_registry.get(tool_id, missing)),
        (
            registry._executor_registry,
            registry._executor_registry.get(tool_id, missing),
        ),
    )
    saved_registered = dogfood_tool._registered
    saved_note_count = dogfood_tool._note_count
    # Start from an unregistered state so other tests cannot satisfy the assert.
    try:
        dogfood_tool.unregister_dogfood_tool()
        registry.unregister_local(dogfood_tool.DOGFOOD_TOOL_ID)
        assert registry.get_local(dogfood_tool.DOGFOOD_TOOL_ID) is None

        with TestClient(app):
            # No tools/list or other discovery call: only the lifespan ran.
            assert registry.get_local(dogfood_tool.DOGFOOD_TOOL_ID) is not None
    finally:
        dogfood_tool.unregister_dogfood_tool()
        monkeypatch.setenv("ENABLE_DOGFOOD_TOOL", "false")
        get_settings.cache_clear()
        for entries, saved_value in saved_entries:
            if saved_value is missing:
                entries.pop(tool_id, None)
            else:
                entries[tool_id] = saved_value
        dogfood_tool._registered = saved_registered
        dogfood_tool._note_count = saved_note_count
