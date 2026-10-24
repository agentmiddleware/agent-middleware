"""SEL-346: API-key usage event schema and continuous daily percentiles."""

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text


def test_045_revision_and_daily_view(tmp_path, monkeypatch) -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_heads() == ["045_api_key_usage_events"]
    assert script.get_revision("045_api_key_usage_events").down_revision == (
        "044_receipt_resolutions"
    )

    db_path = tmp_path / "api-key-usage-migration.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    config = Config("alembic.ini")
    command.upgrade(config, "044_receipt_resolutions")
    command.upgrade(config, "045_api_key_usage_events")

    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    assert "api_key_usage_events" in inspector.get_table_names()
    assert "api_key_usage_daily" in inspector.get_view_names()
    assert {
        column["name"] for column in inspector.get_columns("api_key_usage_events")
    } == {
        "id",
        "key_id",
        "tenant",
        "method",
        "endpoint",
        "status_code",
        "latency_ms",
        "created_at",
    }
    assert any(
        index["name"] == "ix_api_key_usage_events_key_created"
        and index["column_names"] == ["key_id", "created_at"]
        for index in inspector.get_indexes("api_key_usage_events")
    )

    insert = text(
        "INSERT INTO api_key_usage_events "
        "(id, key_id, tenant, method, endpoint, status_code, latency_ms, created_at) "
        "VALUES (:id, :key_id, 'demo', 'GET', '/v1/me/authority', "
        ":status_code, :latency_ms, :created_at)"
    )
    with engine.begin() as connection:
        for event_id, key_id, status_code, latency_ms, created_at in (
            ("1", "key-a", 200, 40, "2026-10-20 23:00:00"),
            ("2", "key-a", 404, 10, "2026-10-20 01:00:00"),
            ("3", "key-a", 500, 30, "2026-10-20 20:00:00"),
            ("4", "key-a", 400, 20, "2026-10-20 12:00:00"),
            ("5", "key-a", 200, 7, "2026-10-21 00:00:00"),
            ("6", "key-b", 200, 100, "2026-10-20 10:00:00"),
        ):
            connection.execute(
                insert,
                {
                    "id": event_id,
                    "key_id": key_id,
                    "status_code": status_code,
                    "latency_ms": latency_ms,
                    "created_at": created_at,
                },
            )
        rows = connection.execute(
            text(
                "SELECT key_id, day, calls, calls_4xx, calls_5xx, "
                "p50_latency_ms, p95_latency_ms, first_call_at, last_call_at "
                "FROM api_key_usage_daily ORDER BY key_id, day"
            )
        ).all()
    engine.dispose()
    assert [tuple(row) for row in rows] == [
        (
            "key-a",
            "2026-10-20",
            4,
            2,
            1,
            25.0,
            38.5,
            "2026-10-20 01:00:00",
            "2026-10-20 23:00:00",
        ),
        (
            "key-a",
            "2026-10-21",
            1,
            0,
            0,
            7.0,
            7.0,
            "2026-10-21 00:00:00",
            "2026-10-21 00:00:00",
        ),
        (
            "key-b",
            "2026-10-20",
            1,
            0,
            0,
            100.0,
            100.0,
            "2026-10-20 10:00:00",
            "2026-10-20 10:00:00",
        ),
    ]

    command.downgrade(config, "044_receipt_resolutions")
    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    assert "api_key_usage_events" not in inspector.get_table_names()
    assert "api_key_usage_daily" not in inspector.get_view_names()
    assert "api_keys" in inspector.get_table_names()
    engine.dispose()
