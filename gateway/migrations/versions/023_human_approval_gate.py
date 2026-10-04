"""Human approval gate: Sentinel-backed pause before governed invokes.

Adds `permits.requires_human_approval` (issuer opts a permit into the gate),
the `human_approvals` table (one row per governed invoke attempt, keyed by
wallet + permit + tool + idempotency key so retries re-check the same
approval), and `receipts.approval_id` linking a receipt to the human decision
that authorized it. Sentinel keeps timed-out approvals "pending" forever, so
`human_approvals.expires_at` carries the middleware-enforced expiry.

`receipts.approval_id` is added without a foreign key: SQLite cannot add FK
constraints via ALTER (same call as 020's `ledger_entry_id`).

Revision ID: 023_human_approval_gate
Revises: 3988bd05deca
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "023_human_approval_gate"
down_revision: Union[str, None] = "3988bd05deca"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "permits",
        sa.Column(
            "requires_human_approval",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )

    op.create_table(
        "human_approvals",
        sa.Column("approval_id", sa.String(length=64), nullable=False),
        sa.Column("wallet_id", sa.String(length=50), nullable=False),
        sa.Column("permit_id", sa.String(length=64), nullable=False),
        sa.Column("tool", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="pending"
        ),
        sa.Column("simulated", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("sentinel_action_id", sa.String(length=64), nullable=True),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("decided_by", sa.String(length=128), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["wallet_id"], ["wallets.wallet_id"]),
        sa.ForeignKeyConstraint(["permit_id"], ["permits.permit_id"]),
        sa.PrimaryKeyConstraint("approval_id"),
        sa.UniqueConstraint(
            "wallet_id",
            "permit_id",
            "tool",
            "idempotency_key",
            name="uq_human_approvals_invoke",
        ),
    )
    op.create_index("ix_human_approvals_wallet_id", "human_approvals", ["wallet_id"])
    op.create_index("ix_human_approvals_permit_id", "human_approvals", ["permit_id"])
    op.create_index("ix_human_approvals_status", "human_approvals", ["status"])
    op.create_index(
        "ix_human_approvals_sentinel_action_id",
        "human_approvals",
        ["sentinel_action_id"],
    )

    op.add_column(
        "receipts",
        sa.Column("approval_id", sa.String(length=64), nullable=True),
    )
    op.create_index("ix_receipts_approval_id", "receipts", ["approval_id"])


def downgrade() -> None:
    op.drop_index("ix_receipts_approval_id", table_name="receipts")
    op.drop_column("receipts", "approval_id")
    op.drop_index("ix_human_approvals_sentinel_action_id", table_name="human_approvals")
    op.drop_index("ix_human_approvals_status", table_name="human_approvals")
    op.drop_index("ix_human_approvals_permit_id", table_name="human_approvals")
    op.drop_index("ix_human_approvals_wallet_id", table_name="human_approvals")
    op.drop_table("human_approvals")
    op.drop_column("permits", "requires_human_approval")
