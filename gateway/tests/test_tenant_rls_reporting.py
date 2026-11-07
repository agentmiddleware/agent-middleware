"""TENANT_RLS_MODE reporting (startup log, /health) and 052 boot columns."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine


class _PostureLogged(Exception):
    pass


@pytest.mark.parametrize("mode", ["off", "enforce"])
async def test_startup_posture_line_reports_tenant_rls_mode(
    clean_database, monkeypatch, mode
):
    import app.main as main_module

    monkeypatch.setattr(main_module.settings, "TENANT_RLS_MODE", mode)
    recorded: list[dict] = []

    def info(event, **fields):
        recorded.append({"event": event, **fields})
        if event == "app_startup" and fields.get("phase") == "runtime_posture":
            raise _PostureLogged

    monkeypatch.setattr(main_module.logger, "info", info)
    with pytest.raises(_PostureLogged):
        async with main_module.lifespan(main_module.app):
            pytest.fail("lifespan should stop at the posture line in this test")
    posture = [r for r in recorded if r.get("phase") == "runtime_posture"]
    assert len(posture) == 1
    assert posture[0]["tenant_rls_mode"] == mode


@pytest.mark.parametrize("mode", ["off", "enforce"])
async def test_health_reports_tenant_rls_mode(clean_database, monkeypatch, mode):
    from app.core.config import get_settings
    from app.main import app

    monkeypatch.setattr(get_settings(), "TENANT_RLS_MODE", mode)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["tenant_rls_mode"] == mode


def test_tenant_rls_mode_default_stays_off():
    from app.core.config import Settings

    assert Settings().TENANT_RLS_MODE == "off"


def test_receipt_mode_build_columns_are_required_at_boot():
    from app.db.database import REQUIRED_TRUST_COLUMNS

    assert {"enforcement_mode", "gateway_build"} <= REQUIRED_TRUST_COLUMNS["receipts"]


async def test_boot_check_names_missing_receipt_mode_build_columns():
    from app.db.database import _missing_required_columns

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                "CREATE TABLE receipts (id TEXT PRIMARY KEY, "
                "action_contract_version TEXT, action_payload_hash TEXT, "
                "action_schema_id TEXT, action_schema_version TEXT, "
                "action_public_tool_id TEXT, action_upstream_binding_hash TEXT)"
            )
            missing = await conn.run_sync(_missing_required_columns)
    finally:
        await engine.dispose()
    assert {"receipts.enforcement_mode", "receipts.gateway_build"} <= missing
