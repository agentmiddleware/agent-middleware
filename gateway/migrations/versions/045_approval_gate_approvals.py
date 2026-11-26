"""Add durable rows for the AMW V2 human approval gate.

Revision ID: 045_approval_gate_approvals
Revises: 044_receipt_resolutions
"""

from alembic import op
import sqlalchemy as sa

revision: str = "045_approval_gate_approvals"
down_revision: str = "044_receipt_resolutions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "approval_gate_approvals",
        sa.Column("nonce", sa.String(length=64), primary_key=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("args_hash", sa.String(length=128), nullable=False),
        sa.Column("principal", sa.String(length=255), nullable=False),
        sa.Column("tool", sa.String(length=128), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("deadline", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'denied', 'expired')",
            name="ck_approval_gate_approvals_status",
        ),
    )
    for column in ("idempotency_key", "status"):
        op.create_index(
            f"ix_approval_gate_approvals_{column}",
            "approval_gate_approvals",
            [column],
            unique=False,
        )


def downgrade() -> None:
    for column in ("idempotency_key", "status"):
        op.drop_index(
            f"ix_approval_gate_approvals_{column}",
            table_name="approval_gate_approvals",
        )
    op.drop_table("approval_gate_approvals")
