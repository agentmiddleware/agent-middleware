"""HTTP readiness reflects the required database and durable state."""

import asyncio
from types import SimpleNamespace
import pytest
from httpx import ASGITransport, AsyncClient
from app import main
from app.core import health


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "database",
        "state_down",
        "state_error",
        "state_timeout",
        "database_timeout",
    ],
)
async def test_readiness_dependency_failures(monkeypatch, clean_database, fault):
    async def state_report():
        if fault == "state_error":
            raise ConnectionError("synthetic private state details")
        if fault == "state_timeout":
            await asyncio.Event().wait()
        return {"ok": fault != "state_down", "backend": "memory"}

    monkeypatch.setattr(
        main, "get_durable_state", lambda: SimpleNamespace(health_report=state_report)
    )
    monkeypatch.setattr(main, "CHECK_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(health, "CHECK_TIMEOUT_SECONDS", 0.01)
    if fault in ("database", "database_timeout"):

        class Connection:
            async def __aenter__(self):
                if fault == "database_timeout":
                    await asyncio.Event().wait()
                raise ConnectionError("synthetic private database details")

            async def __aexit__(self, *args):
                pass

        import app.db.database

        monkeypatch.setattr(
            app.db.database, "get_engine", lambda: SimpleNamespace(connect=Connection)
        )
    async with AsyncClient(
        transport=ASGITransport(app=main.app), base_url="http://test"
    ) as client:
        result = await client.get("/health/ready")
    assert result.status_code == (200 if fault == "none" else 503)
    assert result.json()["status"] == ("ready" if fault == "none" else "not_ready")
    assert "private" not in result.text
    if fault.startswith("database"):
        assert result.json()["checks"]["database"]["status"] == "down"
