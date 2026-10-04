"""Add exactly-once ledger, receipt, and remote dispatch persistence.

Revision ID: 026_governed_mcp_persistence
Revises: 025_remove_plaintext_owner_keys
Create Date: 2026-08-06
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "026_governed_mcp_persistence"
down_revision: Union[str, None] = "025_remove_plaintext_owner_keys"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("idempotency_records") as batch_op:
        batch_op.add_column(
            sa.Column("operation_kind", sa.String(length=32), nullable=True)
        )
        batch_op.create_index(
            "ix_idempotency_records_operation_kind",
            ["operation_kind"],
        )

    with op.batch_alter_table("ledger_entries") as batch_op:
        batch_op.add_column(
            sa.Column("operation_key", sa.String(length=128), nullable=True)
        )
        batch_op.create_index(
            "ix_ledger_entries_operation_key",
            ["operation_key"],
        )
        batch_op.create_unique_constraint(
            "uq_ledger_wallet_operation_key",
            ["wallet_id", "operation_key"],
        )

    with op.batch_alter_table("receipts") as batch_op:
        batch_op.add_column(
            sa.Column(
                "idempotency_record_id",
                sa.String(length=64),
                nullable=True,
            )
        )
        batch_op.create_foreign_key(
            "fk_receipts_idempotency_record_id",
            "idempotency_records",
            ["idempotency_record_id"],
            ["record_id"],
        )

    # Historical receipts gain the linkage only when exactly one idempotency
    # record points to that receipt. Ambiguous or unreferenced rows remain NULL.
    op.execute(
        sa.text(
            """
            UPDATE receipts
            SET idempotency_record_id = (
                SELECT MIN(idempotency_records.record_id)
                FROM idempotency_records
                WHERE idempotency_records.response_reference = receipts.receipt_id
                  AND idempotency_records.wallet_id = receipts.wallet_id
                  AND idempotency_records.request_hash = receipts.request_hash
            )
            WHERE receipts.receipt_id IN (
                SELECT response_reference
                FROM idempotency_records
                WHERE response_reference IS NOT NULL
                GROUP BY response_reference
                HAVING COUNT(*) = 1
            )
              AND EXISTS (
                SELECT 1
                FROM idempotency_records
                WHERE idempotency_records.response_reference = receipts.receipt_id
                  AND idempotency_records.wallet_id = receipts.wallet_id
                  AND idempotency_records.request_hash = receipts.request_hash
              )
            """
        )
    )

    with op.batch_alter_table("receipts") as batch_op:
        batch_op.create_index(
            "ix_receipts_idempotency_record_id",
            ["idempotency_record_id"],
            unique=True,
        )

    op.create_table(
        "mcp_dispatch_attempts",
        sa.Column("attempt_id", sa.String(length=64), primary_key=True),
        sa.Column("idempotency_record_id", sa.String(length=64), nullable=False),
        sa.Column("wallet_id", sa.String(length=50), nullable=False),
        sa.Column("permit_id", sa.String(length=64), nullable=False),
        sa.Column("approval_id", sa.String(length=64), nullable=True),
        sa.Column("key_id", sa.String(length=50), nullable=True),
        sa.Column("public_tool_id", sa.String(length=128), nullable=False),
        sa.Column("upstream_tool_name", sa.String(length=128), nullable=False),
        sa.Column("upstream_origin", sa.String(length=512), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("ledger_entry_id", sa.String(length=50), nullable=True),
        sa.Column(
            "credits_authorized",
            sa.Numeric(precision=20, scale=8),
            nullable=False,
        ),
        sa.Column(
            "credits_charged",
            sa.Numeric(precision=20, scale=8),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "state",
            sa.String(length=32),
            nullable=False,
            server_default="prepared",
        ),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("result_size_bytes", sa.Integer(), nullable=True),
        sa.Column("response_hash", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("debit_refunded_at", sa.DateTime(), nullable=True),
        sa.Column("budget_released_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("dispatched_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["idempotency_record_id"],
            ["idempotency_records.record_id"],
        ),
        sa.ForeignKeyConstraint(["wallet_id"], ["wallets.wallet_id"]),
        sa.ForeignKeyConstraint(["permit_id"], ["permits.permit_id"]),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            ["human_approvals.approval_id"],
        ),
        sa.ForeignKeyConstraint(["key_id"], ["api_keys.key_id"]),
        sa.ForeignKeyConstraint(
            ["ledger_entry_id"],
            ["ledger_entries.entry_id"],
        ),
    )
    op.create_index(
        "ix_mcp_dispatch_attempts_idempotency_record_id",
        "mcp_dispatch_attempts",
        ["idempotency_record_id"],
        unique=True,
    )
    for column in (
        "wallet_id",
        "permit_id",
        "approval_id",
        "key_id",
        "public_tool_id",
        "request_hash",
        "ledger_entry_id",
        "state",
        "error_code",
        "debit_refunded_at",
        "budget_released_at",
        "created_at",
        "updated_at",
        "dispatched_at",
        "completed_at",
    ):
        op.create_index(
            f"ix_mcp_dispatch_attempts_{column}",
            "mcp_dispatch_attempts",
            [column],
        )

    with op.batch_alter_table("receipts") as batch_op:
        batch_op.add_column(
            sa.Column(
                "dispatch_attempt_id",
                sa.String(length=64),
                nullable=True,
            )
        )
        batch_op.create_foreign_key(
            "fk_receipts_dispatch_attempt_id",
            "mcp_dispatch_attempts",
            ["dispatch_attempt_id"],
            ["attempt_id"],
        )
        batch_op.create_index(
            "ix_receipts_dispatch_attempt_id",
            ["dispatch_attempt_id"],
            unique=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("receipts") as batch_op:
        batch_op.drop_index("ix_receipts_dispatch_attempt_id")
        batch_op.drop_constraint(
            "fk_receipts_dispatch_attempt_id",
            type_="foreignkey",
        )
        batch_op.drop_column("dispatch_attempt_id")

    for column in reversed(
        (
            "wallet_id",
            "permit_id",
            "approval_id",
            "key_id",
            "public_tool_id",
            "request_hash",
            "ledger_entry_id",
            "state",
            "error_code",
            "debit_refunded_at",
            "budget_released_at",
            "created_at",
            "updated_at",
            "dispatched_at",
            "completed_at",
        )
    ):
        op.drop_index(
            f"ix_mcp_dispatch_attempts_{column}",
            table_name="mcp_dispatch_attempts",
        )
    op.drop_index(
        "ix_mcp_dispatch_attempts_idempotency_record_id",
        table_name="mcp_dispatch_attempts",
    )
    op.drop_table("mcp_dispatch_attempts")

    with op.batch_alter_table("receipts") as batch_op:
        batch_op.drop_index("ix_receipts_idempotency_record_id")
        batch_op.drop_constraint(
            "fk_receipts_idempotency_record_id",
            type_="foreignkey",
        )
        batch_op.drop_column("idempotency_record_id")

    with op.batch_alter_table("ledger_entries") as batch_op:
        batch_op.drop_constraint(
            "uq_ledger_wallet_operation_key",
            type_="unique",
        )
        batch_op.drop_index("ix_ledger_entries_operation_key")
        batch_op.drop_column("operation_key")

    with op.batch_alter_table("idempotency_records") as batch_op:
        batch_op.drop_index("ix_idempotency_records_operation_kind")
        batch_op.drop_column("operation_kind")
