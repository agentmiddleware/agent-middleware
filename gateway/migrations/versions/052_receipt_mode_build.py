"""Record the enforcement mode and gateway build on receipts.

Revision ID: 052_receipt_mode_build
Revises: 051_sqlite_boolean_defaults

Adds nullable ``receipts.enforcement_mode`` and ``receipts.gateway_build``.
Existing receipts stay NULL, and their signatures do not cover these fields,
so they keep verifying. Columns are added only when missing.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "052_receipt_mode_build"
down_revision: str = "051_sqlite_boolean_defaults"
branch_labels = None
depends_on = None

COLUMNS = (("enforcement_mode", 16), ("gateway_build", 64))


def upgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("receipts")}
    missing = [(name, size) for name, size in COLUMNS if name not in existing]
    if not missing:
        return
    with op.batch_alter_table("receipts") as batch_op:
        for name, size in missing:
            batch_op.add_column(sa.Column(name, sa.String(length=size), nullable=True))


def downgrade() -> None:
    # A receipt signed with these fields cannot verify once they are dropped,
    # so refuse rather than silently invalidate signed evidence.
    signed = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT COUNT(*) FROM receipts "
                "WHERE enforcement_mode IS NOT NULL OR gateway_build IS NOT NULL"
            )
        )
        .scalar_one()
    )
    if signed:
        raise RuntimeError(
            f"052 downgrade refused: {signed} receipt(s) carry signed "
            "enforcement_mode/gateway_build values that 051 cannot store"
        )
    with op.batch_alter_table("receipts") as batch_op:
        batch_op.drop_column("gateway_build")
        batch_op.drop_column("enforcement_mode")
