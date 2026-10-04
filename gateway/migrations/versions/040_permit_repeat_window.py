"""add permit repeat_window_seconds

Revision ID: 040_permit_repeat_window
Revises: 039_permit_allow_ident_repeats
Create Date: 2026-09-28 22:20:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "040_permit_repeat_window"
down_revision: str = "039_permit_allow_ident_repeats"
branch_labels = None
depends_on = None


def upgrade():
    """Add repeat_window_seconds column to permits."""
    with op.batch_alter_table("permits", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("repeat_window_seconds", sa.Integer(), nullable=True)
        )


def downgrade():
    """Remove repeat_window_seconds column."""
    with op.batch_alter_table("permits", schema=None) as batch_op:
        batch_op.drop_column("repeat_window_seconds")
