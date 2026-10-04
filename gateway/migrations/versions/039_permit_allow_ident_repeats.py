"""add permit allow_identical_repeats

Revision ID: 039
Revises: 038
Create Date: 2026-09-27

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "039_permit_allow_ident_repeats"
down_revision: str = "038_dispatch_call_slot_dup_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add allow_identical_repeats column to permits.

    This field provides per-permit opt-out from cross-key duplicate detection.
    When true, identical requests under different idempotency keys are allowed,
    for tools that legitimately repeat identical calls (e.g., repeated purchases
    of the same item). Defaults to false (duplicate detection is on by default).
    """
    op.add_column(
        "permits",
        sa.Column(
            "allow_identical_repeats", sa.Boolean(), nullable=False, server_default="0"
        ),
    )


def downgrade() -> None:
    """Remove allow_identical_repeats column."""
    with op.batch_alter_table("permits", schema=None) as batch_op:
        batch_op.drop_column("allow_identical_repeats")
