"""Add agent_comms_messages for durable A2A store (Phase 1 comms).

Revision ID: 011_agent_comms_messages
Revises: 010_oracle_crawl_payload_hash
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "011_agent_comms_messages"
down_revision: Union[str, None] = "010_oracle_crawl_payload_hash"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_comms_messages",
        sa.Column("message_id", sa.String(64), primary_key=True),
        sa.Column("from_agent", sa.String(100), nullable=False, index=True),
        sa.Column("to_agent", sa.String(100), nullable=False, index=True),
        sa.Column("message_type", sa.String(30), nullable=False),
        sa.Column("priority", sa.String(20), nullable=False),
        sa.Column("subject", sa.String(500), nullable=False, server_default=""),
        sa.Column("body_json", sa.Text(), nullable=True),
        sa.Column("correlation_id", sa.String(100), nullable=True, index=True),
        sa.Column("reply_to", sa.String(64), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, index=True),
        sa.Column("payload_hash", sa.String(128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
            index=True,
        ),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("agent_comms_messages")
