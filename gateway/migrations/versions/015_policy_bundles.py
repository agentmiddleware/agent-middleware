"""Policy bundles.

Revision ID: 015_policy_bundles
Revises: 014_control_plane_audit
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "015_policy_bundles"
down_revision: Union[str, None] = "014_control_plane_audit"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "policy_bundles",
        sa.Column("policy_id", sa.String(length=64), primary_key=True),
        sa.Column("wallet_id", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("allowed_tools_json", sa.Text(), nullable=True),
        sa.Column("allowed_service_categories_json", sa.Text(), nullable=True),
        sa.Column(
            "max_cost_per_action", sa.Numeric(precision=18, scale=8), nullable=True
        ),
        sa.Column(
            "daily_spend_limit", sa.Numeric(precision=18, scale=8), nullable=True
        ),
        sa.Column(
            "require_real_effects",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "risk_tier", sa.String(length=20), nullable=False, server_default="medium"
        ),
        sa.Column(
            "human_approval_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["wallet_id"], ["wallets.wallet_id"]),
    )
    op.create_index("ix_policy_bundles_wallet_id", "policy_bundles", ["wallet_id"])
    op.create_index("ix_policy_bundles_is_active", "policy_bundles", ["is_active"])
    op.create_index("ix_policy_bundles_created_at", "policy_bundles", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_policy_bundles_created_at", table_name="policy_bundles")
    op.drop_index("ix_policy_bundles_is_active", table_name="policy_bundles")
    op.drop_index("ix_policy_bundles_wallet_id", table_name="policy_bundles")
    op.drop_table("policy_bundles")
