"""Add ledger_entries.stripe_event_id for refund-webhook idempotency.

A redelivered Stripe ``charge.refunded`` event previously debited the wallet
again, because the refund ledger entry carried no unique key (the original
mint entry already holds ``payment_intent_id`` under its unique constraint).
This adds a nullable, unique ``stripe_event_id`` set on refund entries so a
duplicate delivery is rejected at the DB level, mirroring the mint path.

Revision ID: 021_ledger_stripe_event_id
Revises: 020_idempotency_ledger_entry
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "021_ledger_stripe_event_id"
down_revision: Union[str, None] = "020_idempotency_ledger_entry"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ledger_entries",
        sa.Column("stripe_event_id", sa.String(length=100), nullable=True),
    )
    op.create_index(
        "ix_ledger_entries_stripe_event_id",
        "ledger_entries",
        ["stripe_event_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ledger_entries_stripe_event_id",
        table_name="ledger_entries",
    )
    op.drop_column("ledger_entries", "stripe_event_id")
