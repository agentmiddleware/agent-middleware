"""Per-wallet audit chain head pointer.

Adds the `audit_chain_heads` table. One row per wallet serializes concurrent
same-wallet writers so they cannot read the same predecessor and fork the hash
chain. Serialization is optimistic, not lock-based: an append reads `last_seq`
and advances the head with a conditional `UPDATE ... WHERE last_seq =
<observed>`, retrying when the update matches no rows. See
`append_chained_audit_event` in `app/services/audit_chain.py`.

Revision ID: 019_audit_chain_heads
Revises: 018_permit_updated_at
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "019_audit_chain_heads"
down_revision: Union[str, None] = "018_permit_updated_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audit_chain_heads",
        sa.Column("wallet_key", sa.String(length=64), primary_key=True),
        sa.Column("last_chain_hash", sa.String(length=64), nullable=True),
        sa.Column("last_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("audit_chain_heads")
