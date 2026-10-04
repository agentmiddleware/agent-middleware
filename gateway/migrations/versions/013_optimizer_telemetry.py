"""Planner telemetry table for constrained optimizer.

Revision ID: 013_optimizer_telemetry
Revises: 012_content_factory_generations
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "013_optimizer_telemetry"
down_revision: Union[str, None] = "012_content_factory_generations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "optimizer_telemetry",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ts", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("wallet_id", sa.String(length=64), nullable=False),
        sa.Column("agent_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=100), nullable=True),
        sa.Column("request_id", sa.String(length=100), nullable=True),
        sa.Column("endpoint", sa.String(length=128), nullable=False),
        sa.Column("action_features", sa.JSON(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("credits_delta", sa.Float(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("error_class", sa.String(length=64), nullable=True),
        sa.Column("risk_flags", sa.JSON(), nullable=True),
        sa.Column("payload_hash", sa.String(length=64), nullable=True),
    )
    op.create_index("ix_optimizer_telemetry_ts", "optimizer_telemetry", ["ts"])
    op.create_index(
        "ix_optimizer_telemetry_wallet_id", "optimizer_telemetry", ["wallet_id"]
    )
    op.create_index(
        "ix_optimizer_telemetry_agent_id", "optimizer_telemetry", ["agent_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_optimizer_telemetry_agent_id", table_name="optimizer_telemetry")
    op.drop_index("ix_optimizer_telemetry_wallet_id", table_name="optimizer_telemetry")
    op.drop_index("ix_optimizer_telemetry_ts", table_name="optimizer_telemetry")
    op.drop_table("optimizer_telemetry")
