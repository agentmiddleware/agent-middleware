"""Ported agent-middleware-api migrations 047-050 on this repo's chain."""

import asyncio

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

RECONCILE_COLUMNS = {
    "reconcile_status",
    "reconcile_lease_owner",
    "reconcile_lease_expires_at",
    "reconcile_next_probe_at",
    "reconcile_probe_count",
    "reconciled_at",
}
RECONCILE_INDEXES = {
    "ix_mcp_dispatch_attempts_reconcile_status",
    "ix_mcp_dispatch_attempts_reconcile_lease_expires_at",
    "ix_mcp_dispatch_attempts_reconcile_next_probe_at",
}


def _config(tmp_path, monkeypatch):
    db_path = tmp_path / "port.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    return Config("alembic.ini"), f"sqlite:///{db_path}"


def _upgrade(config, target):
    command.upgrade(config, target)
    asyncio.set_event_loop(asyncio.new_event_loop())


def _downgrade(config, target):
    command.downgrade(config, target)
    asyncio.set_event_loop(asyncio.new_event_loop())


def test_single_head_and_ported_order():
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert len(script.get_heads()) == 1
    chain = {
        "047_dispatch_reconcile_lease": "046_api_key_usage_events",
        "048_tenant_rls": "047_dispatch_reconcile_lease",
        "049_demo_key_attribution": "048_tenant_rls",
        "050_demo_key_first_used": "049_demo_key_attribution",
    }
    for revision, down in chain.items():
        assert script.get_revision(revision).down_revision == down


def test_fresh_upgrade_to_head_and_round_trip(tmp_path, monkeypatch):
    config, sync_url = _config(tmp_path, monkeypatch)
    # 051 refuses SQLite downgrade, so the round trip runs at the ported 050.
    _upgrade(config, "050_demo_key_first_used")
    engine = create_engine(sync_url)
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("mcp_dispatch_attempts")}
    assert RECONCILE_COLUMNS <= cols
    idx = {i["name"] for i in insp.get_indexes("mcp_dispatch_attempts")}
    assert RECONCILE_INDEXES <= idx
    assert "demo_key_funnel_by_source" in insp.get_view_names()
    for table in (
        "receipt_resolutions",
        "approval_gate_approvals",
        "api_key_usage_events",
    ):
        assert table in insp.get_table_names()
    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            == "050_demo_key_first_used"
        )
    engine.dispose()

    _downgrade(config, "046_api_key_usage_events")
    engine = create_engine(sync_url)
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("mcp_dispatch_attempts")}
    assert not (RECONCILE_COLUMNS & cols)
    # 049/050 leave 043's columns and view in place.
    key_cols = {c["name"] for c in insp.get_columns("api_keys")}
    assert {"utm_source", "referrer", "first_used_at"} <= key_cols
    assert "demo_key_funnel_by_source" in insp.get_view_names()
    engine.dispose()

    _upgrade(config, "head")
    engine = create_engine(sync_url)
    cols = {c["name"] for c in inspect(engine).get_columns("mcp_dispatch_attempts")}
    assert RECONCILE_COLUMNS <= cols
    engine.dispose()


def test_upgrade_at_head_is_noop(tmp_path, monkeypatch):
    config, sync_url = _config(tmp_path, monkeypatch)
    _upgrade(config, "head")
    _upgrade(config, "head")
    engine = create_engine(sync_url)
    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            == ScriptDirectory.from_config(config).get_current_head()
        )
    engine.dispose()
