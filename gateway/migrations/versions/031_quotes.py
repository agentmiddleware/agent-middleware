"""Signed quotes: a price commitment an agent can rely on.

Adds the `quotes` table. A quote is a signed statement that one wallet calling
one tool costs a fixed number of credits until it expires; presenting it on the
governed invoke charges that number even if the registered price has moved.

The row carries `status` and `expires_at` because both are load-bearing at
spend time: the quote is consumed by an atomic `active -> consumed` UPDATE that
also requires `expires_at > now`, which is what makes a quote single-use and
keeps the active-vs-expired boundary decided in one place.

Revision ID: 031_quotes
Revises: 030_permit_requests
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "031_quotes"
down_revision: Union[str, None] = "030_permit_requests"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "quotes",
        sa.Column("quote_id", sa.String(length=64), nullable=False),
        sa.Column("wallet_id", sa.String(length=50), nullable=False),
        sa.Column("tool", sa.String(length=128), nullable=False),
        sa.Column("quoted_credits", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="active"
        ),
        sa.Column("issued_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.Column("consumed_by_idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["wallet_id"], ["wallets.wallet_id"]),
        sa.ForeignKeyConstraint(["key_id"], ["signing_keys.key_id"]),
        sa.PrimaryKeyConstraint("quote_id"),
    )
    op.create_index("ix_quotes_wallet_id", "quotes", ["wallet_id"])
    op.create_index("ix_quotes_tool", "quotes", ["tool"])
    op.create_index("ix_quotes_status", "quotes", ["status"])
    op.create_index("ix_quotes_expires_at", "quotes", ["expires_at"])
    op.create_index("ix_quotes_key_id", "quotes", ["key_id"])


def downgrade() -> None:
    op.drop_index("ix_quotes_key_id", table_name="quotes")
    op.drop_index("ix_quotes_expires_at", table_name="quotes")
    op.drop_index("ix_quotes_status", table_name="quotes")
    op.drop_index("ix_quotes_tool", table_name="quotes")
    op.drop_index("ix_quotes_wallet_id", table_name="quotes")
    op.drop_table("quotes")
