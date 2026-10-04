"""Drop the optimizer_telemetry table, which nothing ever wrote.

013 created this table for planner telemetry and no code path ever inserted a
row: the only references outside the model were test fixtures truncating it.
The planner itself stays (docs/PROOF_SURFACES.md keeps it deferred pending a
product decision) -- this removes the unwritten schema behind it, in the spirit
of preferring deletion of unused scaffolding over growing it.

Dropping a table is destructive, so the downgrade recreates it exactly as 013
did, columns and indexes alike. Any rows are unrecoverable, which is acceptable
only because there is no writer that could have produced one.

Revision ID: 033_drop_optimizer_telemetry
Revises: 032_receipt_reason_code
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "033_drop_optimizer_telemetry"
down_revision: Union[str, None] = "032_receipt_reason_code"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_optimizer_telemetry_agent_id", table_name="optimizer_telemetry")
    op.drop_index("ix_optimizer_telemetry_wallet_id", table_name="optimizer_telemetry")
    op.drop_index("ix_optimizer_telemetry_ts", table_name="optimizer_telemetry")
    op.drop_table("optimizer_telemetry")


def downgrade() -> None:
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
