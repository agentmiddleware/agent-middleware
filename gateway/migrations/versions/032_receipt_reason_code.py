"""Add a signed machine-readable reason to non-success receipts.

The column is nullable so existing receipt signatures continue to verify over
their original payloads. New receipts include the field in the signing input
only when a governed failure has a stable reason code.

Revision ID: 032_receipt_reason_code
Revises: 031_quotes
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "032_receipt_reason_code"
down_revision: Union[str, None] = "031_quotes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("receipts") as batch_op:
        batch_op.add_column(
            sa.Column("reason_code", sa.String(length=128), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("receipts") as batch_op:
        batch_op.drop_column("reason_code")
