"""053 restores 044-046 objects missing from databases built by the api chain."""

import asyncio

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

TABLES = (
    "receipt_resolutions",
    "resolution_chain_heads",
    "approval_gate_approvals",
    "api_key_usage_events",
)
VIEW = "api_key_usage_daily"


def _config(tmp_path, monkeypatch):
    db_path = tmp_path / "repair.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    return Config("alembic.ini"), f"sqlite:///{db_path}"


def _run(fn, config, target):
    fn(config, target)
    asyncio.set_event_loop(asyncio.new_event_loop())


def _shape(sync_url):
    engine = create_engine(sync_url)
    insp = inspect(engine)
    shape = {
        name: (
            [
                (c["name"], str(c["type"]), c["nullable"])
                for c in insp.get_columns(name)
            ],
            sorted(
                (i["name"], tuple(i["column_names"]), bool(i["unique"]))
                for i in insp.get_indexes(name)
            ),
            sorted(str(c["sqltext"]) for c in insp.get_check_constraints(name)),
            sorted(
                (tuple(f["constrained_columns"]), f["referred_table"])
                for f in insp.get_foreign_keys(name)
            ),
        )
        for name in TABLES
    }
    views = set(insp.get_view_names())
    engine.dispose()
    return shape, views


def test_053_follows_052():
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    rev = script.get_revision("053_repair_api_lineage")
    assert rev.down_revision == "052_receipt_mode_build"
    assert len(script.get_heads()) == 1


def test_fresh_chain_upgrade_is_noop_for_053(tmp_path, monkeypatch):
    config, sync_url = _config(tmp_path, monkeypatch)
    _run(command.upgrade, config, "052_receipt_mode_build")
    before = _shape(sync_url)
    _run(command.upgrade, config, "053_repair_api_lineage")
    assert _shape(sync_url) == before


def test_missing_objects_are_recreated_with_main_shape(tmp_path, monkeypatch):
    config, sync_url = _config(tmp_path, monkeypatch)
    _run(command.upgrade, config, "052_receipt_mode_build")
    expected = _shape(sync_url)
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        conn.execute(text(f"DROP VIEW {VIEW}"))
        for name in (
            "receipt_resolutions",
            "resolution_chain_heads",
            "approval_gate_approvals",
            "api_key_usage_events",
        ):
            conn.execute(text(f"DROP TABLE {name}"))
    engine.dispose()

    _run(command.upgrade, config, "head")
    shape, views = _shape(sync_url)
    assert shape == expected[0]
    assert VIEW in views
    # A second upgrade is a no-op and downgrade leaves every object in place.
    _run(command.upgrade, config, "head")
    _run(command.downgrade, config, "052_receipt_mode_build")
    shape, views = _shape(sync_url)
    assert shape == expected[0] and VIEW in views
