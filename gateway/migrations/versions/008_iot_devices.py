"""Add iot_devices + iot_device_events tables

Revision ID: 008_iot_devices
Revises: 007_content_factory
Create Date: 2026-04-20

Replaces iot_bridge.DeviceRegistry's in-memory dict with a PG-backed
state table plus an append-only audit log. See issue #32.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "008_iot_devices"
down_revision: Union[str, None] = "007_content_factory"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "iot_devices",
        sa.Column("device_id", sa.String(100), primary_key=True),
        sa.Column("protocol", sa.String(20), nullable=False, index=True),
        sa.Column("broker_url", sa.String(500), nullable=True),
        sa.Column("topic_acl_json", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(30),
            nullable=False,
            server_default="registered",
            index=True,
        ),
        sa.Column(
            "registered_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
            index=True,
        ),
        sa.Column("last_message_at", sa.DateTime(), nullable=True),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "iot_device_events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("device_id", sa.String(100), nullable=False, index=True),
        sa.Column("event_type", sa.String(30), nullable=False, index=True),
        sa.Column("topic", sa.String(500), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.Column(
            "timestamp",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
            index=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("iot_device_events")
    op.drop_table("iot_devices")
