"""Initial billing tables

Revision ID: 001_initial
Revises:
Create Date: 2026-04-16

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Wallets table
    op.create_table(
        "wallets",
        sa.Column("wallet_id", sa.String(50), primary_key=True),
        sa.Column("wallet_type", sa.String(20), nullable=False, index=True),
        sa.Column("owner_name", sa.String(255), nullable=True),
        sa.Column("owner_key", sa.String(255), nullable=False, index=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column(
            "balance", sa.Numeric(precision=20, scale=8), nullable=False, default=0
        ),
        sa.Column(
            "lifetime_credits",
            sa.Numeric(precision=20, scale=8),
            nullable=False,
            default=0,
        ),
        sa.Column(
            "lifetime_debits",
            sa.Numeric(precision=20, scale=8),
            nullable=False,
            default=0,
        ),
        sa.Column(
            "parent_wallet_id",
            sa.String(50),
            sa.ForeignKey("wallets.wallet_id"),
            nullable=True,
            index=True,
        ),
        sa.Column("agent_id", sa.String(100), nullable=True, index=True),
        sa.Column("daily_limit", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column(
            "daily_spent", sa.Numeric(precision=20, scale=8), nullable=False, default=0
        ),
        sa.Column("daily_reset_at", sa.DateTime(), nullable=True),
        sa.Column("auto_refill", sa.Boolean(), nullable=False, default=False),
        sa.Column(
            "auto_refill_threshold", sa.Numeric(precision=20, scale=8), nullable=True
        ),
        sa.Column(
            "auto_refill_amount", sa.Numeric(precision=20, scale=8), nullable=True
        ),
        sa.Column("status", sa.String(20), nullable=False, default="active"),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
    )

    # Ledger entries table
    op.create_table(
        "ledger_entries",
        sa.Column("entry_id", sa.String(50), primary_key=True),
        sa.Column(
            "wallet_id",
            sa.String(50),
            sa.ForeignKey("wallets.wallet_id"),
            nullable=False,
            index=True,
        ),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("amount", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("balance_after", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("service_category", sa.String(50), nullable=True, index=True),
        sa.Column("description", sa.String(500), nullable=False, default=""),
        sa.Column("request_path", sa.String(255), nullable=True),
        sa.Column("compute_cost", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("margin", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("correlation_id", sa.String(100), nullable=True, index=True),
        sa.Column(
            "timestamp",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
            index=True,
        ),
    )

    # Billing alerts table
    op.create_table(
        "billing_alerts",
        sa.Column("alert_id", sa.String(50), primary_key=True),
        sa.Column(
            "wallet_id",
            sa.String(50),
            sa.ForeignKey("wallets.wallet_id"),
            nullable=False,
            index=True,
        ),
        sa.Column("alert_type", sa.String(30), nullable=False, index=True),
        sa.Column("threshold_amount", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("current_balance", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("message", sa.String(500), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False, default="info"),
        sa.Column("acknowledged", sa.Boolean(), nullable=False, default=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
            index=True,
        ),
    )

    # Daily balance snapshots table
    op.create_table(
        "daily_balance_snapshots",
        sa.Column("snapshot_id", sa.String(50), primary_key=True),
        sa.Column(
            "wallet_id",
            sa.String(50),
            sa.ForeignKey("wallets.wallet_id"),
            nullable=False,
            index=True,
        ),
        sa.Column("date", sa.DateTime(), nullable=False, index=True),
        sa.Column("opening_balance", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("closing_balance", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column(
            "total_credits",
            sa.Numeric(precision=20, scale=8),
            nullable=False,
            default=0,
        ),
        sa.Column(
            "total_debits", sa.Numeric(precision=20, scale=8), nullable=False, default=0
        ),
        sa.Column("transaction_count", sa.Integer(), nullable=False, default=0),
    )

    # Create indexes for performance
    op.create_index("ix_wallets_status", "wallets", ["status"])
    op.create_index("ix_wallets_created_at", "wallets", ["created_at"])
    op.create_index(
        "ix_ledger_entries_wallet_timestamp",
        "ledger_entries",
        ["wallet_id", "timestamp"],
    )


def downgrade() -> None:
    op.drop_table("daily_balance_snapshots")
    op.drop_table("billing_alerts")
    op.drop_table("ledger_entries")
    op.drop_table("wallets")
