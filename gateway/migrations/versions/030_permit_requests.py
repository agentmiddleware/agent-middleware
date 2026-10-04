"""Permit requests: agent asks, human approves, middleware mints.

Adds the `permit_requests` table backing the request -> notify -> approve ->
mint -> poll flow. The row is the authority of record for what the human
reviewed: the requested scopes, tools, budget, and permit expiry are frozen at
request time and hashed into `request_hash`, so the permit is minted from the
reviewed terms rather than from whatever the agent sends when polling.

`reserved_permit_id` is allocated up front and becomes the minted permit's
primary key, making the mint idempotent — a mint retried after a crash
collides on `permits.permit_id` instead of issuing a second permit that
carries the same authority.

`permit_id` is added without a foreign key for the same reason 023 added
`receipts.approval_id` without one: SQLite cannot add FK constraints via
ALTER, and the column is written after the referenced row exists.

Revision ID: 030_permit_requests
Revises: 029_add_permit_v2_constraints
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "030_permit_requests"
down_revision: Union[str, None] = "029_add_permit_v2_constraints"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "permit_requests",
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("issuer_wallet_id", sa.String(length=50), nullable=False),
        sa.Column("subject_wallet_id", sa.String(length=50), nullable=False),
        sa.Column("subject_key_id", sa.String(length=50), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("scopes_json", sa.Text(), nullable=False),
        sa.Column("allowed_tools_json", sa.Text(), nullable=False),
        sa.Column("max_credits", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("permit_expires_at", sa.DateTime(), nullable=False),
        sa.Column(
            "requires_human_approval",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("justification", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="pending"
        ),
        sa.Column("simulated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sentinel_action_id", sa.String(length=64), nullable=True),
        sa.Column("approval_url", sa.String(length=512), nullable=True),
        sa.Column("reserved_permit_id", sa.String(length=64), nullable=False),
        sa.Column("permit_id", sa.String(length=64), nullable=True),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("decided_by", sa.String(length=128), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("mint_started_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["issuer_wallet_id"], ["wallets.wallet_id"]),
        sa.ForeignKeyConstraint(["subject_wallet_id"], ["wallets.wallet_id"]),
        sa.PrimaryKeyConstraint("request_id"),
        sa.UniqueConstraint(
            "subject_wallet_id",
            "idempotency_key",
            name="uq_permit_requests_idempotency",
        ),
    )
    op.create_index(
        "ix_permit_requests_issuer_wallet_id", "permit_requests", ["issuer_wallet_id"]
    )
    op.create_index(
        "ix_permit_requests_subject_wallet_id", "permit_requests", ["subject_wallet_id"]
    )
    op.create_index(
        "ix_permit_requests_subject_key_id", "permit_requests", ["subject_key_id"]
    )
    op.create_index("ix_permit_requests_status", "permit_requests", ["status"])
    op.create_index(
        "ix_permit_requests_sentinel_action_id",
        "permit_requests",
        ["sentinel_action_id"],
    )
    op.create_index("ix_permit_requests_permit_id", "permit_requests", ["permit_id"])


def downgrade() -> None:
    op.drop_index("ix_permit_requests_permit_id", table_name="permit_requests")
    op.drop_index("ix_permit_requests_sentinel_action_id", table_name="permit_requests")
    op.drop_index("ix_permit_requests_status", table_name="permit_requests")
    op.drop_index("ix_permit_requests_subject_key_id", table_name="permit_requests")
    op.drop_index("ix_permit_requests_subject_wallet_id", table_name="permit_requests")
    op.drop_index("ix_permit_requests_issuer_wallet_id", table_name="permit_requests")
    op.drop_table("permit_requests")
