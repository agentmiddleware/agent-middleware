"""Add permit/approval digests, counterparty, and per-permit receipt chain.

Revision ID: 056_receipt_digests_chain
Revises: 055_wallet_product_activation

Adds nullable ``receipts.permit_digest``, ``receipts.approval_digest``,
``receipts.counterparty``, ``receipts.sequence``, and
``receipts.prev_receipt_digest``. Existing receipts stay NULL, and their
signatures do not cover these fields, so they keep verifying. Columns are
added only when missing.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "056_receipt_digests_chain"
down_revision: str = "055_wallet_product_activation"
branch_labels = None
depends_on = None

COLUMNS = (
    ("permit_digest", 64),
    ("approval_digest", 64),
    ("counterparty", 255),
    ("prev_receipt_digest", 64),
)


def upgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("receipts")}
    missing_str = [(name, size) for name, size in COLUMNS if name not in existing]
    with op.batch_alter_table("receipts") as batch_op:
        for name, size in missing_str:
            batch_op.add_column(sa.Column(name, sa.String(length=size), nullable=True))
        if "sequence" not in existing:
            batch_op.add_column(sa.Column("sequence", sa.Integer(), nullable=True))


def downgrade() -> None:
    signed = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT COUNT(*) FROM receipts WHERE "
                "permit_digest IS NOT NULL OR approval_digest IS NOT NULL OR "
                "counterparty IS NOT NULL OR sequence IS NOT NULL OR "
                "prev_receipt_digest IS NOT NULL"
            )
        )
        .scalar_one()
    )
    if signed:
        raise RuntimeError(
            f"056 downgrade refused: {signed} receipt(s) carry signed "
            "digest/chain values that 055 cannot store"
        )
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("receipts")}
    with op.batch_alter_table("receipts") as batch_op:
        for name, _ in COLUMNS:
            if name in existing:
                batch_op.drop_column(name)
        if "sequence" in existing:
            batch_op.drop_column("sequence")
