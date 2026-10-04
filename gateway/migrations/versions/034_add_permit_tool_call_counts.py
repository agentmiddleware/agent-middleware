"""add permit tool_call_counts

Revision ID: 034
Revises: 033
Create Date: 2026-08-22

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "034_add_permit_tool_call_counts"
down_revision: str = "033_drop_optimizer_telemetry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add tool_call_counts_json column to permits for atomic call-count enforcement.

    The column stores a JSON object mapping tool names to their current successful
    call counts (e.g., {"partner.echo": 2}). This counter is atomically incremented
    when a receipt with outcome="success" is created, enabling max_calls_per_tool
    enforcement without a read-time race between authorization and receipt creation.
    """
    op.add_column(
        "permits", sa.Column("tool_call_counts_json", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    """Remove tool_call_counts_json column."""
    with op.batch_alter_table("permits", schema=None) as batch_op:
        batch_op.drop_column("tool_call_counts_json")
