"""Add velocity monitoring fields to wallets

Revision ID: 003_velocity_monitoring
Revises: 002_stripe_fields
Create Date: 2026-04-16

Adds hourly spend tracking and velocity anomaly detection fields.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "003_velocity_monitoring"
down_revision: Union[str, None] = "002_stripe_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "wallets",
        sa.Column("hourly_limit", sa.Numeric(precision=20, scale=8), nullable=True),
    )
    op.add_column(
        "wallets",
        sa.Column(
            "hourly_spent",
            sa.Numeric(precision=20, scale=8),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "wallets",
        sa.Column("hourly_reset_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "wallets",
        sa.Column("last_charge_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "wallets",
        sa.Column(
            "velocity_alerts_triggered",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("wallets", "velocity_alerts_triggered")
    op.drop_column("wallets", "last_charge_at")
    op.drop_column("wallets", "hourly_reset_at")
    op.drop_column("wallets", "hourly_spent")
    op.drop_column("wallets", "hourly_limit")
