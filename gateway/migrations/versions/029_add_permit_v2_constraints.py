"""Add permit schema v2 constraints and receipt constraints_evaluated.

Revision ID: 029_add_permit_v2_constraints
Revises: 028_revoke_unbound_refresh
Create Date: 2026-08-09
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "029_add_permit_v2_constraints"
down_revision: Union[str, None] = "028_revoke_unbound_refresh"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("permits") as batch_op:
        batch_op.add_column(
            sa.Column("max_calls_per_tool_json", sa.Text(), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "aggregate_value_cap",
                sa.Numeric(precision=20, scale=8),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column("forbidden_fields_json", sa.Text(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("recipient_domain", sa.String(length=255), nullable=True)
        )

    with op.batch_alter_table("receipts") as batch_op:
        batch_op.add_column(
            sa.Column("constraints_evaluated_json", sa.Text(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("receipts") as batch_op:
        batch_op.drop_column("constraints_evaluated_json")

    with op.batch_alter_table("permits") as batch_op:
        batch_op.drop_column("recipient_domain")
        batch_op.drop_column("forbidden_fields_json")
        batch_op.drop_column("aggregate_value_cap")
        batch_op.drop_column("max_calls_per_tool_json")
