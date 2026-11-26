"""Lifespan shutdown must not pretend a fixed sleep drains requests."""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.anyio
async def test_shutdown_closes_resources_without_a_fixed_drain_sleep(monkeypatch):
    """Shutdown closes the database and durable state, then returns.

    The old path slept two seconds and called that a request drain. Nothing
    counted in-flight requests, and the sleep ran after both resources were
    already closed. Uvicorn waits for connections before it enters this
    shutdown, so that sleep did not drain anything.
    """
    import app.main as main_module

    events: list[object] = []

    async def init_db() -> None:
        events.append("init_db")

    async def close_db() -> None:
        events.append("close_db")

    async def close_durable_state() -> None:
        events.append("close_durable_state")

    class _State:
        async def health_report(self) -> dict[str, object]:
            return {"backend": "memory", "enabled": False}

    async def register_configured_upstream_mcp(settings=None):
        return None

    async def recording_sleep(delay, result=None):
        events.append(("sleep", delay))
        # The cleanup loop waits 300 seconds. Block until shutdown cancels
        # it, instead of spinning into database work or waiting on the clock.
        if delay >= 60:
            await asyncio.Event().wait()
        return result

    monkeypatch.setattr(main_module, "init_db", init_db)
    monkeypatch.setattr(main_module, "close_db", close_db)
    monkeypatch.setattr(main_module, "close_durable_state", close_durable_state)
    monkeypatch.setattr(main_module, "get_durable_state", lambda: _State())
    monkeypatch.setattr(
        main_module, "sync_proof_surface_mcp_registration", lambda: None
    )
    monkeypatch.setattr(main_module.asyncio, "sleep", recording_sleep)
    monkeypatch.setattr(
        "app.services.upstream_mcp.register_configured_upstream_mcp",
        register_configured_upstream_mcp,
    )

    async with main_module.lifespan(main_module.app):
        events.append("ready")

    assert "ready" in events
    assert events.index("ready") < events.index("close_db")
    assert events.index("close_db") < events.index("close_durable_state")
    after_close = events[events.index("close_durable_state") + 1 :]
    assert ("sleep", 2) not in events
    assert not any(
        isinstance(item, tuple) and item[0] == "sleep" for item in after_close
    )
