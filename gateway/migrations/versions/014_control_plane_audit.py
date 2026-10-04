"""Durable control-plane audit events.

Revision ID: 014_control_plane_audit
Revises: 013_optimizer_telemetry
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "014_control_plane_audit"
down_revision: Union[str, None] = "013_optimizer_telemetry"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "control_plane_audit_events",
        sa.Column("event_id", sa.String(length=50), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("event", sa.String(length=128), nullable=False),
        sa.Column("wallet_id", sa.String(length=64), nullable=True),
        sa.Column("tool", sa.String(length=128), nullable=True),
        sa.Column("endpoint", sa.String(length=256), nullable=True),
        sa.Column("auth_source", sa.String(length=32), nullable=True),
        sa.Column("key_id", sa.String(length=64), nullable=True),
        sa.Column("policy_decision_id", sa.String(length=64), nullable=True),
        sa.Column("request_id", sa.String(length=100), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_control_plane_audit_events_created_at",
        "control_plane_audit_events",
        ["created_at"],
    )
    op.create_index(
        "ix_control_plane_audit_events_event", "control_plane_audit_events", ["event"]
    )
    op.create_index(
        "ix_control_plane_audit_events_wallet_id",
        "control_plane_audit_events",
        ["wallet_id"],
    )
    op.create_index(
        "ix_control_plane_audit_events_tool", "control_plane_audit_events", ["tool"]
    )
    op.create_index(
        "ix_control_plane_audit_events_endpoint",
        "control_plane_audit_events",
        ["endpoint"],
    )
    op.create_index(
        "ix_control_plane_audit_events_key_id", "control_plane_audit_events", ["key_id"]
    )
    op.create_index(
        "ix_control_plane_audit_events_policy_decision_id",
        "control_plane_audit_events",
        ["policy_decision_id"],
    )
    op.create_index(
        "ix_control_plane_audit_events_request_id",
        "control_plane_audit_events",
        ["request_id"],
    )
    op.create_index(
        "ix_control_plane_audit_events_ok", "control_plane_audit_events", ["ok"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_control_plane_audit_events_ok", table_name="control_plane_audit_events"
    )
    op.drop_index(
        "ix_control_plane_audit_events_request_id",
        table_name="control_plane_audit_events",
    )
    op.drop_index(
        "ix_control_plane_audit_events_policy_decision_id",
        table_name="control_plane_audit_events",
    )
    op.drop_index(
        "ix_control_plane_audit_events_key_id", table_name="control_plane_audit_events"
    )
    op.drop_index(
        "ix_control_plane_audit_events_endpoint",
        table_name="control_plane_audit_events",
    )
    op.drop_index(
        "ix_control_plane_audit_events_tool", table_name="control_plane_audit_events"
    )
    op.drop_index(
        "ix_control_plane_audit_events_wallet_id",
        table_name="control_plane_audit_events",
    )
    op.drop_index(
        "ix_control_plane_audit_events_event", table_name="control_plane_audit_events"
    )
    op.drop_index(
        "ix_control_plane_audit_events_created_at",
        table_name="control_plane_audit_events",
    )
    op.drop_table("control_plane_audit_events")
