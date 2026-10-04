"""Focused coverage for the additive dispatch-claim schema migration."""

import logging

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.db.models import McpDispatchAttemptModel


def test_dispatch_claim_hash_model_is_nullable_and_bounded():
    column = McpDispatchAttemptModel.__table__.c.dispatch_claim_hash

    assert column.nullable is True
    assert column.type.length == 64


def test_dispatch_claim_migration_preserves_legacy_rows_and_downgrades(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "dispatch-claim-migration.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    sync_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", async_url)
    config = Config("alembic.ini")
    sentinel_logger = logging.getLogger("tests.dispatch_claim_migration.sentinel")
    sentinel_logger.disabled = False

    command.upgrade(config, "036_permit_request_hash_anchor")
    assert sentinel_logger.disabled is False

    engine = create_engine(sync_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO mcp_dispatch_attempts (
                    attempt_id, idempotency_record_id, wallet_id, permit_id,
                    public_tool_id, upstream_tool_name, upstream_origin,
                    request_hash, credits_authorized, credits_charged, state
                ) VALUES (
                    'dsp-legacy-claim', 'idm-legacy-claim', 'agt-legacy-claim',
                    'permit-legacy-claim', 'partner.legacy', 'partner_legacy',
                    'https://partner.example', :request_hash,
                    1, 0, 'prepared'
                )
                """
            ),
            {"request_hash": "a" * 64},
        )
    engine.dispose()

    command.upgrade(config, "037_mcp_dispatch_claim_hash")

    engine = create_engine(sync_url)
    columns = {
        column["name"]: column
        for column in inspect(engine).get_columns("mcp_dispatch_attempts")
    }
    assert columns["dispatch_claim_hash"]["nullable"] is True
    assert columns["dispatch_claim_hash"]["type"].length == 64

    with engine.begin() as connection:
        legacy_claim_hash = connection.execute(
            text(
                """
                SELECT dispatch_claim_hash
                FROM mcp_dispatch_attempts
                WHERE attempt_id = 'dsp-legacy-claim'
                """
            )
        ).scalar_one()
        assert legacy_claim_hash is None

        connection.execute(
            text(
                """
                INSERT INTO mcp_dispatch_attempts (
                    attempt_id, idempotency_record_id, wallet_id, permit_id,
                    public_tool_id, upstream_tool_name, upstream_origin,
                    request_hash, credits_authorized, credits_charged, state,
                    dispatch_claim_hash, dispatched_at
                ) VALUES (
                    'dsp-new-claim', 'idm-new-claim', 'agt-new-claim',
                    'permit-new-claim', 'partner.new', 'partner_new',
                    'https://partner.example', :request_hash,
                    1, 1, 'dispatch_claimed', :dispatch_claim_hash,
                    '2026-08-31 12:00:00'
                )
                """
            ),
            {
                "request_hash": "b" * 64,
                "dispatch_claim_hash": "c" * 64,
            },
        )
    engine.dispose()

    command.downgrade(config, "036_permit_request_hash_anchor")

    engine = create_engine(sync_url)
    assert "dispatch_claim_hash" not in {
        column["name"]
        for column in inspect(engine).get_columns("mcp_dispatch_attempts")
    }
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                """
                SELECT attempt_id, state, CAST(dispatched_at AS TEXT)
                FROM mcp_dispatch_attempts
                WHERE attempt_id IN ('dsp-legacy-claim', 'dsp-new-claim')
                ORDER BY attempt_id
                """
            )
        ).all()
        assert rows == [
            ("dsp-legacy-claim", "prepared", None),
            ("dsp-new-claim", "dispatched", "2026-08-31 12:00:00"),
        ]
    engine.dispose()
