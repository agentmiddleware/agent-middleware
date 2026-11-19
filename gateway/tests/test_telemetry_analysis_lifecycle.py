"""The /v1/telemetry anomaly detector runs from the app lifespan (#356).

``AutonomousPM.start_background_analysis`` used to have no caller, so the
telemetry proof surface never detected anything. The lifespan now starts it
where the telemetry router is mounted, and stops it on shutdown before the
database closes.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services.telemetry_pm import AutonomousPM


def _cfg(**overrides):
    values = {
        "ENABLE_PROOF_SURFACES": True,
        "DATABASE_URL": "sqlite+aiosqlite:///./test.db",
        "ENVIRONMENT": "local",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_analysis_gate_follows_proof_surfaces_database_and_environment():
    from app.main import telemetry_analysis_enabled

    assert telemetry_analysis_enabled(_cfg()) is True
    assert telemetry_analysis_enabled(_cfg(ENABLE_PROOF_SURFACES=False)) is False
    assert telemetry_analysis_enabled(_cfg(DATABASE_URL="")) is False
    assert telemetry_analysis_enabled(_cfg(ENVIRONMENT="test")) is False
    assert telemetry_analysis_enabled(_cfg(ENVIRONMENT=" Test ")) is False


@pytest.mark.anyio
async def test_start_is_idempotent_and_stop_waits_for_the_loop():
    pm = AutonomousPM()
    calls = 0
    first_pass = asyncio.Event()

    async def analyze():
        nonlocal calls
        calls += 1
        first_pass.set()
        return []

    pm.detector.analyze = analyze  # type: ignore[method-assign]

    await pm.start_background_analysis(interval_seconds=3600)
    task = pm._analysis_task
    await pm.start_background_analysis(interval_seconds=3600)
    assert pm._analysis_task is task, "a second start must not spawn a loop"

    await asyncio.wait_for(first_pass.wait(), timeout=5)
    assert calls == 1
    assert pm.background_analysis_running

    await pm.stop_background_analysis()
    assert task is not None and task.done()
    assert pm._analysis_task is None
    assert not pm.background_analysis_running
    # Stopping twice is harmless.
    await pm.stop_background_analysis()


def _patch_lifespan_dependencies(monkeypatch, events: list[object]):
    import app.main as main_module

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

    monkeypatch.setattr(main_module, "init_db", init_db)
    monkeypatch.setattr(main_module, "close_db", close_db)
    monkeypatch.setattr(main_module, "close_durable_state", close_durable_state)
    monkeypatch.setattr(main_module, "get_durable_state", lambda: _State())
    monkeypatch.setattr(
        main_module, "sync_proof_surface_mcp_registration", lambda: None
    )
    monkeypatch.setattr(
        "app.services.upstream_mcp.register_configured_upstream_mcp",
        register_configured_upstream_mcp,
    )
    return main_module


@pytest.mark.anyio
async def test_lifespan_runs_analysis_and_stops_it_before_closing_db(monkeypatch):
    events: list[object] = []
    main_module = _patch_lifespan_dependencies(monkeypatch, events)
    monkeypatch.setattr(main_module.settings, "ENABLE_PROOF_SURFACES", True)
    monkeypatch.setattr(main_module.settings, "ENVIRONMENT", "local")

    pm = AutonomousPM()
    first_pass = asyncio.Event()

    async def analyze():
        events.append("analyze")
        first_pass.set()
        return []

    pm.detector.analyze = analyze  # type: ignore[method-assign]
    real_stop = pm.stop_background_analysis

    async def stop():
        events.append("stop_analysis")
        await real_stop()

    pm.stop_background_analysis = stop  # type: ignore[method-assign]
    monkeypatch.setattr("app.core.dependencies.get_autonomous_pm", lambda: pm)

    async with main_module.lifespan(main_module.app):
        await asyncio.wait_for(first_pass.wait(), timeout=5)
        assert pm.background_analysis_running
        task = pm._analysis_task

    assert task is not None and task.done()
    assert not pm.background_analysis_running
    assert events.index("analyze") < events.index("stop_analysis")
    assert events.index("stop_analysis") < events.index("close_db")


@pytest.mark.anyio
async def test_lifespan_leaves_analysis_off_without_proof_surfaces(monkeypatch):
    events: list[object] = []
    main_module = _patch_lifespan_dependencies(monkeypatch, events)
    monkeypatch.setattr(main_module.settings, "ENABLE_PROOF_SURFACES", False)

    def unexpected():
        raise AssertionError("telemetry PM must not be built when disabled")

    monkeypatch.setattr("app.core.dependencies.get_autonomous_pm", unexpected)

    async with main_module.lifespan(main_module.app):
        events.append("ready")

    assert "ready" in events
