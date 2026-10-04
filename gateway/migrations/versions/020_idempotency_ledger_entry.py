"""Track the ledger entry a governed invoke charged before finalizing.

Adds `idempotency_records.ledger_entry_id`, set right after a governed MCP
invoke charges a wallet and before the receipt/audit/complete finalization
sequence runs. This lets a reconciliation sweep distinguish "never charged"
(nothing to repair) from "charged but finalization never completed" (crash
between charge and receipt) for idempotency records stuck in progress.

Revision ID: 020_idempotency_ledger_entry
Revises: 019_audit_chain_heads
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "020_idempotency_ledger_entry"
down_revision: Union[str, None] = "019_audit_chain_heads"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "idempotency_records",
        sa.Column("ledger_entry_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_idempotency_records_ledger_entry_id",
        "idempotency_records",
        ["ledger_entry_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_idempotency_records_ledger_entry_id",
        table_name="idempotency_records",
    )
    op.drop_column("idempotency_records", "ledger_entry_id")
