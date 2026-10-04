"""Add telemetry_events table

Revision ID: 004_telemetry_events
Revises: 003_velocity_monitoring
Create Date: 2026-04-17

Replaces the in-memory EventStore in telemetry_pm with a time-series
persistence layer. On PostgreSQL with the TimescaleDB extension, this
also promotes the table to a hypertable keyed on ingested_at so
retention cleanup is an O(dropped-chunk) operation rather than a full
table scan.

See issue #28.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "004_telemetry_events"
down_revision: Union[str, None] = "003_velocity_monitoring"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "telemetry_events",
        sa.Column("event_id", sa.String(50), primary_key=True),
        sa.Column("batch_id", sa.String(50), nullable=False, index=True),
        sa.Column("event_type", sa.String(20), nullable=False, index=True),
        sa.Column("severity", sa.String(20), nullable=False, index=True),
        sa.Column("source", sa.String(100), nullable=False, index=True),
        sa.Column("message", sa.String(1000), nullable=False, server_default=""),
        sa.Column("stack_trace", sa.Text(), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.Column("event_timestamp", sa.DateTime(), nullable=True, index=True),
        sa.Column(
            "ingested_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
            index=True,
        ),
    )

    # TimescaleDB hypertable promotion — only applies when the extension
    # is available (PostgreSQL deployments). On SQLite or vanilla
    # PostgreSQL without TimescaleDB this silently no-ops.
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        extension = bind.execute(
            sa.text("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
        ).scalar()
        if extension:
            bind.execute(
                sa.text(
                    "SELECT create_hypertable("
                    "'telemetry_events', 'ingested_at', "
                    "chunk_time_interval => INTERVAL '1 day', "
                    "if_not_exists => TRUE)"
                )
            )


def downgrade() -> None:
    op.drop_table("telemetry_events")
