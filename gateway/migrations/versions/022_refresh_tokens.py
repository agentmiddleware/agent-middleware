"""add refresh_tokens table

Revision ID: 3988bd05deca
Revises: 021_ledger_stripe_event_id
Create Date: 2026-08-04 18:57:48.122558

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "3988bd05deca"
down_revision: Union[str, None] = "021_ledger_stripe_event_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "refresh_tokens",
        sa.Column("jti", sa.String(length=64), nullable=False),
        sa.Column("wallet_id", sa.String(length=50), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["wallet_id"], ["wallets.wallet_id"]),
        sa.PrimaryKeyConstraint("jti"),
    )
    op.create_index(
        op.f("ix_refresh_tokens_revoked"), "refresh_tokens", ["revoked"], unique=False
    )
    op.create_index(
        op.f("ix_refresh_tokens_wallet_id"),
        "refresh_tokens",
        ["wallet_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_refresh_tokens_wallet_id"), table_name="refresh_tokens")
    op.drop_index(op.f("ix_refresh_tokens_revoked"), table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
