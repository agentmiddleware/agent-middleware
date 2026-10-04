"""Enforce CreateAPIKeyRequest.max_uses on api_keys.

The schema has always accepted max_uses but the router dropped it, so a
"use-limited" key was silently unlimited. Add the cap and a use counter so
validate_key can enforce it atomically. Both columns are additive: existing
keys get max_uses NULL (unlimited), matching their actual historical behavior.

Revision ID: 035_api_key_max_uses
Revises: 034_add_permit_tool_call_counts
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "035_api_key_max_uses"
down_revision: Union[str, None] = "034_add_permit_tool_call_counts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.add_column(sa.Column("max_uses", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "use_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.drop_column("use_count")
        batch_op.drop_column("max_uses")
