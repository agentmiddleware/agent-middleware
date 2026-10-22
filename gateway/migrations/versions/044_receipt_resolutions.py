"""Add immutable signed receipt resolutions and per-wallet chain heads.

Revision ID: 044_receipt_resolutions
Revises: 043_demo_key_fields
"""

from alembic import op
import sqlalchemy as sa

# SEL-351 may land its own migration first; renumber this revision then.
revision: str = "044_receipt_resolutions"
down_revision: str = "043_demo_key_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "resolution_chain_heads",
        sa.Column("wallet_id", sa.String(length=50), primary_key=True),
        sa.Column("last_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_hash", sa.String(length=64), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["wallet_id"], ["wallets.wallet_id"]),
        sa.CheckConstraint(
            "last_sequence >= 0", name="ck_resolution_chain_heads_sequence"
        ),
    )
    op.create_table(
        "receipt_resolutions",
        sa.Column("resolution_id", sa.String(length=64), primary_key=True),
        sa.Column("receipt_id", sa.String(length=64), nullable=False),
        sa.Column("wallet_id", sa.String(length=50), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("prev_hash", sa.String(length=64), nullable=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("resolution_hash", sa.String(length=64), nullable=False),
        sa.Column("signing_input", sa.Text(), nullable=False),
        sa.Column("signature", sa.String(), nullable=False),
        sa.Column("signature_key_id", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["receipt_id"], ["receipts.receipt_id"]),
        sa.ForeignKeyConstraint(["wallet_id"], ["wallets.wallet_id"]),
        sa.ForeignKeyConstraint(["signature_key_id"], ["signing_keys.key_id"]),
        sa.UniqueConstraint(
            "wallet_id", "sequence", name="uq_receipt_resolutions_wallet_seq"
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_receipt_resolutions_sequence"),
        sa.CheckConstraint(
            "outcome IN ('confirmed_success', 'confirmed_not_delivered')",
            name="ck_receipt_resolutions_outcome",
        ),
    )
    for column in (
        "receipt_id",
        "wallet_id",
        "sequence",
        "receipt_hash",
        "resolution_hash",
        "signature_key_id",
    ):
        op.create_index(
            f"ix_receipt_resolutions_{column}",
            "receipt_resolutions",
            [column],
            unique=column in {"receipt_id", "resolution_hash"},
        )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM receipt_resolutions LIMIT 1")).first():
        raise RuntimeError("resolution_evidence_retained")
    op.drop_table("receipt_resolutions")
    op.drop_table("resolution_chain_heads")
