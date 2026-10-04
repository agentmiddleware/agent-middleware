"""Add Stripe payment fields to ledger entries

Revision ID: 002_stripe_fields
Revises: 001_initial
Create Date: 2026-04-16

Adds payment_intent_id and stripe_session_id columns to ledger_entries
for fiat top-up idempotency tracking.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "002_stripe_fields"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ledger_entries",
        sa.Column(
            "payment_intent_id",
            sa.String(100),
            nullable=True,
        ),
    )
    op.add_column(
        "ledger_entries",
        sa.Column("stripe_session_id", sa.String(100), nullable=True),
    )

    op.create_index(
        "ix_ledger_entries_payment_intent_id",
        "ledger_entries",
        ["payment_intent_id"],
        unique=True,
        if_not_exists=True,
    )
    op.create_index(
        "ix_ledger_entries_stripe_session_id",
        "ledger_entries",
        ["stripe_session_id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ledger_entries_stripe_session_id",
        table_name="ledger_entries",
    )
    op.drop_index(
        "ix_ledger_entries_payment_intent_id",
        table_name="ledger_entries",
    )
    op.drop_column("ledger_entries", "stripe_session_id")
    op.drop_column("ledger_entries", "payment_intent_id")
