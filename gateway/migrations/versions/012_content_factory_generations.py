"""Durable text generations + provenance for Content Factory (Phase 1).

Revision ID: 012_content_factory_generations
Revises: 011_agent_comms_messages
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "012_content_factory_generations"
down_revision: Union[str, None] = "011_agent_comms_messages"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "content_factory_generations",
        sa.Column("content_id", sa.String(36), primary_key=True),
        sa.Column("prompt_hash", sa.String(64), nullable=True, index=True),
        sa.Column("output_hash", sa.String(64), nullable=True),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("provenance_json", sa.Text(), nullable=True),
        sa.Column("output_text", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
            index=True,
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("content_factory_generations")
