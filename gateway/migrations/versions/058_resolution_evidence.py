"""Evidence bound to append-only receipt resolutions.

Revision ID: 058_resolution_evidence
Revises: 057_receipt_permit_seq_unique
"""

from alembic import op
import sqlalchemy as sa

revision = "058_resolution_evidence"
down_revision = "057_receipt_permit_seq_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "receipt_resolutions", sa.Column("evidence", sa.Text(), nullable=True)
    )
    op.add_column(
        "receipt_resolutions", sa.Column("evidence_hash", sa.String(64), nullable=True)
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            sa.text("LOCK TABLE receipt_resolutions IN SHARE ROW EXCLUSIVE MODE")
        )
    if op.get_bind().scalar(
        sa.text(
            "SELECT COUNT(*) FROM receipt_resolutions WHERE evidence_hash IS NOT NULL OR evidence IS NOT NULL"
        )
    ):
        raise RuntimeError(
            "cannot drop signed resolution evidence; restore a pre-v0.2 backup instead"
        )
    op.drop_column("receipt_resolutions", "evidence_hash")
    op.drop_column("receipt_resolutions", "evidence")
